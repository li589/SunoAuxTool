"""和弦解析单元测试。"""

from __future__ import annotations

import pytest

from sunoauxtool.exceptions import ParameterError
from sunoauxtool.models.chords import Chord, ChordProgression


def test_parse_basic_progression():
    """C-G-Am-F 解析出 4 个和弦，根音与和弦音正确。"""
    prog = ChordProgression.parse("C-G-Am-F")
    assert len(prog) == 4
    assert prog.chords[0].symbol == "C"
    assert prog.chords[0].root_pc == 0
    assert sorted(prog.chords[0].chord_tones) == [0, 4, 7]  # C E G

    assert prog.chords[1].root_pc == 7
    assert sorted(prog.chords[1].chord_tones) == [2, 7, 11]  # G B D（大三和弦）

    assert prog.chords[2].symbol == "Am"
    assert prog.chords[2].root_pc == 9
    assert sorted(prog.chords[2].chord_tones) == [0, 4, 9]  # A C E

    assert prog.chords[3].root_pc == 5
    assert sorted(prog.chords[3].chord_tones) == [0, 5, 9]  # F A C


def test_parse_dominant_seventh():
    """属七和弦 G7 -> 根音 7，含 7 音（5）。"""
    prog = ChordProgression.parse("G7")
    assert prog.chords[0].root_pc == 7
    tones = set(prog.chords[0].chord_tones)
    assert tones == {7, 11, 2, 5}  # G B D F


def test_parse_minor_seventh():
    """小七 Am7 -> 根音 9，含 7 音（11）。"""
    prog = ChordProgression.parse("Am7")
    assert prog.chords[0].root_pc == 9
    assert set(prog.chords[0].chord_tones) == {9, 0, 4, 7}


def test_parse_b_flat_major():
    """裸降号和弦 Bb 可解析（QA 缺陷 A 回归）：Bb = A# 等音，根音 10。"""
    prog = ChordProgression.parse("Bb")
    assert prog.chords[0].root_pc == 10
    assert sorted(prog.chords[0].chord_tones) == [2, 5, 10]  # Bb D F
    # 保留用户原始拼写
    assert prog.chords[0].symbol == "Bb"


def test_parse_flat_root_progression():
    """含降号根音的和弦进行（默认和弦池/electronic 预设）可解析。"""
    prog = ChordProgression.parse("Dm-Bb-F-C")
    assert len(prog) == 4
    bb = prog.chords[1]
    assert bb.root_pc == 10
    assert sorted(bb.chord_tones) == [2, 5, 10]


def test_parse_b_flat_seventh_correct_pitches():
    """Bb7 解析为正确的 Bb 属七（根音 10，非 music21 误判的 B7）。"""
    prog = ChordProgression.parse("Bb7")
    assert prog.chords[0].root_pc == 10
    assert sorted(prog.chords[0].chord_tones) == [2, 5, 8, 10]  # Bb D F Ab


def test_parse_other_flat_roots():
    """其他降号根音（Eb/Ab/Db）同样可解析且音级正确。"""
    for symbol, expected_root, expected_tones in [
        ("Eb", 3, [3, 7, 10]),
        ("Ab", 8, [0, 3, 8]),
        ("Db", 1, [1, 5, 8]),
    ]:
        prog = ChordProgression.parse(symbol)
        assert prog.chords[0].root_pc == expected_root
        assert sorted(prog.chords[0].chord_tones) == expected_tones


def test_parse_with_whitespace_and_dash():
    """容忍空格与多余分隔符。"""
    prog = ChordProgression.parse("  C - G - Am - F ")
    assert len(prog) == 4


def test_parse_empty_raises():
    """空字符串 -> ParameterError(1)。"""
    with pytest.raises(ParameterError) as exc:
        ChordProgression.parse("")
    assert exc.value.code == 1


def test_parse_invalid_symbol_raises():
    """非法和弦符号 -> ParameterError(1)。"""
    with pytest.raises(ParameterError) as exc:
        ChordProgression.parse("C-H-G")
    assert exc.value.code == 1


def test_get_chord_loops_over_bars():
    """get_chord 按小节索引循环取和弦。"""
    prog = ChordProgression.parse("C-G-Am-F")
    assert prog.get_chord(0).symbol == "C"
    assert prog.get_chord(3).symbol == "F"
    assert prog.get_chord(4).symbol == "C"  # 循环
    assert prog.get_chord(7).symbol == "F"


def test_chord_contains():
    """contains 判断音级是否属于和弦音。"""
    c = Chord(symbol="C", root_pc=0, chord_tones=[0, 4, 7], beats=4.0)
    assert c.contains(0)
    assert c.contains(4)
    assert not c.contains(1)
    assert c.contains(12)  # 八度等价
