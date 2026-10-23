"""旧模型 → Song 适配器（compose/adapters.py）单测。"""

from __future__ import annotations

import pytest

from sunoauxtool.compose import from_legacy_midi, from_sequence
from sunoauxtool.midi import read_smf, write_smf
from sunoauxtool.models.midi import MidiDocument, MidiTrack
from sunoauxtool.models.notes import Note, NoteSequence


def _sequence(time_signature: str = "4/4", bars: int = 4) -> NoteSequence:
    seq = NoteSequence(bpm=120, key="C major", time_signature=time_signature, bars=bars)
    seq.add_track("chords", 0, 0, [
        Note(60, 0, 3, 70), Note(65, 3, 3, 66),
    ])
    seq.add_track("melody", 68, 1, [
        Note(72, 0, 1.5, 90), Note(74, 2, 1, 84),
    ])
    seq.add_track("bass", 33, 2, [Note(36, 0, 4, 78)])
    seq.add_track("drums", 0, 9, [Note(36, 0, 1, 100)])
    return seq


class TestFromSequence:
    def test_maps_tempo_key_meter_and_tracks(self) -> None:
        song = from_sequence(_sequence(), title="草图")
        assert song.title == "草图"
        assert song.tempo_map[0].bpm == 120
        assert song.key_map[0].key == "C major"
        assert (song.meter_map[0].numerator, song.meter_map[0].denominator) == (4, 4)
        assert song.total_bars == 4
        assert song.duration_beats == 16
        assert [(track.id, track.role, track.channel) for track in song.tracks] == [
            ("chords", "harmony", 0),
            ("melody", "melody", 1),
            ("bass", "bass", 2),
            ("drums", "drums", 9),
        ]
        assert song.tracks[0].instrument.program == 0
        assert song.tracks[1].clips[0].notes[0].pitch == 72

    def test_three_four_meter(self) -> None:
        song = from_sequence(_sequence(time_signature="3/4", bars=4))
        assert song.duration_beats == 12
        assert song.meter_map[0].bar_length_beats == 3.0

    def test_duplicate_channels_fall_back_to_auto(self) -> None:
        seq = NoteSequence(bpm=100, time_signature="4/4", bars=1)
        seq.add_track("a", 0, 0, [Note(60, 0, 4)])
        seq.add_track("b", 1, 0, [Note(64, 0, 4)])
        song = from_sequence(seq)
        assert [track.channel for track in song.tracks] == [0, None]
        midi = song.to_midi()
        assert [track.channel for track in midi.tracks] == [0, 1]

    def test_notes_beyond_declared_bars_expand_section(self) -> None:
        seq = NoteSequence(bpm=120, time_signature="4/4", bars=1)
        seq.add_track("melody", 0, 0, [Note(60, 6, 2)])
        song = from_sequence(seq)
        assert song.total_bars == 2
        assert song.duration_beats == 8

    def test_empty_sequence_is_valid(self) -> None:
        song = from_sequence(NoteSequence(bpm=90, bars=4))
        song.validate()
        assert song.tracks == []
        assert song.total_bars == 4

    def test_role_inference(self) -> None:
        seq = NoteSequence(bpm=120, time_signature="4/4", bars=1)
        for index, name in enumerate(["Bass", "Chords", "Lead Melody", "Pad", "Arp", "Strings"]):
            seq.add_track(name, 0, index, [Note(60, 0, 1)])
        assert [track.role for track in from_sequence(seq).tracks] == [
            "bass", "harmony", "melody", "pad", "arpeggio", "harmony",
        ]


class TestFromLegacyMidi:
    def test_channels_are_reassigned_and_metadata_applied(self) -> None:
        doc = MidiDocument(bpm=100)
        doc.tracks = [
            MidiTrack("Piano", 0, 0, [Note(60, 0, 4)]),
            MidiTrack("Bass", 33, 0, [Note(40, 4, 4)]),  # 旧模型通道不可靠
            MidiTrack("Drums", 0, 9, [Note(36, 0, 1)], is_drum=True),
        ]
        song = from_legacy_midi(doc, title="旧曲", key="A minor", meter="3/4")
        assert song.title == "旧曲"
        assert song.tempo_map[0].bpm == 100
        assert song.key_map[0].key == "A minor"
        assert song.meter_map[0].numerator == 3
        assert song.total_bars == 3
        assert song.duration_beats == 9
        assert [(track.name, track.role, track.channel) for track in song.tracks] == [
            ("Piano", "harmony", None),
            ("Bass", "bass", None),
            ("Drums", "drums", 9),
        ]
        assert song.tracks[0].instrument.program == 0
        assert song.tracks[1].instrument.program == 33

    def test_empty_legacy_document(self) -> None:
        song = from_legacy_midi(MidiDocument(bpm=120), title="空")
        song.validate()
        assert song.total_bars == 1
        assert song.tracks == []

    def test_round_trip_through_midi_io(self, tmp_path) -> None:
        song = from_sequence(_sequence())
        path = write_smf(song.to_midi(), tmp_path / "adapted.mid")
        back = read_smf(path)
        original = [
            (note.pitch, round(note.start, 6), round(note.duration, 6))
            for track in song.tracks for note in track.clips[0].notes
        ]
        restored = [
            (note.pitch, round(note.start, 6), round(note.duration, 6))
            for track in back.tracks for note in track.notes
        ]
        assert sorted(restored) == sorted(original)
        assert [track.channel for track in back.tracks] == [0, 1, 2, 9]

    def test_invalid_meter_rejected(self) -> None:
        from sunoauxtool.exceptions import ParameterError

        with pytest.raises(ParameterError):
            from_legacy_midi(MidiDocument(), meter="4-4")

    def test_non_numeric_meter_rejected(self) -> None:
        from sunoauxtool.exceptions import ParameterError

        with pytest.raises(ParameterError, match="无法解析拍号"):
            from_legacy_midi(MidiDocument(), meter="abc/4")
