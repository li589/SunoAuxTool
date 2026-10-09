"""score.musicxml 测试：结构、时值、延音线、和弦、多声部、打击轨与良构校验。

断言口径说明：MusicXML 是**文本**产物，因此全部断言直接落在解析后的元素树上
（标签、属性、次序），不做像素级比较。需要精确值的地方用 ``_note_summary`` 把
一个 ``<note>`` 压成可读元组，避免测试里到处写 XPath。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from sunoauxtool.score import (
    MusicXmlOptions,
    Score,
    ScoreMeasure,
    ScoreNote,
    ScoreTrack,
    render_musicxml,
    validate_musicxml,
    write_musicxml,
)
from sunoauxtool.score.musicxml import DIVISIONS

# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def make_track(
    name: str = "Piano",
    *,
    clef: str = "treble",
    notes=(),
    capacity: float = 4.0,
    bars: int = 1,
    program: int = 0,
    channel: int = 0,
    is_drum: bool = False,
) -> ScoreTrack:
    """用「全曲拍位」的 notes 构造单轨（自动切小节、无休止符填充）。"""
    measures = []
    for idx in range(1, bars + 1):
        start = (idx - 1) * capacity
        inside = [
            n
            for n in notes
            if n.start >= start - 1e-9 and n.start < start + capacity - 1e-9
        ]
        for n in inside:
            n.measure = idx
            n.beat_in_measure = n.start - start
        measures.append(
            ScoreMeasure(index=idx, start_beat=start, duration_beats=capacity, notes=inside)
        )
    return ScoreTrack(
        name=name,
        program=program,
        channel=channel,
        is_drum=is_drum,
        clef=clef,
        voices=max(1, max((n.voice for n in notes), default=0) + 1),
        notes=list(notes),
        measures=measures,
    )


def make_score(
    tracks,
    *,
    key: str = "C major",
    time_signature: str = "4/4",
    bpm: int = 120,
    bars: int = 1,
    title: str = "Test",
    composer: str = "",
) -> Score:
    return Score(
        title=title,
        composer=composer,
        bpm=bpm,
        key=key,
        time_signature=time_signature,
        bars=bars,
        tracks=list(tracks),
    )


def note(pitch: int, start: float, duration: float, *, voice: int = 0, **kw) -> ScoreNote:
    return ScoreNote(pitch=pitch, start=start, duration=duration, voice=voice, **kw)


def rest(start: float, duration: float, *, voice: int = 0) -> ScoreNote:
    return ScoreNote(pitch=0, start=start, duration=duration, voice=voice, is_rest=True)


def root_of(text: str) -> ET.Element:
    """解析 MusicXML 文本（去掉声明与 DOCTYPE，避免解析器差异）。"""
    body = text.split("?>", 1)[1]
    body = body.split(">", 1)[1]  # 去掉 DOCTYPE 行
    return ET.fromstring(body)


def notes_of(root: ET.Element, part: str = "P1") -> list[ET.Element]:
    return root.findall(f".//part[@id='{part}']/measure/note")


def summary(element: ET.Element) -> tuple:
    """``<note>`` -> ``(chord?, step, alter, octave, duration, type, dots, accidental)``。"""
    pitch = element.find("pitch")
    if pitch is None:
        unpitched = element.find("unpitched")
        pitch = unpitched
    step = pitch.find("step").text if pitch is not None else None
    octave = pitch.find("octave").text if pitch is not None else None
    alter = pitch.find("alter") if pitch is not None else None
    accent = element.find("accidental")
    return (
        element.find("chord") is not None,
        step,
        alter.text if alter is not None else None,
        octave,
        int(element.find("duration").text),
        element.find("type").text,
        len(element.findall("dot")),
        accent.text if accent is not None else None,
    )


def ties_of(element: ET.Element) -> list[str]:
    """``<note>`` 的 ``<tie>`` 类型序列（stop 在前）。"""
    return [t.get("type") for t in element.findall("tie")]


def tied_of(element: ET.Element) -> list[str]:
    """``<notations><tied>`` 类型序列。"""
    notations = element.find("notations")
    if notations is None:
        return []
    return [t.get("type") for t in notations.findall("tied")]


# ---------------------------------------------------------------------------
# 文档骨架与确定性
# ---------------------------------------------------------------------------


def test_declaration_and_doctype():
    text = render_musicxml(make_score([make_track(notes=[note(60, 0.0, 4.0)])]))
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n')
    assert "score-partwise" in text.split("\n")[1]
    assert "MusicXML 4.0 Partwise" in text.split("\n")[1]
    assert '<score-partwise version="4.0">' in text


def test_output_is_lf_only_and_has_no_bom():
    text = render_musicxml(make_score([make_track(notes=[note(60, 0.0, 4.0)])]))
    assert "\r" not in text
    assert not text.startswith("\ufeff")
    assert text.endswith("\n")


def test_render_is_deterministic():
    """同一输入两次导出必须逐字节一致（故默认不写 <encoding-date>）。"""
    score = make_score([make_track(notes=[note(60, 0.0, 4.0)])])
    assert render_musicxml(score) == render_musicxml(score)
    assert "encoding-date" not in render_musicxml(score)


def test_encoding_date_option_is_written_when_given():
    score = make_score([make_track(notes=[note(60, 0.0, 4.0)])])
    text = render_musicxml(score, MusicXmlOptions(encoding_date="2026-09-20"))
    assert "<encoding-date>2026-09-20</encoding-date>" in text


@pytest.mark.parametrize("bad", ["", "2026", "2026/09/20", "20260920"])
def test_encoding_date_option_rejects_bad_format(bad):
    with pytest.raises(ValueError, match="encoding_date"):
        MusicXmlOptions(encoding_date=bad)


def test_write_musicxml_writes_lf_on_disk(tmp_path: Path):
    """落盘必须显式 newline="\\n"：Windows 默认会把 \\n 翻成 \\r\\n。"""
    score = make_score([make_track(notes=[note(60, 0.0, 4.0)])])
    out = Path(write_musicxml(score, tmp_path / "a.musicxml"))
    assert out.is_file()
    assert b"\r" not in out.read_bytes()
    assert out.read_text(encoding="utf-8").startswith("<?xml")


def test_write_musicxml_creates_parent_dirs(tmp_path: Path):
    score = make_score([make_track(notes=[note(60, 0.0, 4.0)])])
    out = Path(write_musicxml(score, tmp_path / "deep" / "nested" / "a.musicxml"))
    assert out.is_file()


def test_metadata_can_be_disabled():
    score = make_score([make_track(notes=[note(60, 0.0, 4.0)])], title="T", composer="C")
    root = root_of(render_musicxml(score, MusicXmlOptions(include_metadata=False)))
    assert root.find("work") is None
    assert root.find("identification") is None


def test_work_title_and_composer_are_written():
    score = make_score(
        [make_track(notes=[note(60, 0.0, 4.0)])], title="夜曲", composer="齐见林"
    )
    root = root_of(render_musicxml(score))
    assert root.find("work/work-title").text == "夜曲"
    creator = root.find("identification/creator")
    assert creator.get("type") == "composer"
    assert creator.text == "齐见林"


def test_composer_omitted_when_empty():
    root = root_of(render_musicxml(make_score([make_track(notes=[note(60, 0.0, 4.0)])])))
    assert root.findall("identification/creator") == []


def test_software_credit_contains_version():
    text = render_musicxml(make_score([make_track(notes=[note(60, 0.0, 4.0)])]))
    assert "SmartNoteGen" in text


# ---------------------------------------------------------------------------
# part-list / part 对应
# ---------------------------------------------------------------------------


def test_part_list_matches_parts():
    tracks = [
        make_track("Melody", notes=[note(72, 0.0, 4.0)]),
        make_track("Bass", clef="bass", notes=[note(36, 0.0, 4.0)]),
    ]
    root = root_of(render_musicxml(make_score(tracks)))
    assert [p.get("id") for p in root.findall("part-list/score-part")] == ["P1", "P2"]
    assert [p.get("id") for p in root.findall("part")] == ["P1", "P2"]


def test_part_name_falls_back_when_empty():
    root = root_of(render_musicxml(make_score([make_track("", notes=[note(60, 0.0, 4.0)])])))
    assert root.find("part-list/score-part/part-name").text == "Part 1"


def test_part_abbreviation_latin_initial_and_cjk_passthrough():
    tracks = [
        make_track("Melody", notes=[note(72, 0.0, 4.0)]),
        make_track("旋律", notes=[note(60, 0.0, 4.0)]),
    ]
    root = root_of(render_musicxml(make_score(tracks)))
    abbrs = [a.text for a in root.findall("part-list/score-part/part-abbreviation")]
    assert abbrs == ["M", "旋律"]


def test_midi_channel_and_program_are_one_based():
    track = make_track("P", notes=[note(60, 0.0, 4.0)], program=24, channel=2)
    root = root_of(render_musicxml(make_score([track])))
    instrument = root.find("part-list/score-part/midi-instrument")
    assert instrument.get("id") == "P1-I1"
    assert instrument.find("midi-channel").text == "3"
    assert instrument.find("midi-program").text == "25"


def test_midi_instrument_can_be_disabled():
    root = root_of(
        render_musicxml(
            make_score([make_track(notes=[note(60, 0.0, 4.0)])]),
            MusicXmlOptions(include_midi_instruments=False),
        )
    )
    assert root.findall("part-list/score-part/midi-instrument") == []


def test_program_index_clamped_to_valid_range():
    """内部 program 越界时不让 XML 非法（钳到 1-128）。"""
    score = make_score([make_track(notes=[note(60, 0.0, 4.0)], program=999)])
    root = root_of(render_musicxml(score))
    assert root.find("part-list/score-part/midi-instrument/midi-program").text == "128"


# ---------------------------------------------------------------------------
# attributes：divisions / key / time / clef
# ---------------------------------------------------------------------------


def test_divisions_written_on_first_measure_only():
    track = make_track(notes=[note(60, 0.0, 4.0), note(62, 4.0, 4.0)], bars=2)
    root = root_of(render_musicxml(make_score([track], bars=2)))
    measures = root.findall(".//part/measure")
    assert measures[0].find("attributes/divisions").text == str(DIVISIONS)
    assert measures[1].find("attributes") is None


def test_divisions_covers_64th_note():
    """最短可记谱时值 0.0625 拍必须正好是 1 个 duration 单位。"""
    assert DIVISIONS * 0.0625 == 1.0


def test_key_fifths_and_mode_major():
    root = root_of(render_musicxml(make_score([make_track(notes=[note(60, 0, 4.0)])], key="F major")))
    assert root.find(".//attributes/key/fifths").text == "-1"
    assert root.find(".//attributes/key/mode").text == "major"


def test_key_fifths_and_mode_minor():
    root = root_of(render_musicxml(make_score([make_track(notes=[note(60, 0, 4.0)])], key="a minor")))
    # A 小调调号 0 个升降号，但要写 mode=minor
    assert root.find(".//attributes/key/fifths").text == "0"
    assert root.find(".//attributes/key/mode").text == "minor"


def test_time_signature_written():
    root = root_of(
        render_musicxml(
            make_score(
                [make_track(notes=[note(60, 0.0, 3.0)], capacity=3.0)],
                time_signature="6/8",
            )
        )
    )
    assert root.find(".//attributes/time/beats").text == "6"
    assert root.find(".//attributes/time/beat-type").text == "8"


@pytest.mark.parametrize(
    ("clef", "sign", "line"),
    [("treble", "G", "2"), ("bass", "F", "4"), ("alto", "C", "3"), ("tenor", "C", "4")],
)
def test_clef_signs(clef, sign, line):
    root = root_of(render_musicxml(make_score([make_track(notes=[note(60, 0, 4.0)], clef=clef)])))
    assert root.find(".//attributes/clef/sign").text == sign
    assert root.find(".//attributes/clef/line").text == line


def test_unknown_clef_falls_back_to_treble():
    root = root_of(
        render_musicxml(make_score([make_track(notes=[note(60, 0, 4.0)], clef="bogus")]))
    )
    assert root.find(".//attributes/clef/sign").text == "G"


# ---------------------------------------------------------------------------
# 速度记号
# ---------------------------------------------------------------------------


def test_tempo_direction_written_in_first_measure():
    root = root_of(render_musicxml(make_score([make_track(notes=[note(60, 0, 4.0)])], bpm=96)))
    direction = root.find(".//part/measure/direction")
    assert direction.get("placement") == "above"
    assert direction.find("direction-type/metronome/beat-unit").text == "quarter"
    assert direction.find("direction-type/metronome/per-minute").text == "96"
    assert direction.find("sound").get("tempo") == "96"


def test_tempo_direction_absent_in_later_measures():
    track = make_track(notes=[note(60, 0.0, 4.0), note(62, 4.0, 4.0)], bars=2)
    root = root_of(render_musicxml(make_score([track], bars=2)))
    measures = root.findall(".//part/measure")
    assert measures[0].find("direction") is not None
    assert measures[1].find("direction") is None


def test_tempo_can_be_disabled():
    root = root_of(
        render_musicxml(
            make_score([make_track(notes=[note(60, 0, 4.0)])]),
            MusicXmlOptions(include_tempo=False),
        )
    )
    assert root.findall(".//part/measure/direction") == []


# ---------------------------------------------------------------------------
# 音高 / 变音记号 / 时值
# ---------------------------------------------------------------------------


def test_pitch_step_octave_and_alter():
    root = root_of(render_musicxml(make_score([make_track(notes=[note(61, 0.0, 4.0)])])))
    assert summary(notes_of(root)[0])[:4] == (False, "C", "1", "4")


def test_natural_pitch_has_no_alter_element():
    root = root_of(render_musicxml(make_score([make_track(notes=[note(60, 0.0, 4.0)])])))
    assert summary(notes_of(root)[0])[2] is None


def test_flat_spelling_for_flat_keys():
    """降号调下 70 (Bb) 应拼成 Bb 而非 A#。"""
    root = root_of(
        render_musicxml(make_score([make_track(notes=[note(70, 0.0, 4.0)])], key="F major"))
    )
    chord, step, alter, octave, *rest_ = summary(notes_of(root)[0])
    assert (step, alter, octave) == ("B", "-1", "4")


def test_accidental_omitted_when_key_signature_covers_it():
    """F 大调里 F 音已被调号覆盖，不应再写 <accidental>。"""
    root = root_of(
        render_musicxml(make_score([make_track(notes=[note(65, 0.0, 4.0)])], key="F major"))
    )
    entry = summary(notes_of(root)[0])
    assert entry[1] == "F" and entry[7] is None


def test_accidental_written_when_not_covered():
    """C 大调里的 C# 必须显式写 sharp（调号未覆盖）。"""
    root = root_of(render_musicxml(make_score([make_track(notes=[note(61, 0.0, 4.0)])])))
    assert summary(notes_of(root)[0])[7] == "sharp"


def test_accidental_written_as_flat_in_flat_keys():
    """F 大调下 66 按降号偏好拼成 Gb -> 变音记号是 flat（不是 sharp）。

    这条同时钉住「拼写偏好会改变 accidental 文本」：pitch 66 是同一个音，
    在 C 大调写 F#/sharp，在 F 大调写 Gb/flat。
    """
    root = root_of(
        render_musicxml(make_score([make_track(notes=[note(66, 0.0, 4.0)])], key="F major"))
    )
    entry = summary(notes_of(root)[0])
    assert (entry[1], entry[2], entry[7]) == ("G", "-1", "flat")


def test_rest_written_with_rest_element():
    root = root_of(render_musicxml(make_score([make_track(notes=[rest(0.0, 4.0)])])))
    entry = notes_of(root)[0]
    assert entry.find("rest") is not None
    assert entry.find("pitch") is None


def test_dotted_note_has_dot_elements():
    """附点二分 = 3 拍 -> 一个 <dot/>。"""
    root = root_of(render_musicxml(make_score([make_track(notes=[note(60, 0.0, 3.0)])])))
    entry = summary(notes_of(root)[0])
    assert entry[5] == "half" and entry[6] == 1


def test_dot_count_matches_decomposition():
    """``<dot/>`` 个数必须等于 ``theory.duration_components`` 给出的附点数。

    注：``DURATION_TABLE`` 里最多只有单附点（全音符+1 点 = 6 拍），所以实际不会
    出现 ``<dot/>`` 叠加两次的情况；这条断言的意义是让它与理论层保持同步，
    将来若表里加了双附点，这里会立刻暴露映射漏写。
    """
    from sunoauxtool.score.theory import duration_components

    for quarters in (3.0, 6.0, 1.5, 0.75, 0.375, 0.1875, 0.09375):
        track = make_track(notes=[note(60, 0.0, quarters)], capacity=6.0)
        root = root_of(
            render_musicxml(make_score([track], time_signature="6/4"))
        )
        entries = notes_of(root)
        expected = [dots for _name, dots, _value in duration_components(quarters)]
        assert [summary(e)[6] for e in entries] == expected, quarters


@pytest.mark.parametrize(
    ("quarters", "expected_type", "expected_dots"),
    [
        (4.0, "whole", 0), (2.0, "half", 0), (1.0, "quarter", 0),
        (0.5, "eighth", 0), (0.25, "16th", 0), (0.125, "32nd", 0), (0.0625, "64th", 0),
    ],
)
def test_note_type_matches_musicxml_vocabulary(quarters, expected_type, expected_dots):
    """时值名必须与 MusicXML note-type 词表逐字一致，否则导入器认不出。"""
    track = make_track(notes=[note(60, 0.0, quarters)], capacity=4.0)
    root = root_of(render_musicxml(make_score([track])))
    entry = summary(notes_of(root)[0])
    assert entry[5] == expected_type
    assert entry[6] == expected_dots
    assert entry[4] == round(quarters * DIVISIONS)


# ---------------------------------------------------------------------------
# 时值分解 / 延音线
# ---------------------------------------------------------------------------


def test_long_note_is_split_into_tied_components():
    """5 拍（5/4 小节）= 全音符(4) + 四分音符(1)，两者用延音线相连。"""
    track = make_track(notes=[note(60, 0.0, 5.0)], capacity=5.0)
    root = root_of(render_musicxml(make_score([track], time_signature="5/4")))
    entries = notes_of(root)
    assert [summary(e)[5] for e in entries] == ["whole", "quarter"]
    assert ties_of(entries[0]) == ["start"]
    assert ties_of(entries[1]) == ["stop"]
    assert tied_of(entries[0]) == ["start"]
    assert tied_of(entries[1]) == ["stop"]


def test_barline_split_note_keeps_tie_across_measures():
    """跨小节被 model 切开的音：首片 tie start、次片 tie stop。"""
    first = note(60, 0.0, 4.0)
    second = note(60, 4.0, 2.0)
    first.tie_to_next = True
    second.tie_from_prev = True
    track = make_track(name="Melody", notes=[first, second], bars=2)
    root = root_of(render_musicxml(make_score([track], bars=2)))
    measures = root.findall(".//part/measure")
    assert ties_of(measures[0].findall("note")[0]) == ["start"]
    assert ties_of(measures[1].findall("note")[0]) == ["stop"]


def test_tie_start_does_not_emit_dangling_arc_alone():
    """只有 tie_to_next 无 tie_from_prev 时也不能出现孤立的 stop。"""
    single = note(60, 0.0, 4.0)
    single.tie_to_next = True
    root = root_of(render_musicxml(make_score([make_track(notes=[single])])))
    entry = notes_of(root)[0]
    assert ties_of(entry) == ["start"]
    assert tied_of(entry) == ["start"]


def test_tie_stop_precedes_start_in_element_order():
    """中间片（既收又发）的 <tie> 顺序为 stop -> start。"""
    mid = note(60, 0.0, 3.0)
    mid.tie_from_prev = True
    mid.tie_to_next = True
    root = root_of(render_musicxml(make_score([make_track(notes=[mid])])))
    assert ties_of(notes_of(root)[0]) == ["stop", "start"]


def test_unrepresentable_remainder_is_padded_with_forward():
    """0.6 拍只能记成 八分(0.5)+附点64分(0.09375)：余量由 <forward> 补齐，不撑破小节。"""
    track = make_track(notes=[note(60, 0.0, 0.6)], capacity=4.0)
    root = root_of(render_musicxml(make_score([track])))
    measure = root.find(".//part/measure")
    forward = measure.find("forward/duration")
    assert forward is not None
    written = sum(
        int(n.find("duration").text)
        for n in measure.findall("note")
        if n.find("chord") is None
    )
    assert written + int(forward.text) == 4 * DIVISIONS
    validate_musicxml(render_musicxml(make_score([track])))


# ---------------------------------------------------------------------------
# 和弦
# ---------------------------------------------------------------------------


def test_chord_members_use_chord_element_and_do_not_advance():
    notes = [note(p, 0.0, 4.0) for p in (60, 64, 67)]
    root = root_of(render_musicxml(make_score([make_track(notes=notes)])))
    entries = notes_of(root)
    assert [summary(e)[0] for e in entries] == [False, True, True]
    assert [summary(e)[1] for e in entries] == ["C", "E", "G"]
    # 只有首成员推进时间
    assert sum(summary(e)[4] for e in entries if not summary(e)[0]) == 4 * DIVISIONS


def test_split_chord_emits_members_per_time_slice():
    """5 拍和弦 = 全音符片 + 四分音符片；每片内成员齐全（chord 标记齐全）。

    这是本模块最容易写错的地方：若按「逐个成员输出它的全部分量」，第二个成员的
    第二片会挂到第一个成员的末段上（音区错位、时值溢出小节）。
    """
    notes = [note(p, 0.0, 5.0) for p in (60, 64, 67)]
    track = make_track(notes=notes, capacity=5.0)
    root = root_of(render_musicxml(make_score([track], time_signature="5/4")))
    entries = notes_of(root)
    assert [(summary(e)[0], summary(e)[5]) for e in entries] == [
        (False, "whole"), (True, "whole"), (True, "whole"),
        (False, "quarter"), (True, "quarter"), (True, "quarter"),
    ]
    assert [ties_of(e) for e in entries] == [
        ["start"], ["start"], ["start"], ["stop"], ["stop"], ["stop"],
    ]


def test_rest_is_never_a_chord_member():
    """休止符与实音同起音时不应被写成和弦成员（休止符按 <note> 顺序排在实音之后）。"""
    notes = [rest(0.0, 2.0), note(60, 0.0, 2.0)]
    root = root_of(render_musicxml(make_score([make_track(notes=notes)])))
    entries = notes_of(root)
    rest_entry = next(e for e in entries if e.find("rest") is not None)
    assert rest_entry.find("chord") is None
    # 实音在前、休止符在后，且休止符单独推进时间
    assert [e.find("rest") is not None for e in entries] == [False, True]
    assert int(rest_entry.find("duration").text) == 2 * DIVISIONS


# ---------------------------------------------------------------------------
# 多声部
# ---------------------------------------------------------------------------


def test_multi_voice_uses_backup_to_return_to_measure_start():
    low = [note(48, 0.0, 4.0, voice=0)]
    high = [note(72, 0.0, 4.0, voice=1)]
    root = root_of(render_musicxml(make_score([make_track(notes=low + high)])))
    measure = root.find(".//part/measure")
    tags = [c.tag for c in measure]
    assert "backup" in tags
    backup = measure.find("backup")
    assert int(backup.find("duration").text) == 4 * DIVISIONS
    voices = [e.find("voice").text for e in notes_of(root)]
    assert voices == ["1", "2"]


def test_short_voice_is_padded_with_forward_so_measure_closes():
    """多声部轨不补休止符，短声部必须用 <forward> 补齐，否则时间游标不闭合。"""
    low = [note(48, 0.0, 4.0, voice=0)]
    high = [note(72, 0.0, 2.0, voice=1)]
    score = make_score([make_track(notes=low + high)])
    text = render_musicxml(score)
    root = root_of(text)
    measure = root.find(".//part/measure")
    forward = measure.find("forward/duration")
    assert forward is not None and int(forward.text) == 2 * DIVISIONS
    validate_musicxml(text)


def test_voice_numbers_are_one_based():
    root = root_of(render_musicxml(make_score([make_track(notes=[note(60, 0.0, 4.0)])])))
    assert notes_of(root)[0].find("voice").text == "1"


# ---------------------------------------------------------------------------
# 打击轨
# ---------------------------------------------------------------------------


def test_drum_track_uses_percussion_clef():
    track = make_track("Drums", is_drum=True, clef="treble", notes=[note(36, 0.0, 1.0)])
    root = root_of(render_musicxml(make_score([track])))
    assert root.find(".//attributes/clef/sign").text == "percussion"


def test_drum_track_uses_unpitched_instead_of_pitch():
    track = make_track("Drums", is_drum=True, notes=[note(36, 0.0, 1.0)])
    root = root_of(render_musicxml(make_score([track])))
    entry = notes_of(root)[0]
    assert entry.find("unpitched") is not None
    assert entry.find("pitch") is None
    assert entry.find("unpitched/display-step").text == "C"
    assert entry.find("unpitched/display-octave").text == "2"
    # 打击乐不写变音记号
    assert entry.find("accidental") is None


# ---------------------------------------------------------------------------
# validate_musicxml
# ---------------------------------------------------------------------------


def test_validate_accepts_generated_documents():
    score = make_score(
        [
            make_track("Melody", notes=[note(72, 0.0, 4.0), note(74, 4.0, 3.0), rest(7.0, 1.0)], bars=2),
            make_track("Bass", clef="bass", notes=[note(36, 0.0, 4.0), note(38, 4.0, 4.0)], bars=2),
        ],
        bars=2,
    )
    assert validate_musicxml(render_musicxml(score)).tag == "score-partwise"


def test_validate_rejects_malformed_xml():
    with pytest.raises(ValueError, match="良构"):
        validate_musicxml("<score-partwise><part></score-partwise>")


def test_validate_rejects_wrong_root():
    with pytest.raises(ValueError, match="根标签"):
        validate_musicxml('<?xml version="1.0"?><foo version="4.0"/>')


def test_validate_rejects_wrong_version():
    with pytest.raises(ValueError, match="version"):
        validate_musicxml('<?xml version="1.0"?><score-partwise version="3.1"/>')


def test_validate_rejects_part_list_mismatch():
    text = (
        '<?xml version="1.0"?><score-partwise version="4.0">'
        '<part-list><score-part id="P1"/></part-list>'
        '<part id="P2"><measure number="1">'
        '<attributes><divisions>16</divisions></attributes>'
        '<note><rest/><duration>64</duration><voice>1</voice><type>whole</type></note>'
        "</measure></part></score-partwise>"
    )
    with pytest.raises(ValueError, match="不一致"):
        validate_musicxml(text)


def test_validate_rejects_empty_part():
    text = (
        '<?xml version="1.0"?><score-partwise version="4.0">'
        '<part-list><score-part id="P1"/></part-list><part id="P1"/></score-partwise>'
    )
    with pytest.raises(ValueError, match="没有任何小节"):
        validate_musicxml(text)


def test_validate_rejects_non_positive_divisions():
    text = (
        '<?xml version="1.0"?><score-partwise version="4.0">'
        '<part-list><score-part id="P1"/></part-list>'
        '<part id="P1"><measure number="1">'
        '<attributes><divisions>0</divisions></attributes>'
        '<note><rest/><duration>64</duration><voice>1</voice><type>whole</type></note>'
        "</measure></part></score-partwise>"
    )
    with pytest.raises(ValueError, match="divisions"):
        validate_musicxml(text)


def test_validate_rejects_measure_that_does_not_close():
    """小节时值合计不等于容量时必须报错（我们的导出器有 <forward> 兜底，故此为负例）。"""
    text = (
        '<?xml version="1.0"?><score-partwise version="4.0">'
        '<part-list><score-part id="P1"/></part-list>'
        '<part id="P1"><measure number="1">'
        '<attributes><divisions>16</divisions>'
        "<time><beats>4</beats><beat-type>4</beat-type></time></attributes>"
        '<note><rest/><duration>48</duration><voice>1</voice><type>half</type></note>'
        "</measure></part></score-partwise>"
    )
    with pytest.raises(ValueError, match="容量"):
        validate_musicxml(text)


def test_validate_rejects_backup_before_measure_start():
    text = (
        '<?xml version="1.0"?><score-partwise version="4.0">'
        '<part-list><score-part id="P1"/></part-list>'
        '<part id="P1"><measure number="1">'
        '<attributes><divisions>16</divisions>'
        "<time><beats>4</beats><beat-type>4</beat-type></time></attributes>"
        '<note><rest/><duration>16</duration><voice>1</voice><type>quarter</type></note>'
        '<backup><duration>64</duration></backup>'
        "</measure></part></score-partwise>"
    )
    with pytest.raises(ValueError, match="backup"):
        validate_musicxml(text)


def test_validate_rejects_measure_without_duration():
    text = (
        '<?xml version="1.0"?><score-partwise version="4.0">'
        '<part-list><score-part id="P1"/></part-list>'
        '<part id="P1"><measure number="1">'
        '<attributes><divisions>16</divisions>'
        "<time><beats>4</beats><beat-type>4</beat-type></time></attributes>"
        "</measure></part></score-partwise>"
    )
    with pytest.raises(ValueError, match="没有任何音符时值"):
        validate_musicxml(text)


def test_validate_carries_time_signature_across_measures():
    """拍号只在首小节声明；校验后续小节时必须沿用，不能回落 4/4。"""
    track = make_track(
        notes=[note(60, 0.0, 3.0), note(62, 3.0, 3.0)], capacity=3.0, bars=2
    )
    text = render_musicxml(make_score([track], time_signature="3/4", bars=2))
    assert 'beat-type>4' in text
    validate_musicxml(text)  # 若回落成 4/4 会在此抛「容量」不符


def test_validate_rejects_chord_only_measure():
    """整小节只有和弦成员（没有推进时间的音）-> 时值合计为 0。"""
    text = (
        '<?xml version="1.0"?><score-partwise version="4.0">'
        '<part-list><score-part id="P1"/></part-list>'
        '<part id="P1"><measure number="1">'
        '<attributes><divisions>16</divisions>'
        "<time><beats>4</beats><beat-type>4</beat-type></time></attributes>"
        '<note><chord/><rest/><duration>64</duration><voice>1</voice><type>whole</type></note>'
        "</measure></part></score-partwise>"
    )
    with pytest.raises(ValueError, match="没有任何音符时值"):
        validate_musicxml(text)


# ---------------------------------------------------------------------------
# 空谱 / 边界
# ---------------------------------------------------------------------------


def test_track_without_measures_still_emits_one_closed_measure():
    """没有任何小节的轨（手工构造）不应产出空 <part>，否则 MusicXML 非法。"""
    track = ScoreTrack(name="Empty", measures=[], notes=[])
    score = Score(title="T", bars=1, tracks=[track])
    text = render_musicxml(score)
    root = root_of(text)
    measures = root.findall(".//part/measure")
    assert len(measures) == 1
    # 无音符的小节必须补整小节休止符，否则时间游标为 0、小节不闭合
    assert measures[0].find("note/rest") is not None
    validate_musicxml(text)


def test_measure_with_no_notes_gets_full_measure_rest():
    track = ScoreTrack(
        name="Silent",
        measures=[ScoreMeasure(index=1, start_beat=0.0, duration_beats=4.0, notes=[])],
        notes=[],
    )
    score = Score(title="T", bars=1, tracks=[track])
    text = render_musicxml(score)
    root = root_of(text)
    assert int(root.find(".//part/measure/note/duration").text) == 4 * DIVISIONS
    assert root.find(".//part/measure/note/type").text == "whole"
    validate_musicxml(text)


def test_measure_with_one_empty_measure_among_content_is_closed():
    """有内容的轨里夹一个空小节（多声部轨不补休止符时会这样）也必须闭合。"""
    track = ScoreTrack(
        name="Sparse",
        measures=[
            ScoreMeasure(index=1, start_beat=0.0, duration_beats=4.0, notes=[note(60, 0.0, 4.0)]),
            ScoreMeasure(index=2, start_beat=4.0, duration_beats=4.0, notes=[]),
        ],
        notes=[note(60, 0.0, 4.0)],
    )
    score = Score(title="T", bars=2, tracks=[track])
    text = render_musicxml(score)
    root = root_of(text)
    measures = root.findall(".//part/measure")
    assert measures[1].find("note/rest") is not None
    validate_musicxml(text)


def test_invalid_time_signature_propagates_value_error():
    score = make_score([make_track(notes=[note(60, 0.0, 4.0)])], time_signature="x/4")
    with pytest.raises(ValueError):
        render_musicxml(score)
