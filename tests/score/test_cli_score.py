"""``sunoauxtool score`` / ``--score`` 的 CLI 端到端测试（#12）。

覆盖三处接入：
- ``score`` 子命令本身（各格式、开关、错误路径与退出码）
- ``generate midi|melody --score``（与 MIDI 同目录同主干落盘）
- ``pipeline --score``（选项在构造期校验 + 谱面进元数据 + 预览页内嵌）
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from sunoauxtool.cli import app
from sunoauxtool.models.midi import MidiDocument
from sunoauxtool.models.notes import Note, NoteSequence
from sunoauxtool.score import validate_musicxml
from sunoauxtool.score_export import SCORE_FORMATS

runner = CliRunner()


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _write_midi(tmp_path: Path, name: str = "sample.mid") -> Path:
    """写一个两小节的最小 .mid（3 轨，含和弦，供谱面渲染）。

    轨道只能经 ``NoteSequence.add_track`` 添加（``models.notes`` 未公开轨道容器类，
    与 ``models.midi.MidiTrack`` 刻意区分）。
    """
    seq = NoteSequence(bpm=120, key="C major", time_signature="4/4", bars=2)
    seq.add_track(
        "melody", 0, 0,
        [
            Note(pitch=72, start=0.0, duration=1.0, velocity=80),
            Note(pitch=74, start=1.0, duration=1.0, velocity=80),
            Note(pitch=76, start=2.0, duration=2.0, velocity=80),
            Note(pitch=71, start=4.0, duration=4.0, velocity=80),
        ],
    )
    seq.add_track(
        "chords", 0, 1,
        [
            Note(pitch=48, start=0.0, duration=4.0, velocity=60),
            Note(pitch=52, start=0.0, duration=4.0, velocity=60),
            Note(pitch=55, start=0.0, duration=4.0, velocity=60),
            Note(pitch=53, start=4.0, duration=4.0, velocity=60),
        ],
    )
    seq.add_track(
        "bass", 32, 2,
        [
            Note(pitch=36, start=0.0, duration=4.0, velocity=70),
            Note(pitch=41, start=4.0, duration=4.0, velocity=70),
        ],
    )
    path = tmp_path / name
    MidiDocument.from_sequence(seq).write(path)
    return path


@pytest.fixture()
def midi_file(tmp_path: Path) -> Path:
    return _write_midi(tmp_path)


def _files(tmp_path: Path) -> set[str]:
    return {p.name for p in tmp_path.iterdir() if p.is_file()}


# ---------------------------------------------------------------------------
# score 子命令：格式与落盘
# ---------------------------------------------------------------------------


def test_score_list_formats():
    result = runner.invoke(app, ["score", "ignored.mid", "--list-formats"])
    assert result.exit_code == 0, result.output
    for name in SCORE_FORMATS:
        assert name in result.output
    assert "light" in result.output and "dark" in result.output


def test_score_default_writes_svg_next_to_midi(midi_file: Path):
    result = runner.invoke(app, ["score", str(midi_file)])
    assert result.exit_code == 0, result.output
    assert (midi_file.parent / "sample.svg").is_file()
    assert "谱面已生成" in result.output
    assert "sample.svg" in result.output


def test_score_format_all_writes_every_file(midi_file: Path):
    result = runner.invoke(app, ["score", str(midi_file), "--format", "all"])
    assert result.exit_code == 0, result.output
    names = _files(midi_file.parent)
    assert "sample.svg" in names
    assert "sample.musicxml" in names
    assert "sample.jianpu.svg" in names
    assert "sample.jianpu.txt" in names


def test_score_output_dir_override(midi_file: Path, tmp_path: Path):
    target = tmp_path / "out"
    result = runner.invoke(app, ["score", str(midi_file), "--output-dir", str(target)])
    assert result.exit_code == 0, result.output
    assert (target / "sample.svg").is_file()
    assert not (midi_file.parent / "sample.svg").exists()


def test_score_format_comma_separated(midi_file: Path):
    result = runner.invoke(app, ["score", str(midi_file), "-f", "musicxml,txt"])
    assert result.exit_code == 0, result.output
    assert (midi_file.parent / "sample.musicxml").is_file()
    assert (midi_file.parent / "sample.jianpu.txt").is_file()
    assert not (midi_file.parent / "sample.svg").exists()


def test_score_multitrack_generates_all_parts(midi_file: Path):
    result = runner.invoke(app, ["score", str(midi_file), "-f", "musicxml"])
    assert result.exit_code == 0, result.output
    text = (midi_file.parent / "sample.musicxml").read_text(encoding="utf-8")
    assert validate_musicxml(text).tag == "score-partwise"
    assert text.count("<score-part ") == 3


# ---------------------------------------------------------------------------
# score 子命令：渲染开关与选项
# ---------------------------------------------------------------------------


def test_score_title_and_composer_and_key(midi_file: Path):
    result = runner.invoke(
        app,
        ["score", str(midi_file), "-f", "musicxml,svg",
         "--title", "夜曲", "--composer", "齐见林", "--key", "F major"],
    )
    assert result.exit_code == 0, result.output
    xml = (midi_file.parent / "sample.musicxml").read_text(encoding="utf-8")
    assert "<work-title>夜曲</work-title>" in xml
    assert "齐见林" in xml
    assert "<fifths>-1</fifths>" in xml
    assert "夜曲" in (midi_file.parent / "sample.svg").read_text(encoding="utf-8")


def test_score_time_signature_option(midi_file: Path):
    result = runner.invoke(app, ["score", str(midi_file), "-f", "musicxml", "--time-signature", "3/4"])
    assert result.exit_code == 0, result.output
    xml = (midi_file.parent / "sample.musicxml").read_text(encoding="utf-8")
    assert "<beats>3</beats>" in xml


def test_score_no_title_removes_title(midi_file: Path):
    result = runner.invoke(app, ["score", str(midi_file), "--no-title", "--title", "Zzz"])
    assert result.exit_code == 0, result.output
    assert "Zzz" not in (midi_file.parent / "sample.svg").read_text(encoding="utf-8")


def test_score_no_tempo_removes_tempo_mark(midi_file: Path):
    """速度记号是自绘矢量图形（不是 ♩ 文本），用红色 accent 判定其存在。"""
    with_ = runner.invoke(app, ["score", str(midi_file), "--output-dir", str(midi_file.parent / "a")])
    without = runner.invoke(
        app,
        ["score", str(midi_file), "--no-tempo", "--output-dir", str(midi_file.parent / "b")],
    )
    assert with_.exit_code == 0 and without.exit_code == 0, with_.output + without.output
    accent = "#8a1c1c"
    assert accent in (midi_file.parent / "a" / "sample.svg").read_text(encoding="utf-8")
    assert accent not in (midi_file.parent / "b" / "sample.svg").read_text(encoding="utf-8")


def test_score_dark_theme(midi_file: Path, tmp_path: Path):
    result = runner.invoke(
        app, ["score", str(midi_file), "--theme", "dark", "--output-dir", str(tmp_path / "d")]
    )
    assert result.exit_code == 0, result.output
    svg = (tmp_path / "d" / "sample.svg").read_text(encoding="utf-8")
    assert "#15171c" in svg and "#e9e7e4" in svg


def test_score_page_width_and_space_options(midi_file: Path, tmp_path: Path):
    result = runner.invoke(
        app,
        ["score", str(midi_file), "--page-width", "600", "--space", "7",
         "--output-dir", str(tmp_path / "w")],
    )
    assert result.exit_code == 0, result.output
    svg = (tmp_path / "w" / "sample.svg").read_text(encoding="utf-8")
    assert 'viewBox="0 0 600.0' in svg


def test_score_clef_override(midi_file: Path):
    result = runner.invoke(
        app,
        ["score", str(midi_file), "-f", "musicxml", "--clef", "melody=bass",
         "--clef", "bass=treble"],
    )
    assert result.exit_code == 0, result.output
    xml = (midi_file.parent / "sample.musicxml").read_text(encoding="utf-8")
    # 轨序 melody / chords / bass：
    #   melody 覆盖为 bass -> F/4
    #   chords 未覆盖，音域 C3-G3 自动判为低音 -> F/4
    #   bass   覆盖为 treble -> G/2
    signs = [xml.split("<clef>")[i].split("<sign>")[1].split("</sign>")[0] for i in (1, 2, 3)]
    assert signs == ["F", "F", "G"]


def test_score_bars_override(midi_file: Path):
    result = runner.invoke(app, ["score", str(midi_file), "-f", "musicxml", "--bars", "4"])
    assert result.exit_code == 0, result.output
    xml = (midi_file.parent / "sample.musicxml").read_text(encoding="utf-8")
    assert '<measure number="4">' in xml


# ---------------------------------------------------------------------------
# score 子命令：错误路径与退出码
# ---------------------------------------------------------------------------


def test_score_missing_midi_exits_3(tmp_path: Path):
    result = runner.invoke(app, ["score", str(tmp_path / "nope.mid")])
    assert result.exit_code == 3
    assert "不存在" in result.output


def test_score_unknown_format_exits_1(midi_file: Path):
    result = runner.invoke(app, ["score", str(midi_file), "--format", "pdf"])
    assert result.exit_code == 1
    assert "pdf" in result.output
    assert "musicxml" in result.output  # 提示可用格式


def test_score_empty_format_exits_1(midi_file: Path):
    result = runner.invoke(app, ["score", str(midi_file), "--format", ""])
    assert result.exit_code == 1
    assert "未指定" in result.output


def test_score_invalid_key_exits_1(midi_file: Path):
    result = runner.invoke(app, ["score", str(midi_file), "--key", "H major"])
    assert result.exit_code == 1


def test_score_invalid_theme_exits_1(midi_file: Path):
    result = runner.invoke(app, ["score", str(midi_file), "--theme", "neon"])
    assert result.exit_code == 1
    assert "主题" in result.output


@pytest.mark.parametrize(
    ("bad_clef", "needle"),
    [
        ("melody", "谱表名=谱号"),
        ("=bass", "不可为空"),
        ("melody=viola", "谱号须为"),
    ],
)
def test_score_bad_clef_exits_1(midi_file: Path, bad_clef: str, needle: str):
    result = runner.invoke(app, ["score", str(midi_file), "--clef", bad_clef])
    assert result.exit_code == 1
    assert needle in result.output


def test_score_duplicate_clef_exits_1(midi_file: Path):
    result = runner.invoke(
        app, ["score", str(midi_file), "--clef", "melody=bass", "--clef", "melody=treble"]
    )
    assert result.exit_code == 1
    assert "重复" in result.output


def test_score_non_positive_page_width_exits_1(midi_file: Path):
    result = runner.invoke(app, ["score", str(midi_file), "--page-width", "0"])
    assert result.exit_code == 1
    assert "页面宽度" in result.output


# ---------------------------------------------------------------------------
# generate --score
# ---------------------------------------------------------------------------


def test_generate_midi_without_score_writes_no_score(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["generate", "midi", "--bars", "2", "--seed", "5"])
    assert result.exit_code == 0, result.output
    assert not list(tmp_path.rglob("*.svg"))
    assert not list(tmp_path.rglob("*.musicxml"))


def test_generate_midi_with_score_writes_alongside_midi(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["generate", "midi", "--bars", "2", "--seed", "5", "--score",
         "--score-format", "svg,musicxml"],
    )
    assert result.exit_code == 0, result.output
    mid = next(tmp_path.rglob("*.mid"))
    assert mid.with_suffix(".svg").is_file()
    assert mid.with_suffix(".musicxml").is_file()
    assert str(mid.with_suffix(".svg")) in result.output
    validate_musicxml(mid.with_suffix(".musicxml").read_text(encoding="utf-8"))


def test_generate_midi_score_key_override(tmp_path: Path, monkeypatch):
    """--score-key 覆盖记谱调式（不影响生成的音高）。"""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["generate", "midi", "--bars", "2", "--seed", "5", "--key", "C major",
         "--score", "--score-format", "musicxml", "--score-key", "a minor"],
    )
    assert result.exit_code == 0, result.output
    xml = next(tmp_path.rglob("*.musicxml")).read_text(encoding="utf-8")
    assert "<fifths>0</fifths>" in xml
    assert "<mode>minor</mode>" in xml


def test_generate_midi_score_bad_format_exits_1(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["generate", "midi", "--bars", "2", "--seed", "5", "--score", "--score-format", "pdf"]
    )
    assert result.exit_code == 1
    assert "pdf" in result.output


def test_generate_melody_with_score(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["generate", "melody", "--bars", "2", "--seed", "9", "--score", "--score-format", "svg"]
    )
    assert result.exit_code == 0, result.output
    assert next(tmp_path.rglob("*.mid")).with_suffix(".svg").is_file()


# ---------------------------------------------------------------------------
# pipeline --score
# ---------------------------------------------------------------------------


def test_pipeline_rejects_bad_score_format_before_any_work(tmp_path: Path, monkeypatch):
    """选项在 Pipeline 构造期校验：写错格式要立刻失败（退出码 1），不留下任何产物。"""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["pipeline", "--bars", "2", "--seed", "5", "--score", "--score-format", "pdf"]
    )
    assert result.exit_code == 1
    assert "pdf" in result.output
    assert not list(tmp_path.rglob("*.mid"))


def test_pipeline_dry_run_writes_no_score(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["pipeline", "--bars", "2", "--seed", "5", "--score", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert not list(tmp_path.rglob("*.svg"))


def test_pipeline_score_bad_theme_exits_1(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["pipeline", "--bars", "2", "--seed", "5", "--score", "--score-theme", "neon"]
    )
    assert result.exit_code == 1
    assert "主题" in result.output


# ---------------------------------------------------------------------------
# --no-preview 修复回归（它曾整行丢弃 DSP 覆盖）
# ---------------------------------------------------------------------------


def test_pipeline_no_preview_keeps_dsp_overrides(tmp_path: Path, monkeypatch):
    """--no-preview 曾写成 ``merged = cfg.merge_cli()``，会把 fade/eq 覆盖一起丢掉。

    回归断言：开启 --no-preview 时，DSP 覆盖仍须体现在合并后的配置里。
    """
    monkeypatch.chdir(tmp_path)
    captured: dict = {}
    from sunoauxtool import pipeline as pipeline_mod

    real_init = pipeline_mod.Pipeline.__init__

    def spy(self, config, *args, **kwargs):
        captured["config"] = config
        return real_init(self, config, *args, **kwargs)

    monkeypatch.setattr(pipeline_mod.Pipeline, "__init__", spy)
    result = runner.invoke(
        app,
        ["pipeline", "--bars", "2", "--seed", "5", "--dry-run",
         "--no-preview", "--fade-in", "120", "--eq"],
    )
    assert result.exit_code == 0, result.output
    cfg = captured["config"]
    assert cfg.preview.enabled is False
    assert cfg.dsp.fade_in_ms == 120
    assert cfg.dsp.eq is True


# ---------------------------------------------------------------------------
# score 子命令：--key / --time-signature 错误归一化（回归守卫）
# ---------------------------------------------------------------------------


def test_score_rejects_bad_key_as_parameter_error(midi_file):
    """非法 --key 必须是错误码 1 的参数错误，而非「意外错误」。

    回归背景：score_cmd 曾直接调 ``Score.from_midi``，key 解析的裸 ``ValueError``
    冒成「意外错误」；现统一走 ``score_export.score_from_midi``，
    与 ``generate --score`` / ``pipeline --score`` 的 ``--score-key`` 同一契约。
    """
    result = runner.invoke(app, ["score", str(midi_file), "--key", "H# weird"])
    assert result.exit_code == 1, result.output
    assert "非法调式" in result.output
    assert "意外错误" not in result.output


def test_score_rejects_bad_time_signature_as_parameter_error(midi_file):
    """非法 --time-signature 同样归一化为参数错误。"""
    result = runner.invoke(
        app, ["score", str(midi_file), "--time-signature", "abc"]
    )
    assert result.exit_code == 1, result.output
    assert "非法拍号" in result.output
    assert "意外错误" not in result.output
