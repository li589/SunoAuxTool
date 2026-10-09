"""谱面核心测试：乐理函数（theory）/ 多轨中间表示（model）/ 排版引擎（layout）。

覆盖重点
--------
- theory：调号五度圈、升降号拼写偏好、变音记号绘制判定、时值贪心分解。
- model：小节线切分与延音线、和弦归入同一声部、多声部识别、单声部补休止符、
  谱号判定的「越界距离」规则。
- layout：全部小节恰好排一次、跨谱表槽位对齐、行断开均衡性、
  符干长度下限、连杠组内同向同符杠数、加线范围、边界（单小节/空谱）。
"""

from __future__ import annotations

import pytest

from sunoauxtool.models.midi import MidiDocument
from sunoauxtool.models.notes import Note, NoteSequence
from sunoauxtool.score import LayoutOptions, Score, layout_score
from sunoauxtool.score.theory import (
    accidental_glyph,
    beat_unit,
    beats_per_measure,
    diatonic_number,
    duration_components,
    key_accidentals,
    key_fifths,
    normalize_key,
    note_beams,
    parse_time_signature,
    prefer_flats,
    spell_pitch,
    spelling_name,
)

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _melody_notes() -> list[Note]:
    """4 小节 4/4 的简单旋律（每拍一个音）。"""
    pitches = [69, 71, 72, 74, 72, 71, 69, 67, 69, 72, 76, 74, 72, 71, 69, 67]
    return [Note(pitch=p, start=float(i), duration=1.0, velocity=80) for i, p in enumerate(pitches)]


def _bass_notes() -> list[Note]:
    """低音轨：每小节一个全音符。"""
    return [Note(pitch=p, start=float(i * 4), duration=4.0, velocity=60) for i, p in enumerate([45, 41, 43, 45])]


def make_sequence(bpm: int = 120, bars: int = 4) -> NoteSequence:
    """构造两轨 NoteSequence（旋律 + 低音）。"""
    seq = NoteSequence(bpm=bpm, key="C major", time_signature="4/4", bars=bars, style="test")
    seq.add_track("melody", 0, 0, _melody_notes())
    seq.add_track("bass", 32, 1, _bass_notes())
    return seq


def make_score(**kwargs) -> Score:
    """从 NoteSequence 构造 Score。"""
    return Score.from_sequence(make_sequence(), title="T", **kwargs)


def _all_clusters(layout):
    """摊平全部簇。"""
    return [c for s in layout.systems for st in s.staffs for m in st.measures for c in m.clusters]


# ---------------------------------------------------------------------------
# theory
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("C major", ("C", "major")),
        ("c", ("C", "major")),
        ("C", ("C", "major")),
        ("Am", ("A", "minor")),
        ("a minor", ("A", "minor")),
        ("A minor", ("A", "minor")),
        ("F#", ("F#", "major")),
        ("f#", ("F#", "major")),
        ("Bb major", ("Bb", "major")),
        ("g#小调", ("G#", "minor")),
        ("D 大调", ("D", "major")),
        ("  Eb  ", ("Eb", "major")),
        ("", ("C", "major")),
    ],
)
def test_normalize_key(raw, expected):
    assert normalize_key(raw) == expected


@pytest.mark.parametrize("raw", ["H", "X major", "C##", "C foo#"])
def test_normalize_key_rejects_invalid(raw):
    with pytest.raises(ValueError):
        normalize_key(raw)


@pytest.mark.parametrize(
    ("key", "fifths"),
    [
        ("C major", 0), ("G major", 1), ("D major", 2), ("F major", -1),
        ("Bb major", -2), ("F# major", 6), ("Gb major", -6),
        ("A minor", 0), ("E minor", 1), ("D minor", -1), ("C# minor", 4),
    ],
)
def test_key_fifths(key, fifths):
    assert key_fifths(key) == fifths


def test_key_accidentals_sharp_order_and_flat_order():
    assert key_accidentals(0) == {}
    assert key_accidentals(1) == {3: 1}                      # F#
    assert key_accidentals(3) == {3: 1, 0: 1, 4: 1}          # F# C# G#
    assert key_accidentals(-1) == {6: -1}                    # Bb
    assert key_accidentals(-3) == {6: -1, 2: -1, 5: -1}      # Bb Eb Ab
    # 超过 7 个时截断，不越界
    assert len(key_accidentals(9)) == 7
    assert len(key_accidentals(-9)) == 7


def test_prefer_flats():
    assert prefer_flats("F major") is True
    assert prefer_flats("Bb major") is True
    assert prefer_flats("C major") is False
    assert prefer_flats("G major") is False


@pytest.mark.parametrize(
    ("pitch", "flats", "expected"),
    [
        (60, False, (0, 28, 0)),
        (60, True, (0, 28, 0)),
        (61, False, (0, 28, 1)),      # C#4
        (61, True, (1, 29, -1)),      # Db4
        (70, False, (5, 33, 1)),      # A#4
        (70, True, (6, 34, -1)),      # Bb4
        (59, False, (6, 27, 0)),      # B3
        (0, False, (0, -7, 0)),       # C-1
    ],
)
def test_spell_pitch(pitch, flats, expected):
    assert spell_pitch(pitch, flats) == expected


@pytest.mark.parametrize(
    ("pitch", "flats", "expected"),
    [
        (60, False, "C4"), (61, False, "C#4"), (61, True, "Db4"),
        (70, False, "A#4"), (70, True, "Bb4"), (57, False, "A3"),
        (21, False, "A0"), (108, False, "C8"),
    ],
)
def test_spelling_name(pitch, flats, expected):
    assert spelling_name(pitch, flats) == expected


def test_diatonic_number_is_linear_in_pitch():
    assert diatonic_number(60) == 28          # C4
    assert diatonic_number(62) == 29          # D4
    assert diatonic_number(64) == 30          # E4（高音谱号最下线）
    assert diatonic_number(43) == 18          # G2（低音谱号最下线）
    assert diatonic_number(61) == 28          # 升号不改变音级序号


@pytest.mark.parametrize(
    ("letter", "acc", "key", "expected"),
    [
        (3, 1, {3: 1}, None),     # G 大调里的 F#：调号已含，不画
        (3, 0, {3: 1}, 0),        # G 大调里要写还原 F
        (3, 1, {}, 1),            # C 大调里要写升 F
        (0, 1, {}, 1),            # C 大调里的 C#：要画升号
        (6, -1, {}, -1),
        (6, 0, {}, None),         # C 大调的 B：什么都不画
        (6, -1, {6: -1}, None),   # F 大调的 Bb：调号已含
    ],
)
def test_accidental_glyph(letter, acc, key, expected):
    assert accidental_glyph(letter, acc, key) == expected


@pytest.mark.parametrize(
    ("quarters", "expected"),
    [
        (6.0, [("whole", 1, 6.0)]),
        (4.0, [("whole", 0, 4.0)]),
        (3.0, [("half", 1, 3.0)]),
        (2.0, [("half", 0, 2.0)]),
        (1.5, [("quarter", 1, 1.5)]),
        (1.0, [("quarter", 0, 1.0)]),
        (0.75, [("eighth", 1, 0.75)]),
        (0.5, [("eighth", 0, 0.5)]),
        (0.375, [("16th", 1, 0.375)]),
        (0.25, [("16th", 0, 0.25)]),
        (0.0625, [("64th", 0, 0.0625)]),
        (5.0, [("whole", 0, 4.0), ("quarter", 0, 1.0)]),
        (3.5, [("half", 1, 3.0), ("eighth", 0, 0.5)]),
        (0.0, []),
        (-1.0, []),
    ],
)
def test_duration_components(quarters, expected):
    assert duration_components(quarters) == expected


def test_duration_components_sums_back():
    for q in (0.125, 0.4375, 1.0, 2.25, 3.75, 7.0, 9.5):
        total = sum(v for _n, _d, v in duration_components(q))
        assert total == pytest.approx(q, abs=1e-9)


def test_duration_components_never_overshoots():
    """分解之和恒 ≤ 输入（宁可少记不可多记）。

    回归背景：兜底分支曾写成「只要还剩一点就再补一个 64 分音符」，于是
    ``duration_components(0.6)`` 返回 0.5 + 0.09375 + 0.0625 = 0.65625，比输入多
    1/64 拍。1/1000 拍网格上 8000 个采样点里有 7936 个溢出。三处消费方都会被
    「多记」伤到：排版层末片越界压住后一个音、简谱层多画一条横线、
    补休止符算出比空隙更长的休止符。

    扫描从 0.063 拍起：低于 1/64 拍（0.0625）的输入走「取最短时值」的例外分支，
    那一档**允许**超出（见 ``test_duration_components_snaps_when_shorter_than_shortest``）。
    """
    for i in range(63, 4001):
        q = i / 1000.0
        total = sum(v for _n, _d, v in duration_components(q))
        assert total <= q + 1e-9, f"{q} 的分解和 {total} 超出输入"


def test_duration_components_drops_unrepresentable_remainder():
    """余量不足 1/64 拍时丢弃（无法记谱），但已能记出的部分照常返回。"""
    assert duration_components(0.6) == [("eighth", 0, 0.5), ("64th", 1, 0.09375)]
    assert duration_components(0.1) == [("64th", 1, 0.09375)]
    # 余量恰好不足最短时值：全音符 + 四分音符后剩 1/64 以下
    assert duration_components(4.01) == [("whole", 0, 4.0)]


def test_duration_components_snaps_when_shorter_than_shortest():
    """输入本身短于 1/64 拍时取最短时值，保证极短音不在谱面消失。

    这是唯一允许「分解和 > 输入」的情形：不这样极短音会凭空消失
    （model 只过滤 ≤1e-6 拍的音，其余会走到这里）。
    """
    for q in (0.001, 0.01, 0.062):
        assert duration_components(q) == [("64th", 0, 0.0625)]
    assert duration_components(0.0625) == [("64th", 0, 0.0625)]


@pytest.mark.parametrize(
    ("note_type", "beams"),
    [("whole", 0), ("half", 0), ("quarter", 0), ("eighth", 1), ("16th", 2), ("32nd", 3), ("64th", 4), ("bogus", 0)],
)
def test_note_beams(note_type, beams):
    assert note_beams(note_type) == beams


def test_parse_time_signature():
    assert parse_time_signature("4/4") == (4, 4)
    assert parse_time_signature("6/8") == (6, 8)
    for bad in ("", "4", "x/4", "4/0", "0/4", "4/4/4"):
        with pytest.raises(ValueError):
            parse_time_signature(bad)


def test_beats_per_measure_and_beat_unit():
    assert beats_per_measure("4/4") == 4.0
    assert beats_per_measure("3/4") == 3.0
    assert beats_per_measure("6/8") == 3.0
    assert beat_unit("4/4") == 1.0
    assert beat_unit("6/8") == 0.5


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------


def test_from_sequence_basic_fields():
    score = make_score()
    assert score.key == "C major"
    assert score.time_signature == "4/4"
    assert score.bars == 4
    assert score.bpm == 120
    assert len(score.tracks) == 2
    assert score.measure_capacity == 4.0
    assert score.total_beats == 16.0
    assert score.duration_seconds() == pytest.approx(8.0)


def test_track_name_and_clef_inference():
    score = make_score()
    names = [t.name for t in score.tracks]
    assert names == ["melody", "bass"]
    # 名称约定优先：melody -> 高音谱号，bass -> 低音谱号
    assert score.tracks[0].clef == "treble"
    assert score.tracks[1].clef == "bass"


def test_clef_by_pitch_distribution_when_name_is_neutral():
    seq = NoteSequence(bpm=100, key="C major", bars=2)
    seq.add_track("part", 0, 0, [Note(40, 0.0, 4.0), Note(43, 4.0, 4.0)])
    low = Score.from_sequence(seq)
    assert low.tracks[0].clef == "bass"

    seq2 = NoteSequence(bpm=100, key="C major", bars=2)
    seq2.add_track("part", 0, 0, [Note(76, 0.0, 4.0), Note(79, 4.0, 4.0)])
    high = Score.from_sequence(seq2)
    assert high.tracks[0].clef == "treble"


def test_clef_explicit_override_wins():
    score = Score.from_sequence(make_sequence(), clefs={"melody": "bass", "bass": "treble"})
    assert score.tracks[0].clef == "bass"
    assert score.tracks[1].clef == "treble"


def test_measures_partition_notes_and_beats_are_absolute():
    score = make_score()
    for track in score.tracks:
        assert len(track.measures) == score.bars
        for measure in track.measures:
            assert measure.start_beat == pytest.approx((measure.index - 1) * 4.0)
            for note in measure.notes:
                assert note.measure == measure.index
                assert measure.start_beat - 1e-9 <= note.start
                assert note.beat_end <= measure.start_beat + measure.duration_beats + 1e-9


def test_chord_members_share_a_voice():
    seq = NoteSequence(bpm=100, key="C major", bars=1)
    seq.add_track("chords", 0, 0, [Note(60, 0.0, 4.0), Note(64, 0.0, 4.0), Note(67, 0.0, 4.0)])
    score = Score.from_sequence(seq)
    track = score.tracks[0]
    assert track.voices == 1, "同起音同时值的和弦必须落在同一声部"
    assert {n.voice for n in track.notes} == {0}


def test_polyphony_splits_into_two_voices():
    seq = NoteSequence(bpm=100, key="C major", bars=1)
    seq.add_track(
        "piano", 0, 0,
        [
            Note(36, 0.0, 4.0),   # 持续低音
            Note(72, 0.0, 1.0),   # 上方旋律
            Note(74, 1.0, 1.0),
            Note(76, 2.0, 1.0),
            Note(77, 3.0, 1.0),
        ],
    )
    score = Score.from_sequence(seq)
    track = score.tracks[0]
    assert track.voices == 2
    low = [n for n in track.notes if n.voice == 0]
    high = [n for n in track.notes if n.voice == 1]
    assert [n.pitch for n in low] == [36]
    assert [n.pitch for n in high] == [72, 74, 76, 77]


def test_long_note_is_split_at_barline_with_ties():
    seq = NoteSequence(bpm=100, key="C major", bars=2)
    seq.add_track("pad", 0, 0, [Note(60, 2.0, 4.0)])   # 从第 3 拍起，跨过第 1 小节线
    score = Score.from_sequence(seq)
    frags = sorted((n for n in score.tracks[0].notes if not n.is_rest), key=lambda n: n.start)
    assert len(frags) == 2
    first, second = frags
    assert first.measure == 1 and second.measure == 2
    assert first.tie_to_next is True
    assert first.tie_from_prev is False
    assert second.tie_from_prev is True
    assert second.tie_to_next is False
    assert first.duration + second.duration == pytest.approx(4.0)


def test_truncated_note_drops_dangling_tie_flag():
    """音符越过声明小节数被截断时，末片不得留下无延续端的 ``tie_to_next``。"""
    seq = NoteSequence(bpm=100, key="C major", bars=1)
    seq.add_track("pad", 0, 0, [Note(60, 0.0, 8.0)])   # 8 拍塞进 1 个 4/4 小节
    score = Score.from_sequence(seq)
    frags = sorted((n for n in score.tracks[0].notes if not n.is_rest), key=lambda n: n.start)
    assert len(frags) == 1
    assert frags[0].duration == pytest.approx(4.0)     # 截断到小节容量
    assert frags[0].tie_to_next is False
    assert not any(n.tie_to_next for n in score.tracks[0].notes if not n.is_rest)


def test_rests_are_filled_for_monophonic_track():
    seq = NoteSequence(bpm=100, key="C major", bars=1)
    seq.add_track("lead", 0, 0, [Note(72, 0.0, 1.0), Note(74, 2.0, 1.0)])
    score = Score.from_sequence(seq)
    rests = [n for n in score.tracks[0].notes if n.is_rest]
    assert rests, "单声部轨的空隙应补休止符"
    assert sum(n.duration for n in rests) == pytest.approx(2.0)


def test_empty_measure_gets_full_measure_rest():
    seq = NoteSequence(bpm=100, key="C major", bars=3)
    seq.add_track("lead", 0, 0, [Note(72, 8.0, 1.0)])   # 仅第 3 小节有音
    score = Score.from_sequence(seq)
    m1 = score.tracks[0].measures[0]
    rests = [n for n in m1.notes if n.is_rest]
    assert len(rests) == 1
    assert rests[0].duration == pytest.approx(4.0)


def test_no_rests_added_for_polyphonic_track():
    seq = NoteSequence(bpm=100, key="C major", bars=1)
    seq.add_track("piano", 0, 0, [Note(36, 0.0, 4.0), Note(72, 0.0, 1.0)])
    score = Score.from_sequence(seq)
    assert score.tracks[0].voices == 2
    assert not [n for n in score.tracks[0].notes if n.is_rest]


def test_zero_duration_notes_are_dropped():
    seq = NoteSequence(bpm=100, key="C major", bars=1)
    seq.add_track("x", 0, 0, [Note(60, 0.0, 1.0)])
    seq.tracks[0].notes.append(Note(62, 1.0, 1.0))
    score = Score.from_sequence(seq)
    assert all(n.duration > 0 for n in score.tracks[0].notes)


def test_note_count_excludes_rests():
    score = make_score()
    assert score.note_count == sum(
        1 for t in score.tracks for n in t.notes if not n.is_rest
    )
    assert score.note_count > 0


def test_to_dict_shape_and_json_serializable():
    import json

    data = make_score().to_dict()
    assert data["title"] == "T"
    assert data["fifths"] == 0
    assert len(data["tracks"]) == 2
    assert set(data["tracks"][0]) >= {"name", "clef", "program", "is_drum", "voices", "notes", "range"}
    json.dumps(data)   # 不抛异常即通过


def test_from_midi_round_trip(tmp_path):
    seq = make_sequence()
    mid = tmp_path / "x.mid"
    MidiDocument.from_sequence(seq).write(mid)

    score = Score.from_midi(mid, title="Round", composer="QA", key="C major", time_signature="4/4")
    assert score.title == "Round"
    assert score.composer == "QA"
    assert score.bpm == 120
    assert [t.name for t in score.tracks] == ["melody", "bass"]
    assert score.bars == 4
    assert score.note_count >= 20


def test_from_midi_default_title_is_filename(tmp_path):
    mid = tmp_path / "song_01.mid"
    MidiDocument.from_sequence(make_sequence()).write(mid)
    assert Score.from_midi(mid).title == "song_01"


def test_from_midi_missing_file_raises_input_error(tmp_path):
    from sunoauxtool.exceptions import InputFileError

    with pytest.raises(InputFileError) as exc:
        Score.from_midi(tmp_path / "nope.mid")
    assert exc.value.code == 3


def test_bars_auto_computed_from_last_note():
    # 显式不给 bars 时按最后落音向上取整；NoteSequence 总拍数 16 -> 4 小节
    score = Score.from_sequence(make_sequence(bars=4), bars=None)
    assert score.bars == 4


def test_minor_key_preserved_and_fifths():
    seq = NoteSequence(bpm=72, key="a minor", time_signature="4/4", bars=2)
    seq.add_track("m", 0, 0, [Note(69, 0.0, 2.0), Note(71, 2.0, 2.0)])
    score = Score.from_sequence(seq)
    assert score.key == "A minor"
    assert score.fifths == 0

    seq2 = NoteSequence(bpm=72, key="e minor", time_signature="4/4", bars=1)
    seq2.add_track("m", 0, 0, [Note(76, 0.0, 4.0)])
    assert Score.from_sequence(seq2).fifths == 1


# ---------------------------------------------------------------------------
# layout
# ---------------------------------------------------------------------------


def test_layout_covers_every_measure_exactly_once():
    score = make_score()
    layout = layout_score(score)
    assert [idx for idx, _x, _w in layout.measure_slots] == list(range(1, score.bars + 1))


def test_all_staffs_share_measure_slots_within_a_system():
    layout = layout_score(make_score())
    for system in layout.systems:
        assert system.staff_count == 2
        n = len(system.staffs[0].measures)
        for k in range(n):
            xs = {round(st.measures[k].x, 6) for st in system.staffs}
            ws = {round(st.measures[k].width, 6) for st in system.staffs}
            assert len(xs) == 1 and len(ws) == 1


def test_measures_do_not_overlap_and_are_ordered():
    layout = layout_score(make_score())
    for system in layout.systems:
        ms = system.staffs[0].measures
        for a, b in zip(ms, ms[1:]):
            assert a.right <= b.x + 1e-6
            assert b.index == a.index + 1


def test_clusters_stay_inside_their_measure():
    layout = layout_score(make_score())
    for system in layout.systems:
        for staff in system.staffs:
            for m in staff.measures:
                for c in m.clusters:
                    assert m.x - 1e-6 <= c.x <= m.right + 1e-6


def test_right_margin_is_consistent_across_systems():
    """多行曲子：除末行外都必须顶到右页边；末行齐左、只允许不足。"""
    layout = layout_score(Score.from_sequence(make_sequence(bars=64)))
    assert len(layout.systems) >= 2
    opts = layout.options
    target = opts.page_width - opts.margin
    for system in layout.systems[:-1]:
        assert system.staffs[0].measures[-1].right == pytest.approx(target, abs=1.0)
    assert layout.systems[-1].staffs[0].measures[-1].right <= target + 1.0


def test_single_system_score_fills_the_page_width():
    """单行曲子（4 小节）应整行撑满，而不是缩在页面左侧。"""
    layout = layout_score(make_score())
    assert len(layout.systems) == 1
    opts = layout.options
    assert layout.systems[0].staffs[0].measures[-1].right == pytest.approx(
        opts.page_width - opts.margin, abs=1.0
    )


def test_first_system_is_indented_more_than_later_ones():
    score = make_score()
    score.tracks[0].measures = score.tracks[0].measures
    layout = layout_score(Score.from_sequence(make_sequence(bars=16)))
    assert len(layout.systems) >= 2
    first_x = layout.systems[0].staffs[0].measures[0].x
    second_x = layout.systems[1].staffs[0].measures[0].x
    assert first_x > second_x


def test_line_breaking_balances_uniform_measures():
    # 20 个等宽小节：应切成 3 行，且每行小节数最多差 1
    pitches = [60 + (i % 5) for i in range(80)]
    seq = NoteSequence(bpm=120, key="C major", time_signature="4/4", bars=20)
    seq.add_track("m", 0, 0, [Note(p, float(i), 1.0) for i, p in enumerate(pitches)])
    layout = layout_score(Score.from_sequence(seq))
    counts = [len(s.staffs[0].measures) for s in layout.systems]
    assert sum(counts) == 20
    assert max(counts) - min(counts) <= 1, counts


def test_every_system_starts_with_its_first_measure_flagged():
    layout = layout_score(Score.from_sequence(make_sequence(bars=16)))
    for system in layout.systems:
        ms = system.staffs[0].measures
        assert ms[0].is_system_start is True
        assert all(m.is_system_start is False for m in ms[1:])


def test_stem_length_lower_bound_and_direction_consistency():
    layout = layout_score(make_score())
    groups: dict[int, list] = {}
    for c in _all_clusters(layout):
        if c.is_rest:
            assert c.stem_len == 0.0
            continue
        assert c.stem_len >= 3.5 - 1e-9
        if c.beam_group is not None:
            groups.setdefault(c.beam_group, []).append(c)
    for gid, members in groups.items():
        assert len(members) >= 2, gid
        assert len({c.stem_up for c in members}) == 1, gid
        assert len({c.beams for c in members}) == 1, gid


def test_beam_groups_never_cross_a_beat():
    layout = layout_score(Score.from_sequence(make_sequence(bars=16)))
    for system in layout.systems:
        for staff in system.staffs:
            for m in staff.measures:
                groups: dict[int, list] = {}
                for c in m.clusters:
                    if c.beam_group is not None:
                        groups.setdefault(c.beam_group, []).append(c)
                for members in groups.values():
                    beats = {int(c.start - m.start_beat) for c in members}
                    assert len(beats) == 1, (m.index, sorted(beats))


def test_eighth_note_pairs_are_beamed():
    seq = NoteSequence(bpm=120, key="C major", time_signature="4/4", bars=1)
    seq.add_track("m", 0, 0, [Note(72, i * 0.5, 0.5) for i in range(8)])
    layout = layout_score(Score.from_sequence(seq))
    clusters = [c for c in layout.systems[0].staffs[0].measures[0].clusters]
    assert len(clusters) == 8
    assert all(c.beam_group is not None for c in clusters), "全部八分音符应连杠"
    assert all(c.flags == 0 for c in clusters)


def test_mixed_sixteenths_and_eighths_form_separate_groups():
    """同一拍内符尾数不同 → 分组；且符杠数各自正确。"""
    seq = NoteSequence(bpm=120, key="C major", time_signature="4/4", bars=1)
    notes = [
        Note(72, 0.0, 0.25), Note(74, 0.25, 0.25),    # 一拍内两个 16 分
        Note(76, 1.0, 0.5), Note(77, 1.5, 0.5),        # 一拍内两个 8 分
        Note(79, 2.0, 1.0), Note(81, 3.0, 1.0),        # 两个四分收尾
    ]
    seq.add_track("m", 0, 0, notes)
    layout = layout_score(Score.from_sequence(seq))
    clusters = layout.systems[0].staffs[0].measures[0].clusters
    by_type = {c.note_type: c for c in clusters}
    assert by_type["16th"].beam_group is not None
    assert by_type["16th"].beams == 2
    assert by_type["eighth"].beam_group is not None
    assert by_type["eighth"].beams == 1
    assert by_type["16th"].beam_group != by_type["eighth"].beam_group
    assert by_type["quarter"].beam_group is None


def test_beam_never_crosses_a_beat_boundary():
    """4/4 中 0.5 拍与 1.0 拍的两个八分属不同拍，不应连杠。"""
    seq = NoteSequence(bpm=120, key="C major", time_signature="4/4", bars=1)
    seq.add_track("m", 0, 0, [Note(72, 0.5, 0.5), Note(74, 1.0, 0.5), Note(76, 2.0, 2.0)])
    layout = layout_score(Score.from_sequence(seq))
    clusters = layout.systems[0].staffs[0].measures[0].clusters
    eighths = [c for c in clusters if c.note_type == "eighth" and not c.is_rest]
    assert len(eighths) == 2, [(c.start, c.is_rest) for c in clusters]
    assert all(c.beam_group is None for c in eighths)
    assert all(c.flags == 1 for c in eighths)


def test_ledger_lines_for_out_of_staff_notes():
    seq = NoteSequence(bpm=100, key="C major", bars=1)
    seq.add_track("m", 0, 0, [Note(60, 0.0, 4.0)])   # C4：高音谱表需 1 条下加线
    layout = layout_score(Score.from_sequence(seq))
    note = layout.systems[0].staffs[0].measures[0].clusters[0].notes[0]
    assert note.step_offset == -2
    assert note.ledger_steps == [-2]


def test_no_ledger_lines_inside_the_staff():
    seq = NoteSequence(bpm=100, key="C major", bars=1)
    seq.add_track("m", 0, 0, [Note(71, 0.0, 4.0)])   # B4：高音谱表中线
    layout = layout_score(Score.from_sequence(seq))
    note = layout.systems[0].staffs[0].measures[0].clusters[0].notes[0]
    assert note.ledger_steps == []


def test_accidental_not_repeated_within_a_measure():
    seq = NoteSequence(bpm=100, key="C major", bars=1)
    seq.add_track("m", 0, 0, [Note(61, 0.0, 1.0), Note(61, 1.0, 1.0), Note(61, 2.0, 2.0)])
    layout = layout_score(Score.from_sequence(seq))
    clusters = layout.systems[0].staffs[0].measures[0].clusters
    accidentals = [c.notes[0].accidental for c in clusters if not c.is_rest]
    assert accidentals[0] == 1, "首音需画升号"
    assert accidentals[1:] == [None, None], "同小节同音高不重复画"


def test_key_signature_suppresses_redundant_accidental():
    seq = NoteSequence(bpm=100, key="G major", time_signature="4/4", bars=1)
    seq.add_track("m", 0, 0, [Note(66, 0.0, 2.0), Note(65, 2.0, 2.0)])  # F#4 后接 F4
    layout = layout_score(Score.from_sequence(seq))
    assert layout.fifths == 1
    clusters = layout.systems[0].staffs[0].measures[0].clusters
    assert clusters[0].notes[0].accidental is None, "G 大调的 F# 由调号承担"
    assert clusters[1].notes[0].accidental == 0, "随后的 F 还原需画还原号"


def test_full_measure_rest_is_marked():
    seq = NoteSequence(bpm=100, key="C major", bars=2)
    seq.add_track("m", 0, 0, [Note(72, 0.0, 1.0)])
    layout = layout_score(Score.from_sequence(seq))
    rests = [c for c in layout.systems[0].staffs[0].measures[1].clusters if c.is_rest]
    assert len(rests) == 1
    assert rests[0].full_measure is True
    assert rests[0].note_type == "whole"


def test_compound_meter_uses_dotted_beat_for_beaming():
    seq = NoteSequence(bpm=120, key="C major", time_signature="6/8", bars=1)
    seq.add_track("m", 0, 0, [Note(72, i * 0.5, 0.5) for i in range(6)])
    layout = layout_score(Score.from_sequence(seq))
    clusters = layout.systems[0].staffs[0].measures[0].clusters
    groups = {c.beam_group for c in clusters}
    assert None not in groups, "6/8 的六个八分应按附点拍连成两组"
    assert len(groups) == 2, groups


def test_layout_empty_score_is_safe():
    layout = layout_score(Score(title="empty", bars=4))
    assert layout.systems == []
    assert layout.page_height > 0
    assert layout.measure_slots == []


def test_layout_single_measure_single_track():
    seq = NoteSequence(bpm=100, key="C major", bars=1)
    seq.add_track("m", 0, 0, [Note(60, 0.0, 4.0)])
    layout = layout_score(Score.from_sequence(seq))
    assert len(layout.systems) == 1
    assert layout.systems[0].staff_count == 1
    assert layout.measure_slots[0][0] == 1


def test_layout_page_height_grows_with_systems():
    small = layout_score(Score.from_sequence(make_sequence(bars=4)))
    big = layout_score(Score.from_sequence(make_sequence(bars=64)))
    assert big.page_height > small.page_height


def test_cluster_lookup_helper():
    score = make_score()
    layout = layout_score(score)
    hit = layout.cluster_at(1, 0.0)
    assert hit is not None
    assert hit.measure == 1
    assert layout.cluster_at(1, 999.0) is None


def test_layout_options_are_respected():
    opts = LayoutOptions(page_width=800.0, margin=20.0, space=7.0)
    layout = layout_score(Score.from_sequence(make_sequence()), options=opts)
    assert layout.page_width == 800.0
    assert layout.space == 7.0
    for system in layout.systems:
        assert system.staffs[0].measures[-1].right <= 800.0 - 20.0 + 1.0


def test_to_dict_shape():
    import json

    data = layout_score(make_score()).to_dict()
    assert data["systems"] >= 1
    assert data["tracks"] == ["melody", "bass"]
    assert len(data["page"]) == 2
    json.dumps(data)


def test_bass_clef_reference_places_g2_at_bottom_line():
    seq = NoteSequence(bpm=100, key="C major", bars=1)
    seq.add_track("bass", 32, 1, [Note(43, 0.0, 4.0)])   # G2
    layout = layout_score(Score.from_sequence(seq))
    note = layout.systems[0].staffs[0].measures[0].clusters[0].notes[0]
    assert layout.systems[0].staffs[0].clef == "bass"
    assert note.step_offset == 0
    assert note.ledger_steps == []


def test_chord_notes_sorted_bottom_up():
    seq = NoteSequence(bpm=100, key="C major", bars=1)
    seq.add_track("c", 0, 0, [Note(67, 0.0, 4.0), Note(60, 0.0, 4.0), Note(64, 0.0, 4.0)])
    layout = layout_score(Score.from_sequence(seq))
    cluster = layout.systems[0].staffs[0].measures[0].clusters[0]
    steps = [ln.step_offset for ln in cluster.notes]
    assert steps == sorted(steps)
    assert len(cluster.notes) == 3


def test_dense_measure_is_wider_than_sparse_one():
    dense = _MeasureWidthProbe([Note(72, i * 0.25, 0.25) for i in range(16)])
    sparse = _MeasureWidthProbe([Note(72, 0.0, 4.0)])
    assert dense.width > sparse.width


class _MeasureWidthProbe:
    """构造同小节不同密度的两份布局，比较自然宽度。"""

    def __init__(self, notes):
        seq = NoteSequence(bpm=120, key="C major", time_signature="4/4", bars=1)
        seq.add_track("m", 0, 0, notes)
        score = Score.from_sequence(seq)
        layout = layout_score(score)
        self.width = layout.systems[0].staffs[0].measures[0].natural_width
