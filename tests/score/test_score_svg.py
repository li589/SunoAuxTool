"""五线谱 SVG 渲染测试。

覆盖重点
--------
- 文档骨架：``<svg>`` 完整性、viewBox 与页面一致、主题配色与 XML 转义。
- 谱表要素：五条谱线、加线、自绘谱号（高音描边路径 / 低音豆形 + 两点）、符干/符头。
- 调号与变音记号：数量随五度圈；调号已含的变音不再标在符头上。
- 拍号与小节线：拍号只在首系统、终止粗细线只在末系统且落在末小节右界。
- 小节号只出现在系统首条谱表。
- 休止符、符杠（成对相连）、符尾（孤立短音符）。
- 延音线：同小节、跨小节、跨系统三种情形（跨系统画为出入两段）。
- 落盘 ``write_svg``、选项开关、空谱边界。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sunoauxtool.models.notes import Note, NoteSequence
from sunoauxtool.score import LayoutOptions, Score, layout_score
from sunoauxtool.score.svg import (
    ACCIDENTAL_STROKES,
    BARLINE_THICK_W,
    BARLINE_W,
    BASS_CLEF_BODY,
    BASS_CLEF_TAIL,
    LEDGER_W,
    NOTEHEAD_RX,
    QUARTER_REST,
    STAFF_LINE_W,
    STEM_W,
    SvgOptions,
    SvgTheme,
    WHOLE_RX,
    render_svg,
    write_svg,
)

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

_LINE_RE = re.compile(
    r'<line x1="(-?[\d.]+)" y1="(-?[\d.]+)" x2="(-?[\d.]+)" y2="(-?[\d.]+)" '
    r'stroke="(#[0-9a-fA-F]{6})" stroke-width="([\d.]+)"/>'
)
_TEXT_RE = re.compile(
    r'<text x="(-?[\d.]+)" y="(-?[\d.]+)" font-family="([^"]*)" font-size="([\d.]+)" '
    r'fill="(#[0-9a-fA-F]{6})" text-anchor="(\w+)" font-weight="(\w+)" '
    r'font-style="(\w+)">(.*?)</text>'
)
_FILL_RE = re.compile(r'<text [^>]*fill="(#[0-9a-fA-F]{6})"[^>]*>(.*?)</text>')

#: 自绘字形在 SVG 中的可识别前缀（取路径开头若干字符足以唯一定位）。
SHARP_STROKE = ACCIDENTAL_STROKES[1][0][0]
FLAT_STROKE = ACCIDENTAL_STROKES[-1][0][0]


def make_sequence(
    key: str = "C major",
    time_signature: str = "4/4",
    bars: int = 4,
    bpm: int = 120,
    melody: list[Note] | None = None,
    with_bass: bool = True,
) -> NoteSequence:
    """构造 1~2 轨的 NoteSequence。"""
    seq = NoteSequence(
        bpm=bpm, key=key, time_signature=time_signature, bars=bars, style="test"
    )
    if melody is None:
        pitches = [72, 74, 76, 74, 72, 71, 69, 67, 69, 72, 76, 74, 72, 71, 69, 67]
        melody = [
            Note(pitch=p, start=float(i), duration=1.0, velocity=80)
            for i, p in enumerate(pitches[: bars * 4])
        ]
    seq.add_track("melody", 0, 0, melody)
    if with_bass:
        seq.add_track(
            "bass",
            32,
            1,
            [Note(pitch=p, start=float(i * 4), duration=4.0, velocity=60)
             for i, p in enumerate([45, 41, 43, 45][:bars])],
        )
    return seq


def make_score(title: str = "T", **kwargs) -> Score:
    """从 NoteSequence 构造 Score。"""
    return Score.from_sequence(make_sequence(**kwargs), title=title)


def make_layout(**kwargs) -> object:
    """一步到位：构造 Score 并排版。"""
    title = kwargs.pop("title", "T")
    return layout_score(make_score(title=title, **kwargs))


def render(**kwargs) -> tuple[object, str]:
    """返回 (layout, svg)。"""
    layout = make_layout(**kwargs)
    return layout, render_svg(layout)


def all_clusters(layout):
    """摊平全部实音簇（不含休止符）。"""
    return [
        c
        for system in layout.systems
        for staff in system.staffs
        for measure in staff.measures
        for c in measure.clusters
        if not c.is_rest
    ]


#: 同义别名，读起来更贴语境。
_sounding = all_clusters


def parse_lines(svg: str) -> list[tuple[float, float, float, float, str, float]]:
    """把全部 ``<line>`` 解析为 (x1, y1, x2, y2, stroke, width)。"""
    return [
        (float(a), float(b), float(c), float(d), e, float(f))
        for a, b, c, d, e, f in _LINE_RE.findall(svg)
    ]


def parse_texts(svg: str) -> list[dict]:
    """把全部 ``<text>`` 解析为字典列表（x/y/内容/颜色/锚点/字重/字形）。"""
    keys = ("x", "y", "family", "size", "fill", "anchor", "weight", "style", "content")
    out = []
    for match in _TEXT_RE.finditer(svg):
        item = dict(zip(keys, match.groups()))
        item["x"] = float(item["x"])
        item["y"] = float(item["y"])
        item["size"] = float(item["size"])
        out.append(item)
    return out


def text_element(svg: str, content: str) -> list[dict]:
    """按文本内容取全部 ``<text>``。"""
    return [t for t in parse_texts(svg) if t["content"] == content]


def staff_line_count(layout) -> int:
    """期望的谱线根数：每谱表 5 根。"""
    return 5 * sum(len(system.staffs) for system in layout.systems)


def barline_xs(layout) -> set[float]:
    """所有小节线的 x（以第一谱表为准，各谱表槽位一致）。"""
    return {
        round(measure.right, 2)
        for system in layout.systems
        for measure in system.staffs[0].measures
    }


def stem_x(cluster) -> float:
    """簇的符干 x（与渲染层同一口径）。"""
    return cluster.x + (NOTEHEAD_RX * 9.0 if cluster.stem_up else -NOTEHEAD_RX * 9.0)


# ---------------------------------------------------------------------------
# 文档骨架与主题
# ---------------------------------------------------------------------------


def test_render_returns_complete_svg_document():
    _, svg = render()
    stripped = svg.strip()
    assert stripped.startswith("<svg ")
    assert stripped.endswith("</svg>")
    assert svg.count("<svg ") == 1
    assert 'xmlns="http://www.w3.org/2000/svg"' in svg


def test_viewbox_matches_layout_page():
    layout, svg = render()
    assert f'viewBox="0 0 {layout.page_width:.1f} {layout.page_height:.1f}"' in svg
    assert f'width="{layout.page_width:.1f}" height="{layout.page_height:.1f}"' in svg


def test_background_rect_uses_theme_paper():
    _, svg = render()
    assert '<rect width="1240.0" height=' in svg
    assert f'fill="{SvgTheme().paper}"' in svg


def test_dark_theme_swaps_paper_and_ink():
    layout = make_layout()
    dark = SvgTheme.dark()
    svg = render_svg(layout, SvgOptions(theme=dark))
    assert f'fill="{dark.paper}"' in svg
    assert f'stroke="{dark.ink}"' in svg
    assert SvgTheme().paper not in svg


def test_accent_colour_used_for_tempo_only():
    """强调色专供速度记号：符头椭圆 + 符干 + 数字各一处。"""
    _, svg = render()
    accent = SvgTheme().accent
    assert svg.count(f'fill="{accent}"') == 2      # 符头椭圆、``= 120`` 文字
    assert svg.count(f'stroke="{accent}"') == 1    # 符干
    assert "♩" not in svg


def test_xml_special_chars_in_title_are_escaped():
    layout = make_layout(title="A & B <C>")
    svg = render_svg(layout)
    assert "A &amp; B &lt;C&gt;" in svg
    assert "A & B <C>" not in svg


def test_options_can_override_title_text():
    layout = make_layout(title="Original")
    svg = render_svg(layout, SvgOptions(title="Override"))
    assert ">Override</text>" in svg
    assert ">Original</text>" not in svg


# ---------------------------------------------------------------------------
# 谱线与谱表
# ---------------------------------------------------------------------------


def test_staff_lines_are_five_per_staff():
    layout, svg = render()
    expected = staff_line_count(layout)
    assert expected == 10  # 1 系统 × 2 谱表
    width = f"{STAFF_LINE_W * layout.space:.3f}"
    assert svg.count(f'stroke-width="{width}"') == expected


def test_staff_lines_span_margin_to_last_measure():
    layout, svg = render()
    width = STAFF_LINE_W * layout.space
    staff_xs = [x1 for x1, _, _, _, _, w in parse_lines(svg) if abs(w - width) < 1e-9]
    assert all(x == pytest.approx(layout.options.margin - 1.5 * layout.space) for x in staff_xs)
    right = layout.systems[0].staffs[0].measures[-1].right
    assert all(x2 == pytest.approx(right) for _, _, x2, _, _, w in parse_lines(svg)
               if abs(w - width) < 1e-9)


def test_empty_score_has_no_staff_lines():
    layout = layout_score(Score(title="Empty", bars=0))
    svg = render_svg(layout)
    assert layout.systems == []
    width = f"{STAFF_LINE_W * layout.space:.3f}"
    assert f'stroke-width="{width}"' not in svg
    assert svg.startswith("<svg ")


def test_high_pitch_draws_ledger_lines():
    """C6 在高音谱表上方两线 → 2 根加线，长度 1.24 space，居中于符头。"""
    melody = [Note(pitch=84, start=0.0, duration=4.0, velocity=80)]
    layout = make_layout(melody=melody, bars=1, with_bass=False)
    svg = render_svg(layout)
    ledger_width = float(f"{LEDGER_W * layout.space:.3f}")
    ledgers = [line for line in parse_lines(svg) if line[5] == ledger_width]
    assert len(ledgers) == 2
    cluster = all_clusters(layout)[0]
    half = 0.62 * layout.space
    for x1, _, x2, _, _, _ in ledgers:
        assert x1 == pytest.approx(cluster.x - half)
        assert x2 == pytest.approx(cluster.x + half)


def test_diatonic_pitch_inside_staff_has_no_ledger_lines():
    melody = [Note(pitch=72, start=float(i), duration=1.0, velocity=80) for i in range(4)]
    layout = make_layout(melody=melody, bars=1)
    svg = render_svg(layout)
    ledger_width = float(f"{LEDGER_W * layout.space:.3f}")
    assert not [line for line in parse_lines(svg) if line[5] == ledger_width]


def test_stem_drawn_at_expected_x_for_first_cluster():
    layout, svg = render()
    cluster = all_clusters(layout)[0]
    x = stem_x(cluster)
    stem_width = float(f"{STEM_W * layout.space:.3f}")
    matches = [
        line
        for line in parse_lines(svg)
        if abs(line[0] - x) < 0.01 and abs(line[2] - x) < 0.01 and line[5] == stem_width
    ]
    assert len(matches) == 1


def test_whole_note_has_neither_stem_nor_flag():
    melody = [Note(pitch=72, start=0.0, duration=4.0, velocity=80)]
    layout = make_layout(melody=melody, bars=1, with_bass=False)
    svg = render_svg(layout)
    cluster = all_clusters(layout)[0]
    assert cluster.note_type == "whole"
    assert cluster.beams == 0
    x = stem_x(cluster)
    stem_width = float(f"{STEM_W * layout.space:.3f}")
    assert not [
        line for line in parse_lines(svg)
        if abs(line[0] - x) < 0.01 and line[5] == stem_width
    ]
    assert f'rx="{WHOLE_RX * layout.space:.3f}"' in svg


def test_half_note_head_is_hollow():
    melody = [Note(pitch=72, start=0.0, duration=2.0, velocity=80),
              Note(pitch=74, start=2.0, duration=2.0, velocity=80)]
    layout = make_layout(melody=melody, bars=1, with_bass=False)
    svg = render_svg(layout)
    assert SvgTheme().paper in svg  # 空心符头以内填纸色实现
    assert f'stroke-width="{0.17 * layout.space:.3f}"' in svg


# ---------------------------------------------------------------------------
# 谱号
# ---------------------------------------------------------------------------


def test_treble_clef_drawn_as_stroke_path():
    layout, svg = render()
    assert "M -0.50,5.45 " in svg           # TREBLE_CLEF 起点
    trebles = [s for s in layout.systems[0].staffs if s.clef == "treble"]
    assert trebles
    assert layout.systems[0].staffs[1].clef == "bass"


def test_bass_clef_draws_body_tail_and_two_dots():
    layout, svg = render()
    assert BASS_CLEF_BODY[:14] in svg
    assert BASS_CLEF_TAIL[:14] in svg
    assert 'stroke-width="0.240"' in svg    # 低音谱号右上细尾（宽度统一按 :.3f 序列化）
    assert svg.count("<circle ") == 2        # 两点
    assert layout.systems[0].staffs[1].clef == "bass"


def test_single_treble_score_has_no_bass_glyph():
    layout, svg = render(with_bass=False)
    assert layout.systems[0].staffs[0].clef == "treble"
    assert BASS_CLEF_BODY[:14] not in svg
    assert svg.count("<circle ") == 0


def test_wide_range_piano_track_gets_bass_clef():
    """非约定名轨道：低音区音符多，应由「越界距离取小」判为低音谱号。"""
    seq = make_sequence(bars=2, with_bass=False)
    seq.tracks[0].name = "piano"          # 避开 melody/lead 的轨道名约定
    seq.tracks[0].notes = [
        Note(pitch=p, start=float(i), duration=1.0, velocity=80)
        for i, p in enumerate([36, 40, 43, 45, 48, 52, 55, 57])
    ]
    layout = layout_score(Score.from_sequence(seq, title="T"))
    assert layout.systems[0].staffs[0].clef == "bass"


def test_track_name_convention_overrides_pitch_distribution():
    """显式命名优先于音高分布：叫 melody 的轨即使全在低音区也用高音谱号。"""
    seq = make_sequence(bars=1, with_bass=False)
    seq.tracks[0].notes = [
        Note(pitch=p, start=float(i), duration=1.0, velocity=80)
        for i, p in enumerate([36, 40, 43, 45])
    ]
    layout = layout_score(Score.from_sequence(seq, title="T"))
    assert layout.systems[0].staffs[0].clef == "treble"


# ---------------------------------------------------------------------------
# 调号与变音记号
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "fifths"),
    [("C major", 0), ("G major", 1), ("D major", 2), ("A major", 3), ("F# major", 6)],
)
def test_sharp_key_signature_count(key, fifths):
    layout, svg = render(key=key, bars=2)
    n_staffs = sum(len(system.staffs) for system in layout.systems)
    assert layout.fifths == fifths
    assert svg.count(SHARP_STROKE) == fifths * n_staffs


@pytest.mark.parametrize(
    ("key", "fifths"),
    [("F major", -1), ("Bb major", -2), ("Eb major", -3), ("Gb major", -6)],
)
def test_flat_key_signature_count(key, fifths):
    layout, svg = render(key=key, bars=2)
    n_staffs = sum(len(system.staffs) for system in layout.systems)
    assert layout.fifths == fifths
    assert svg.count(FLAT_STROKE) == abs(fifths) * n_staffs


def test_no_key_signature_in_c_major():
    layout, svg = render(key="C major")
    assert layout.fifths == 0
    assert SHARP_STROKE not in svg
    assert FLAT_STROKE not in svg


def test_chromatic_note_gets_accidental_glyph():
    """C 大调里的 F#（非调号音）必须标升记号。"""
    melody = [Note(pitch=p, start=float(i), duration=1.0, velocity=80)
              for i, p in enumerate([66, 67, 69, 71, 69, 67, 66, 64])]
    layout, svg = render(key="C major", melody=melody, bars=2, with_bass=False)
    assert layout.fifths == 0
    assert svg.count(SHARP_STROKE) >= 1


def test_key_signature_note_carries_no_extra_accidental():
    """G 大调里的 F# 是调号音，不应在符头上重复标升号。"""
    melody = [Note(pitch=66, start=float(i), duration=1.0, velocity=80) for i in range(4)]
    layout = make_layout(key="G major", melody=melody, bars=1, with_bass=False)
    svg = render_svg(layout)
    assert layout.fifths == 1
    n_staffs = sum(len(system.staffs) for system in layout.systems)
    assert svg.count(SHARP_STROKE) == 1 * n_staffs


# ---------------------------------------------------------------------------
# 拍号
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("ts", "num", "den"), [("3/4", "3", "4"), ("6/8", "6", "8"), ("5/4", "5", "4")])
def test_time_signature_digits(ts, num, den):
    """分子占谱表上半、分母占下半，各自居中于同一 x。"""
    layout, svg = render(time_signature=ts, bars=2, with_bass=False)
    staff_y = layout.systems[0].y + layout.systems[0].staffs[0].y
    mid_line = staff_y - 2 * layout.space
    top_line = staff_y - 4 * layout.space
    numerator = text_element(svg, num)
    denominator = text_element(svg, den)
    assert len(numerator) == 1
    assert len(denominator) == 1
    assert numerator[0]["y"] < mid_line
    assert numerator[0]["y"] > top_line - 3 * layout.space   # 不越出谱表
    assert denominator[0]["y"] > mid_line
    assert denominator[0]["y"] < staff_y + layout.space
    assert numerator[0]["x"] == pytest.approx(denominator[0]["x"])
    assert numerator[0]["anchor"] == "middle"


def test_common_time_signature_four_four():
    """4/4：分子分母同为 "4"，共两个数字文本，一上一下。"""
    layout, svg = render(time_signature="4/4", bars=2, with_bass=False)
    staff_y = layout.systems[0].y + layout.systems[0].staffs[0].y
    mid_line = staff_y - 2 * layout.space
    fours = text_element(svg, "4")
    assert len(fours) == 2
    above, below = sorted(fours, key=lambda t: t["y"])
    assert above["y"] < mid_line < below["y"]


def test_time_signature_drawn_once_only_on_first_system():
    notes = []
    for bar in range(16):
        notes.extend(
            Note(pitch=69, start=float(bar * 4 + k), duration=1.0, velocity=80)
            for k in range(4)
        )
    layout = make_layout(melody=notes, bars=16, time_signature="3/4", with_bass=False)
    svg = render_svg(layout)
    assert len(layout.systems) >= 2
    assert len(text_element(svg, "3")) == 1     # 只有首个系统画一次


# ---------------------------------------------------------------------------
# 小节线
# ---------------------------------------------------------------------------


def _barlines(layout, svg):
    """取出小节线（按 x 落在小节右界过滤；符干线宽与小节线相同，必须靠 x 区分）。"""
    bar_width = float(f"{BARLINE_W * layout.space:.3f}")
    xs = barline_xs(layout)
    return [
        line for line in parse_lines(svg)
        if line[5] == bar_width and round(line[0], 2) in xs
    ]


def test_one_barline_per_measure_boundary():
    layout, svg = render()
    n_measures = sum(len(system.staffs[0].measures) for system in layout.systems)
    drawn = _barlines(layout, svg)
    assert len(drawn) == n_measures
    assert {round(line[0], 2) for line in drawn} == barline_xs(layout)


def test_barlines_not_multiplied_by_staff_count():
    """双谱表也只画一条贯穿线，而不是每条谱表各画一条。"""
    layout, svg = render()
    assert len(layout.systems[0].staffs) == 2
    n_measures = len(layout.systems[0].staffs[0].measures)
    assert len(_barlines(layout, svg)) == n_measures


def test_final_thick_barline_only_on_last_system():
    notes = []
    for bar in range(16):
        notes.extend(
            Note(pitch=69, start=float(bar * 4 + k), duration=1.0, velocity=80)
            for k in range(4)
        )
    layout = make_layout(melody=notes, bars=16, with_bass=False)
    svg = render_svg(layout)
    assert len(layout.systems) >= 2
    thick_width = float(f"{BARLINE_THICK_W * layout.space:.3f}")
    thick = [line for line in parse_lines(svg) if line[5] == thick_width]
    assert len(thick) == 1
    last_right = layout.systems[-1].staffs[0].measures[-1].right
    assert thick[0][0] == pytest.approx(last_right - thick_width, abs=0.01)
    first_right = layout.systems[0].staffs[0].measures[-1].right
    assert abs(thick[0][0] - (first_right - thick_width)) > 1.0


def test_barline_spans_first_staff_top_to_last_staff_bottom():
    layout, svg = render()
    bar = _barlines(layout, svg)[0]
    system = layout.systems[0]
    assert bar[1] == pytest.approx(system.y + system.staffs[0].y - 4 * layout.space)
    assert bar[3] == pytest.approx(system.y + system.staffs[-1].y)


# ---------------------------------------------------------------------------
# 小节号
# ---------------------------------------------------------------------------


def test_measure_numbers_only_on_first_staff():
    layout, svg = render()
    n_systems = len(layout.systems)
    faint = SvgTheme().faint
    faint_texts = [m.group(2) for m in _FILL_RE.finditer(svg) if m.group(1) == faint]
    # 每系统一个小节号（只画在首条谱表）+ 页脚一条调号说明
    assert len(faint_texts) == n_systems + 1
    assert faint_texts[0] == str(layout.systems[0].staffs[0].measures[0].index)


def test_measure_number_sits_above_first_staff():
    layout, svg = render()
    staff = layout.systems[0].staffs[0]
    top_line = layout.systems[0].y + staff.y - 4 * layout.space
    numbers = [
        t for t in parse_texts(svg)
        if t["fill"] == SvgTheme().faint and t["content"].isdigit()
    ]
    assert len(numbers) == len(layout.systems)
    assert all(t["y"] < top_line for t in numbers)
    assert numbers[0]["content"] == str(staff.measures[0].index)


def test_measure_numbers_can_be_disabled():
    layout = make_layout()
    svg = render_svg(layout, SvgOptions(show_measure_numbers=False))
    faint_texts = [m.group(2) for m in _FILL_RE.finditer(svg) if m.group(1) == SvgTheme().faint]
    assert len(faint_texts) == 1  # 只剩页脚


# ---------------------------------------------------------------------------
# 页眉页脚
# ---------------------------------------------------------------------------


def test_title_is_centred_and_bold():
    layout = make_layout(title="Nocturne")
    svg = render_svg(layout)
    match = re.search(r'<text x="([\d.]+)" y="[\d.]+"[^>]*text-anchor="middle"[^>]*>Nocturne</text>', svg)
    assert match
    assert float(match.group(1)) == pytest.approx(layout.page_width / 2.0)
    assert 'font-weight="bold"' in match.group(0)


def test_composer_is_right_aligned_italic():
    score = make_score()
    score.composer = "F. Chopin"
    layout = layout_score(score)
    svg = render_svg(layout)
    assert 'font-style="italic">F. Chopin</text>' in svg


def test_tempo_mark_and_summary_line():
    """速度记号自绘四分音符 + ``= 132`` 文字：不用 ``♩``（缺字体环境会变方框）。"""
    layout, svg = render(bpm=132)
    assert "♩" not in svg
    assert "= 132</text>" in svg
    assert "C major  ·  4/4  ·  4 小节</text>" in svg


def test_tempo_mark_is_drawn_as_vector_glyph():
    """符头为椭圆、符干为向上竖直短线，两者都用强调色。"""
    layout, svg = render(bpm=120)
    marker = SvgTheme().accent
    head = re.search(
        rf'<ellipse cx="0" cy="0" rx="[\d.]+" ry="[\d.]+" fill="{marker}"[^/]*/>', svg
    )
    assert head, "速度记号应有符头椭圆"
    stem = re.search(
        rf'<line x1="([\d.]+)" y1="([\d.]+)" x2="\1" y2="([\d.]+)" stroke="{marker}"', svg
    )
    assert stem, "速度记号应有竖直符干（x1 == x2）"
    assert float(stem.group(3)) < float(stem.group(2)), "符干应向上"


def test_title_and_tempo_can_be_hidden():
    layout = make_layout(title="Hidden")
    svg = render_svg(layout, SvgOptions(show_title=False, show_tempo=False))
    assert ">Hidden</text>" not in svg
    # 强调色在本模块里只用于速度记号，故它不出现即说明记号没画
    assert 'fill="' + SvgTheme().accent + '"' not in svg


# ---------------------------------------------------------------------------
# 符杠与符尾
# ---------------------------------------------------------------------------


def test_beams_connect_eighths_within_a_beat():
    melody = [
        Note(pitch=72, start=float(bar * 4 + k * 0.5), duration=0.5, velocity=80)
        for bar in range(2)
        for k in range(8)
    ]
    layout = make_layout(melody=melody, bars=2, with_bass=False)
    svg = render_svg(layout)
    beamed = [c for c in all_clusters(layout) if c.beam_group is not None]
    assert len(beamed) == 16
    # 符杠是「无 transform 的填充路径」；符尾则带 scale 变换
    beam_paths = [line for line in svg.splitlines()
                  if line.startswith('<path d="M ') and 'Z" fill=' in line
                  and "transform=" not in line]
    assert beam_paths
    # 4/4 每拍一组二分八分 → 每拍 1 条符杠
    assert len(beam_paths) == 8 * len(layout.systems)


def test_isolated_eighth_gets_flag_not_beam():
    melody = [
        Note(pitch=72, start=0.0, duration=0.5, velocity=80),
        Note(pitch=74, start=1.0, duration=1.0, velocity=80),
        Note(pitch=76, start=2.0, duration=1.0, velocity=80),
        Note(pitch=77, start=3.0, duration=1.0, velocity=80),
    ]
    layout = make_layout(melody=melody, bars=1, with_bass=False)
    svg = render_svg(layout)
    cluster = all_clusters(layout)[0]
    assert cluster.beams == 1
    assert cluster.beam_group is None
    flags = [line for line in svg.splitlines()
             if line.startswith('<path d="M ') and 'Z" fill=' in line and 'transform="scale(' in line]
    assert len(flags) == 1


def test_beam_never_crosses_a_beat_boundary():
    melody = [
        Note(pitch=72, start=0.5, duration=0.5, velocity=80),
        Note(pitch=74, start=1.0, duration=0.5, velocity=80),
        Note(pitch=76, start=1.5, duration=0.5, velocity=80),
        Note(pitch=77, start=2.0, duration=1.0, velocity=80),
        Note(pitch=79, start=3.0, duration=1.0, velocity=80),
    ]
    layout = make_layout(melody=melody, bars=1, with_bass=False)
    groups: dict[int, set[float]] = {}
    for cluster in all_clusters(layout):
        if cluster.beam_group is not None:
            groups.setdefault(cluster.beam_group, set()).add(int(cluster.start))
    assert groups
    for starts in groups.values():
        assert len(starts) == 1


# ---------------------------------------------------------------------------
# 休止符
# ---------------------------------------------------------------------------


def test_quarter_rest_glyph_drawn():
    melody = [
        Note(pitch=67, start=0.0, duration=1.0, velocity=80),
        Note(pitch=69, start=2.0, duration=1.0, velocity=80),
    ]
    layout = make_layout(melody=melody, bars=1, with_bass=False)
    svg = render_svg(layout)
    rests = [
        c
        for system in layout.systems
        for staff in system.staffs
        for measure in staff.measures
        for c in measure.clusters
        if c.is_rest
    ]
    assert any(c.note_type == "quarter" for c in rests)
    assert QUARTER_REST[:14] in svg


def test_whole_measure_rest_drawn_as_rect():
    melody = [Note(pitch=67, start=0.0, duration=1.0, velocity=80)]
    layout = make_layout(melody=melody, bars=2, with_bass=False)
    svg = render_svg(layout)
    full = [
        c
        for system in layout.systems
        for staff in system.staffs
        for measure in staff.measures
        for c in measure.clusters
        if c.full_measure
    ]
    assert full
    assert "<rect " in svg


# ---------------------------------------------------------------------------
# 延音线
# ---------------------------------------------------------------------------

_ARC_RE = re.compile(r'<path d="M [\d.]+,[\d.]+ Q [\d.]+,[\d.]+ [\d.]+,[\d.]+" fill="none"')


def _arcs(svg: str) -> list[str]:
    """取出全部延音线弧。"""
    return _ARC_RE.findall(svg)


def test_tie_within_a_measure():
    """3.5 拍无法单值记谱 → 分解为附点二分 + 八分，两者在同一小节内用延音线相连。"""
    melody = [
        Note(pitch=60, start=0.0, duration=3.5, velocity=80),
        Note(pitch=62, start=3.5, duration=0.5, velocity=80),
    ]
    layout = make_layout(melody=melody, bars=1, with_bass=False)
    svg = render_svg(layout)
    clusters = _sounding(layout)
    head = next(c for c in clusters if c.tie_to_next)
    tail = next(c for c in clusters if c.tie_from_prev)
    assert head.measure == tail.measure == 1
    assert head.note_type == "half" and head.dots == 1
    arcs = _arcs(svg)
    assert len(arcs) == 1
    x1, x2 = _arc_span(arcs[0])
    assert x1 == pytest.approx(head.x + NOTEHEAD_RX * layout.space * 0.95, abs=0.01)
    assert x2 == pytest.approx(tail.x - NOTEHEAD_RX * layout.space * 0.95, abs=0.01)


def test_tie_crossing_a_barline_is_drawn():
    """回归：跨小节延音线此前被漏画（只在同一小节内找延续簇）。"""
    melody = [
        Note(pitch=67, start=0.0, duration=1.0, velocity=80),
        Note(pitch=67, start=1.0, duration=3.5, velocity=80),   # 跨 1|2 小节
        Note(pitch=69, start=4.5, duration=3.5, velocity=80),   # 小节内切分
        Note(pitch=71, start=8.0, duration=4.0, velocity=80),
        Note(pitch=72, start=12.0, duration=4.0, velocity=80),
    ]
    layout = make_layout(melody=melody, bars=4, with_bass=False)
    svg = render_svg(layout)
    heads = [c for c in _sounding(layout) if c.tie_to_next]
    assert len(heads) == 2
    assert len(_arcs(svg)) == 2
    # 其中一条必须横跨小节线
    slot = {measure.index: measure for measure in layout.systems[0].staffs[0].measures}
    crossing = [
        arc for arc in _arcs(svg)
        if _arc_span(arc)[0] < slot[1].right < _arc_span(arc)[1]
    ]
    assert crossing


def test_tie_crossing_a_system_break_draws_out_and_in_segments():
    notes = []
    for bar in range(10):
        notes.extend(
            Note(pitch=69, start=float(bar * 4 + k), duration=1.0, velocity=80)
            for k in range(4)
        )
    notes.append(Note(pitch=74, start=36.0, duration=8.0, velocity=80))  # 第 10→11 小节
    for bar in range(11, 16):
        notes.extend(
            Note(pitch=69, start=float(bar * 4 + k), duration=1.0, velocity=80)
            for k in range(4)
        )
    layout = make_layout(melody=notes, bars=16, with_bass=False)
    svg = render_svg(layout)
    assert len(layout.systems) == 2
    heads = [c for c in _sounding(layout) if c.tie_to_next]
    assert len(heads) == 1
    arcs = _arcs(svg)
    assert len(arcs) == 2                       # 出侧 + 入侧
    system_right = layout.systems[0].staffs[0].measures[-1].right
    entry_x = layout.systems[1].staffs[0].measures[0].x
    assert any(abs(_arc_span(arc)[1] - (system_right - 0.10 * layout.space)) < 0.01 for arc in arcs)
    assert any(abs(_arc_span(arc)[0] - (entry_x + 0.10 * layout.space)) < 0.01 for arc in arcs)


def test_truncated_note_leaves_no_dangling_tie_arc():
    """音符被小节数截断时，谱面上不能出现只有起点没有终点的孤立延音线。"""
    seq = NoteSequence(bpm=120, key="C major", time_signature="4/4", bars=1, style="test")
    seq.add_track("piano", 0, 0, [Note(pitch=60, start=0.0, duration=8.0, velocity=80)])
    layout = layout_score(Score.from_sequence(seq, title="Cut"))
    svg = render_svg(layout)
    assert not [c for c in _sounding(layout) if c.tie_to_next]
    assert not _arcs(svg)


def test_ties_can_be_disabled():
    melody = [
        Note(pitch=67, start=0.0, duration=1.0, velocity=80),
        Note(pitch=67, start=1.0, duration=3.5, velocity=80),
        Note(pitch=69, start=4.5, duration=3.5, velocity=80),
        Note(pitch=71, start=8.0, duration=4.0, velocity=80),
        Note(pitch=72, start=12.0, duration=4.0, velocity=80),
    ]
    layout = make_layout(melody=melody, bars=4, with_bass=False)
    svg = render_svg(layout, SvgOptions(show_ties=False))
    assert not _arcs(svg)


def _arc_span(arc: str) -> tuple[float, float]:
    """从弧路径取 (起点 x, 终点 x)。"""
    numbers = re.findall(r"[\d.]+,[\d.]+", arc)
    return float(numbers[0].split(",")[0]), float(numbers[-1].split(",")[0])


# ---------------------------------------------------------------------------
# 落盘与选项
# ---------------------------------------------------------------------------


def test_write_svg_creates_file_and_returns_absolute_path(tmp_path: Path):
    layout = make_layout()
    target = tmp_path / "sub" / "score.svg"
    returned = write_svg(layout, target)
    assert Path(returned).is_absolute()
    assert Path(returned).exists()
    assert Path(returned).read_text(encoding="utf-8") == render_svg(layout)


def test_write_svg_accepts_string_path(tmp_path: Path):
    layout = make_layout()
    returned = write_svg(layout, str(tmp_path / "s.svg"))
    assert Path(returned).read_text(encoding="utf-8").startswith("<svg ")


def test_custom_font_family_propagates():
    layout = make_layout()
    svg = render_svg(layout, SvgOptions(font_family="Helvetica"))
    assert 'font-family="Helvetica"' in svg


def test_single_system_score_fills_page_width():
    layout, svg = render()
    assert len(layout.systems) == 1
    last = layout.systems[0].staffs[0].measures[-1].right
    assert last == pytest.approx(layout.page_width - layout.options.margin, abs=0.5)


def test_layout_options_scale_everything_consistently():
    """谱线间距可调，几何跟着缩放。"""
    options = LayoutOptions(space=12.0, page_width=1600.0)
    score = make_score()
    layout = layout_score(score, options)
    svg = render_svg(layout)
    assert layout.space == 12.0
    width = f"{STAFF_LINE_W * 12.0:.3f}"
    assert svg.count(f'stroke-width="{width}"') == staff_line_count(layout)
    assert 'viewBox="0 0 1600.0 ' in svg
