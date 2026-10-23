"""鼓组与律动（compose/rhythm.py）单测。"""

from __future__ import annotations

import pytest

from sunoauxtool.compose.rhythm import (
    CRASH,
    GROOVES,
    HAT,
    KICK,
    SNARE,
    TOM_LOW,
    bar_hits,
    default_groove,
    section_hits,
)
from sunoauxtool.compose.rng import rng_for


class TestGrooves:
    def test_known_grooves(self) -> None:
        assert set(GROOVES) == {
            "pop", "rock", "electronic", "half_time", "funk", "waltz", "ballad",
        }

    def test_each_groove_has_kick(self) -> None:
        for name, groove in GROOVES.items():
            assert any(hit.pitch == KICK for hit in groove.hits), name

    def test_waltz_is_three_beats(self) -> None:
        assert GROOVES["waltz"].beats == 3.0
        assert all(hit.offset < 3.0 for hit in GROOVES["waltz"].hits)

    def test_offsets_within_bar(self) -> None:
        for name, groove in GROOVES.items():
            assert all(0.0 <= hit.offset < groove.beats for hit in groove.hits), name

    def test_velocities_in_midi_range(self) -> None:
        for groove in GROOVES.values():
            assert all(1 <= hit.velocity <= 127 for hit in groove.hits)


class TestDefaultGroove:
    @pytest.mark.parametrize(("energy", "expected"), [
        (0.1, "ballad"), (0.5, "ballad"), (0.7, "pop"), (0.9, "rock"), (1.0, "rock"),
    ])
    def test_by_energy(self, energy: float, expected: str) -> None:
        assert default_groove(energy) == expected

    def test_three_four_is_waltz(self) -> None:
        assert default_groove(0.9, bar_length=3.0) == "waltz"


class TestBarHits:
    def test_pop_bar(self) -> None:
        hits = bar_hits("pop", bar_length=4.0)
        kicks = [hit.offset for hit in hits if hit.pitch == KICK]
        snares = [hit.offset for hit in hits if hit.pitch == SNARE]
        assert kicks == [0.0, 2.0]
        assert snares == [1.0, 3.0]
        assert sum(1 for hit in hits if hit.pitch == HAT) == 8

    def test_unknown_groove_falls_back(self) -> None:
        assert bar_hits("nope", bar_length=4.0) == bar_hits("pop", bar_length=4.0)

    def test_scaled_to_bar_length(self) -> None:
        hits = bar_hits("waltz", bar_length=6.0)
        assert max(hit.offset for hit in hits) < 6.0
        assert min(hit.offset for hit in hits) >= 0.0

    def test_crash_added_at_zero(self) -> None:
        hits = bar_hits("pop", bar_length=4.0, crash=True)
        assert hits[0].pitch == CRASH and hits[0].offset == 0.0

    def test_fill_replaces_last_beat(self) -> None:
        base = bar_hits("pop", bar_length=4.0)
        filled = bar_hits("pop", bar_length=4.0, fill=True, rng=rng_for(1, "a", "d", "f"))
        assert len(filled) != len(base)
        assert all(hit.offset < 4.0 for hit in filled)
        assert any(hit.pitch in (SNARE, TOM_LOW) and hit.offset >= 3.0 for hit in filled)

    def test_fill_requires_rng(self) -> None:
        assert bar_hits("pop", bar_length=4.0, fill=True) == bar_hits("pop", bar_length=4.0)


class TestSectionHits:
    def test_bar_count(self) -> None:
        events = section_hits("pop", start=0.0, beats=16.0, bar_length=4.0, seed=1,
                              section_id="v", energy=0.5, fill_last=False, crash_first=False)
        assert all(0.0 <= event.start < 16.0 for event in events)
        assert len(events) == 4 * len(bar_hits("pop", bar_length=4.0))

    def test_offsets_shift_by_start(self) -> None:
        events = section_hits("pop", start=32.0, beats=4.0, bar_length=4.0, seed=1,
                              section_id="v", energy=0.5, fill_last=False, crash_first=False)
        assert min(event.start for event in events) == 32.0
        assert max(event.start for event in events) < 36.0

    def test_deterministic(self) -> None:
        first = section_hits("pop", start=0.0, beats=32.0, seed=9, section_id="c", energy=0.9)
        second = section_hits("pop", start=0.0, beats=32.0, seed=9, section_id="c", energy=0.9)
        assert [(e.start, e.pitch, e.velocity) for e in first] == \
            [(e.start, e.pitch, e.velocity) for e in second]

    def test_energy_scales_velocity(self) -> None:
        quiet = section_hits("pop", start=0.0, beats=4.0, seed=1, section_id="a",
                             energy=0.0, fill_last=False, crash_first=False)
        loud = section_hits("pop", start=0.0, beats=4.0, seed=1, section_id="a",
                            energy=1.0, fill_last=False, crash_first=False)
        assert max(e.velocity for e in loud) > max(e.velocity for e in quiet)

    def test_notes_sorted_and_oriented(self) -> None:
        events = section_hits("funk", start=0.0, beats=8.0, seed=3, section_id="a", energy=0.8)
        starts = [(event.start, event.pitch) for event in events]
        assert starts == sorted(starts)
        assert all(event.duration > 0 for event in events)

    def test_zero_bars(self) -> None:
        assert section_hits("pop", start=0.0, beats=0.0, seed=1) == []
