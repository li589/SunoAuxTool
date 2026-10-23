"""旋律与对位（compose/melody.py）单测。"""

from __future__ import annotations

import pytest

from sunoauxtool.compose.harmony import parse_chord, parse_key
from sunoauxtool.compose.melody import Motif, counter_line, make_motif, phrase
from sunoauxtool.compose.rng import rng_for

KEY = parse_key("D minor")


def _rng(nonce: str = ""):
    return rng_for(11, "verse", "lead", f"melody{nonce}")


class TestMotif:
    def test_operators(self) -> None:
        motif = Motif(pitches=(60, 62, 64), durations=(0.5, 0.5, 1.0))
        assert motif.transposed(2).pitches == (62, 64, 66)
        assert motif.inverted().pitches == (60, 58, 56)
        assert motif.retrograded().pitches == (64, 62, 60)
        assert motif.retrograded().durations == (1.0, 0.5, 0.5)
        assert motif.augmented(2).durations == (1.0, 1.0, 2.0)
        assert motif.length == 2.0

    def test_starts(self) -> None:
        motif = Motif(pitches=(60, 62), durations=(0.5, 1.0))
        assert motif.starts() == [0.0, 0.5]

    def test_to_events(self) -> None:
        events = Motif(pitches=(60, 62), durations=(1.0, 1.0)).to_events(start=4.0, velocity=90)
        assert [(event.start, event.pitch, event.velocity) for event in events] == [
            (4.0, 60, 90), (5.0, 62, 90),
        ]

    def test_clamp_folds_octaves(self) -> None:
        motif = Motif(pitches=(30, 100), durations=(1.0, 1.0))
        clamped = motif.clamp(60, 84)
        assert all(60 <= pitch <= 84 for pitch in clamped.pitches)

    def test_clamp_snaps_to_key(self) -> None:
        motif = Motif(pitches=(61,), durations=(1.0,))
        assert motif.clamp(55, 70, KEY).pitches[0] == 60  # C# -> C（D 小调无 C#）

    def test_length_mismatch_rejected(self) -> None:
        with pytest.raises(ValueError, match="数量必须一致"):
            Motif(pitches=(60,), durations=(1.0, 1.0))

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError, match="不能为空"):
            Motif(pitches=(), durations=())


class TestMakeMotif:
    def test_within_range_and_scale(self) -> None:
        motif = make_motif(KEY, parse_chord("Dm"), low=60, high=84, rng=_rng(),
                           beats=4.0, density=0.7)
        assert all(60 <= pitch <= 84 for pitch in motif.pitches)
        assert all(pitch % 12 in set(KEY.pitch_classes) for pitch in motif.pitches)

    def test_starts_on_chord_tone(self) -> None:
        chord = parse_chord("Dm")
        motif = make_motif(KEY, chord, low=60, high=84, rng=_rng("a"), density=0.6)
        assert motif.pitches[0] % 12 in set(chord.pitch_classes)

    def test_density_controls_note_count(self) -> None:
        sparse = make_motif(KEY, parse_chord("Dm"), low=60, high=84, rng=_rng("s"), density=0.0)
        dense = make_motif(KEY, parse_chord("Dm"), low=60, high=84, rng=_rng("s"), density=1.0)
        assert len(dense.pitches) >= len(sparse.pitches)

    @pytest.mark.parametrize("contour", ["arch", "asc", "desc", "wave"])
    def test_contours(self, contour: str) -> None:
        motif = make_motif(KEY, parse_chord("Dm"), low=60, high=84, rng=_rng(contour),
                           density=0.8, contour=contour)
        assert len(motif.pitches) >= 2

    def test_bounded_by_bar_length(self) -> None:
        motif = make_motif(KEY, parse_chord("Dm"), low=60, high=84, rng=_rng(), beats=3.0)
        assert motif.length <= 3.0

    def test_deterministic(self) -> None:
        left = make_motif(KEY, parse_chord("Dm"), low=60, high=84, rng=_rng("d"))
        right = make_motif(KEY, parse_chord("Dm"), low=60, high=84, rng=_rng("d"))
        assert left == right


class TestPhrase:
    def test_one_bar_per_chord(self) -> None:
        chords = [parse_chord(s) for s in ("Dm", "Bb", "F", "C")]
        events = phrase(KEY, chords, low=60, high=84, rng=_rng("p"), density=0.6)
        assert events
        assert all(event.start < 16.0 for event in events)
        assert all(60 <= event.pitch <= 84 for event in events)

    def test_cadence_lands_on_chord_tone(self) -> None:
        chords = [parse_chord("Bb"), parse_chord("C")]
        events = phrase(KEY, chords, low=60, high=84, rng=_rng("c"), density=0.6, cadence=True)
        last = events[-1]
        assert last.pitch % 12 in set(chords[-1].pitch_classes)

    def test_no_cadence(self) -> None:
        chords = [parse_chord("Dm")]
        events = phrase(KEY, chords, low=60, high=84, rng=_rng("n"), cadence=False)
        assert events

    def test_empty_chords(self) -> None:
        assert phrase(KEY, [], low=60, high=84, rng=_rng()) == []

    def test_notes_inside_bars(self) -> None:
        chords = [parse_chord(s) for s in ("Dm", "Bb")]
        events = phrase(KEY, chords, low=60, high=84, bar_length=3.0, rng=_rng("b"),
                        density=0.9)
        assert all(event.start + event.duration <= 6.0 + 1e-9 for event in events)

    def test_deterministic(self) -> None:
        chords = [parse_chord("Dm"), parse_chord("Bb")]
        first = phrase(KEY, chords, low=60, high=84, rng=_rng("x"), density=0.7)
        second = phrase(KEY, chords, low=60, high=84, rng=_rng("x"), density=0.7)
        assert [(e.start, e.pitch, e.duration) for e in first] == \
            [(e.start, e.pitch, e.duration) for e in second]


class TestCounterLine:
    def test_shifts_register(self) -> None:
        melody = phrase(KEY, [parse_chord("Dm"), parse_chord("Bb")], low=60, high=84,
                        rng=_rng("m"), density=0.5)
        counter = counter_line(KEY, melody, low=48, high=72, rng=_rng("ct"), velocity=60)
        assert len(counter) == len(melody)
        assert all(48 <= event.pitch <= 72 for event in counter)
        assert all(event.velocity == 60 for event in counter)

    def test_avoids_unison_with_melody(self) -> None:
        melody = phrase(KEY, [parse_chord("Dm")], low=60, high=84, rng=_rng("u"), density=0.6)
        counter = counter_line(KEY, melody, low=48, high=72, rng=_rng("u"), velocity=60)
        for main, other in zip(melody, counter):
            assert main.pitch % 12 != other.pitch % 12

    def test_empty_melody(self) -> None:
        assert counter_line(KEY, [], low=48, high=72, rng=_rng()) == []


class TestClampEdges:
    def test_snap_pushes_above_range(self) -> None:
        """吸附后越上界时折回八度。"""
        motif = Motif(pitches=(83,), durations=(1.0,))
        clamped = motif.clamp(60, 83, KEY)
        assert 60 <= clamped.pitches[0] <= 83

    def test_cadence_without_tones(self) -> None:
        from sunoauxtool.compose.melody import _cadence
        motif = Motif(pitches=(60,), durations=(1.0,))
        # 音域内没有该和弦音：原样返回
        assert _cadence(motif, parse_chord("B"), low=60, high=61).pitches == (60,)
