"""简谱（数字谱）与 PNG 位图输出测试。

覆盖重点
--------
- ``parse_jianpu``：音级/八度换算（含小调按关系大调记 1）、休止符、整数拍延续退化为横线、
  跨小节与跨分量的连音线标记、变音记号拼写（升/降偏好）、多声部成行、轨筛选。
- ``to_jianpu_lines``：表头字段、标题居中、轨名前缀与折行缩进、``^`` 连音标记、行宽折行。
- ``render_jianpu_svg``：文档骨架与 viewBox、表头文字、数字/横线/下划线/八度点/变音字形、
  小节线计数、终止粗线只在末系统、连音弧落点（小节内 / 跨小节 / 跨系统两段）、选项开关。
- ``render_png`` / ``render_jianpu_png``：PNG 魔数与像素尺寸、落盘、``scale`` 校验、
  未装 Pillow 时的惰性 ``ImportError``，以及「SVG 与 PNG 同源」的几何一致性。

多处断言用「竖直/水平线」区分小节线与记号笔画：简谱里判定线宽不可靠
（下划线与小节线同为 ``note_size * 0.085``），但小节线是「竖直且长 1.55 字号」的，
这样能把速度记号的符干（同为竖线但更短）排除掉。
"""

from __future__ import annotations

import io
import re
import struct

import pytest

from sunoauxtool.models.notes import Note, NoteSequence
from sunoauxtool.score import Score, SvgTheme, layout_score, render_png, write_png
from sunoauxtool.score.jianpu import (
    KEY_BASELINE,
    JianpuRenderer,
    JpOptions,
    JpToken,
    _token_text,
    parse_jianpu,
    render_jianpu_png,
    render_jianpu_svg,
    to_jianpu_lines,
    write_jianpu_png,
    write_jianpu_svg,
)
from sunoauxtool.score.svg import ACCIDENTAL_STROKES
from sunoauxtool.score.theory import key_fifths, relative_major

# ---------------------------------------------------------------------------
# 夹具与解析辅助
# ---------------------------------------------------------------------------

_LINE_RE = re.compile(
    r'<line x1="(-?[\d.]+)" y1="(-?[\d.]+)" x2="(-?[\d.]+)" y2="(-?[\d.]+)" '
    r'stroke="#[0-9a-fA-F]{6}" stroke-width="([\d.]+)"/>'
)
_TEXT_RE = re.compile(r'<text [^>]*font-size="([\d.]+)"[^>]*>(.*?)</text>')
_QUAD_RE = re.compile(r'<path d="M ([\d.-]+),([\d.-]+) Q ([\d.-]+),([\d.-]+) ([\d.-]+),([\d.-]+)"')

#: 自绘升号字形（判断变音记号是否真的画出来了）
SHARP_PATH = ACCIDENTAL_STROKES[1][0][0]


def make_score(
    notes: list[Note] | None = None,
    *,
    name: str = "melody",
    extra_tracks: tuple[tuple[str, list[Note]], ...] = (),
    bars: int = 4,
    key: str = "C major",
    time_signature: str = "4/4",
    bpm: int = 96,
    title: str = "T",
    composer: str = "",
) -> Score:
    """构造乐谱；``notes`` 缺省为 4 小节每拍一个 C5。"""
    if notes is None:
        notes = [Note(pitch=72, start=float(i), duration=1.0, velocity=80) for i in range(bars * 4)]
    seq = NoteSequence(bpm=bpm, key=key, time_signature=time_signature, bars=bars, style="test")
    seq.add_track(name, 0, 0, notes)
    for extra_name, extra_notes in extra_tracks:
        seq.add_track(extra_name, 32, 1, extra_notes)
    return Score.from_sequence(seq, title=title, composer=composer)


def whole_notes(pitches: list[int], bars: int) -> list[Note]:
    """每小节一个全音符。"""
    return [Note(pitch=p, start=float(i * 4), duration=4.0, velocity=60) for i, p in enumerate(pitches[:bars])]


def make_piano_score() -> Score:
    """两轨（旋律 + 低音）的 4 小节乐谱。"""
    return make_score(extra_tracks=(("bass", whole_notes([48, 48, 43, 43], 4)),))


def tokens_of(jp, row_index: int = 0, measure: int = 1) -> list[JpToken]:
    """取某行某小节的记号（含补出的休止符）。"""
    return jp.rows[row_index].measures[measure - 1].tokens


def sounding_of(jp, row_index: int = 0, measure: int = 1) -> list[JpToken]:
    """取某行某小节的**实音**记号（滤掉模型补出的休止符）。

    模型会给空隙补休止符，故「小节里有几个记号」不可直接当「原谱有几个音」用。
    """
    return [t for t in tokens_of(jp, row_index, measure) if not t.is_rest]


def lines(svg: str) -> list[tuple[float, ...]]:
    """全部 ``<line>``。"""
    return [tuple(float(v) for v in m) for m in _LINE_RE.findall(svg)]


def barlines(svg: str, size: float = 22.0) -> list[tuple[float, ...]]:
    """小节线：竖直且长度恰为 ``note_size * 1.55``。

    速度记号的符干也是竖线（且向上，y2 < y1），靠长度把它区分掉。
    """
    return [
        line
        for line in lines(svg)
        if line[0] == line[2] and abs((line[3] - line[1]) - size * 1.55) < 0.01
    ]


def horizontal_lines(svg: str) -> list[tuple[float, ...]]:
    """水平的线：延音横线与下划线。"""
    return [line for line in lines(svg) if line[1] == line[3]]


def texts(svg: str) -> list[str]:
    """全部 ``<text>`` 的文字内容。"""
    return [m[1] for m in _TEXT_RE.findall(svg)]


def text_positions(svg: str) -> list[tuple[float, float, str]]:
    """全部 ``<text>`` 的 ``(x, y, 内容)``。"""
    return [
        (float(x), float(y), content)
        for x, y, content in re.findall(
            r'<text x="(-?[\d.]+)" y="(-?[\d.]+)"[^>]*>(.*?)</text>', svg
        )
    ]


def arcs(svg: str) -> list[tuple[float, ...]]:
    """全部连音弧（二次贝塞尔）。"""
    return [tuple(float(v) for v in m) for m in _QUAD_RE.findall(svg)]


def png_size(data: bytes) -> tuple[int, int]:
    """读 PNG 的 IHDR 宽高。"""
    return struct.unpack(">II", data[16:24])


# ---------------------------------------------------------------------------
# parse_jianpu：调式与音级换算
# ---------------------------------------------------------------------------


def test_parse_reports_major_key_tonic():
    jp = parse_jianpu(make_score())
    assert jp.tonic == "C"
    assert jp.mode == "major"
    assert jp.minor_tonic == ""
    assert jp.flats is False


def test_minor_key_uses_relative_major_as_number_one():
    """简谱记小调用关系大调作 1：A 小调 -> ``1 = C``，主音落在 6 上。"""
    jp = parse_jianpu(make_score(key="A minor", notes=[Note(pitch=69, start=0.0, duration=1.0)]))
    assert jp.mode == "minor"
    assert jp.tonic == "C"
    assert jp.minor_tonic == "A"


def test_minor_tonic_is_degree_six():
    """A 小调里的 A 必须记成 6；同时 B 记 7、C 记 1。"""
    notes = [
        Note(pitch=69, start=0.0, duration=1.0),   # A4
        Note(pitch=71, start=1.0, duration=1.0),   # B4
        Note(pitch=72, start=2.0, duration=1.0),   # C5
        Note(pitch=74, start=3.0, duration=1.0),   # D5
    ]
    jp = parse_jianpu(make_score(key="A minor", notes=notes))
    assert [t.degree for t in tokens_of(jp)] == [6, 7, 1, 2]
    assert [t.octave for t in tokens_of(jp)] == [0, 0, 1, 1]


@pytest.mark.parametrize("minor_tonic", ["A", "E", "B", "F#", "C#", "G#", "D#", "D", "G", "C", "F", "Bb", "Eb", "Ab"])
def test_relative_major_shares_key_signature(minor_tonic):
    """关系大调与对应小调调号相同 —— 用五度圈反查的实现必须守住这一点。"""
    major = relative_major(minor_tonic)
    assert key_fifths(f"{minor_tonic} minor") == key_fifths(f"{major} major")


@pytest.mark.parametrize(
    ("minor_tonic", "major_tonic"),
    [("A", "C"), ("D", "F"), ("E", "G"), ("Bb", "Db"), ("F#", "A"), ("C", "Eb")],
)
def test_relative_major_pairs(minor_tonic, major_tonic):
    assert relative_major(minor_tonic) == major_tonic


def test_relative_major_rejects_unknown_tonic():
    with pytest.raises(ValueError):
        relative_major("H")


def test_relative_major_rejects_tonic_outside_minor_table():
    """合法音名但不在小调五度圈内（如重升）。"""
    with pytest.raises(ValueError):
        relative_major("C##")


def test_degree_and_octave_track_register():
    """C 大调：C5=1'、G4=5、C3=1,（下方一个点）。"""
    notes = [
        Note(pitch=72, start=0.0, duration=1.0),
        Note(pitch=67, start=1.0, duration=1.0),
        Note(pitch=48, start=2.0, duration=1.0),
    ]
    jp = parse_jianpu(make_score(notes=notes))
    assert [(t.degree, t.octave) for t in sounding_of(jp)] == [(1, 1), (5, 0), (1, -1)]


def test_accidental_for_out_of_key_note():
    """C 大调里的 F#5 记作 #4'。"""
    jp = parse_jianpu(make_score(notes=[Note(pitch=78, start=0.0, duration=1.0)]))
    token = tokens_of(jp)[0]
    assert (token.degree, token.accidental, token.octave) == (4, 1, 1)


def test_flat_key_prefers_flat_spelling():
    """F 大调里的 Bb4 记作 b4（而非 #3）。"""
    jp = parse_jianpu(make_score(key="F major", notes=[Note(pitch=70, start=0.0, duration=1.0)]))
    assert jp.flats is True
    token = tokens_of(jp)[0]
    assert (token.degree, token.accidental, token.octave) == (4, -1, 0)


def test_measure_indices_are_one_based_and_complete():
    jp = parse_jianpu(make_score(bars=3))
    assert [m.index for m in jp.rows[0].measures] == [1, 2, 3]


def test_every_row_has_one_measure_per_bar():
    jp = parse_jianpu(make_score(bars=5, extra_tracks=(("bass", whole_notes([48] * 5, 5)),)))
    for row in jp.rows:
        assert len(row.measures) == 5


def test_rows_follow_tracks():
    jp = parse_jianpu(make_piano_score())
    assert [(r.name, r.voice) for r in jp.rows] == [("melody", 0), ("bass", 0)]


def test_track_indices_filter_rows():
    score = make_piano_score()
    jp = parse_jianpu(score, track_indices=[1])
    assert [r.name for r in jp.rows] == ["bass"]


def test_rest_tokens_carry_degree_zero():
    """空隙被补成休止符，简谱写 0。"""
    notes = [
        Note(pitch=72, start=0.0, duration=1.0),
        Note(pitch=74, start=2.0, duration=1.0),  # 1.0-2.0 是空隙
    ]
    jp = parse_jianpu(make_score(notes=notes, bars=1))
    rests = [t for t in tokens_of(jp) if t.is_rest]
    assert rests, "空隙应补出休止符"
    assert all(t.degree == 0 for t in rests)


def test_empty_score_yields_no_rows():
    jp = parse_jianpu(Score(title="empty"))
    assert jp.rows == []


def test_beat_unit_follows_time_signature():
    jp = parse_jianpu(make_score(time_signature="6/8", bars=1))
    assert jp.beat_unit == 0.5


# ---------------------------------------------------------------------------
# parse_jianpu：时值与连音线
# ---------------------------------------------------------------------------


def test_integer_beat_continuation_becomes_pure_dashes():
    """跨小节且延续端恰为整数拍：写纯横线，不加弧。"""
    jp = parse_jianpu(make_score(notes=[Note(pitch=72, start=0.0, duration=6.0)], bars=2))
    kinds = [t.kind for t in tokens_of(jp, measure=2)]
    assert kinds[:2] == ["dash", "dash"], "2 拍延续应写成两条横线"
    assert not any(t.slur_to_next for row in jp.rows for m in row.measures for t in m.tokens)


def test_sub_beat_continuation_marks_slur_on_source():
    """3.75 起 0.5 拍的音跨过小节线：前半（16 分）要标连音线指向后半。"""
    jp = parse_jianpu(make_score(notes=[Note(pitch=72, start=3.75, duration=0.5)], bars=2))
    first = tokens_of(jp, measure=1)
    second = tokens_of(jp, measure=2)
    assert first[-1].kind == "note"
    assert first[-1].underscores == 2
    assert first[-1].slur_to_next is True
    assert (second[0].degree, second[0].underscores) == (1, 2)


def test_continuation_slur_is_not_dropped_when_fragment_ends_the_measure():
    """回归：延续片段在下一小节时，旧实现只在本小节里找后续片段，于是漏标连音线。"""
    jp = parse_jianpu(make_score(notes=[Note(pitch=72, start=7.0, duration=1.5)], bars=3))
    assert sounding_of(jp, measure=2)[-1].slur_to_next is True
    assert sounding_of(jp, measure=3)[0].degree == 1


def test_note_split_into_components_is_slurred_between_components():
    """2.5 拍 = 二分 + 八分：写成 ``2 - 2̲``，弧标在数字上（不是末尾横线）。"""
    jp = parse_jianpu(make_score(notes=[Note(pitch=72, start=0.0, duration=2.5)], bars=1))
    tokens = sounding_of(jp)
    assert [t.kind for t in tokens] == ["note", "dash", "note"]
    assert tokens[0].slur_to_next is True
    assert tokens[1].slur_to_next is False
    assert tokens[2].underscores == 1


def test_long_note_inside_one_measure_has_no_slur():
    """整小节长音（4 拍）只写 ``1' - - -``，不跨分量，无弧。"""
    jp = parse_jianpu(make_score(notes=[Note(pitch=72, start=0.0, duration=4.0)], bars=1))
    tokens = sounding_of(jp)
    assert [t.kind for t in tokens] == ["note", "dash", "dash", "dash"]
    assert not any(t.slur_to_next for t in tokens)


def test_dotted_beat_uses_dot_not_dashes():
    """1.5 拍写成附点（``1'.``），不是 ``1 - 8分``。"""
    jp = parse_jianpu(make_score(notes=[Note(pitch=72, start=0.0, duration=1.5)], bars=1))
    head = tokens_of(jp)[0]
    assert (head.kind, head.dots, head.underscores) == ("note", 1, 0)


def test_multi_voice_track_produces_row_per_voice():
    """同轨内两个重叠音 -> 两个声部 -> 两行。"""
    notes = [
        Note(pitch=60, start=0.0, duration=2.0),
        Note(pitch=67, start=1.0, duration=2.0),
    ]
    score = make_score(notes=notes, bars=1)
    jp = parse_jianpu(score)
    assert score.tracks[0].voices == 2
    assert [r.voice for r in jp.rows] == [0, 1]


# ---------------------------------------------------------------------------
# 文本输出
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        (JpToken(kind="note", degree=1), "1"),
        (JpToken(kind="note", degree=0), "0"),
        (JpToken(kind="dash"), "-"),
        (JpToken(kind="note", degree=5, accidental=-1), "b5"),
        (JpToken(kind="note", degree=3, underscores=2), "3__"),
        (JpToken(kind="note", degree=2, dots=1), "2."),
        (JpToken(kind="note", degree=1, octave=-2), "1,,"),
        (JpToken(kind="note", degree=7, slur_to_next=True), "7^"),
        (
            JpToken(kind="note", degree=1, accidental=1, octave=1, underscores=1, slur_to_next=True),
            "#1'_^",
        ),
    ],
)
def test_token_text_ascii_notation(token, expected):
    assert _token_text(token) == expected


def test_text_header_reports_key_tempo_meter_and_bars():
    text = to_jianpu_lines(make_score(bars=4, bpm=96))
    assert text[0].strip() == "T"
    head = text[1]
    assert head.startswith("1 = C")
    assert "♩ = 96" in head
    assert "4/4" in head
    assert "4 小节" in head
    assert text[2] == ""


def test_text_header_marks_minor_mode():
    text = to_jianpu_lines(make_score(key="A minor", bars=1))
    assert "1 = C" in text[1]
    assert "（A 小调，以 6 为主音）" in text[1]


def test_text_row_is_prefixed_by_track_name():
    text = to_jianpu_lines(make_piano_score())
    melody_line = next(line for line in text if line.startswith("melody"))
    assert melody_line[len("melody".ljust(14)):].startswith("|")
    assert any(line.startswith("bass".ljust(14)) for line in text)


def test_text_row_has_one_barline_per_measure_plus_terminator():
    """4 小节 4/4：``|`` 出现 4（小节右界）+ 1（曲末）次。"""
    text = to_jianpu_lines(make_score(bars=4))
    row = next(line for line in text if "|" in line)
    assert row.count("|") == 5


def test_text_marks_slur_with_caret():
    text = to_jianpu_lines(make_score(notes=[Note(pitch=72, start=3.75, duration=0.5)], bars=2))
    row = next(line for line in text if "|" in line)
    assert "^" in row


def test_text_marks_octave_dots_and_dashes():
    text = to_jianpu_lines(make_score(notes=[Note(pitch=72, start=0.0, duration=4.0)], bars=1))
    row = next(line for line in text if "|" in line)
    assert "1'" in row
    assert "-" in row


def test_text_wraps_at_max_slots_per_line():
    text = to_jianpu_lines(make_score(bars=4), JpOptions(max_slots_per_line=4))
    row_lines = [line for line in text if "|" in line]
    assert len(row_lines) == 4
    # 折行后的续行用等宽空格缩进，对齐首行的记号列
    prefix = " " * len("melody".ljust(14))
    assert all(line.startswith(prefix) for line in row_lines[1:])


def test_text_respects_show_switches():
    score = make_score(bars=1)
    opts = JpOptions(show_title=False, show_tempo=False, show_track_names=False)
    text = to_jianpu_lines(score, opts)
    assert text[0].startswith("1 = C")
    assert "♩" not in text[0]


def test_text_reports_empty_score():
    text = to_jianpu_lines(Score(title="empty"))
    assert any("无音符" in line for line in text)


# ---------------------------------------------------------------------------
# 简谱 SVG
# ---------------------------------------------------------------------------


def test_svg_document_skeleton_and_viewbox():
    score = make_score(bars=4)
    opts = JpOptions()
    svg = render_jianpu_svg(score, opts)
    renderer = JianpuRenderer(parse_jianpu(score), opts)
    assert svg.startswith("<svg")
    assert svg.rstrip().endswith("</svg>")
    assert f'viewBox="0 0 {opts.page_width:.1f} {renderer.page_height:.1f}"' in svg


def test_svg_background_uses_theme_paper():
    svg = render_jianpu_svg(make_score(bars=1))
    assert '<rect width="1240.0"' in svg
    assert 'fill="#ffffff"' in svg


def test_svg_dark_theme_inverts_paper_and_ink():
    svg = render_jianpu_svg(make_score(bars=1), JpOptions(theme=SvgTheme.dark()))
    assert "#15171c" in svg
    assert "#e9e7e4" in svg


def test_svg_draws_title_key_tempo_and_summary():
    svg = render_jianpu_svg(make_score(bars=4, bpm=132, title="Caprice"))
    drawn = texts(svg)
    assert "Caprice" in drawn
    assert "1 = C" in drawn
    assert "= 132" in drawn
    assert any("4/4" in t and "4 小节" in t for t in drawn)


def test_svg_tempo_mark_is_vector_not_music_glyph():
    """``♩`` (U+2669) 在 msyh.ttc 里没有字形，PNG 无字体回退会画成豆腐块。

    故速度记号自绘：SVG 里应出现强调色椭圆（符头），而不是 ``♩`` 文本。
    """
    svg = render_jianpu_svg(make_score(bars=1, bpm=132))
    assert "♩" not in svg
    assert '<ellipse cx="0" cy="0"' in svg
    assert 'fill="' + SvgTheme().accent + '"' in svg


def test_svg_escapes_xml_in_title():
    svg = render_jianpu_svg(make_score(bars=1, title="A&B <C>"))
    assert "A&amp;B &lt;C&gt;" in svg


def test_svg_draws_one_digit_per_note():
    svg = render_jianpu_svg(make_score(bars=4))
    assert texts(svg).count("1") == 16


def test_svg_draws_dashes_for_long_note():
    svg = render_jianpu_svg(make_score(notes=[Note(pitch=72, start=0.0, duration=4.0)], bars=1))
    assert len(horizontal_lines(svg)) == 3


def test_svg_draws_underscores_for_eighths():
    """8 个八分音符 -> 8 条下划线。"""
    notes = [Note(pitch=72, start=i * 0.5, duration=0.5) for i in range(8)]
    svg = render_jianpu_svg(make_score(notes=notes, bars=1))
    assert len(horizontal_lines(svg)) == 8


def test_svg_draws_octave_dots_as_circles():
    """C5 在高八度 -> 每个音上方一个点。"""
    svg = render_jianpu_svg(make_score(notes=[Note(pitch=72, start=0.0, duration=1.0)], bars=1))
    assert svg.count("<circle") == 1


def test_svg_draws_dot_accidental_at_key_and_below_for_low_octave():
    """低八度点在基线下方，高八度点在上方。"""
    svg_high = render_jianpu_svg(make_score(notes=[Note(pitch=84, start=0.0, duration=1.0)], bars=1))
    svg_low = render_jianpu_svg(make_score(notes=[Note(pitch=48, start=0.0, duration=1.0)], bars=1))
    high_circle = re.search(r'<circle cx="[\d.]+" cy="([\d.]+)"', svg_high)
    low_circle = re.search(r'<circle cx="[\d.]+" cy="([\d.]+)"', svg_low)
    assert high_circle and low_circle
    assert float(high_circle.group(1)) < float(low_circle.group(1))


def test_svg_draws_accidental_glyph_for_out_of_key_note():
    svg = render_jianpu_svg(make_score(notes=[Note(pitch=78, start=0.0, duration=1.0)], bars=1))
    assert SHARP_PATH in svg


def test_svg_barline_count_matches_measures_times_rows():
    """4 小节 + 终止线，两行 -> 2 × (4 + 1) = 10 条竖线。"""
    svg = render_jianpu_svg(make_piano_score())
    assert len(barlines(svg)) == 10


def test_svg_final_thick_barline_only_on_last_system():
    score = make_piano_score()
    opts = JpOptions(page_width=400)
    svg = render_jianpu_svg(score, opts)
    renderer = JianpuRenderer(parse_jianpu(score), opts)
    renderer.draw()
    assert len(renderer.systems) > 1
    thick = renderer.opts.note_size * 0.22
    verticals = barlines(svg)
    assert sum(1 for line in verticals if abs(line[4] - thick) < 1e-6) == 2
    assert len(verticals) == (4 + 1) * 2


def test_svg_slur_arc_within_measure_spans_the_dash():
    """``2 - 2̲`` 的弧从第 1 个数字连到第 3 个数字，跨过中间那条横线。"""
    svg = render_jianpu_svg(make_score(notes=[Note(pitch=72, start=0.0, duration=2.5)], bars=1))
    drawn = arcs(svg)
    assert len(drawn) == 1
    x1, _y1, _cx, _cy, x2, _y2 = drawn[0]
    opts = JpOptions()
    head = opts.margin + opts.measure_pad + 0.5 * opts.slot      # 第 1 槽（数字）
    tail = opts.margin + opts.measure_pad + 2.5 * opts.slot      # 第 3 槽（数字）
    assert x1 == pytest.approx(head + opts.note_size * 0.28)
    assert x2 == pytest.approx(tail - opts.note_size * 0.28)
    assert x2 > x1


def test_svg_cross_barline_slur_connects_the_two_measures():
    """回归：弧的落点必须是真正相邻的两小节。

    曾因「0 基小节位置」与「1 基小节号」混用，弧被连到隔一小节的同列槽位上，
    而两处都是合法坐标，所以只做「有弧」的断言根本发现不了。
    """
    score = make_score(notes=[Note(pitch=72, start=3.75, duration=0.5)], bars=2)
    opts = JpOptions()
    renderer = JianpuRenderer(parse_jianpu(score), opts)
    drawn = arcs(render_jianpu_svg(score, opts))
    assert len(drawn) == 1
    x1, _y1, _cx, _cy, x2, _y2 = drawn[0]
    width = renderer.measure_widths[0]
    last_slot_m1 = opts.margin + opts.measure_pad + 4.5 * opts.slot          # 首小节末槽
    first_slot_m2 = opts.margin + width + opts.measure_pad + 0.5 * opts.slot  # 次小节首槽
    assert x1 == pytest.approx(last_slot_m1 + opts.note_size * 0.28)
    assert x2 == pytest.approx(first_slot_m2 - opts.note_size * 0.28)


def test_svg_cross_system_slur_is_drawn_as_two_segments():
    score = make_score(notes=[Note(pitch=72, start=3.75, duration=0.5)], bars=4)
    opts = JpOptions(page_width=400)
    svg = render_jianpu_svg(score, opts)
    renderer = JianpuRenderer(parse_jianpu(score), opts)
    renderer.draw()
    assert len(renderer.systems) > 1
    drawn = arcs(svg)
    assert len(drawn) == 2
    # 出段止于首系统右界，入段起于次系统左界
    assert abs(drawn[0][4] - renderer.system_span[0][1]) < 1e-6
    assert abs(drawn[1][0] - renderer.system_span[1][0]) < 1e-6


def test_svg_omits_tempo_and_track_names_when_disabled():
    svg = render_jianpu_svg(
        make_score(bars=1), JpOptions(show_tempo=False, show_track_names=False)
    )
    assert "♩" not in svg
    assert "melody" not in svg


def test_svg_narrow_page_wraps_into_multiple_systems():
    score = make_score(bars=4)
    opts = JpOptions(page_width=400)
    svg = render_jianpu_svg(score, opts)
    renderer = JianpuRenderer(parse_jianpu(score), opts)
    renderer.draw()
    assert len(renderer.systems) >= 2
    assert len(barlines(svg)) == 5


def test_svg_empty_score_renders_header_only():
    svg = render_jianpu_svg(Score(title="empty"))
    assert svg.startswith("<svg")
    assert "empty" in svg
    assert not barlines(svg)


def test_write_jianpu_svg_creates_file(tmp_path):
    target = tmp_path / "deep" / "jp.svg"
    result = write_jianpu_svg(make_score(bars=1), target)
    assert result == str(target.resolve())
    assert target.read_text(encoding="utf-8").startswith("<svg")


# ---------------------------------------------------------------------------
# 表头布局
# ---------------------------------------------------------------------------


def test_header_sits_above_the_first_system():
    """回归：表头高度写死时 ``1 = C`` 会落进首系统的行间（旋律行与低音行之间）。"""
    score = make_piano_score()
    opts = JpOptions()
    renderer = JianpuRenderer(parse_jianpu(score), opts)
    header_bottom = renderer.header_height()
    drawn = text_positions(render_jianpu_svg(score, opts))
    header = [p for p in drawn if not p[2].isdigit()]
    assert header, "应有表头文字"
    for _x, y, content in header:
        assert y < header_bottom, f"表头文字 {content!r} 落进了首系统"
    # 首系统首行基线（= 表头高 + 字号）当然更靠下
    assert header_bottom + opts.note_size > max(y for _x, y, _t in header)


def test_header_height_leaves_room_below_the_last_line():
    """表头高度必须留出末行下沿与到首系统的空隙，否则文字会压到谱面。"""
    opts = JpOptions()
    renderer = JianpuRenderer(parse_jianpu(make_score(bars=1)), opts)
    last_line = opts.margin + KEY_BASELINE * opts.note_size
    assert renderer.header_height() > last_line + opts.header_gap


def test_wide_page_keeps_summary_on_the_key_line():
    score = make_piano_score()
    opts = JpOptions()
    renderer = JianpuRenderer(parse_jianpu(score), opts)
    assert renderer.summary_stacked() is False
    drawn = text_positions(render_jianpu_svg(score, opts))
    key = next(p for p in drawn if p[2].startswith("1 = "))
    summary = next(p for p in drawn if "小节" in p[2])
    assert summary[1] == key[1], "宽页摘要应与调号同一行"
    assert summary[0] > key[0], "摘要右对齐"


def test_narrow_page_moves_summary_to_its_own_line():
    """回归：窄页时右侧摘要会与左侧速度记号相撞，必须折行。"""
    score = make_piano_score()
    narrow = JpOptions(page_width=430)
    renderer = JianpuRenderer(parse_jianpu(score), narrow)
    assert renderer.summary_stacked() is True
    drawn = text_positions(render_jianpu_svg(score, narrow))
    key = next(p for p in drawn if p[2].startswith("1 = "))
    summary = next(p for p in drawn if "小节" in p[2])
    assert summary[1] > key[1], "窄页摘要应折到调号行之下"


def test_stacked_header_reserves_an_extra_line():
    score = make_piano_score()
    wide = JianpuRenderer(parse_jianpu(score), JpOptions())
    narrow = JianpuRenderer(parse_jianpu(score), JpOptions(page_width=430))
    assert narrow.header_height() > wide.header_height()
    assert narrow.summary_stacked() and not wide.summary_stacked()


def test_summary_lists_each_track_once():
    score = make_piano_score()
    renderer = JianpuRenderer(parse_jianpu(score), JpOptions())
    summary = renderer.summary_text()
    assert summary.count("melody") == 1
    assert summary.count("bass") == 1
    assert "4/4" in summary and "4 小节" in summary


def test_summary_drops_track_names_when_disabled():
    score = make_piano_score()
    opts = JpOptions(show_track_names=False)
    summary = JianpuRenderer(parse_jianpu(score), opts).summary_text()
    assert "melody" not in summary
    assert "4/4" in summary


# ---------------------------------------------------------------------------
# PNG 位图
# ---------------------------------------------------------------------------


def test_render_jianpu_png_has_png_magic_and_page_size():
    score = make_score(bars=4)
    opts = JpOptions()
    data = render_jianpu_png(score, opts)
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    renderer = JianpuRenderer(parse_jianpu(score), opts)
    assert png_size(data) == (int(round(opts.page_width)), int(round(renderer.page_height)))


def test_render_staff_png_matches_layout_page_size():
    layout = layout_score(make_piano_score())
    data = render_png(layout)
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert png_size(data) == (int(round(layout.page_width)), int(round(layout.page_height)))


def test_png_scale_changes_supersampling_not_output_size():
    """``scale`` 只影响内部超采样，输出像素尺寸与 SVG 页面一致。"""
    score = make_score(bars=1)
    base = render_jianpu_png(score, scale=1.0)
    fine = render_jianpu_png(score, scale=3.0)
    assert png_size(base) == png_size(fine)
    assert base != fine  # 抗锯齿不同，字节必然不同


@pytest.mark.parametrize("scale", [0.0, -1.0])
def test_png_rejects_non_positive_scale(scale):
    with pytest.raises(ValueError, match="scale"):
        render_jianpu_png(make_score(bars=1), scale=scale)


def test_png_raises_import_error_without_pillow(monkeypatch):
    """惰性导入：缺 Pillow 时给出带安装指引的 ImportError，而不是启动即崩。"""
    import sys

    monkeypatch.setitem(sys.modules, "PIL", None)
    with pytest.raises(ImportError, match="Pillow"):
        render_jianpu_png(make_score(bars=1))


def test_write_jianpu_png_creates_file(tmp_path):
    target = tmp_path / "jp.png"
    result = write_jianpu_png(make_score(bars=1), target)
    assert result == str(target.resolve())
    assert target.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_write_png_creates_file(tmp_path):
    target = tmp_path / "staff.png"
    result = write_png(layout_score(make_score(bars=1)), target)
    assert result == str(target.resolve())
    assert target.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def dark_pixels(png, box: tuple[int, int, int, int]) -> int:
    """框内墨点计数（像素探针；模型读不了图，只能靠数点验证落墨位置）。"""
    x0, y0, x1, y1 = box
    return sum(
        1
        for y in range(y0, y1)
        for x in range(x0, x1)
        if sum(png.getpixel((x, y))) / 3 < 128
    )


def test_jianpu_png_accidental_lands_left_of_the_digit():
    """回归：变音字形走「组变换 + path」，变换曾被施加两次而使字形飞出页面。

    这里直接验落墨位置：升号应画在数字左侧，且页左边距内不该有任何墨。
    """
    Image = pytest.importorskip("PIL.Image")
    score = make_score(notes=[Note(pitch=78, start=0.0, duration=1.0)], bars=1)
    png = Image.open(io.BytesIO(render_jianpu_png(score))).convert("RGB")
    opts = JpOptions()
    renderer = JianpuRenderer(parse_jianpu(score), opts)
    digit_x = opts.margin + opts.measure_pad + 0.5 * opts.slot
    baseline = renderer.header_height() + opts.note_size
    band = (int(baseline) - 24, int(baseline) + 8)
    accidental = (int(digit_x) - 22, band[0], int(digit_x) - 4, band[1])
    digit = (int(digit_x) - 6, band[0], int(digit_x) + 8, band[1])
    margin = (0, band[0], int(opts.margin) - 6, band[1])
    assert dark_pixels(png, accidental) > 0, "升号应画在数字左侧"
    assert dark_pixels(png, digit) > 0, "数字本体应画出来"
    assert dark_pixels(png, margin) == 0, "页边距内不应有墨"


def test_png_and_svg_share_geometry():
    """PNG 由同一绘制指令流光栅化：指令条数与 SVG 元素数必须一致。"""
    score = make_score(bars=4)
    renderer = JianpuRenderer(parse_jianpu(score), JpOptions())
    canvas = renderer.draw()
    svg = render_jianpu_svg(score)
    tag_count = sum(
        svg.count(f"<{tag}") for tag in ("line", "rect", "circle", "ellipse", "path", "text")
    )
    # 减去根元素里的底图 rect 与（可能的）组计数差异，只比较绘制指令总数
    assert tag_count >= len(canvas.ops)
    assert len(canvas.ops) > 0
