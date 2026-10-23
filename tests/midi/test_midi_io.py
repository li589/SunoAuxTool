"""SMF 读写（io.py）测试：往返、事件布局、确定性、容错。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import mido
import pytest

from sunoauxtool.exceptions import ExportError, InputFileError, ParameterError
from sunoauxtool.midi import (
    ControlChange,
    KeyPoint,
    MeterPoint,
    MidiDocumentV2,
    MidiTrackV2,
    NoteEvent,
    RawEvent,
    TempoPoint,
    read_smf,
    write_smf,
)

GOLDEN = Path(__file__).parent / "golden" / "basic_two_tracks.events.json"


# 夹具刻意内联：tests/ 子目录再建 conftest.py 会以模块名 "conftest" 覆盖根 conftest，
# 导致旧测试里的 ``from conftest import box`` 指向错误文件（PR-A 实测踩中，全量红）。
@pytest.fixture
def basic_doc() -> MidiDocumentV2:
    """两轨 + 鼓 + 三点速度表 + 变拍 + 中文标记的测试文档。"""
    return MidiDocumentV2(
        title="夜航 Night Flight",
        tempo_map=[
            TempoPoint(beat=0, bpm=96),
            TempoPoint(beat=32, bpm=104),
            TempoPoint(beat=64, bpm=120),
        ],
        meter_map=[
            MeterPoint(beat=0, numerator=4, denominator=4),
            MeterPoint(beat=64, numerator=3, denominator=4),
        ],
        key_map=[KeyPoint(beat=0, key="D minor")],
        markers=[(0.0, "前奏"), (16.0, "主歌")],
        tracks=[
            MidiTrackV2(
                name="Piano",
                channel=0,
                program=0,
                volume=96,
                pan=52,
                notes=[
                    NoteEvent(pitch=62, start=0, duration=2, velocity=80),
                    NoteEvent(pitch=65, start=2, duration=2, velocity=72),
                    NoteEvent(pitch=69, start=4, duration=4, velocity=90),
                ],
                control_changes=[ControlChange(beat=8, controller=64, value=127)],
            ),
            MidiTrackV2(
                name="Cello",
                channel=1,
                program=42,
                bank_lsb=1,
                notes=[
                    NoteEvent(pitch=38, start=0, duration=4, velocity=70),
                    NoteEvent(pitch=41, start=4, duration=4, velocity=74),
                ],
            ),
            MidiTrackV2(
                name="Drums",
                channel=9,
                is_drum=True,
                notes=[
                    NoteEvent(pitch=36, start=0, duration=1, velocity=100),
                    NoteEvent(pitch=42, start=1, duration=1, velocity=60),
                    NoteEvent(pitch=38, start=2, duration=1, velocity=90),
                ],
            ),
        ],
    )


def _clean_text(data: dict) -> dict:
    """把 mido 以 latin1 解出的文本还原为 UTF-8（仅用于断言可读性）。"""
    for key in ("name", "text"):
        value = data.get(key)
        if isinstance(value, str):
            try:
                data[key] = value.encode("latin1").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
    return data


def _events(path: Path) -> list[list[dict]]:
    """按 mido track 分组的事件快照（绝对 tick）。"""
    midi = mido.MidiFile(str(path))
    result: list[list[dict]] = []
    for track in midi.tracks:
        tick = 0
        items: list[dict] = []
        for message in track:
            tick += int(message.time)
            data = message.dict()
            data.pop("time", None)
            items.append({"tick": tick, **_clean_text(data)})
        result.append(items)
    return result


def _find(events: list[dict], **criteria) -> list[dict]:
    return [event for event in events if all(event.get(k) == v for k, v in criteria.items())]


class TestRoundTrip:
    def test_maps_tracks_and_notes(self, basic_doc: MidiDocumentV2, tmp_path: Path) -> None:
        path = write_smf(basic_doc, tmp_path / "song.mid")
        back = read_smf(path)

        assert back.ppq == 960
        assert "夜航" in back.title
        assert [(point.beat, round(point.bpm, 3)) for point in back.tempo_map] == [
            (0.0, 96.0), (32.0, 104.0), (64.0, 120.0)
        ]
        assert [(p.beat, p.numerator, p.denominator) for p in back.meter_map] == [
            (0.0, 4, 4), (64.0, 3, 4)
        ]
        assert [(p.beat, p.key) for p in back.key_map] == [(0.0, "D minor")]
        assert back.markers == [(0.0, "前奏"), (16.0, "主歌")]

        assert [(t.name, t.channel, t.program, t.is_drum) for t in back.tracks] == [
            ("Piano", 0, 0, False),
            ("Cello", 1, 42, False),
            ("Drums", 9, 0, True),
        ]
        piano, cello, drums = back.tracks
        assert (piano.volume, piano.pan) == (96, 52)
        assert [(round(c.beat, 6), c.controller, c.value) for c in piano.control_changes] == [
            (8.0, 64, 127)
        ]
        assert cello.bank_lsb == 1
        assert [(n.pitch, round(n.start, 6), round(n.duration, 6), n.velocity)
                for n in piano.notes] == [
            (62, 0.0, 2.0, 80), (65, 2.0, 2.0, 72), (69, 4.0, 4.0, 90)
        ]
        assert [n.pitch for n in drums.notes] == [36, 42, 38]

    def test_unicode_track_name_and_marker(self, tmp_path: Path) -> None:
        doc = MidiDocumentV2(
            title="测试曲",
            tracks=[MidiTrackV2(name="主旋律", channel=0,
                                notes=[NoteEvent(60, 0, 1)])],
            markers=[(0.0, "第一段")],
        )
        back = read_smf(write_smf(doc, tmp_path / "cn.mid"))
        assert back.title == "测试曲"
        assert back.tracks[0].name == "主旋律"
        assert back.markers == [(0.0, "第一段")]

    def test_ppq_is_preserved(self, tmp_path: Path) -> None:
        doc = MidiDocumentV2(ppq=480, tracks=[MidiTrackV2(name="x", channel=0,
                                                           notes=[NoteEvent(60, 0, 1)])])
        assert read_smf(write_smf(doc, tmp_path / "p.mid")).ppq == 480

    def test_note_overlap_is_truncated(self, tmp_path: Path) -> None:
        doc = MidiDocumentV2(tracks=[MidiTrackV2(name="x", channel=0, notes=[
            NoteEvent(60, 0, 4),
            NoteEvent(60, 2, 2),
        ])])
        back = read_smf(write_smf(doc, tmp_path / "o.mid"))
        assert [(round(n.start, 6), round(n.duration, 6)) for n in back.tracks[0].notes] == [
            (0.0, 2.0), (2.0, 2.0)
        ]

    def test_offset_ticks_shift_note(self, tmp_path: Path) -> None:
        doc = MidiDocumentV2(tracks=[MidiTrackV2(name="x", channel=0, notes=[
            NoteEvent(60, 1.0, 1.0, offset_ticks=10),
        ])])
        back = read_smf(write_smf(doc, tmp_path / "offset.mid"))
        assert back.tracks[0].notes[0].start == pytest.approx(970 / 960, abs=1e-9)

    def test_raw_events_preserved(self, tmp_path: Path) -> None:
        doc = MidiDocumentV2(
            tracks=[MidiTrackV2(
                name="x", channel=0,
                notes=[NoteEvent(60, 0, 1)],
                raw_events=[RawEvent(0.5, {"type": "pitchwheel", "channel": 0, "pitch": 200})],
            )],
            raw_events=[RawEvent(0.25, {"type": "sysex", "data": (1, 2, 3)})],
        )
        back = read_smf(write_smf(doc, tmp_path / "raw.mid"))
        assert [event.message["type"] for event in back.tracks[0].raw_events] == ["pitchwheel"]
        assert back.tracks[0].raw_events[0].beat == pytest.approx(0.5)
        assert [event.message["type"] for event in back.raw_events] == ["sysex"]
        assert back.raw_events[0].beat == pytest.approx(0.25)


class TestEventLayout:
    def test_conductor_and_track_events(self, basic_doc: MidiDocumentV2,
                                        tmp_path: Path) -> None:
        events = _events(write_smf(basic_doc, tmp_path / "layout.mid"))
        conductor, piano, cello, drums = events

        tempo = [event for event in conductor if event["type"] == "set_tempo"]
        assert [event["tick"] for event in tempo] == [0, 32 * 960, 64 * 960]

        meters = [event for event in conductor if event["type"] == "time_signature"]
        assert [(event["tick"], event["numerator"], event["denominator"]) for event in meters] == [
            (0, 4, 4), (64 * 960, 3, 4)
        ]
        markers = [event for event in conductor if event["type"] == "marker"]
        assert [(event["tick"], event["text"]) for event in markers] == [
            (0, "前奏"), (16 * 960, "主歌")
        ]

        # 初始事件顺序：轨名 → program → CC7 → CC10 → note_on
        types = [event["type"] for event in piano[:5]]
        assert types == ["track_name", "program_change", "control_change",
                         "control_change", "note_on"]
        assert (piano[2]["control"], piano[2]["value"]) == (7, 96)
        assert (piano[3]["control"], piano[3]["value"]) == (10, 52)

        # 音色库选择先于 program_change（Cello：CC0/CC32 → program 42）
        bank = [event for event in cello if event["type"] == "control_change"
                and event["control"] in (0, 32)]
        assert [(event["control"], event["value"]) for event in bank] == [(0, 0), (32, 1)]
        assert cello.index(bank[-1]) < cello.index(
            next(event for event in cello if event["type"] == "program_change"))

        # 鼓轨全部在通道 9
        assert {event.get("channel") for event in drums
                if event["type"] in ("note_on", "note_off")} == {9}

    def test_note_on_off_offsets(self, basic_doc: MidiDocumentV2, tmp_path: Path) -> None:
        events = _events(write_smf(basic_doc, tmp_path / "notes.mid"))
        piano = events[1]
        on = _find(piano, type="note_on", note=69)
        off = _find(piano, type="note_off", note=69)
        assert on[0]["tick"] == 4 * 960
        assert off[0]["tick"] == 8 * 960


class TestDeterminism:
    def test_same_doc_same_bytes(self, basic_doc: MidiDocumentV2, tmp_path: Path) -> None:
        first = write_smf(basic_doc, tmp_path / "a.mid")
        second = write_smf(basic_doc, tmp_path / "b.mid")
        assert first.read_bytes() == second.read_bytes()

    def test_golden_event_snapshot(self, basic_doc: MidiDocumentV2, tmp_path: Path) -> None:
        """golden 事件快照（回归保护）。

        重新生成：``SUNO_REGEN_MIDI_GOLDEN=1 pytest tests/midi/test_midi_io.py::TestDeterminism::test_golden_event_snapshot``
        """
        actual = json.loads(json.dumps(
            _events(write_smf(basic_doc, tmp_path / "golden.mid")), ensure_ascii=False))
        if os.environ.get("SUNO_REGEN_MIDI_GOLDEN"):
            GOLDEN.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN.write_text(json.dumps(actual, ensure_ascii=False, indent=1) + "\n",
                              encoding="utf-8")
        expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
        assert actual == expected


class TestReader:
    def test_default_maps_when_absent(self, tmp_path: Path) -> None:
        midi = mido.MidiFile(type=1, ticks_per_beat=480)
        track = mido.MidiTrack()
        track.append(mido.MetaMessage("track_name", name="plain"))
        track.append(mido.Message("note_on", note=60, velocity=80))
        track.append(mido.Message("note_off", note=60, velocity=0, time=480))
        midi.tracks.append(track)
        path = tmp_path / "plain.mid"
        midi.save(str(path))

        doc = read_smf(path)
        assert [(point.beat, point.bpm) for point in doc.tempo_map] == [(0.0, 120.0)]
        assert [(p.beat, p.numerator, p.denominator) for p in doc.meter_map] == [(0.0, 4, 4)]
        assert [(p.beat, p.key) for p in doc.key_map] == [(0.0, "C major")]
        assert doc.tracks[0].notes[0].duration == pytest.approx(1.0)

    def test_format0_multi_channel_split(self, tmp_path: Path) -> None:
        midi = mido.MidiFile(type=0, ticks_per_beat=480)
        track = mido.MidiTrack()
        track.append(mido.MetaMessage("track_name", name="External"))
        track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(100)))
        track.append(mido.Message("program_change", channel=2, program=24))
        track.append(mido.Message("note_on", channel=2, note=60, velocity=90))
        track.append(mido.Message("note_on", channel=5, note=64, velocity=80, time=0))
        track.append(mido.Message("note_off", channel=2, note=60, velocity=0, time=480))
        track.append(mido.Message("note_off", channel=5, note=64, velocity=0, time=0))
        midi.tracks.append(track)
        path = tmp_path / "format0.mid"
        midi.save(str(path))

        doc = read_smf(path)
        assert sorted(track.channel for track in doc.tracks) == [2, 5]
        assert next(t for t in doc.tracks if t.channel == 2).program == 24
        assert all(note.duration == pytest.approx(1.0)
                   for track in doc.tracks for note in track.notes)
        assert doc.tempo_map[0].bpm == pytest.approx(100.0)

    def test_unclosed_note_closes_at_track_end(self, tmp_path: Path) -> None:
        midi = mido.MidiFile(type=1, ticks_per_beat=480)
        track = mido.MidiTrack()
        track.append(mido.Message("note_on", note=60, velocity=88))
        track.append(mido.MetaMessage("marker", text="end", time=960))
        midi.tracks.append(track)
        path = tmp_path / "unclosed.mid"
        midi.save(str(path))

        doc = read_smf(path)
        note = doc.tracks[0].notes[0]
        assert note.duration == pytest.approx(2.0)
        assert note.velocity == 88

    def test_second_program_change_kept_as_raw(self, tmp_path: Path) -> None:
        midi = mido.MidiFile(type=1, ticks_per_beat=480)
        track = mido.MidiTrack()
        track.append(mido.Message("program_change", channel=0, program=10))
        track.append(mido.Message("program_change", channel=0, program=40, time=480))
        track.append(mido.Message("note_on", channel=0, note=60, velocity=70, time=0))
        track.append(mido.Message("note_off", channel=0, note=60, velocity=0, time=480))
        midi.tracks.append(track)
        path = tmp_path / "prog.mid"
        midi.save(str(path))

        doc = read_smf(path)
        assert doc.tracks[0].program == 10
        assert [event.message["type"] for event in doc.tracks[0].raw_events] == [
            "program_change"
        ]

    def test_late_volume_cc_becomes_automation(self, tmp_path: Path) -> None:
        midi = mido.MidiFile(type=1, ticks_per_beat=480)
        track = mido.MidiTrack()
        track.append(mido.Message("control_change", channel=0, control=7, value=90))
        track.append(mido.Message("control_change", channel=0, control=7, value=50, time=480))
        track.append(mido.Message("note_on", channel=0, note=60, velocity=70))
        track.append(mido.Message("note_off", channel=0, note=60, velocity=0, time=480))
        midi.tracks.append(track)
        path = tmp_path / "cc.mid"
        midi.save(str(path))

        doc = read_smf(path)
        assert doc.tracks[0].volume == 90
        assert [(round(change.beat, 6), change.controller, change.value)
                for change in doc.tracks[0].control_changes] == [(1.0, 7, 50)]

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(InputFileError) as excinfo:
            read_smf(tmp_path / "nope.mid")
        assert excinfo.value.code == 3

    def test_corrupt_file(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.mid"
        path.write_bytes(b"not a midi file at all")
        with pytest.raises(InputFileError) as excinfo:
            read_smf(path)
        assert excinfo.value.code == 3

    def test_format2_rejected(self, tmp_path: Path) -> None:
        data = (
            b"MThd" + (6).to_bytes(4, "big") + (2).to_bytes(2, "big")
            + (0).to_bytes(2, "big") + (96).to_bytes(2, "big")
        )
        path = tmp_path / "format2.mid"
        path.write_bytes(data)
        with pytest.raises(InputFileError, match="format 2"):
            read_smf(path)


class TestWriterErrors:
    def test_invalid_document_rejected(self, tmp_path: Path) -> None:
        doc = MidiDocumentV2(tracks=[
            MidiTrackV2(name="a", channel=0),
            MidiTrackV2(name="b", channel=0),
        ])
        with pytest.raises(ParameterError):
            write_smf(doc, tmp_path / "x.mid")

    def test_unwritable_target_raises_export_error(self, basic_doc: MidiDocumentV2,
                                                   tmp_path: Path) -> None:
        with pytest.raises(ExportError) as excinfo:
            write_smf(basic_doc, tmp_path)  # 目标本身是目录
        assert excinfo.value.code == 5
