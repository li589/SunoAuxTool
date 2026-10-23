"""编曲模型（compose/song.py）单测。"""

from __future__ import annotations

import json

import pytest

from sunoauxtool.compose import (
    Clip,
    InstrumentRef,
    MixStrip,
    Section,
    Song,
    Track,
    flatten_notes,
)
from sunoauxtool.exceptions import ParameterError
from sunoauxtool.midi import (
    ChannelAllocationError,
    KeyPoint,
    MeterPoint,
    NoteEvent,
    TempoPoint,
)


def _base_song() -> Song:
    """两段（verse/chorus）+ 三轨（钢琴/贝斯/鼓）的基准曲。"""
    song = Song(
        title="夜航",
        tempo_map=[TempoPoint(0, 96)],
        meter_map=[MeterPoint(0, 4, 4)],
        key_map=[KeyPoint(0, "D minor")],
    )
    song.add_section("verse", bars=4, energy=0.5, progression=["Dm", "Bb", "F", "C"])
    song.add_section("chorus", bars=4, energy=0.9, progression=["Bb", "C", "Dm", "Dm"])
    song.add_track(Track(
        id="piano", name="钢琴", role="harmony", instrument=InstrumentRef(program=0),
        clips=[Clip(0, 16, [NoteEvent(62, 0, 2), NoteEvent(65, 2, 2)], "verse")],
        mix=MixStrip(gain_db=-6, pan=-0.2, reverb_send=0.2),
    ))
    song.add_track(Track(
        id="bass", name="贝斯", role="bass", instrument=InstrumentRef(program=33),
        clips=[Clip(0, 16, [NoteEvent(38, 0, 4)], "verse")],
    ))
    song.add_track(Track(
        id="drums", name="鼓", role="drums", instrument=InstrumentRef(program=0),
        clips=[Clip(0, 16, [NoteEvent(36, 0, 1)], "verse")],
    ))
    return song


class TestMixStrip:
    def test_defaults_map_to_midi_defaults(self) -> None:
        strip = MixStrip()
        assert strip.volume_cc == 100
        assert strip.pan_cc == 64

    @pytest.mark.parametrize(("gain_db", "expected"), [
        (-60.0, 0), (-6.0, 50), (0.0, 100), (12.0, 127),
    ])
    def test_gain_to_cc7(self, gain_db: float, expected: int) -> None:
        assert MixStrip(gain_db=gain_db).volume_cc == expected

    @pytest.mark.parametrize(("pan", "expected"), [(-1.0, 0), (0.0, 64), (1.0, 127)])
    def test_pan_to_cc10(self, pan: float, expected: int) -> None:
        assert MixStrip(pan=pan).pan_cc == expected

    def test_range_validation(self) -> None:
        with pytest.raises(ParameterError):
            MixStrip(gain_db=20)
        with pytest.raises(ParameterError):
            MixStrip(pan=1.5)
        with pytest.raises(ParameterError):
            MixStrip(reverb_send=-0.1)


class TestInstrument:
    def test_program_priority(self) -> None:
        assert InstrumentRef(program=42).resolved_program() == 42
        assert InstrumentRef(preset="cello").resolved_program() == 0
        assert InstrumentRef(program=42).has_explicit_program is True
        assert InstrumentRef(preset="cello").has_explicit_program is False

    def test_invalid_values(self) -> None:
        with pytest.raises(ParameterError):
            InstrumentRef(program=128)
        with pytest.raises(ParameterError):
            InstrumentRef(bank="  ")
        with pytest.raises(ParameterError):
            InstrumentRef(preset="")


class TestClip:
    def test_bounds(self) -> None:
        with pytest.raises(ParameterError):
            Clip(start=-1, length=4)
        with pytest.raises(ParameterError):
            Clip(start=0, length=0)

    def test_transpose_preserves_metadata(self) -> None:
        clip = Clip(0, 4, [
            NoteEvent(60, 0, 1, velocity=90, articulation="accent", offset_ticks=3),
        ], section_id="verse", label="motif")
        moved = clip.transposed(12)
        assert moved.notes[0].pitch == 72
        assert moved.notes[0].velocity == 90
        assert moved.notes[0].articulation == "accent"
        assert moved.notes[0].offset_ticks == 3
        assert moved.section_id == "verse"
        assert moved.label == "motif"
        # 原片段不变
        assert clip.notes[0].pitch == 60


class TestTrack:
    def test_unknown_role_rejected(self) -> None:
        with pytest.raises(ParameterError):
            Track(id="x", name="x", role="whatever", instrument=InstrumentRef())

    def test_drum_channel_rules(self) -> None:
        with pytest.raises(ParameterError):
            Track(id="d", name="d", role="drums", instrument=InstrumentRef(), channel=3)
        with pytest.raises(ParameterError):
            Track(id="p", name="p", role="harmony", instrument=InstrumentRef(), channel=9)

    def test_drum_role_defaults_are_valid(self) -> None:
        track = Track(id="d", name="d", role="drums", instrument=InstrumentRef())
        assert track.is_drum is True
        assert track.channel is None

    def test_octave_and_range_validation(self) -> None:
        with pytest.raises(ParameterError):
            Track(id="x", name="x", role="harmony", instrument=InstrumentRef(), octave=10)
        with pytest.raises(ParameterError):
            Track(id="x", name="x", role="harmony", instrument=InstrumentRef(), range=(80, 60))

    def test_end_beat(self) -> None:
        track = Track(id="x", name="x", role="harmony", instrument=InstrumentRef(),
                      clips=[Clip(0, 8), Clip(8, 4)])
        assert track.end_beat == 12


class TestSection:
    def test_validation(self) -> None:
        with pytest.raises(ParameterError):
            Section(id="", name="x", start=0, bars=4)
        with pytest.raises(ParameterError):
            Section(id="a", name="x", start=0, bars=0)
        with pytest.raises(ParameterError):
            Section(id="a", name="x", start=0, bars=4, energy=1.5)

    def test_add_section_chains_starts(self) -> None:
        song = Song()
        first = song.add_section("a", bars=4)
        second = song.add_section("b", bars=2)
        assert first.start == 0
        assert second.start == 16
        assert song.total_bars == 6

    def test_duplicate_section_id(self) -> None:
        song = Song()
        song.add_section("a")
        with pytest.raises(ParameterError):
            song.add_section("a")


class TestSongValidation:
    def test_base_song_is_valid(self) -> None:
        song = _base_song()
        song.validate()
        assert song.total_bars == 8
        assert song.duration_beats == 32
        assert song.duration_seconds() == pytest.approx(20.0)
        assert song.total_bars_measured() == pytest.approx(8.0)
        assert song.section_by_id("chorus") is not None
        assert song.track_by_id("bass") is not None

    def test_empty_song_is_valid(self) -> None:
        Song().validate()

    def test_sections_must_touch(self) -> None:
        song = _base_song()
        song.sections[1].start = 17
        with pytest.raises(ParameterError, match="不相接"):
            song.validate()

    def test_section_must_start_on_bar_line(self) -> None:
        """小节线判定：段落必须落在网格上（段间接续保证不漂移，故直测谓词）。"""
        song = Song()
        assert song._is_bar_boundary(0.0)
        assert song._is_bar_boundary(4.0)
        assert not song._is_bar_boundary(1.5)
        song.add_section("a", bars=2)
        song.sections[0].start = 1.5  # 绕过 add_section 的自动接续
        with pytest.raises(ParameterError, match="不相接"):
            song.validate()

    def test_meter_change_must_be_on_section_boundary(self) -> None:
        song = _base_song()
        # 4/4 从 beat 0，3/4 从 beat 4：段落起点 16 仍落在小节线上，但 4 不是段落边界
        song.meter_map = [MeterPoint(0, 4, 4), MeterPoint(4, 3, 4)]
        with pytest.raises(ParameterError, match="段落边界"):
            song.validate()

    def test_pickup_too_long(self) -> None:
        song = Song(pickup_beats=5)
        with pytest.raises(ParameterError, match="弱起"):
            song.validate()

    def test_duplicate_track_id(self) -> None:
        song = _base_song()
        song.tracks[1].id = "piano"
        with pytest.raises(ParameterError, match="轨道 id 重复"):
            song.validate()

    def test_duplicate_explicit_channel(self) -> None:
        song = _base_song()
        song.tracks[0].channel = 0
        song.tracks[1].channel = 0
        with pytest.raises(ParameterError, match="通道 0"):
            song.validate()

    def test_multiple_drum_tracks(self) -> None:
        song = _base_song()
        song.add_track(Track(id="drums2", name="鼓2", role="drums",
                             instrument=InstrumentRef()))
        with pytest.raises(ParameterError):
            song.validate()

    def test_clip_references_unknown_section(self) -> None:
        song = _base_song()
        song.tracks[0].clips[0].section_id = "nope"
        with pytest.raises(ParameterError, match="不存在的段落"):
            song.validate()

    def test_clip_outside_section(self) -> None:
        song = _base_song()
        song.tracks[0].clips[0].length = 20  # verse 只有 16 拍
        with pytest.raises(ParameterError, match="超出段落"):
            song.validate()

    def test_note_outside_clip(self) -> None:
        song = _base_song()
        song.tracks[0].clips[0].notes.append(NoteEvent(72, 20, 1))
        with pytest.raises(ParameterError, match="超出片段"):
            song.validate()

    def test_note_outside_track_range(self) -> None:
        song = _base_song()
        song.tracks[1].range = (48, 72)  # 贝斯音符 38 越界
        with pytest.raises(ParameterError, match="超出音域"):
            song.validate()

    def test_bar_length_at_meter_change(self) -> None:
        song = _base_song()
        song.meter_map = [MeterPoint(0, 4, 4), MeterPoint(16, 3, 4)]
        assert song.bar_length_at(0) == 4.0
        assert song.bar_length_at(16) == 3.0
        assert song.bar_length_at(31.9) == 3.0


class TestSongToMidi:
    def test_channels_programs_and_mix(self) -> None:
        song = _base_song()
        midi = song.to_midi()
        assert [(t.name, t.channel, t.program, t.is_drum) for t in midi.tracks] == [
            ("钢琴", 0, 0, False),
            ("贝斯", 1, 33, False),
            ("鼓", 9, 0, True),
        ]
        piano = midi.tracks[0]
        assert (piano.volume, piano.pan) == (50, 51)
        assert midi.markers == [(0.0, "verse"), (16.0, "chorus")]
        assert midi.tempo_map[0].bpm == 96
        assert midi.key_map[0].key == "D minor"
        assert midi.duration_beats == 32  # 尾部无音符的 chorus 也占足长度（end_beat）
        assert midi.end_beat == song.duration_beats

    def test_explicit_channel_preserved(self) -> None:
        song = _base_song()
        song.tracks[0].channel = 5
        midi = song.to_midi()
        assert midi.tracks[0].channel == 5

    def test_channel_exhaustion(self) -> None:
        song = Song(sections=[])
        song.add_section("a", bars=1)
        for index in range(16):
            song.add_track(Track(id=f"t{index}", name=f"T{index}", role="harmony",
                                 instrument=InstrumentRef()))
        song.validate()
        with pytest.raises(ChannelAllocationError) as excinfo:
            song.to_midi()
        assert excinfo.value.code == 45


class TestSongSerialization:
    def test_json_round_trip(self) -> None:
        song = _base_song()
        song.tracks[0].clips[0].notes[0].articulation = "accent"
        song.tracks[0].clips[0].notes[0].offset_ticks = 4
        payload = json.loads(json.dumps(song.to_dict(), ensure_ascii=False))
        restored = Song.from_dict(payload)
        assert restored.title == song.title
        assert restored.schema_version == 1
        assert [section.id for section in restored.sections] == ["verse", "chorus"]
        assert [track.id for track in restored.tracks] == ["piano", "bass", "drums"]
        note = restored.tracks[0].clips[0].notes[0]
        assert (note.pitch, note.articulation, note.offset_ticks) == (62, "accent", 4)
        assert restored.tracks[0].mix.gain_db == -6
        assert restored.duration_beats == song.duration_beats

    def test_from_dict_validates_by_default(self) -> None:
        payload = _base_song().to_dict()
        payload["sections"][1]["start"] = 17
        with pytest.raises(ParameterError):
            Song.from_dict(payload)
        restored = Song.from_dict(payload, validate=False)
        assert restored.sections[1].start == 17

    def test_add_duplicate_track_id(self) -> None:
        song = _base_song()
        with pytest.raises(ParameterError, match="轨道 id 重复"):
            song.add_track(Track(id="piano", name="x", role="harmony",
                                 instrument=InstrumentRef()))


class TestNumericGuards:
    def test_gain_must_be_number(self) -> None:
        with pytest.raises(ParameterError, match="必须是数值"):
            MixStrip(gain_db="loud")  # type: ignore[arg-type]

    def test_gain_must_be_finite(self) -> None:
        with pytest.raises(ParameterError, match="必须是有限数值"):
            MixStrip(gain_db=float("nan"))


class TestTrackGuards:
    def test_blank_id(self) -> None:
        with pytest.raises(ParameterError, match="track.id 不能为空"):
            Track(id="   ", name="X", role="harmony", instrument=InstrumentRef())

    def test_blank_name(self) -> None:
        with pytest.raises(ParameterError, match="track.name 不能为空"):
            Track(id="x", name="", role="harmony", instrument=InstrumentRef())

    def test_channel_out_of_range(self) -> None:
        with pytest.raises(ParameterError, match="channel 超出范围"):
            Track(id="x", name="X", role="harmony", instrument=InstrumentRef(), channel=16)


class TestSectionGuards:
    def test_negative_start(self) -> None:
        with pytest.raises(ParameterError, match="段落起始拍不能为负"):
            Section(id="a", name="A", start=-1.0, bars=1)

    def test_blank_chord(self) -> None:
        with pytest.raises(ParameterError, match="和弦不能为空"):
            Section(id="a", name="A", start=0.0, bars=1, progression=["Dm", " "])


class TestSongGuards:
    def test_negative_pickup(self) -> None:
        song = _base_song()
        song.pickup_beats = -1.0
        with pytest.raises(ParameterError, match="弱起拍不能为负"):
            song.validate()

    def test_duplicate_section_id_detected_on_validate(self) -> None:
        song = _base_song()
        # 绕过 add_section 的唯一性检查，直接篡改列表
        song.sections.append(Section(id="verse", name="dup", start=32.0, bars=4))
        with pytest.raises(ParameterError, match="段落 id 重复"):
            song.validate()

    def test_empty_song_duration_falls_back_to_pickup(self) -> None:
        song = Song(pickup_beats=1.0)
        assert song.duration_beats == 1.0

    def test_flatten_notes_crosses_tracks(self) -> None:
        song = _base_song()
        notes = list(flatten_notes(song))
        assert len(notes) == 4  # 钢琴 2 + 贝斯 1 + 鼓 1
        assert [note.pitch for note in notes] == [62, 65, 38, 36]

    def test_pickup_anchors_first_bar_grid(self) -> None:
        """弱起：首段小节网格从弱起起点算起（否则弱起曲无法通过校验）。"""
        song = Song(pickup_beats=1.0, meter_map=[MeterPoint(0, 4, 4)],
                    tempo_map=[TempoPoint(0, 120)], key_map=[KeyPoint(0, "C major")])
        song.add_section("a", bars=2)
        song.add_section("b", bars=2)
        song.validate()
        assert [section.start for section in song.sections] == [1.0, 9.0]
