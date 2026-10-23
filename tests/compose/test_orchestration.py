"""配器与人性化（compose/orchestration.py, compose/humanize.py）单测。"""

from __future__ import annotations

import pytest

from sunoauxtool.compose.humanize import humanize, jitter_ticks, jitter_velocity
from sunoauxtool.compose.orchestration import (
    DOUBLING_ENERGY,
    ROLE_PROGRAMS,
    ROLE_RANGES,
    base_velocity,
    default_instrument,
    effective_range,
    roles_for_energy,
)
from sunoauxtool.compose.rng import rng_for
from sunoauxtool.compose.song import KNOWN_ROLES
from sunoauxtool.midi.document import NoteEvent


class TestRoleMatrix:
    def test_every_role_has_program_and_range(self) -> None:
        for role in KNOWN_ROLES:
            assert role in ROLE_PROGRAMS, role
            assert role in ROLE_RANGES, role

    @pytest.mark.parametrize(("energy", "expected"), [
        (0.0, ("pad",)),
        (0.29, ("pad",)),
        (0.30, ("harmony", "bass", "drums")),
        (0.59, ("harmony", "bass", "drums")),
        (0.60, ("harmony", "bass", "drums", "melody")),
        (0.84, ("harmony", "bass", "drums", "melody")),
        (0.85, ("harmony", "bass", "drums", "melody", "counter", "arpeggio")),
        (1.0, ("harmony", "bass", "drums", "melody", "counter", "arpeggio")),
    ])
    def test_roles_for_energy(self, energy: float, expected: tuple[str, ...]) -> None:
        assert roles_for_energy(energy) == expected

    def test_density_grows_with_energy(self) -> None:
        """能量越高配器越密（角色数量单调不减）。"""
        counts = [len(roles_for_energy(step / 100)) for step in range(0, 101, 5)]
        assert counts == sorted(counts)

    def test_pad_only_in_quietest_tier(self) -> None:
        """低能量用长音铺底；中高能量改用和弦块状 + 节奏组。"""
        assert "pad" in roles_for_energy(0.1)
        assert "pad" not in roles_for_energy(0.5)
        assert "harmony" not in roles_for_energy(0.1)

    def test_no_drums_below_third(self) -> None:
        assert "drums" not in roles_for_energy(0.29)

    def test_out_of_range_energy_clamped(self) -> None:
        assert roles_for_energy(-1.0) == ("pad",)
        assert roles_for_energy(2.0) == roles_for_energy(1.0)


class TestInstruments:
    def test_default_instrument(self) -> None:
        instrument = default_instrument("bass")
        assert instrument.resolved_program() == ROLE_PROGRAMS["bass"]
        assert instrument.preset == "bass"

    def test_unknown_role_falls_back(self) -> None:
        assert default_instrument("theremin").resolved_program() == ROLE_PROGRAMS["harmony"]

    def test_bank_preserved(self) -> None:
        assert default_instrument("pad", bank="mybank").bank == "mybank"
        assert default_instrument("pad", bank="").bank == "gm"


class TestRanges:
    def test_role_default(self) -> None:
        assert effective_range("bass") == ROLE_RANGES["bass"]

    def test_octave_shift(self) -> None:
        low, high = effective_range("harmony", octave=5)
        assert (low, high) == (ROLE_RANGES["harmony"][0] + 12, ROLE_RANGES["harmony"][1] + 12)

    def test_octave_clamped(self) -> None:
        low, high = effective_range("bass", octave=0)
        assert low >= 0 and high <= 127

    def test_drums_ignore_octave(self) -> None:
        assert effective_range("drums", octave=6) == ROLE_RANGES["drums"]

    def test_explicit_range_wins(self) -> None:
        assert effective_range("melody", octave=2, explicit=(70, 80)) == (70, 80)


class TestVelocity:
    def test_energy_scales_base(self) -> None:
        assert base_velocity("melody", 0.0) < base_velocity("melody", 1.0)

    def test_within_midi_range(self) -> None:
        for role in KNOWN_ROLES:
            for energy in (0.0, 0.5, 1.0):
                assert 1 <= base_velocity(role, energy) <= 127

    def test_doubling_threshold_is_high(self) -> None:
        assert 0.8 <= DOUBLING_ENERGY <= 1.0


class TestHumanize:
    def test_velocity_within_range(self) -> None:
        rng = rng_for(1, "a", "b", "humanize")
        for _ in range(50):
            assert 1 <= jitter_velocity(100, rng) <= 127

    def test_accent_raises_velocity(self) -> None:
        rng = rng_for(2, "a", "b", "h")
        assert jitter_velocity(80, rng, spread=0.0, accent=0.1) == 88
        assert jitter_velocity(80, rng, spread=0.0, accent=0.0) == 80

    def test_zero_spread_is_exact(self) -> None:
        rng = rng_for(3, "a", "b", "h")
        assert jitter_velocity(64, rng, spread=0.0) == 64

    def test_clamped_low(self) -> None:
        rng = rng_for(4, "a", "b", "h")
        assert jitter_velocity(2, rng, spread=1.0) >= 1

    def test_ticks_within_bounds(self) -> None:
        rng = rng_for(5, "a", "b", "h")
        for _ in range(50):
            value = jitter_ticks(rng, ppq=960, amount=0.015)
            assert -15 <= value <= 15

    def test_humanize_returns_new_list(self) -> None:
        notes = [NoteEvent(60, 0.0, 1.0, 90), NoteEvent(62, 1.0, 1.0, 90)]
        result = humanize(notes, rng_for(6, "a", "b", "h"), velocity_spread=0.1, timing=0.02)
        assert len(result) == 2
        assert result is not notes
        assert all(note.start == source.start for note, source in zip(result, notes))
        assert any(note.offset_ticks != 0 for note in result)

    def test_humanize_can_disable_timing(self) -> None:
        notes = [NoteEvent(60, 0.0, 1.0, 90)]
        result = humanize(notes, rng_for(7, "a", "b", "h"), timing=0.0, velocity_spread=0.0)
        assert result[0].offset_ticks == 0
        assert result[0].velocity == 90

    def test_humanize_deterministic(self) -> None:
        notes = [NoteEvent(60, 0.0, 1.0, 90)]
        first = humanize(notes, rng_for(8, "a", "b", "h"))
        second = humanize(notes, rng_for(8, "a", "b", "h"))
        assert (first[0].velocity, first[0].offset_ticks) == (
            second[0].velocity, second[0].offset_ticks,
        )
