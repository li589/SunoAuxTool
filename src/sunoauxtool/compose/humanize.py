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
    base: int, rng, *, jitter: int = 8, spread: float | None = None
) -> int:
    """力度抖动：默认 ±8（绝对 MIDI 力度），也兼容旧的相对比例写法。"""
    if spread:
        limit = abs(base * spread)
    else:
        limit = abs(int(jitter))
    if limit <= 0:
        return max(1, min(127, int(base)))
    return max(1, min(127, int(round(base + rng.uniform(-limit, limit)))))


def jitter_ticks(
    rng, *, ppq: int = 960, ms: float = 10.0, bpm: float = 120.0
) -> int:
    """微时序：以**毫秒**为单位（默认 ±10ms），换算成当前速度下的 tick。

    用毫秒而不是"拍的比例"是因为 M3 的听感指标是时间量：同样 ±10ms，慢歌里
    占比更大、快歌里更小，正是演奏者手上的感觉。
    """
    if ms <= 0:
        return 0
    ticks_per_second = ppq * (float(bpm) / 60.0)
    limit = max(1, int(round(ticks_per_second * (float(ms) / 1000.0))))
    return int(round(rng.uniform(-limit, limit)))


def humanize(
    notes: Iterable[NoteEvent],
    rng,
    *,
    ppq: int = 960,
    velocity_jitter: int = 8,
    timing_ms: float = 10.0,
    bpm: float = 120.0,
) -> list[NoteEvent]:
    """返回人性化后的新音符列表（不修改入参）。

    Args:
        notes: 待处理音符。
        rng: 随机源（由 :func:`compose.rng.rng_for` 派生）。
        ppq: 每拍 tick 数（微时序的量纲）。
        velocity_jitter: 力度抖动幅度（±MIDI 力度，0 = 不抖）。
        timing_ms: 时序抖动幅度（±毫秒，0 = 不抖）。
        bpm: 当前速度（毫秒 → tick 换算）。
    """
    result: list[NoteEvent] = []
    for note in notes:
        result.append(NoteEvent(
            pitch=note.pitch,
            start=note.start,
            duration=note.duration,
            velocity=jitter_velocity(note.velocity, rng, jitter=velocity_jitter),
            release_velocity=note.release_velocity,
            articulation=note.articulation,
            offset_ticks=note.offset_ticks + jitter_ticks(
                rng, ppq=ppq, ms=timing_ms, bpm=bpm
            ),
        ))
    return result
