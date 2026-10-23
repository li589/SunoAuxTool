"""MIDI 模型与时间表（document.py）单测。"""

from __future__ import annotations

import math

import pytest

from sunoauxtool.exceptions import ParameterError
from sunoauxtool.midi import (
    ControlChange,
    KeyPoint,
    MeterPoint,
    MidiDocumentV2,
    MidiTrackV2,
    NoteEvent,
    TempoPoint,
    beat_to_seconds,
    seconds_between,
    total_bars,
    validate_timeline,
)


class TestNoteEvent:
    def test_valid_note(self) -> None:
        note = NoteEvent(pitch=60, start=1.5, duration=2, velocity=90)
        assert note.end == 3.5
        assert note.release_velocity == 64

    @pytest.mark.parametrize("pitch", [-1, 128])
    def test_rejects_out_of_range_pitch(self, pitch: int) -> None:
        with pytest.raises(ParameterError):
            NoteEvent(pitch=pitch, start=0, duration=1)

    def test_rejects_zero_velocity(self) -> None:
        # MIDI 中 note_on velocity=0 等价 note_off，模型层不允许
        with pytest.raises(ParameterError):
            NoteEvent(pitch=60, start=0, duration=1, velocity=0)

    def test_rejects_non_positive_duration(self) -> None:
        with pytest.raises(ParameterError):
            NoteEvent(pitch=60, start=0, duration=0)

    def test_rejects_negative_start(self) -> None:
        with pytest.raises(ParameterError):
            NoteEvent(pitch=60, start=-1, duration=1)

    def test_rejects_non_finite(self) -> None:
        with pytest.raises(ParameterError):
            NoteEvent(pitch=60, start=math.inf, duration=1)


class TestTimelinePoints:
    def test_tempo_bpm_bounds(self) -> None:
        with pytest.raises(ParameterError):
            TempoPoint(beat=0, bpm=0)
        with pytest.raises(ParameterError):
            TempoPoint(beat=0, bpm=401)

    def test_tempo_curve_validated(self) -> None:
        with pytest.raises(ParameterError):
            TempoPoint(beat=0, bpm=120, curve="smooth")

    def test_meter_bar_length(self) -> None:
        assert MeterPoint(beat=0, numerator=6, denominator=8).bar_length_beats == 3.0
        assert MeterPoint(beat=0, numerator=3, denominator=4).bar_length_beats == 3.0

    def test_meter_rejects_bad_denominator(self) -> None:
        with pytest.raises(ParameterError):
            MeterPoint(beat=0, numerator=4, denominator=3)

    def test_key_requires_text(self) -> None:
        with pytest.raises(ParameterError):
            KeyPoint(beat=0, key="  ")

    def test_control_change_bounds(self) -> None:
        with pytest.raises(ParameterError):
            ControlChange(beat=0, controller=128, value=0)
        with pytest.raises(ParameterError):
            ControlChange(beat=0, controller=1, value=128)


class TestTrack:
    def test_channel_9_forces_drum(self) -> None:
        track = MidiTrackV2(name="kit", channel=9)
        assert track.is_drum is True

    def test_drum_flag_requires_channel_9(self) -> None:
        with pytest.raises(ParameterError):
            MidiTrackV2(name="kit", channel=0, is_drum=True)

    def test_rejects_bad_channel_and_program(self) -> None:
        with pytest.raises(ParameterError):
            MidiTrackV2(name="x", channel=16)
        with pytest.raises(ParameterError):
            MidiTrackV2(name="x", channel=0, program=128)

    def test_end_beat(self) -> None:
        track = MidiTrackV2(
            name="x", channel=0,
            notes=[NoteEvent(60, 0, 2), NoteEvent(62, 4, 3)],
        )
        assert track.end_beat == 7


class TestTimelineValidation:
    def test_first_point_must_be_zero(self) -> None:
        with pytest.raises(ParameterError):
            validate_timeline(
                [TempoPoint(beat=1, bpm=120)],
                [MeterPoint(beat=0, numerator=4, denominator=4)],
                [KeyPoint(beat=0, key="C major")],
            )

    def test_beats_must_increase(self) -> None:
        with pytest.raises(ParameterError):
            validate_timeline(
                [TempoPoint(beat=0, bpm=120), TempoPoint(beat=0, bpm=90)],
                [MeterPoint(beat=0, numerator=4, denominator=4)],
                [KeyPoint(beat=0, key="C major")],
            )

    def test_empty_table_rejected(self) -> None:
        with pytest.raises(ParameterError):
            validate_timeline([], [MeterPoint(0, 4, 4)], [KeyPoint(0, "C major")])


class TestBeatToSeconds:
    def test_constant_tempo(self) -> None:
        assert beat_to_seconds(4, [TempoPoint(0, 120)]) == pytest.approx(2.0)
        assert beat_to_seconds(0, [TempoPoint(0, 120)]) == 0.0

    def test_step_change(self) -> None:
        tempo = [TempoPoint(0, 120), TempoPoint(4, 60)]
        # 4 拍 @120 = 2s；再接 4 拍 @60 = 4s
        assert beat_to_seconds(8, tempo) == pytest.approx(6.0)
        assert beat_to_seconds(4, tempo) == pytest.approx(2.0)

    def test_linear_ramp_integral(self) -> None:
        # 0→4 拍内 BPM 从 60 线性升到 120：∫ 60/bpm db = 4*ln(2)
        tempo = [TempoPoint(0, 60, curve="linear"), TempoPoint(4, 120)]
        assert beat_to_seconds(4, tempo) == pytest.approx(4 * math.log(2), rel=1e-9)

    def test_seconds_between(self) -> None:
        tempo = [TempoPoint(0, 120), TempoPoint(8, 60)]
        # 4-8 拍仍属 120BPM 段：4 拍 × 0.5s = 2s
        assert seconds_between(4, 8, tempo) == pytest.approx(2.0)

    def test_before_first_point_rejected(self) -> None:
        with pytest.raises(ParameterError):
            beat_to_seconds(-1, [TempoPoint(0, 120)])


class TestTotalBars:
    def test_constant_meter(self) -> None:
        assert total_bars([MeterPoint(0, 4, 4)], 8) == pytest.approx(2.0)

    def test_meter_change(self) -> None:
        meters = [MeterPoint(0, 4, 4), MeterPoint(8, 3, 4)]
        # 0-8 拍 = 2 小节；8-14 拍 = 2 小节 3/4
        assert total_bars(meters, 14) == pytest.approx(4.0)


class TestDocument:
    def test_duplicate_channel_rejected(self) -> None:
        doc = MidiDocumentV2(tracks=[
            MidiTrackV2(name="a", channel=0),
            MidiTrackV2(name="b", channel=0),
        ])
        with pytest.raises(ParameterError):
            doc.validate()

    def test_multiple_drums_rejected(self) -> None:
        doc = MidiDocumentV2(tracks=[
            MidiTrackV2(name="d1", channel=9),
            MidiTrackV2(name="d2", channel=9),
        ])
        with pytest.raises(ParameterError):
            doc.validate()

    def test_duration_seconds_uses_tempo_map(self) -> None:
        doc = MidiDocumentV2(
            tempo_map=[TempoPoint(0, 120), TempoPoint(4, 60)],
            tracks=[MidiTrackV2(name="x", channel=0,
                                notes=[NoteEvent(60, 0, 8)])],
        )
        assert doc.duration_beats == 8
        assert doc.duration_seconds() == pytest.approx(6.0)

    def test_bad_ppq(self) -> None:
        with pytest.raises(ParameterError):
            MidiDocumentV2(ppq=0).validate()

    def test_track_by_channel(self) -> None:
        track = MidiTrackV2(name="x", channel=3)
        doc = MidiDocumentV2(tracks=[track])
        assert doc.track_by_channel(3) is track
        assert doc.track_by_channel(4) is None

    def test_end_beat_extends_duration(self) -> None:
        """尾部静音（段落无音符）靠 end_beat 保住长度。"""
        doc = MidiDocumentV2(
            tracks=[MidiTrackV2(name="x", channel=0, notes=[NoteEvent(60, 0, 4)])],
            end_beat=16.0,
        )
        doc.validate()
        assert doc.duration_beats == 16.0
        assert doc.duration_seconds() == pytest.approx(8.0)  # 120 BPM

    def test_end_beat_cannot_shrink_duration(self) -> None:
        doc = MidiDocumentV2(
            tracks=[MidiTrackV2(name="x", channel=0, notes=[NoteEvent(60, 0, 8)])],
            end_beat=2.0,
        )
        assert doc.duration_beats == 8.0

    def test_end_beat_validation(self) -> None:
        with pytest.raises(ParameterError, match="不能为负"):
            MidiDocumentV2(end_beat=-1.0).validate()
        with pytest.raises(ParameterError, match="有限数值"):
            MidiDocumentV2(end_beat=float("inf")).validate()
