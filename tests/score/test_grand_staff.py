"""大谱表（2.6 F+）：钢琴两行谱的拆分、排版配对、SVG 花括号与 MusicXML brace 组。"""

from __future__ import annotations

import re


from sunoauxtool.compose.arranger import ArrangeOptions, arrange
from sunoauxtool.compose.spec import SongSpec
from sunoauxtool.score.layout import layout_score
from sunoauxtool.score.model import Score, _split_hands
from sunoauxtool.score.musicxml import render_musicxml
from sunoauxtool.score.svg import render_svg


def _song(program: int = 0, *, tracks: list | None = None):
    data = {
        "schema": 1,
        "song": {"title": "钢琴小品", "key": "A minor", "tempo": 84, "seed": 20261029},
        "section": [{"id": "a", "bars": 4, "energy": 0.6,
                     "progression": ["Am", "F", "C", "G"]}],
        "track": tracks or [{"id": "piano", "role": "harmony", "program": program,
                             "sections": ["a"]}],
    }
    spec = SongSpec.from_dict(data)
    return arrange(spec, ArrangeOptions(humanize=False))


class TestSplitHands:
    def test_partition_by_pitch(self) -> None:
        from sunoauxtool.midi.document import NoteEvent

        notes = [NoteEvent(48, 0, 1, 80), NoteEvent(72, 0, 1, 80), NoteEvent(60, 5, 1, 80)]
        upper, lower = _split_hands(notes)
        assert [n.pitch for n in upper] == [72, 60]
        assert [n.pitch for n in lower] == [48]

    def test_piano_track_becomes_two_staves(self) -> None:
        score = Score.from_song(_song())
        assert [t.brace for t in score.tracks] == ["piano", "piano"]
        assert [t.clef for t in score.tracks] == ["treble", "bass"]
        assert all(t.name == score.tracks[0].name for t in score.tracks)

    def test_hands_partition_actual_notes(self) -> None:
        score = Score.from_song(_song())
        upper = [n.pitch for n in score.tracks[0].notes if not n.is_rest]
        lower = [n.pitch for n in score.tracks[1].notes if not n.is_rest]
        assert upper and lower
        assert min(upper) >= 60 > max(lower)

    def test_non_piano_program_stays_single(self) -> None:
        # 25 = 尼龙吉他：不该拆两行
        score = Score.from_song(_song(program=25))
        assert [t.brace for t in score.tracks] == [""]

    def test_can_disable(self) -> None:
        score = Score.from_song(_song(), grand_staff=False)
        assert [t.brace for t in score.tracks] == [""]
        assert len(score.tracks) == 1


class TestLayoutPairs:
    def test_brace_pair_recorded(self) -> None:
        layout = layout_score(Score.from_song(_song()))
        assert layout.systems[0].brace_pairs == [(0, 1)]

    def test_gap_inside_pair_is_tighter(self) -> None:
        layout = layout_score(Score.from_song(_song()))
        staffs = layout.systems[0].staffs
        assert len(staffs) == 2
        gap = staffs[1].y - staffs[0].y
        # 对大谱表内行距明显小于"普通谱表间距 + 两行自身高度"的常规值
        assert gap < 150

    def test_single_staff_has_no_pair(self) -> None:
        layout = layout_score(Score.from_song(_song(), grand_staff=False))
        assert layout.systems[0].brace_pairs == []


class TestBraceDrawing:
    def test_svg_has_brace_path(self) -> None:
        svg = render_svg(layout_score(Score.from_song(_song())))
        # 花括号是 scale(space) 组里的描边路径（fill="none"）
        groups = re.findall(r'<g transform="scale\([0-9.]+\)">(.*?)</g>', svg, re.S)
        assert any('fill="none"' in group and "<path" in group for group in groups)

    def test_no_brace_when_disabled(self) -> None:
        svg = render_svg(layout_score(Score.from_song(_song(), grand_staff=False)))
        assert '<g transform="scale(' not in svg


class TestMusicXmlBrace:
    def _xml(self, *, enabled: bool = True) -> str:
        return render_musicxml(Score.from_song(_song(), grand_staff=enabled))

    def test_brace_group_written(self) -> None:
        xml = self._xml()
        assert '<group-symbol>brace</group-symbol>' in xml
        assert '<group-name>Piano</group-name>' in xml

    def test_groups_nest_lifo(self) -> None:
        xml = self._xml()
        order = re.findall(r'<part-group type="(\w+)" number="(\d+)"', xml)
        # 键盘组先开、brace 后开（编号接续）；收口时先关内层 brace 再关外层
        assert order[0][0] == "start" and order[1][0] == "start"
        outer, inner = order[0][1], order[1][1]
        stops = [item for item in order if item[0] == "stop"]
        assert stops[:2] == [("stop", inner), ("stop", outer)]

    def test_two_parts_for_piano(self) -> None:
        xml = self._xml()
        assert xml.count("<score-part ") == 2

    def test_disabled_has_no_brace_group(self) -> None:
        # part_groups 仍在（键盘组 bracket 照旧），只是没有大谱表的 brace 组
        xml = self._xml(enabled=False)
        assert "<group-symbol>brace</group-symbol>" not in xml


class TestPianoWithOthers:
    def test_piano_pair_then_other_track(self) -> None:
        data_tracks = [
            {"id": "piano", "role": "harmony", "program": 0, "sections": ["a"]},
            {"id": "lead", "role": "melody", "sections": ["a"]},
        ]
        score = Score.from_song(_song(tracks=data_tracks))
        assert [t.brace for t in score.tracks][:2] == ["piano", "piano"]
        assert score.tracks[-1].brace == ""
        layout = layout_score(score)
        assert layout.systems[0].brace_pairs == [(0, 1)]
