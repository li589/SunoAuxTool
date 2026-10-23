"""人性化（2.0 PR-D）：力度抖动 + 微时序（tick 偏移）。

只改"手感"，不改结构：``NoteEvent.start`` 保持节拍网格值（渲染/总谱依赖它），
微时序写在 ``offset_ticks`` 上，由 SMF 写入器叠加到实际 tick。

确定性：随机来自传入的 ``rng``，同一 seed 结果一致。幅度刻意做小
（力度 ±6%、时序 ±15 ms 量级），避免破坏"机器可复现"的承诺。
"""
from __future__ import annotations

from typing import Iterable

from sunoauxtool.midi.document import NoteEvent


def jitter_velocity(
    base: int, rng, *, spread: float = 0.06, accent: float = 0.0
) -> int:
    """力度抖动：``base`` 的 ±spread 倍，另加段落重音 ``accent``。"""
    delta = base * spread
    value = base + rng.uniform(-delta, delta) + accent * base
    return max(1, min(127, int(round(value))))


def jitter_ticks(rng, *, ppq: int = 960, amount: float = 0.015) -> int:
    """微时序：正负若干个 tick（默认 ±15% 拍以内，实际取 ±1.5% 拍）。"""
    limit = max(1, int(round(ppq * amount)))
    return int(round(rng.uniform(-limit, limit)))


def humanize(
    notes: Iterable[NoteEvent],
    rng,
    *,
    ppq: int = 960,
    velocity_spread: float = 0.06,
    timing: float = 0.015,
    accent: float = 0.0,
) -> list[NoteEvent]:
    """返回人性化后的新音符列表（不修改入参）。

    Args:
        notes: 待处理音符。
        rng: 随机源（由 :func:`compose.rng.rng_for` 派生）。
        ppq: 每拍 tick 数（微时序的量纲）。
        velocity_spread: 力度抖动比例（0 = 不抖）。
        timing: 时序抖动比例（占一拍的比例，0 = 不抖）。
        accent: 整体重音（0.1 = 全体 +10%）。
    """
    result: list[NoteEvent] = []
    for note in notes:
        result.append(NoteEvent(
            pitch=note.pitch,
            start=note.start,
            duration=note.duration,
            velocity=jitter_velocity(note.velocity, rng, spread=velocity_spread, accent=accent),
            release_velocity=note.release_velocity,
            articulation=note.articulation,
            offset_ticks=note.offset_ticks + (
                jitter_ticks(rng, ppq=ppq, amount=timing) if timing else 0
            ),
        ))
    return result
