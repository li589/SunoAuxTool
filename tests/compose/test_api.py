"""Python DSL（compose/api.py）单测：与清单同构、编译结果一致。"""

from __future__ import annotations

import json

import pytest

from sunoauxtool.compose import MixStrip, SongSpec
from sunoauxtool.compose.api import SongBuilder
from sunoauxtool.compose.spec import ARRANGEMENT_ERROR, SpecError
from sunoauxtool.exceptions import ParameterError


def _builder() -> SongBuilder:
    return (
        SongBuilder("夜航", key="D minor", tempo=96, seed=7)
        .section("verse", bars=2, energy=0.4, progression="Dm-Bb")
        .section("chorus", bars=2, energy=0.9, progression=["Bb", "C"])
        .track("piano", name="钢琴", role="harmony", program=0,
               sections=["verse"], mix={"gain_db": -6.0, "pan": -0.2})
        .track("bass", name="贝斯", role="bass", program=33, octave=2)
        .note("piano", beat=0.0, pitch=62, duration=2.0, velocity=80)
    )


class TestCompile:
    def test_build_song(self) -> None:
        song = _builder().build()
        assert song.title == "夜航"
        assert [section.id for section in song.sections] == ["verse", "chorus"]
        assert [track.id for track in song.tracks] == ["piano", "bass"]
        assert song.track_by_id("piano").mix.gain_db == -6.0
        assert song.duration_beats == 16.0

    def test_midi_channels(self) -> None:
        document = _builder().build().to_midi()
        assert [(track.name, track.channel, track.program) for track in document.tracks] == [
            ("钢琴", 0, 0), ("贝斯", 1, 33),
        ]

    def test_spec_equals_equivalent_dict(self) -> None:
        """DSL 与 TOML/JSON 走同一条管线：两种写法的清单字典必须相等。"""
        data = {
            "schema": 1,
            "song": {"title": "夜航", "key": "D minor", "tempo": 96.0,
                     "meter": "4/4", "seed": 7},
            "section": [
                {"id": "verse", "name": "", "bars": 2, "energy": 0.4,
                 "progression": ["Dm", "Bb"], "label": ""},
                {"id": "chorus", "name": "", "bars": 2, "energy": 0.9,
                 "progression": ["Bb", "C"], "label": ""},
            ],
            "track": [
                {"id": "piano", "name": "钢琴", "role": "harmony", "instrument": "",
                 "bank": "gm", "program": 0, "octave": 4, "sections": ["verse"],
                 "mix": {"gain_db": -6.0, "pan": -0.2, "mute": False, "solo": False,
                         "reverb_send": 0.0, "chorus_send": 0.0},
                 "muted": False, "note": []},
                {"id": "bass", "name": "贝斯", "role": "bass", "instrument": "",
                 "bank": "gm", "program": 33, "octave": 2, "sections": [],
                 "muted": False, "note": []},
            ],
        }
        builder_spec = SongBuilder("夜航", key="D minor", tempo=96, seed=7)
        builder_spec.section("verse", bars=2, energy=0.4, progression="Dm-Bb")
        builder_spec.section("chorus", bars=2, energy=0.9, progression=["Bb", "C"])
        builder_spec.track("piano", name="钢琴", role="harmony", program=0,
                           sections=["verse"], mix={"gain_db": -6.0, "pan": -0.2})
        builder_spec.track("bass", name="贝斯", role="bass", program=33, octave=2)
        assert SongSpec.from_dict(builder_spec.to_dict()).to_dict() == \
            SongSpec.from_dict(data).to_dict()

    def test_to_dict_json_serializable(self) -> None:
        builder = _builder()
        payload = builder.to_dict()
        text = json.dumps(payload, ensure_ascii=False)
        assert "夜航" in text
        # 经 JSON 往返后再编译：与直接编译的规范形清单一致
        restored = SongSpec.from_dict(json.loads(text))
        assert restored.to_dict() == builder.to_spec().to_dict()


class TestOptions:
    def test_full_options(self) -> None:
        spec = (
            SongBuilder()
            .key("A minor")
            .tempo(140)
            .meter("3/4")
            .seed(99)
            .pickup(1.0)
            .tempo_change(0, 140)
            .tempo_change(24, 160, "linear")
            .section("a", bars=4)
            .track("t", range=(40, 90), density=0.7, groove="funk",
                   mix=MixStrip(gain_db=-3.0), channel=3, muted=True)
            .to_spec()
        )
        assert (spec.key, spec.tempo, spec.meter, spec.seed, spec.pickup_beats) == (
            "A minor", 140.0, "3/4", 99, 1.0,
        )
        assert spec.tempo_map == [(0.0, 140.0, "step"), (24.0, 160.0, "linear")]
        track = spec.tracks[0]
        assert (track.range, track.density, track.groove) == ((40, 90), 0.7, "funk")
        assert track.mix.gain_db == -3.0
        assert (track.channel, track.muted) == (3, True)

    def test_constructor_options(self) -> None:
        spec = SongBuilder(
            "夜航", key="D minor", tempo=96, meter="3/4", seed=11, pickup_beats=1.0
        ).section("a").to_spec()
        assert spec.pickup_beats == 1.0
        assert (spec.meter, spec.seed) == ("3/4", 11)
        assert spec.to_dict()["song"]["pickup_beats"] == 1.0

    def test_undotted_builder_defaults(self) -> None:
        spec = SongBuilder().section("a").track("t").to_spec()
        assert spec.title == "" and spec.sections[0].bars == 4
        assert spec.tracks[0].role == "harmony"


class TestErrors:
    def test_note_for_unknown_track(self) -> None:
        with pytest.raises(ParameterError, match="未声明轨道"):
            SongBuilder().note("ghost", beat=0, pitch=60, duration=1)

    def test_validate_reports_pointer(self) -> None:
        builder = SongBuilder().section("a", bars=2).track("t", program=200)
        with pytest.raises(SpecError) as excinfo:
            builder.validate()
        assert excinfo.value.path == "/track/0/program"
        assert excinfo.value.code == ARRANGEMENT_ERROR

    def test_build_rejects_empty(self) -> None:
        with pytest.raises(SpecError, match="至少需要一个段落"):
            SongBuilder().build()
