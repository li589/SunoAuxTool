"""总谱 v2（2.0 PR-F）：``Score.from_song`` / 分谱 / 移调 / 分谱组。

只测本 PR 新增能力；排版几何（SVG 路径、行中心对称）由既有 ``tests/score`` 覆盖。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from sunoauxtool.compose.arranger import arrange
from sunoauxtool.compose.spec import SongSpec
from sunoauxtool.score.model import Score
from sunoauxtool.score.musicxml import MusicXmlOptions, render_musicxml, validate_musicxml


def _song(*, transpose: int = 0, extra_track: bool = False):
    tracks = [
        {"id": "piano", "name": "钢琴", "role": "harmony"},
        {"id": "clarinet", "name": "Bb单簧管", "role": "melody", "transpose": transpose},
        {"id": "bass", "name": "贝斯", "role": "bass"},
        {"id": "drums", "name": "鼓组", "role": "drums"},
    ]
    if extra_track:
        tracks.insert(1, {"id": "epiano", "name": "电钢琴", "role": "harmony"})
    spec = SongSpec.from_dict({
        "schema": 1,
        "song": {"title": "夜航", "key": "D minor", "tempo": 96, "seed": 11},
        "section": [
            {"id": "a", "bars": 2, "energy": 0.9, "progression": ["Dm", "Bb"]},
            {"id": "b", "bars": 2, "energy": 0.5, "progression": ["F", "C"]},
        ],
        "track": tracks,
    })
    return arrange(spec)


class TestFromSong:
    def test_basic_metadata(self) -> None:
        score = Score.from_song(_song())
        assert score.title == "夜航"
        assert score.key == "D minor"
        assert score.time_signature == "4/4"
        assert score.bpm == 96
        assert score.bars == 4
        assert len(score.tracks) == 4

    def test_track_metadata(self) -> None:
        score = Score.from_song(_song())
        assert [track.name for track in score.tracks] == [
            "钢琴", "Bb单簧管", "贝斯", "鼓组",
        ]
        assert score.tracks[0].program == 0
        assert score.tracks[1].program == 73
        assert score.tracks[2].clef == "bass"
        assert score.tracks[3].is_drum is True

    def test_part_groups_by_role(self) -> None:
        score = Score.from_song(_song(extra_track=True))
        assert [track.part_group for track in score.tracks] == [
            "键盘组", "键盘组", "旋律组", "低音组", "节奏组",
        ]

    def test_part_groups_can_be_disabled(self) -> None:
        score = Score.from_song(_song(), part_groups=False)
        assert all(track.part_group == "" for track in score.tracks)

    def test_notes_are_carried_over(self) -> None:
        song = _song()
        score = Score.from_song(song)
        total = sum(len(track.notes) for track in score.tracks)
        expected = sum(len(clip.notes) for track in song.tracks for clip in track.clips)
        assert total >= expected  # 会补休止符，所以只会多不会少

    def test_transpose_propagated(self) -> None:
        score = Score.from_song(_song(transpose=2))
        clarinet = score.tracks[1]
        assert clarinet.transpose == 2
        assert clarinet.is_transposing
        assert clarinet.written_pitch(60) == 62

    def test_title_override_and_composer(self) -> None:
        score = Score.from_song(_song(), title="另起一名", composer="齐见林")
        assert score.title == "另起一名"
        assert score.composer == "齐见林"

    def test_clef_override(self) -> None:
        score = Score.from_song(_song(), clefs={"钢琴": "bass"})
        assert score.tracks[0].clef == "bass"

    def test_invalid_song_rejected(self) -> None:
        """谱面只接受通过校验的曲子（段落不成链 → 直接拒）。"""
        from sunoauxtool.exceptions import ParameterError

        song = _song()
        song.sections[1].start = 7.5  # 破坏段落首尾相接
        with pytest.raises(ParameterError):
            Score.from_song(song)


class TestParts:
    def test_split_one_per_track(self) -> None:
        score = Score.from_song(_song())
        parts = score.parts()
        assert len(parts) == len(score.tracks)
        assert [part.tracks[0].name for part in parts] == [t.name for t in score.tracks]

    def test_parts_keep_metadata(self) -> None:
        score = Score.from_song(_song(transpose=2))
        part = score.parts()[1]
        assert part.title.endswith("Bb单簧管")
        assert part.key == score.key
        assert part.time_signature == score.time_signature
        assert part.bars == score.bars
        assert part.tracks[0].transpose == 2

    def test_single_track_score_has_one_part(self) -> None:
        score = Score.from_song(_song())
        assert len(score.parts()) == 4


class TestMusicXml:
    def _root(self, song, **kwargs) -> ET.Element:
        score = Score.from_song(song)
        return validate_musicxml(render_musicxml(score, kwargs.pop("options", None)))

    def test_valid_structure(self) -> None:
        root = self._root(_song())
        assert root.tag == "score-partwise"
        assert len(root.findall("part")) == 4
        assert root.find("part-list") is not None

    def test_transpose_node(self) -> None:
        root = self._root(_song(transpose=2))
        nodes = root.findall(".//transpose")
        assert len(nodes) == 1
        assert nodes[0].findtext("chromatic") == "2"
        assert nodes[0].findtext("diatonic") == "1"

    def test_no_transpose_when_concert(self) -> None:
        assert self._root(_song()).findall(".//transpose") == []

    def test_written_pitch_is_transposed(self) -> None:
        """Bb 单簧管：记谱比实音高 2 个半音（实测 F4 实音 → G4 记谱）。"""
        song = _song(transpose=2)
        score = Score.from_song(song)
        clarinet_index = [track.transpose for track in score.tracks].index(2)
        root = validate_musicxml(render_musicxml(score))
        part = root.findall("part")[clarinet_index]
        steps = [
            (note.findtext("pitch/step"), note.findtext("pitch/octave"))
            for note in part.findall(".//note") if note.find("pitch") is not None
        ]
        sounding = [n.pitch for n in score.tracks[clarinet_index].notes if not n.is_rest]
        written = [score.tracks[clarinet_index].written_pitch(pitch) for pitch in sounding]
        assert len(steps) == len(written)
        # 用 MIDI 反查音名验证一致（不依赖 SVG 拼写规则）
        from sunoauxtool.score.theory import spell_pitch

        for index, (step, octave) in enumerate(steps[:8]):
            letter, diatonic, _alter = spell_pitch(written[index], True)
            assert (step, octave) == (["C", "D", "E", "F", "G", "A", "B"][letter],
                                      str(diatonic // 7))

    def test_part_group_markers(self) -> None:
        root = self._root(_song(extra_track=True))
        part_list = root.find("part-list")
        tags = [child.tag for child in part_list]
        assert tags.count("part-group") == 8  # 4 组 × start/stop
        starts = [child for child in part_list
                  if child.tag == "part-group" and child.get("type") == "start"]
        assert [marker.findtext("group-name") for marker in starts] == [
            "键盘组", "旋律组", "低音组", "节奏组",
        ]

    def test_part_groups_can_be_disabled(self) -> None:
        score = Score.from_song(_song())
        root = validate_musicxml(render_musicxml(
            score, MusicXmlOptions(include_part_groups=False)
        ))
        assert root.find("part-list").findall("part-group") == []

    def test_part_group_spans_merge_consecutive(self) -> None:
        from sunoauxtool.score.musicxml import _part_group_spans

        score = Score.from_song(_song(extra_track=True))
        spans = _part_group_spans(score)
        assert spans[0] == (0, 1, "键盘组")

    def test_part_score_musicxml(self) -> None:
        score = Score.from_song(_song())
        part = score.parts()[1]
        root = validate_musicxml(render_musicxml(part))
        assert len(root.findall("part")) == 1

    def test_deterministic_output(self) -> None:
        first = render_musicxml(Score.from_song(_song()))
        second = render_musicxml(Score.from_song(_song()))
        assert first == second

    @pytest.mark.parametrize("transpose", [-2, 2, -3, 7])
    def test_transpose_variants(self, transpose: int) -> None:
        root = self._root(_song(transpose=transpose))
        node = root.find(".//transpose")
        assert node is not None
        assert node.findtext("chromatic") == str(transpose)
