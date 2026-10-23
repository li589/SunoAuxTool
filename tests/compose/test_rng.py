"""确定性种子派生（compose/rng.py）单测。"""

from __future__ import annotations

from sunoauxtool.compose.rng import seed_for, rng_for


def test_same_inputs_same_seed() -> None:
    assert seed_for(42, "verse", "piano", "drums") == seed_for(42, "verse", "piano", "drums")


def test_seed_is_64_bit() -> None:
    assert 0 <= seed_for(1) < 2**64


def test_parts_change_seed() -> None:
    base = seed_for(42, "verse", "piano", "melody")
    assert base != seed_for(43, "verse", "piano", "melody")
    assert base != seed_for(42, "chorus", "piano", "melody")
    assert base != seed_for(42, "verse", "bass", "melody")
    assert base != seed_for(42, "verse", "piano", "harmony")


def test_parts_order_matters() -> None:
    assert seed_for(1, "a", "b") != seed_for(1, "b", "a")


def test_defaults_are_empty_parts() -> None:
    assert seed_for(7) == seed_for(7, "", "", "")


def test_rng_is_independent_instance() -> None:
    first = rng_for(5, "a", "b", "c")
    second = rng_for(5, "a", "b", "c")
    assert [first.random() for _ in range(3)] == [second.random() for _ in range(3)]


def test_rng_differs_across_parts() -> None:
    left = rng_for(5, "a", "b", "c").random()
    right = rng_for(5, "a", "d", "c").random()
    assert left != right


def test_int_seed_normalized() -> None:
    """字符串型 seed 与整型一致（清单可能来自 JSON）。"""
    assert seed_for("42") == seed_for(42)  # type: ignore[arg-type]
