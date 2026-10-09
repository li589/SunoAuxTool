"""score_export 测试：格式归一化、选项校验、各格式落盘、缺 Pillow 的降级路径。

这些测试全部走**文件系统 + 解析后内容**断言，不做像素比较：位图的像素正确性由
``test_score_drawing.py`` / ``test_score_jianpu.py`` 的像素探针负责，这里只确保
「该写的文件写了、内容对得上、错误路径给出可操作的提示」。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sunoauxtool.exceptions import InputFileError, ParameterError
from sunoauxtool.models.midi import MidiDocument
from sunoauxtool.models.notes import Note, NoteSequence
from sunoauxtool.score import Score, ScoreMeasure, ScoreNote, ScoreTrack
from sunoauxtool.score_export import (
    FORMAT_SUFFIX,
    SCORE_FORMATS,
    ScoreExportOptions,
    export_score,
    format_help,
    normalize_formats,
    score_from_midi,
    score_from_sequence,
    score_jianpu_svg_text,
    score_svg_text,
)

# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def small_score(*, key: str = "C major", is_drum: bool = False) -> Score:
    """两小节单轨小谱（含休止符与和弦，覆盖主要元素）。"""
    notes = [
        ScoreNote(pitch=72, start=0.0, duration=1.0, voice=0, measure=1),
        ScoreNote(pitch=76, start=0.0, duration=1.0, voice=0, measure=1),
        ScoreNote(pitch=74, start=1.0, duration=1.0, voice=0, measure=1),
        ScoreNote(pitch=0, start=2.0, duration=2.0, voice=0, measure=1, is_rest=True),
        ScoreNote(pitch=71, start=4.0, duration=4.0, voice=0, measure=2),
    ]
    measures = [
        ScoreMeasure(index=1, start_beat=0.0, duration_beats=4.0, notes=notes[:4]),
        ScoreMeasure(index=2, start_beat=4.0, duration_beats=4.0, notes=notes[4:]),
    ]
    track = ScoreTrack(
        name="Melody", clef="treble", voices=1, notes=notes, measures=measures,
        is_drum=is_drum,
    )
    return Score(title="T", composer="", bpm=120, key=key, time_signature="4/4", bars=2, tracks=[track])


class _SeqTrack:
    """最小 NoteSequence 轨替身（只需 name/program/channel/notes）。"""

    def __init__(self, name: str = "M", program: int = 0, channel: int = 0, notes=()):
        self.name = name
        self.program = program
        self.channel = channel
        self.notes = list(notes)


class _SeqNote:
    def __init__(self, pitch: int, start: float, duration: float, velocity: int = 64):
        self.pitch = pitch
        self.start = start
        self.duration = duration
        self.velocity = velocity


class _Seq:
    """最小 NoteSequence 替身（只需 Score.from_sequence 用到的字段）。"""

    def __init__(self, *, key: str = "C major", time_signature: str = "4/4", bars: int = 1, bpm: int = 120):
        self.key = key
        self.time_signature = time_signature
        self.bars = bars
        self.bpm = bpm
        self.tracks = [
            _SeqTrack("Melody", notes=[_SeqNote(72, 0.0, 4.0)]),
        ]

    def total_beats(self) -> float:
        return float(self.bars) * 4.0


# ---------------------------------------------------------------------------
# 格式归一化
# ---------------------------------------------------------------------------


def test_normalize_formats_single():
    assert normalize_formats("svg") == ["svg"]


def test_normalize_formats_comma_separated_and_case_insensitive():
    assert normalize_formats("SVG, Png") == ["svg", "png"]


def test_normalize_formats_semicolon_and_iterable():
    assert normalize_formats("svg;png") == ["svg", "png"]
    assert normalize_formats(["svg", "png"]) == ["svg", "png"]


def test_normalize_formats_dedupes_preserving_order():
    assert normalize_formats("png,svg,png") == ["png", "svg"]


def test_normalize_formats_all_expands_to_every_format():
    assert normalize_formats("all") == list(SCORE_FORMATS)


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("xml", ["musicxml"]),
        ("music-xml", ["musicxml"]),
        ("txt", ["jianpu-txt"]),
        ("text", ["jianpu-txt"]),
        ("np", ["jianpu"]),
        ("number", ["jianpu"]),
        ("jianpu-svg", ["jianpu"]),
    ],
)
def test_normalize_formats_aliases(alias, expected):
    assert normalize_formats(alias) == expected


def test_normalize_formats_unknown_raises_with_help():
    with pytest.raises(ParameterError) as err:
        normalize_formats("pdf")
    message = str(err.value)
    assert "pdf" in message
    assert "musicxml" in message  # 提示里要列出可用格式


def test_normalize_formats_empty_raises():
    with pytest.raises(ParameterError, match="未指定"):
        normalize_formats("")
    with pytest.raises(ParameterError, match="未指定"):
        normalize_formats(["", "  "])


def test_format_help_lists_every_format():
    text = format_help()
    for name in SCORE_FORMATS:
        assert name in text


def test_every_format_has_a_suffix():
    assert set(FORMAT_SUFFIX) == set(SCORE_FORMATS)


# ---------------------------------------------------------------------------
# 选项校验
# ---------------------------------------------------------------------------


def test_options_resolve_formats_on_construction():
    opts = ScoreExportOptions(formats="all")
    assert opts.resolved == list(SCORE_FORMATS)


def test_options_default_is_svg_only():
    assert ScoreExportOptions().resolved == ["svg"]


@pytest.mark.parametrize("bad", ["nope", "pdf,"])
def test_options_reject_unknown_format(bad):
    with pytest.raises(ParameterError):
        ScoreExportOptions(formats=bad)


@pytest.mark.parametrize("scale", [0.0, -1.0])
def test_options_reject_non_positive_scale(scale):
    with pytest.raises(ParameterError, match="超采样"):
        ScoreExportOptions(scale=scale)


@pytest.mark.parametrize("width", [0.0, -10.0])
def test_options_reject_non_positive_page_width(width):
    with pytest.raises(ParameterError, match="页面宽度"):
        ScoreExportOptions(page_width=width)


def test_options_reject_non_positive_space():
    with pytest.raises(ParameterError, match="谱线间距"):
        ScoreExportOptions(space=0.0)


def test_options_reject_unknown_theme():
    with pytest.raises(ParameterError, match="主题"):
        ScoreExportOptions(theme="neon")


def test_options_theme_dark_swaps_paper_and_ink():
    dark = ScoreExportOptions(theme="dark").svg_options().theme
    light = ScoreExportOptions().svg_options().theme
    assert dark.paper != light.paper
    assert dark.ink != light.ink


def test_options_page_width_and_space_propagate_to_layout():
    opts = ScoreExportOptions(page_width=800.0, space=12.0)
    layout_opts = opts.layout_options()
    assert layout_opts.page_width == 800.0
    assert layout_opts.space == 12.0


def test_options_default_layout_is_untouched():
    layout_opts = ScoreExportOptions().layout_options()
    assert layout_opts.page_width == 1240.0
    assert layout_opts.space == 9.0


def test_options_switches_propagate_to_svg_and_jianpu():
    opts = ScoreExportOptions(show_title=False, show_tempo=False, show_ties=False,
                              show_measure_numbers=False)
    svg = opts.svg_options()
    jp = opts.jianpu_options()
    assert (svg.show_title, svg.show_tempo, svg.show_ties, svg.show_measure_numbers) == (
        False, False, False, False,
    )
    assert (jp.show_title, jp.show_tempo) == (False, False)


def test_options_musicxml_switches_follow_title_and_tempo():
    off = ScoreExportOptions(show_title=False, show_tempo=False).musicxml_options()
    on = ScoreExportOptions().musicxml_options()
    assert (off.include_metadata, off.include_tempo) == (False, False)
    assert (on.include_metadata, on.include_tempo) == (True, True)


# ---------------------------------------------------------------------------
# 各格式落盘
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", list(SCORE_FORMATS))
def test_export_writes_each_format(tmp_path: Path, fmt):
    written = export_score(small_score(), tmp_path, "piece", ScoreExportOptions(formats=fmt))
    assert list(written) == [fmt]
    path = Path(written[fmt])
    assert path.is_file()
    assert path.name == f"piece{FORMAT_SUFFIX[fmt]}"
    assert path.stat().st_size > 0


def test_export_all_writes_expected_file_set(tmp_path: Path):
    written = export_score(small_score(), tmp_path, "piece", ScoreExportOptions(formats="all"))
    names = sorted(Path(p).name for p in written.values())
    assert names == sorted(f"piece{s}" for s in FORMAT_SUFFIX.values())


def test_export_creates_output_dir(tmp_path: Path):
    target = tmp_path / "deep" / "nested"
    export_score(small_score(), target, "piece")
    assert target.is_dir()


def test_export_returns_absolute_paths(tmp_path: Path):
    written = export_score(small_score(), tmp_path, "piece")
    for path in written.values():
        assert Path(path).is_absolute()


def test_export_svg_is_well_formed_xml(tmp_path: Path):
    import xml.etree.ElementTree as ET

    written = export_score(small_score(), tmp_path, "p", ScoreExportOptions(formats="svg"))
    root = ET.fromstring(Path(written["svg"]).read_text(encoding="utf-8"))
    assert root.tag.endswith("svg")


def test_export_musicxml_passes_validation(tmp_path: Path):
    from sunoauxtool.score import validate_musicxml

    written = export_score(small_score(), tmp_path, "p", ScoreExportOptions(formats="musicxml"))
    text = Path(written["musicxml"]).read_text(encoding="utf-8")
    assert validate_musicxml(text).tag == "score-partwise"


def test_export_jianpu_txt_ends_with_newline(tmp_path: Path):
    written = export_score(small_score(), tmp_path, "p", ScoreExportOptions(formats="jianpu-txt"))
    text = Path(written["jianpu-txt"]).read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert "1 = C" in text


def test_export_respects_dark_theme(tmp_path: Path):
    light = export_score(small_score(), tmp_path / "l", "p", ScoreExportOptions(theme="light"))
    dark = export_score(small_score(), tmp_path / "d", "p", ScoreExportOptions(theme="dark"))
    light_svg = Path(light["svg"]).read_text(encoding="utf-8")
    dark_svg = Path(dark["svg"]).read_text(encoding="utf-8")
    assert "#ffffff" in light_svg and "#15171c" in dark_svg


def test_export_no_title_removes_title_block(tmp_path: Path):
    on = export_score(small_score(), tmp_path / "a", "p")
    off = export_score(small_score(), tmp_path / "b", "p", ScoreExportOptions(show_title=False))
    assert "T</text>" in Path(on["svg"]).read_text(encoding="utf-8")
    assert "T</text>" not in Path(off["svg"]).read_text(encoding="utf-8")


def test_export_png_is_a_png(tmp_path: Path):
    pytest.importorskip("PIL")
    written = export_score(small_score(), tmp_path, "p", ScoreExportOptions(formats="png"))
    assert Path(written["png"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_export_jianpu_png_is_a_png(tmp_path: Path):
    pytest.importorskip("PIL")
    written = export_score(small_score(), tmp_path, "p", ScoreExportOptions(formats="jianpu-png"))
    assert Path(written["jianpu-png"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_export_overwrites_existing_file(tmp_path: Path):
    target = tmp_path / "p.svg"
    target.write_text("stale", encoding="utf-8")
    export_score(small_score(), tmp_path, "p")
    assert target.read_text(encoding="utf-8").startswith("<svg")


def test_export_stem_with_chinese_characters(tmp_path: Path):
    written = export_score(small_score(), tmp_path, "夜曲-01", ScoreExportOptions(formats="svg,musicxml"))
    assert Path(written["svg"]).name == "夜曲-01.svg"
    assert Path(written["musicxml"]).is_file()


# ---------------------------------------------------------------------------
# 缺 Pillow 的降级路径
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["png", "jianpu-png"])
def test_missing_pillow_turns_into_actionable_parameter_error(tmp_path: Path, monkeypatch, fmt):
    """OCR 不到：把 PIL 置为 None 让 import 抛 ImportError，检查提示可操作。

    ``sys.modules['PIL'] = None`` 会让后续 ``import PIL`` 直接抛 ImportError，
    比删包干净。
    """
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setitem(sys.modules, "PIL.Image", None)
    with pytest.raises(ParameterError) as err:
        export_score(small_score(), tmp_path, "p", ScoreExportOptions(formats=fmt))
    message = str(err.value)
    assert "Pillow" in message
    assert "--format" in message  # 必须给出退路


def test_other_formats_still_work_without_pillow(tmp_path: Path, monkeypatch):
    """缺 Pillow 不影响零依赖格式（svg / musicxml / jianpu-txt）。"""
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setitem(sys.modules, "PIL.Image", None)
    written = export_score(
        small_score(), tmp_path, "p", ScoreExportOptions(formats="svg,musicxml,jianpu-txt")
    )
    assert len(written) == 3


# ---------------------------------------------------------------------------
# 不落盘的 SVG 文本（预览页内嵌用）
# ---------------------------------------------------------------------------


def test_score_svg_text_matches_written_svg(tmp_path: Path):
    score = small_score()
    written = export_score(score, tmp_path, "p", ScoreExportOptions(formats="svg"))
    assert score_svg_text(score) == Path(written["svg"]).read_text(encoding="utf-8")


def test_score_svg_text_has_no_xml_prolog():
    """预览页用 innerHTML 内嵌，带 <?xml?> 会被当成 bogus comment 而不渲染。"""
    text = score_svg_text(small_score())
    assert text.startswith("<svg")
    assert "<?xml" not in text


def test_score_svg_text_respects_options():
    dark = score_svg_text(small_score(), ScoreExportOptions(theme="dark"))
    assert "#15171c" in dark


def test_score_jianpu_svg_text_is_svg():
    assert score_jianpu_svg_text(small_score()).startswith("<svg")


# ---------------------------------------------------------------------------
# score_from_sequence
# ---------------------------------------------------------------------------


def test_score_from_sequence_uses_sequence_key():
    score = score_from_sequence(_Seq(key="G major"), title="X")
    assert score.key == "G major"
    assert score.title == "X"


def test_score_from_sequence_key_override_is_normalized():
    score = score_from_sequence(_Seq(key="G major"), title="X", key="a minor")
    assert score.key == "A minor"
    assert score.fifths == 0


@pytest.mark.parametrize("bad", ["H major", "C##", "blues"])
def test_score_from_sequence_rejects_bad_key_override(bad):
    with pytest.raises(ParameterError, match="调式"):
        score_from_sequence(_Seq(), title="X", key=bad)


def test_score_from_sequence_none_key_keeps_sequence_key():
    score = score_from_sequence(_Seq(key="D minor"), title="X", key=None)
    assert score.key == "D minor"


def test_drum_sequence_track_is_exportable():
    """打击轨走 unpitched + percussion 谱号，导出不应抛错且 MusicXML 应合法。"""
    from sunoauxtool.score import validate_musicxml

    seq = _Seq()
    seq.tracks = [_SeqTrack("Drums", channel=9, notes=[_SeqNote(36, 0.0, 1.0), _SeqNote(38, 1.0, 1.0)])]
    score = score_from_sequence(seq, title="D")
    assert score.tracks[0].is_drum is True
    validate_musicxml(__import__(
        "sunoauxtool.score", fromlist=["render_musicxml"]
    ).render_musicxml(score))


# ---------------------------------------------------------------------------
# score_from_midi：参数错误归一化（score --key 缺陷的回归守卫）
# ---------------------------------------------------------------------------


def _midi_file(tmp_path: Path, name: str = "in.mid") -> Path:
    """最小单轨 .mid（score_from_midi 的输入，两小节四分音符）。"""
    seq = NoteSequence(bpm=120, key="C major", time_signature="4/4", bars=2)
    seq.add_track(
        "melody", 0, 0,
        [Note(pitch=72, start=0.0, duration=1.0), Note(pitch=74, start=1.0, duration=1.0)],
    )
    path = tmp_path / name
    MidiDocument.from_sequence(seq).write(path)
    return path


def test_score_from_midi_bad_key_is_parameter_error(tmp_path: Path):
    """非法调式必须是 ParameterError（错误码 1），不得冒成裸 ValueError。"""
    path = _midi_file(tmp_path)
    with pytest.raises(ParameterError) as ei:
        score_from_midi(path, key="H# weird")
    assert "非法调式" in str(ei.value)
    assert ei.value.code == 1


def test_score_from_midi_bad_time_signature_is_parameter_error(tmp_path: Path):
    """非法拍号同样归一化为 ParameterError（与 key 同一契约）。"""
    path = _midi_file(tmp_path)
    with pytest.raises(ParameterError) as ei:
        score_from_midi(path, time_signature="abc")
    assert "非法拍号" in str(ei.value)
    assert ei.value.code == 1


def test_score_from_midi_missing_file_passes_through(tmp_path: Path):
    """InputFileError 不是 ValueError 子类，必须原样透传（错误码 3，不被误包装）。"""
    with pytest.raises(InputFileError) as ei:
        score_from_midi(tmp_path / "nope.mid")
    assert ei.value.code == 3


def test_score_from_midi_defaults(tmp_path: Path):
    """key / time_signature 缺省时按 C major / 4/4 记谱，正常出谱。"""
    path = _midi_file(tmp_path)
    score = score_from_midi(path)
    assert score.key == "C major"
    assert score.time_signature == "4/4"


def test_score_from_midi_key_override_applied(tmp_path: Path):
    """合法 key 覆盖生效（归一化后的 'a minor' 写回 score.key）。"""
    path = _midi_file(tmp_path)
    score = score_from_midi(path, key="Am")
    assert score.key == "A minor"
