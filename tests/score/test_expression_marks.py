"""谱面记号（2.2 M3）：演奏法 → ScoreNote、段落力度 → dynamics/wedges、MusicXML/SVG 输出。

验收口径是"能看得到"：不仅是内部字段填对，还要落到 MusicXML 的
``<articulations>`` / ``<dynamics>`` / ``<wedge>`` 与 SVG 的文字/连线指令上。
"""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET

from sunoauxtool.compose.arranger import ArrangeOptions, arrange
from sunoauxtool.compose.spec import SongSpec
from sunoauxtool.score.layout import layout_score
from sunoauxtool.score.model import Score, ScoreNote, _split_at_barlines
from sunoauxtool.score.musicxml import render_musicxml
from sunoauxtool.score.svg import render_svg


def _spec() -> dict:
    """intro（能量低，piano 不发声）→ verse（cresc）→ chorus（ff）。"""
    return {
        "schema": 1,
        "song": {"title": "夜航", "key": "D minor", "tempo": 96, "seed": 2026},
        "section": [
            {"id": "intro", "bars": 2, "energy": 0.1, "progression": ["Dm", "Bb"],
             "dynamic": "pp"},
            {"id": "verse", "bars": 4, "energy": 0.55, "progression": ["Dm", "Bb", "F", "C"],
             "ramp": "cresc"},
            {"id": "chorus", "bars": 4, "energy": 0.95, "progression": ["Bb", "C", "Dm", "Dm"],
             "dynamic": "ff", "ramp": "dim"},
        ],
        "track": [
            {"id": "piano", "role": "harmony", "sections": ["intro", "verse", "chorus"],
             "articulation": "staccato"},
            {"id": "lead", "role": "melody", "sections": ["chorus"], "articulation": "accent"},
            {"id": "drums", "role": "drums", "sections": ["verse", "chorus"]},
        ],
    }


def _score(data: dict | None = None, **options) -> Score:
    spec = SongSpec.from_dict(copy.deepcopy(data or _spec()))
    song = arrange(spec, ArrangeOptions(**options) if options else ArrangeOptions(
        humanize=False))
    return Score.from_song(song)


class TestArticulationPropagation:
    def test_note_carries_articulation(self) -> None:
        score = _score()
        piano = score.tracks[0]
        assert {n.articulation for n in piano.notes if not n.is_rest} == {"staccato"}

    def test_tags_survive_barline_split(self) -> None:
        # 6 拍长音跨 4/4 小节线：切出 2 片，记号只挂音头那片
        note = ScoreNote(pitch=60, start=0.0, duration=6.0, velocity=90,
                         articulation="staccato")
        parts = _split_at_barlines(note, capacity=4.0, bars=4)
        assert len(parts) == 2
        assert parts[0].articulation == "staccato"
        assert parts[1].articulation == ""
        assert parts[0].tie_to_next and parts[1].tie_from_prev

    def test_legacy_notes_without_field(self) -> None:
        # 旧模型 NoteSequence 的音符没有 articulation 属性，不应炸
        class Legacy:
            pitch = 62
            start = 0.0
            duration = 1.0
            velocity = 70

        score = Score._assemble(
            raw_tracks=[("old", 0, 0, False, [Legacy()])],
            title="t", composer="", key="C major", time_signature="4/4",
            bars=1, bpm=120, clefs=None, last_beat=1.0,
        )
        assert score.tracks[0].notes[0].articulation == ""


class TestSectionMarks:
    def test_dynamics_follow_sections(self) -> None:
        piano = _score().tracks[0]
        # verse 起于第 3 小节、chorus 起于第 7 小节（各段 4 小节）
        assert piano.dynamics == {3: "mf", 7: "ff"}

    def test_silent_section_gets_no_mark(self) -> None:
        # intro 能量 0.1 只有 pad 层，piano 整段无音 → 不在第 1 小节写记号
        piano = _score().tracks[0]
        assert 1 not in piano.dynamics

    def test_wedges_span_the_section(self) -> None:
        piano = _score().tracks[0]
        assert piano.wedges == {3: "crescendo", 7: "diminuendo"}
        # verse 4 小节 = 3..6；chorus 4 小节 = 7..10
        assert piano.wedge_ends == {6: True, 10: True}

    def test_drums_get_no_marks(self) -> None:
        drums = _score().tracks[2]
        assert drums.is_drum
        assert drums.dynamics == {} and drums.wedges == {}

    def test_explicit_dynamic_wins_over_energy(self) -> None:
        data = _spec()
        data["section"][2]["energy"] = 0.75  # 能量档位是 f，但显式写了 ff
        assert _score(data).tracks[0].dynamics[7] == "ff"


class TestMusicXmlMarks:
    def _root(self) -> ET.Element:
        text = render_musicxml(_score())
        body = text[text.index("<score-partwise"):]
        return ET.fromstring(body)

    def test_dynamics_written(self) -> None:
        root = self._root()
        marks = [node.tag for node in root.iter() if node.tag in ("p", "mf", "ff")]
        assert "mf" in marks and "ff" in marks

    def test_wedge_and_stop_with_offset(self) -> None:
        root = self._root()
        kinds = [
            (node.get("type"), node.get("number"))
            for node in root.iter("wedge")
        ]
        assert ("crescendo", "1") in kinds
        assert ("stop", "1") in kinds
        stops = [
            float(offset.text) for offset in root.iter("offset")
        ]
        # 收口用 offset 推到小节末（4/4 一小节 = 4 拍 × DIVISIONS 16）
        assert stops and all(value == 64.0 for value in stops)

    def test_articulations_written(self) -> None:
        root = self._root()
        names = {node.tag for node in root.iter() if node.tag == "staccato"}
        assert names == {"staccato"}
        # 记号必须挂在 <notations> 里，且不重复挂在延音线后片
        for notations in root.iter("notations"):
            assert len(notations.findall("articulations")) <= 1

    def test_accent_element_for_lead(self) -> None:
        root = self._root()
        assert any(node.tag == "accent" for node in root.iter())


class TestSvgMarks:
    def test_dynamic_text_and_hairpin_are_drawn(self) -> None:
        svg = render_svg(layout_score(_score()))
        assert ">mf<" in svg and ">ff<" in svg
        # 楔形是两段线：每小节 2 条，verse + chorus = 8 小节 → 至少 16 条细线
        assert svg.count("<line") > 16

    def test_marks_reserve_vertical_room(self) -> None:
        plain = _score()
        for track in plain.tracks:
            track.dynamics.clear()
            track.wedges.clear()
            track.wedge_ends.clear()
        with_marks = layout_score(_score())
        without = layout_score(plain)
        assert with_marks.page_height > without.page_height
