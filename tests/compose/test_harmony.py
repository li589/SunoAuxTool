"""和声模块（compose/harmony.py）单测：调式/和弦解析 + 声部配置。"""

from __future__ import annotations

import pytest

from sunoauxtool.compose.harmony import (
    CHORD_QUALITIES,
    Chord,
    Key,
    parse_chord,
    parse_key,
    parse_pitch_class,
    parse_progression,
    voice_chord,
)
from sunoauxtool.exceptions import ParameterError


class TestPitchClass:
    @pytest.mark.parametrize(("text", "expected"), [
        ("C", 0), ("C#", 1), ("Db", 1), ("D", 2), ("E", 4), ("F", 5),
        ("F#", 6), ("G", 7), ("A", 9), ("Bb", 10), ("B", 11), ("Cb", 11),
    ])
    def test_parse(self, text: str, expected: int) -> None:
        assert parse_pitch_class(text) == expected

    @pytest.mark.parametrize("text", ["", "H", "Cx", "1"])
    def test_invalid(self, text: str) -> None:
        with pytest.raises(ParameterError):
            parse_pitch_class(text)


class TestKey:
    def test_major(self) -> None:
        key = parse_key("C major")
        assert key.tonic == 0 and key.mode == "major"
        assert key.pitch_classes == (0, 2, 4, 5, 7, 9, 11)

    def test_minor(self) -> None:
        key = parse_key("D minor")
        assert key.pitch_classes == (2, 4, 5, 7, 9, 10, 0)

    def test_default_mode_is_major(self) -> None:
        assert parse_key("F#").mode == "major"
        assert parse_key("F#").tonic == 6

    @pytest.mark.parametrize("text", ["a major", "A maj", "A M"])
    def test_mode_aliases(self, text: str) -> None:
        assert parse_key(text).mode == "major"

    @pytest.mark.parametrize("text", ["a minor", "a min", "a m"])
    def test_minor_aliases(self, text: str) -> None:
        assert parse_key(text).mode == "minor"

    def test_dorian(self) -> None:
        key = parse_key("D dorian")
        assert key.pitch_classes == (2, 4, 5, 7, 9, 11, 0)

    def test_empty_key(self) -> None:
        with pytest.raises(ParameterError, match="调性不能为空"):
            parse_key("  ")

    def test_unknown_mode(self) -> None:
        with pytest.raises(ParameterError, match="未知调式"):
            parse_key("D bluesy")

    def test_pitches_in_range(self) -> None:
        key = parse_key("C major")
        assert key.pitches(60, 64) == [60, 62, 64]

    def test_snap(self) -> None:
        key = parse_key("C major")
        assert key.snap(61) == 60
        assert key.snap(66) == 65   # F# -> F
        assert key.snap(63) == 62   # Eb -> D（等距取低音）
        assert key.snap(60) == 60


class TestChord:
    def test_major_triad(self) -> None:
        chord = parse_chord("C")
        assert chord.root == 0
        assert chord.pitch_classes == (0, 4, 7)

    def test_minor_triad(self) -> None:
        assert parse_chord("Dm").pitch_classes == (2, 5, 9)

    def test_flat_root(self) -> None:
        assert parse_chord("Bb").root == 10

    def test_seventh(self) -> None:
        assert parse_chord("G7").pitch_classes == (7, 11, 2, 5)
        assert parse_chord("Cmaj7").pitch_classes == (0, 4, 7, 11)
        assert parse_chord("Dm7").pitch_classes == (2, 5, 9, 0)

    def test_slash_chord(self) -> None:
        chord = parse_chord("F/A")
        assert chord.root == 5 and chord.bass == 9
        assert chord.pitch_classes == (5, 9, 0)  # A 在和弦内，不额外插入

    def test_slash_with_foreign_bass(self) -> None:
        chord = parse_chord("C/D")
        assert chord.bass == 2
        assert chord.pitch_classes[0] == 2  # 外来低音补在最前

    def test_slash_bass_inside_chord(self) -> None:
        assert parse_chord("C/G").pitch_classes == (0, 4, 7)

    def test_unknown_quality(self) -> None:
        with pytest.raises(ParameterError, match="无法解析和弦"):
            parse_chord("Cadd9")

    def test_empty(self) -> None:
        with pytest.raises(ParameterError, match="和弦不能为空"):
            parse_chord("")

    def test_tones_in_range(self) -> None:
        assert parse_chord("Dm").tones(60, 64) == [62]
        assert parse_chord("Dm").tones(60, 69) == [62, 65, 69]

    def test_root_pitch(self) -> None:
        assert parse_chord("Dm").root_pitch(28, 52) == 38    # D2
        assert parse_chord("Bb").root_pitch(28, 52) == 34    # Bb1
        assert parse_chord("F/A").root_pitch(28, 52) == 33   # 低音 A1（最低可用 A）

    def test_root_pitch_clamped(self) -> None:
        assert parse_chord("B").root_pitch(60, 65) == 60     # 无可用音时退化为 low

    def test_bad_quality_constructor(self) -> None:
        with pytest.raises(ParameterError, match="未知和弦性质"):
            Chord(root=0, quality="xx")

    def test_parse_progression(self) -> None:
        chords = parse_progression(["Dm", "Bb", "F/A"])
        assert [chord.root for chord in chords] == [2, 10, 5]

    def test_qualities_table_has_basics(self) -> None:
        for name in ("", "m", "7", "maj7", "m7", "dim", "sus4"):
            assert name in CHORD_QUALITIES


class TestVoiceChord:
    def test_three_voices_cover_chord(self) -> None:
        voicing = voice_chord(parse_chord("Dm"), low=52, high=76)
        assert len(voicing) == 3
        assert {pitch % 12 for pitch in voicing} == {2, 5, 9}
        assert voicing == sorted(voicing)

    def test_four_voices(self) -> None:
        voicing = voice_chord(parse_chord("Dm"), low=48, high=76, voices=4)
        assert len(voicing) == 4

    def test_voice_leading_is_smooth(self) -> None:
        first = voice_chord(parse_chord("Dm"), low=55, high=76)
        second = voice_chord(parse_chord("Bb"), low=55, high=76, previous=first)
        motion = sum(abs(a - b) for a, b in zip(first, second))
        assert motion <= 6  # 平滑连接：总移动不超过 6 个半音

    def test_centered_in_range(self) -> None:
        voicing = voice_chord(parse_chord("C"), low=48, high=72)
        center = (voicing[0] + voicing[-1]) / 2
        assert abs(center - 60) <= 8          # 靠近音域中部
        assert voicing[-1] - voicing[0] <= 16  # 不散架

    def test_empty_range_rejected(self) -> None:
        with pytest.raises(ParameterError, match="没有可用音"):
            voice_chord(parse_chord("Dm"), low=61, high=61)

    def test_deterministic(self) -> None:
        left = voice_chord(parse_chord("G7"), low=50, high=74)
        right = voice_chord(parse_chord("G7"), low=50, high=74)
        assert left == right
