"""表现力（compose/expression.py）单测：演奏法 / 力度 / 摇摆 + 清单校验。

三条验收各自的可观测点：

- 演奏法 → ``NoteEvent.duration``（gate 缩放）与 ``velocity``（偏移 + 标签）；
- 力度 → 整组力度的缩放系数，``cresc``/``dim`` 在组内线性插值；
- 摇摆 → 只有反拍的 ``offset_ticks`` 变化，``start`` 保持网格值。
"""

from __future__ import annotations

import copy
import re

import pytest
from typer.testing import CliRunner

from sunoauxtool.compose.arranger import ArrangeOptions, arrange
from sunoauxtool.compose.cli import app as _app
from sunoauxtool.compose.expression import (
    ARTICULATION_NAMES,
    ARTICULATIONS,
    DYNAMIC_ORDER,
    DYNAMICS,
    MAX_SWING,
    RAMPS,
    apply_articulation,
    apply_articulations,
    apply_dynamics,
    apply_swing,
    articulation_or_none,
    dynamic_for_energy,
    swing_offset_ticks,
)
from sunoauxtool.compose.spec import ARRANGEMENT_ERROR, SongSpec, SpecError
from sunoauxtool.midi.document import NoteEvent

#: rich 在 CI 上会把选项名拆成多段上色，断言 help 文本前必须先剥 ANSI
_ANSI_RE = re.compile(chr(27) + r"\[[0-9;]*m")
_runner = CliRunner()


def _note(pitch: int = 60, start: float = 0.0, duration: float = 1.0,
          velocity: int = 80, articulation: str = "") -> NoteEvent:
    return NoteEvent(
        pitch=pitch, start=start, duration=duration, velocity=velocity,
        articulation=articulation,
    )


class TestArticulationTable:
    def test_names_match_table(self) -> None:
        assert set(ARTICULATION_NAMES) == set(ARTICULATIONS)

    @pytest.mark.parametrize("name", sorted(ARTICULATIONS))
    def test_gate_is_sane(self, name: str) -> None:
        spec = ARTICULATIONS[name]
        assert 0.0 < spec.gate <= 1.0
        assert spec.name == name

    def test_staccato_is_half(self) -> None:
        assert ARTICULATIONS["staccato"].gate == 0.5

    def test_lookup_is_case_insensitive(self) -> None:
        assert articulation_or_none(" Staccato ") is ARTICULATIONS["staccato"]

    def test_unknown_returns_none(self) -> None:
        assert articulation_or_none("flautando") is None
        assert articulation_or_none("") is None


class TestApplyArticulation:
    def test_gate_scales_duration(self) -> None:
        result = apply_articulation([_note(duration=2.0)], "staccato")
        assert result[0].duration == pytest.approx(1.0)

    def test_velocity_delta_applied(self) -> None:
        result = apply_articulation([_note(velocity=80)], "accent")
        assert result[0].velocity == 80 + ARTICULATIONS["accent"].velocity_delta

    def test_velocity_clamped(self) -> None:
        result = apply_articulation([_note(velocity=127)], "marcato")
        assert result[0].velocity == 127

    def test_tag_written(self) -> None:
        result = apply_articulation([_note()], "tenuto")
        assert result[0].articulation == "tenuto"

    def test_min_duration_floor(self) -> None:
        result = apply_articulation([_note(duration=0.06)], "staccatissimo")
        assert result[0].duration == pytest.approx(0.05)

    def test_unknown_leaves_notes_alone(self) -> None:
        notes = [_note()]
        assert apply_articulation(notes, "nope") == notes

    def test_input_not_mutated(self) -> None:
        notes = [_note(duration=2.0)]
        apply_articulation(notes, "staccato")
        assert notes[0].duration == 2.0
        assert notes[0].articulation == ""

    def test_per_note_beats_default(self) -> None:
        notes = [_note(articulation="accent"), _note(start=1.0)]
        result = apply_articulations(notes, "staccato")
        assert result[0].articulation == "accent"
        assert result[1].articulation == "staccato"

    def test_none_default_is_passthrough(self) -> None:
        notes = [_note()]
        assert apply_articulations(notes, None)[0] is notes[0]


class TestDynamics:
    def test_energy_is_monotonic(self) -> None:
        levels = [DYNAMICS[dynamic_for_energy(e / 10.0)] for e in range(11)]
        assert levels == sorted(levels)

    def test_energy_extremes(self) -> None:
        assert dynamic_for_energy(0.0) == "pp"
        assert dynamic_for_energy(1.0) == "ff"

    def test_flat_dynamic_scales_velocity(self) -> None:
        result = apply_dynamics([_note(velocity=100)], dynamic="p")
        assert result[0].velocity == round(100 * DYNAMICS["p"])

    def test_empty_notes(self) -> None:
        assert apply_dynamics([], dynamic="f") == []

    def test_crescendo_is_increasing(self) -> None:
        notes = [_note(start=float(i), velocity=100) for i in range(4)]
        result = apply_dynamics(notes, dynamic="p", ramp="cresc")
        velocities = [n.velocity for n in result]
        assert velocities == sorted(velocities)
        assert velocities[-1] > velocities[0]

    def test_diminuendo_is_decreasing(self) -> None:
        notes = [_note(start=float(i), velocity=100) for i in range(4)]
        result = apply_dynamics(notes, dynamic="f", ramp="dim")
        velocities = [n.velocity for n in result]
        assert velocities == sorted(velocities, reverse=True)

    def test_explicit_target(self) -> None:
        notes = [_note(start=0.0), _note(start=9.0)]
        result = apply_dynamics(notes, dynamic="pp", ramp="cresc", ramp_to="fff")
        assert result[0].velocity == round(80 * DYNAMICS["pp"])
        assert result[1].velocity == round(80 * DYNAMICS["fff"])

    def test_ramp_steps_one_level(self) -> None:
        index = DYNAMIC_ORDER.index("mf")
        notes = [_note(start=0.0), _note(start=1.0)]
        result = apply_dynamics(notes, dynamic="mf", ramp="cresc")
        assert result[1].velocity == round(80 * DYNAMICS[DYNAMIC_ORDER[index + 1]])


class TestSwing:
    def test_ratio_half_is_straight(self) -> None:
        assert swing_offset_ticks(0.5, 0.5) == 0
        assert swing_offset_ticks(1.0, 0.5) == 0

    def test_on_beat_never_shifts(self) -> None:
        assert swing_offset_ticks(1.0, 0.66) == 0
        assert swing_offset_ticks(2.0, 0.66) == 0

    def test_off_beat_shifts(self) -> None:
        # 0.5 拍的第二个八分位：0.66 - 0.5 = 0.16 拍 → 0.16 * 960 ≈ 154 tick
        assert swing_offset_ticks(0.5, 0.66) == round((0.66 - 0.5) * 1.0 * 960)

    def test_scales_with_ppq(self) -> None:
        value = swing_offset_ticks(0.5, 0.66, ppq=480)
        assert value == round((0.66 - 0.5) * 1.0 * 480)

    def test_apply_swing_keeps_start(self) -> None:
        notes = [_note(start=0.0), _note(start=0.5)]
        result = apply_swing(notes, 0.66)
        assert [n.start for n in result] == [0.0, 0.5]
        assert result[0].offset_ticks == 0
        assert result[1].offset_ticks > 0

    def test_ratio_clamped_to_max(self) -> None:
        notes = [_note(start=0.5)]
        result = apply_swing(notes, 5.0)
        assert result[0].offset_ticks == round((MAX_SWING - 0.5) * 960)

    def test_straight_returns_copy(self) -> None:
        notes = [_note()]
        result = apply_swing(notes, 0.5)
        assert result == notes and result is not notes


def _spec() -> dict:
    return {
        "schema": 1,
        "song": {"title": "夜航", "key": "D minor", "tempo": 96, "seed": 7},
        "section": [
            {"id": "verse", "bars": 2, "energy": 0.4, "progression": ["Dm", "Bb"]},
            {"id": "chorus", "bars": 2, "energy": 0.9, "progression": ["Bb", "C"]},
        ],
        "track": [
            {"id": "piano", "role": "harmony", "sections": ["verse", "chorus"],
             "note": [{"beat": 0.0, "pitch": 62, "duration": 2.0}]},
        ],
    }


def _load(data: dict) -> SongSpec:
    spec = SongSpec.from_dict(copy.deepcopy(data))
    spec.validate()
    return spec


class TestSpecExpression:
    def test_defaults_are_off(self) -> None:
        spec = _load(_spec())
        assert spec.sections[0].dynamic == ""
        assert spec.sections[0].ramp == ""
        assert spec.tracks[0].articulation == ""
        assert spec.tracks[0].swing == 0.0

    def test_fields_flow_into_song(self) -> None:
        data = _spec()
        data["song"]["swing"] = 0.6
        data["section"][0]["dynamic"] = "p"
        data["section"][0]["ramp"] = "cresc"
        data["track"][0]["articulation"] = "staccato"
        song = _load(data).to_song()
        assert song.swing == pytest.approx(0.6)
        assert song.sections[0].dynamic == "p"
        assert song.sections[0].ramp == "cresc"
        assert song.tracks[0].articulation == "staccato"

    def test_round_trip_keeps_fields(self) -> None:
        data = _spec()
        data["section"][1]["dynamic"] = "ff"
        data["section"][1]["ramp"] = "dim"
        data["track"][0]["swing"] = 0.62
        spec = _load(data)
        again = _load(spec.to_dict())
        assert again.sections[1].dynamic == "ff"
        assert again.sections[1].ramp == "dim"
        assert again.tracks[0].swing == pytest.approx(0.62)

    @pytest.mark.parametrize(
        "field,value,path,match",
        [
            ("dynamic", "loud", "/section/0/dynamic", "未知力度记号"),
            ("ramp", "up", "/section/0/ramp", "未知渐变"),
        ],
    )
    def test_bad_section_value(self, field: str, value: str, path: str,
                              match: str) -> None:
        data = _spec()
        data["section"][0][field] = value
        with pytest.raises(SpecError) as excinfo:
            _load(data)
        assert excinfo.value.path == path
        assert excinfo.value.code == ARRANGEMENT_ERROR
        assert match in str(excinfo.value)

    def test_bad_articulation(self) -> None:
        data = _spec()
        data["track"][0]["articulation"] = "flautando"
        with pytest.raises(SpecError) as excinfo:
            _load(data)
        assert excinfo.value.path == "/track/0/articulation"
        assert "未知演奏法" in str(excinfo.value)

    @pytest.mark.parametrize("swing", [-0.1, 0.9])
    def test_bad_track_swing(self, swing: float) -> None:
        data = _spec()
        data["track"][0]["swing"] = swing
        with pytest.raises(SpecError) as excinfo:
            _load(data)
        assert excinfo.value.path == "/track/0/swing"

    def test_bad_song_swing(self) -> None:
        data = _spec()
        data["song"]["swing"] = 1.5
        with pytest.raises(SpecError) as excinfo:
            _load(data)
        assert excinfo.value.path == "/song/swing"

    def test_unknown_key_rejected(self) -> None:
        data = _spec()
        data["track"][0]["swinging"] = True
        with pytest.raises(SpecError) as excinfo:
            _load(data)
        assert "swinging" in str(excinfo.value)

    def test_ramps_table(self) -> None:
        assert RAMPS == ("cresc", "dim")


def _expression_spec(section: dict | None = None, **track_fields) -> dict:
    """一小节、四个八分音符的清单（拍点 0/0.5/1/1.5，正好覆盖摇摆的强弱拍）。"""
    track = {
        "id": "lead", "role": "melody", "sections": ["a"],
        "note": [
            {"beat": beat, "pitch": 72, "duration": 0.5}
            for beat in (0.0, 0.5, 1.0, 1.5)
        ],
    }
    track.update(track_fields)
    data = {
        "schema": 1,
        "song": {"title": "摇摆测试", "key": "C major", "tempo": 120, "seed": 5},
        "section": [{"id": "a", "bars": 1, "energy": 0.5, "progression": ["C"]}],
        "track": [track],
    }
    if section:
        data["section"][0].update(section)
    return data


class TestArrangerExpression:
    """编排链路：摇摆 → 演奏法 → 力度 → 人性化（顺序变了结果就不同）。"""

    def _notes(self, section: dict | None = None, **track_fields) -> list[NoteEvent]:
        spec = _load(_expression_spec(section, **track_fields))
        song = arrange(spec, ArrangeOptions(humanize=False))
        return list(song.tracks[0].clips[0].notes)

    def test_staccato_halves_duration(self) -> None:
        plain = self._notes()[0].duration
        short = self._notes(articulation="staccato")[0].duration
        assert short == pytest.approx(plain * 0.5)

    def test_accent_raises_velocity(self) -> None:
        # 偏移作用在力度记号缩放**之前**（重音是相对当前力度层的强调），
        # 因此最终力度不等于"原值 + delta"
        plain = self._notes()[0].velocity
        loud = self._notes(articulation="accent")[0].velocity
        assert loud > plain

    def test_dynamic_scales_velocity(self) -> None:
        soft = self._notes(section={"dynamic": "p"})[0].velocity
        loud = self._notes(section={"dynamic": "ff"})[0].velocity
        assert loud > soft
        ratio = loud / soft
        assert ratio == pytest.approx(DYNAMICS["ff"] / DYNAMICS["p"], rel=0.02)

    def test_crescendo_ramps_inside_section(self) -> None:
        notes = self._notes(section={"dynamic": "p", "ramp": "cresc"})
        velocities = [n.velocity for n in notes]
        assert velocities == sorted(velocities)
        assert velocities[-1] > velocities[0]

    def test_swing_only_touches_offbeats(self) -> None:
        notes = self._notes(swing=0.66)
        assert [n.start for n in notes] == [0.0, 0.5, 1.0, 1.5]
        assert notes[0].offset_ticks == 0 and notes[2].offset_ticks == 0
        assert notes[1].offset_ticks > 0 and notes[3].offset_ticks > 0
        # 0.66 → 每对八分后半延后 0.16 拍 = 154 tick（PPQ 960）
        assert notes[1].offset_ticks == round((0.66 - 0.5) * 960)

    def test_song_level_swing_applies(self) -> None:
        data = _expression_spec()
        data["song"]["swing"] = 0.6
        song = arrange(_load(data), ArrangeOptions(humanize=False))
        offsets = [n.offset_ticks for n in song.tracks[0].clips[0].notes]
        assert offsets[1] > 0

    def test_cli_swing_overrides_manifest(self) -> None:
        data = _expression_spec(swing=0.55)
        song = arrange(
            _load(data), ArrangeOptions(humanize=False, swing=0.7)
        )
        offsets = [n.offset_ticks for n in song.tracks[0].clips[0].notes]
        assert offsets[1] == round((0.7 - 0.5) * 960)

    def test_humanize_offsets_stay_small(self) -> None:
        spec = _load(_expression_spec())
        song = arrange(spec, ArrangeOptions(timing_ms=10.0, velocity_jitter=0, ppq=960))
        notes = list(song.tracks[0].clips[0].notes)
        # ±10ms @120BPM = ±19 tick
        assert all(abs(n.offset_ticks) <= 19 for n in notes)

    def test_humanize_off_is_exact(self) -> None:
        notes = self._notes()
        assert all(n.offset_ticks == 0 for n in notes)


class TestCliOptions:
    def test_help_mentions_expression_flags(self) -> None:
        # Linux CI 下 rich 会给 `-`/`-swing` 分别上色，断言前必须先剥 ANSI
        plain = _ANSI_RE.sub("", _runner.invoke(_app, ["build", "--help"]).output)
        for flag in ("--swing", "--timing-ms", "--velocity-jitter", "--no-humanize"):
            assert flag in plain, flag

    def test_build_accepts_swing(self, tmp_path) -> None:
        made = _runner.invoke(_app, ["init", "demo", "-o", str(tmp_path)])
        assert made.exit_code == 0, made.output
        toml = tmp_path / "song.toml"
        result = _runner.invoke(
            _app, ["build", str(toml), "-o", str(tmp_path / "o"), "--dry-run",
                   "--swing", "0.7", "--timing-ms", "6", "--velocity-jitter", "4"],
        )
        assert result.exit_code == 0, result.output


