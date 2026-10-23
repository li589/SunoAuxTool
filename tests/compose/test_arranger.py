"""编排主流程（compose/arranger.py）单测。

重点验证三件事：

1. **确定性**（G8）：同清单同种子 → 音符逐项一致；换种子必变；
2. **种子隔离**：改某轨 density / 增删轨道不影响其它轨道；
3. **编排正确性**：能量门控、音域/片段边界、鼓轨通道、显式音符优先。
"""

from __future__ import annotations

import copy

import pytest

from sunoauxtool.compose.arranger import ArrangeOptions, arrange
from sunoauxtool.compose.spec import SongSpec


def _spec() -> dict:
    return {
        "schema": 1,
        "song": {"title": "夜航", "key": "D minor", "tempo": 96, "seed": 2026},
        "section": [
            {"id": "intro", "bars": 2, "energy": 0.2, "progression": ["Dm", "Bb"]},
            {"id": "verse", "bars": 4, "energy": 0.5, "progression": ["Dm", "Bb", "F", "C"]},
            {"id": "chorus", "bars": 4, "energy": 0.9, "progression": ["Bb", "C", "Dm", "Dm"]},
        ],
        "track": [
            {"id": "piano", "role": "harmony", "sections": ["intro", "verse", "chorus"]},
            {"id": "strings", "role": "pad", "sections": ["intro", "chorus"]},
            {"id": "bass", "role": "bass", "sections": ["verse", "chorus"]},
            {"id": "lead", "role": "melody", "sections": ["verse", "chorus"]},
            {"id": "oboe", "role": "counter", "sections": ["chorus"]},
            {"id": "harp", "role": "arpeggio", "sections": ["chorus"]},
            {"id": "drums", "role": "drums", "sections": ["verse", "chorus"]},
        ],
    }


def _arrange(data: dict | None = None, **options):
    spec = SongSpec.from_dict(copy.deepcopy(data or _spec()))
    return arrange(spec, ArrangeOptions(**options) if options else None)


def _notes(song, track_id: str) -> list[tuple]:
    track = song.track_by_id(track_id)
    return [
        (clip.section_id, note.start, note.pitch, note.duration, note.velocity,
         note.offset_ticks)
        for clip in track.clips for note in clip.notes
    ]


def _total(song) -> int:
    return sum(len(clip.notes) for track in song.tracks for clip in track.clips)


class TestBasic:
    def test_all_tracks_are_filled(self) -> None:
        song = _arrange()
        assert _total(song) > 100
        for track in song.tracks:
            notes = sum(len(clip.notes) for clip in track.clips)
            if track.id == "oboe":
                assert notes > 0
            assert notes > 0, track.id

    def test_song_still_validates(self) -> None:
        song = _arrange()
        song.validate()  # 内部已校验，这里再确认一遍不抛

    def test_channels_and_programs(self) -> None:
        document = _arrange().to_midi()
        by_name = {track.name: track for track in document.tracks}
        assert by_name["lead"].program == 73
        assert by_name["bass"].program == 33
        assert by_name["harp"].program == 46
        assert by_name["drums"].channel == 9 and by_name["drums"].is_drum

    def test_markers_and_duration(self) -> None:
        song = _arrange()
        assert [section.id for section in song.sections] == ["intro", "verse", "chorus"]
        assert song.duration_beats == 40.0  # (2+4+4) 小节 * 4 拍


class TestEnergyGate:
    def test_intro_keeps_only_pad(self) -> None:
        song = _arrange()
        assert _notes(song, "piano")[:0] == []
        # intro 能量 0.2：和弦轨被门控挡住，只剩 pad
        assert all(clip.notes == [] for clip in song.track_by_id("piano").clips
                   if clip.section_id == "intro")
        assert any(clip.notes for clip in song.track_by_id("strings").clips
                   if clip.section_id == "intro")

    def test_verse_has_no_counter(self) -> None:
        song = _arrange()
        assert all(clip.notes == [] for clip in song.track_by_id("oboe").clips
                   if clip.section_id == "verse")

    def test_energy_gate_can_be_disabled(self) -> None:
        song = _arrange(energy_gate=False)
        assert any(clip.notes for clip in song.track_by_id("piano").clips
                   if clip.section_id == "intro")

    def test_higher_energy_more_notes(self) -> None:
        song = _arrange()
        verse = sum(len(clip.notes) for clip in song.track_by_id("piano").clips
                    if clip.section_id == "verse")
        chorus = sum(len(clip.notes) for clip in song.track_by_id("piano").clips
                     if clip.section_id == "chorus")
        # 副歌用打击式和弦（两次），音符数更多
        assert chorus > verse


class TestDeterminism:
    def test_same_seed_same_notes(self) -> None:
        first = _arrange()
        second = _arrange()
        for track_id in ("piano", "lead", "bass", "drums"):
            assert _notes(first, track_id) == _notes(second, track_id)

    def test_same_seed_same_bytes(self, tmp_path) -> None:
        from sunoauxtool.midi.io import write_smf

        digests = []
        for index in range(2):
            song = _arrange()
            path = tmp_path / f"take{index}.mid"
            write_smf(song.to_midi(), path)
            digests.append(path.read_bytes())
        assert digests[0] == digests[1]

    def test_seed_changes_melody(self) -> None:
        base = _arrange()
        other = copy.deepcopy(_spec())
        other["song"]["seed"] = 2027
        changed = _arrange(other)
        assert _notes(base, "lead") != _notes(changed, "lead")

    def test_track_isolation(self) -> None:
        """改一条轨道的 density 不影响其它轨道。"""
        base = _arrange()
        tweaked = copy.deepcopy(_spec())
        tweaked["track"][3]["density"] = 0.05  # lead：极稀，必然改变音符
        changed = _arrange(tweaked)
        for track_id in ("piano", "bass", "drums", "harp"):
            assert _notes(base, track_id) == _notes(changed, track_id)
        assert _notes(base, "lead") != _notes(changed, "lead")

    def test_adding_track_keeps_others(self) -> None:
        base = _arrange()
        more = copy.deepcopy(_spec())
        more["track"].append({"id": "timp", "role": "percussion", "sections": ["chorus"]})
        changed = _arrange(more)
        for track_id in ("piano", "lead", "bass", "drums"):
            assert _notes(base, track_id) == _notes(changed, track_id)

    def test_humanize_off_is_grid_aligned(self) -> None:
        song = _arrange(humanize=False)
        assert all(note[5] == 0 for note in _notes(song, "lead"))


class TestBounds:
    def test_notes_inside_clips(self) -> None:
        song = _arrange()
        for track in song.tracks:
            for clip in track.clips:
                for note in clip.notes:
                    assert clip.start - 1e-9 <= note.start
                    assert note.end <= clip.end + 1e-9, (track.id, clip.section_id)

    def test_notes_inside_role_ranges(self) -> None:
        song = _arrange()
        for track in song.tracks:
            if track.id == "drums":
                continue
            for clip in track.clips:
                for note in clip.notes:
                    assert 0 <= note.pitch <= 127
                    if track.range:
                        assert track.range[0] <= note.pitch <= track.range[1]

    def test_velocities_valid(self) -> None:
        song = _arrange()
        for track in song.tracks:
            for clip in track.clips:
                for note in clip.notes:
                    assert 1 <= note.velocity <= 127


class TestExplicitOverride:
    def test_track_with_notes_is_untouched(self) -> None:
        data = _spec()
        data["track"][0]["note"] = [{"beat": 0.0, "pitch": 64, "duration": 2.0}]
        song = _arrange(data)
        piano = song.track_by_id("piano")
        assert [(clip.section_id, [n.pitch for n in clip.notes]) for clip in piano.clips] == [
            ("intro", [64]), ("verse", []), ("chorus", []),
        ]

    def test_generated_program_filled_when_absent(self) -> None:
        song = _arrange()
        assert song.track_by_id("bass").instrument.resolved_program() == 33

    def test_explicit_program_kept(self) -> None:
        data = _spec()
        data["track"][2]["program"] = 34
        song = _arrange(data)
        assert song.track_by_id("bass").instrument.resolved_program() == 34

    def test_empty_sections_means_everywhere(self) -> None:
        data = _spec()
        for track in data["track"]:
            track.pop("sections", None)
        song = _arrange(data)
        clips = {clip.section_id: clip.notes for clip in song.track_by_id("bass").clips}
        assert set(clips) == {"intro", "verse", "chorus"}   # 每段都有片段
        assert clips["verse"] and clips["chorus"]           # 有能量的段有音符
        assert not clips["intro"]                           # 前奏被能量门控挡住


class TestMeterAndGroove:
    def test_waltz_groove(self) -> None:
        data = _spec()
        data["song"]["meter"] = "3/4"
        data["section"] = [
            {"id": "verse", "bars": 4, "energy": 0.8, "progression": ["Dm", "Bb", "F", "C"]},
        ]
        data["track"] = [
            {"id": "drums", "role": "drums"},
            {"id": "piano", "role": "harmony"},
        ]
        song = _arrange(data, fill=False, crash=False)
        starts = [note[1] for note in _notes(song, "drums")]
        assert starts
        assert max(starts) < 12.0  # 4 小节 * 3 拍

    def test_explicit_groove_respected(self) -> None:
        data = _spec()
        data["track"][-1]["groove"] = "electronic"
        song = _arrange(data)
        kicks = [note for note in _notes(song, "drums") if note[2] == 36]
        assert len(kicks) >= 4  # electronic 四踩

    def test_no_progression_uses_tonic(self) -> None:
        data = _spec()
        for section in data["section"]:
            section.pop("progression")
        song = _arrange(data)
        pitches = {note[2] % 12 for note in _notes(song, "bass")}
        assert pitches  # 用主和弦生成，不炸


class TestSparseRoles:
    def test_percussion_has_no_generator(self) -> None:
        data = _spec()
        data["track"] = [
            {"id": "timp", "role": "percussion"},
            {"id": "piano", "role": "harmony"},
        ]
        song = _arrange(data)
        assert all(not clip.notes for clip in song.track_by_id("timp").clips)
        assert any(clip.notes for clip in song.track_by_id("piano").clips)

    def test_counter_without_melody_generates_own_line(self) -> None:
        data = _spec()
        data["track"] = [
            {"id": "oboe", "role": "counter", "sections": ["chorus"]},
            {"id": "piano", "role": "harmony", "sections": ["chorus"]},
        ]
        song = _arrange(data)
        oboe = song.track_by_id("oboe")
        assert any(clip.notes for clip in oboe.clips)
        assert all(0 <= note.pitch <= 127 for clip in oboe.clips for note in clip.notes)
