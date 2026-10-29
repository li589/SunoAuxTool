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
        score = Score.from_song(_song(), grand_staff=False)
        assert score.title == "夜航"
        assert score.key == "D minor"
        assert score.time_signature == "4/4"
        assert score.bpm == 96
        assert score.bars == 4
        assert len(score.tracks) == 4

    def test_track_metadata(self) -> None:
        score = Score.from_song(_song(), grand_staff=False)
        assert [track.name for track in score.tracks] == [
            "钢琴", "Bb单簧管", "贝斯", "鼓组",
        ]
        assert score.tracks[0].program == 0
        assert score.tracks[1].program == 73
        assert score.tracks[2].clef == "bass"
        assert score.tracks[3].is_drum is True

    def test_part_groups_by_role(self) -> None:
        score = Score.from_song(_song(extra_track=True), grand_staff=False)
        assert [track.part_group for track in score.tracks] == [
            "键盘组", "键盘组", "旋律组", "低音组", "节奏组",
        ]

    def test_part_groups_can_be_disabled(self) -> None:
        score = Score.from_song(_song(), part_groups=False)
        assert all(track.part_group == "" for track in score.tracks)

    def test_notes_are_carried_over(self) -> None:
        song = _song()
        score = Score.from_song(song, grand_staff=False)
        total = sum(len(track.notes) for track in score.tracks)
        expected = sum(len(clip.notes) for track in song.tracks for clip in track.clips)
        assert total >= expected  # 会补休止符，所以只会多不会少

    def test_transpose_propagated(self) -> None:
        score = Score.from_song(_song(transpose=2), grand_staff=False)
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
            Score.from_song(song, grand_staff=False)


class TestParts:
    def test_split_one_per_track(self) -> None:
        score = Score.from_song(_song(), grand_staff=False)
        parts = score.parts()
        assert len(parts) == len(score.tracks)
        assert [part.tracks[0].name for part in parts] == [t.name for t in score.tracks]

    def test_parts_keep_metadata(self) -> None:
        score = Score.from_song(_song(transpose=2), grand_staff=False)
        part = score.parts()[1]
        assert part.title.endswith("Bb单簧管")
        assert part.key == score.key
        assert part.time_signature == score.time_signature
        assert part.bars == score.bars
        assert part.tracks[0].transpose == 2

    def test_single_track_score_has_one_part(self) -> None:
        score = Score.from_song(_song(), grand_staff=False)
        assert len(score.parts()) == 4


class TestMusicXml:
    def _root(self, song, **kwargs) -> ET.Element:
        score = Score.from_song(song, grand_staff=False)
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
        score = Score.from_song(song, grand_staff=False)
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
        score = Score.from_song(_song(), grand_staff=False)
        root = validate_musicxml(render_musicxml(
            score, MusicXmlOptions(include_part_groups=False)
        ))
        assert root.find("part-list").findall("part-group") == []

    def test_part_group_spans_merge_consecutive(self) -> None:
        from sunoauxtool.score.musicxml import _part_group_spans

        score = Score.from_song(_song(extra_track=True), grand_staff=False)
        spans = _part_group_spans(score)
        assert spans[0] == (0, 1, "键盘组")

    def test_part_score_musicxml(self) -> None:
        score = Score.from_song(_song(), grand_staff=False)
        part = score.parts()[1]
        root = validate_musicxml(render_musicxml(part))
        assert len(root.findall("part")) == 1

    def test_deterministic_output(self) -> None:
        first = render_musicxml(Score.from_song(_song(), grand_staff=False))
        second = render_musicxml(Score.from_song(_song(), grand_staff=False))
        assert first == second

    @pytest.mark.parametrize("transpose", [-2, 2, -3, 7])
    def test_transpose_variants(self, transpose: int) -> None:
        root = self._root(_song(transpose=transpose))
        node = root.find(".//transpose")
        assert node is not None
        assert node.findtext("chromatic") == str(transpose)


class TestStemGeometry:
    """回归：符干锚点必须是中线，不能是谱表顶线（否则低音符干跨整条谱表）。"""

    def _clusters(self, pitches: list[int]):
        from sunoauxtool.models.notes import Note, NoteSequence
        from sunoauxtool.score.layout import layout_score

        seq = NoteSequence(bpm=120, key="C major", time_signature="4/4", bars=1)
        seq.add_track("melody", program=0, channel=0, notes=[
            Note(pitch=pitch, start=index * 1.0, duration=1.0, velocity=80)
            for index, pitch in enumerate(pitches)
        ])
        laid = layout_score(Score.from_sequence(seq, title="t", bars=1))
        return [c for c in laid.systems[0].staffs[0].measures[0].clusters
                if not c.is_rest and c.step_offsets]

    def test_low_note_stem_is_three_and_half_spaces(self) -> None:
        """C4（高音谱下一线之下）的符干应恰好 3.5 格，而不是拉到谱表顶线。"""
        clusters = self._clusters([60])
        cluster = clusters[0]
        assert cluster.stem_up is True
        assert cluster.stem_len == pytest.approx(3.5)

    def test_stem_tip_stays_near_notehead(self) -> None:
        """符干末端距符头不超过 3.5 格（低音不再跨整条谱表）。"""
        for cluster in self._clusters([55, 57, 60, 62]):
            top = max(cluster.step_offsets)
            assert cluster.stem_len * 2 <= (top - min(cluster.step_offsets)) + 7 + 1e-9

    def test_stem_length_follows_middle_line_rule(self) -> None:
        """不变式：符干 = 3.5 格，且远端必须越过中线（两端对称，锚点都是中线 0）。"""
        for pitch in (55, 60, 67, 72, 79):
            cluster = self._clusters([pitch])[0]
            lo, hi = min(cluster.step_offsets), max(cluster.step_offsets)
            if cluster.stem_up:
                expected = max(hi + 7, 0) - lo
            else:
                expected = hi - min(lo - 7, 0)
            assert cluster.stem_len * 2 == pytest.approx(expected), (pitch, cluster.step_offsets)
            assert cluster.stem_len >= 3.5 - 1e-9
