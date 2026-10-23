"""律动与鼓组（2.0 PR-D）：groove 名 → 一小节鼓点图案 → 一个段落的鼓谱。

图案以"小节内偏移（拍）"描述，因此可以套到 4/4、3/4、6/8 等任意小节长度上
（``bar_length`` 与图案自带长度不同时按比例缩放）。鼓件用 GM 打击乐音高。

确定性：段落级随机（fill 变体）来自 :func:`compose.rng.rng_for`，绝不使用全局 random。
"""
from __future__ import annotations

from dataclasses import dataclass

from sunoauxtool.compose.rng import rng_for
from sunoauxtool.midi.document import NoteEvent

# --- GM 打击乐音高 ---------------------------------------------------------
KICK = 36
SNARE = 38
CLAP = 39
HAT = 42
HAT_OPEN = 46
TOM_HIGH = 48
TOM_MID = 47
TOM_LOW = 45
CRASH = 49
RIDE = 51


@dataclass(frozen=True)
class Hit:
    """一个小节内的一次鼓击（``offset`` 为小节内拍偏移）。"""

    offset: float
    pitch: int
    velocity: int = 90


@dataclass(frozen=True)
class Groove:
    """鼓组图案：名称 + 名义拍数（小节长度）+ 鼓击列表。"""

    name: str
    beats: float
    hits: tuple[Hit, ...]


def _eighths(beats: float, pitch: int, velocity: int) -> tuple[Hit, ...]:
    """0.5 拍网格的连续八分（用于踩镲铺底）。"""
    count = int(round(beats / 0.5))
    return tuple(Hit(index * 0.5, pitch, velocity) for index in range(count))


GROOVES: dict[str, Groove] = {
    "pop": Groove("pop", 4.0, (
        Hit(0.0, KICK, 104), Hit(2.0, KICK, 96),
        Hit(1.0, SNARE, 94), Hit(3.0, SNARE, 96),
        *_eighths(4.0, HAT, 62),
    )),
    "rock": Groove("rock", 4.0, (
        Hit(0.0, KICK, 108), Hit(2.5, KICK, 96),
        Hit(1.0, SNARE, 100), Hit(3.0, SNARE, 102),
        *_eighths(4.0, HAT, 70),
    )),
    "electronic": Groove("electronic", 4.0, (
        Hit(0.0, KICK, 110), Hit(1.0, KICK, 100),
        Hit(2.0, KICK, 106), Hit(3.0, KICK, 100),
        Hit(1.0, CLAP, 92), Hit(3.0, CLAP, 92),
        Hit(0.5, HAT, 66), Hit(1.5, HAT, 58), Hit(2.5, HAT, 66), Hit(3.5, HAT, 58),
    )),
    "half_time": Groove("half_time", 4.0, (
        Hit(0.0, KICK, 106), Hit(2.5, KICK, 88),
        Hit(2.0, SNARE, 104),
        *_eighths(4.0, HAT, 60),
    )),
    "funk": Groove("funk", 4.0, (
        Hit(0.0, KICK, 104), Hit(1.5, KICK, 88), Hit(2.75, KICK, 84),
        Hit(1.0, SNARE, 96), Hit(3.0, SNARE, 100),
        Hit(0.5, HAT, 64), Hit(1.5, HAT, 56), Hit(2.5, HAT, 64), Hit(3.5, HAT, 56),
        Hit(3.75, HAT, 50),
    )),
    "waltz": Groove("waltz", 3.0, (
        Hit(0.0, KICK, 102),
        Hit(1.0, SNARE, 88),
        Hit(2.0, HAT, 62),
    )),
    "ballad": Groove("ballad", 4.0, (
        Hit(0.0, KICK, 96), Hit(2.0, KICK, 84),
        Hit(1.0, SNARE, 80), Hit(3.0, SNARE, 82),
        Hit(1.5, HAT, 52), Hit(3.5, HAT, 52),
    )),
}

#: 段落能量 → 默认 groove（未显式指定 groove 时）
ENERGY_GROOVE = ((0.6, "ballad"), (0.85, "pop"), (1.01, "rock"))


def default_groove(energy: float, *, bar_length: float = 4.0) -> str:
    """按段落能量与拍号选默认 groove（3/4 一律用 waltz）。"""
    if abs(float(bar_length) - 3.0) < 1e-6:
        return "waltz"
    for threshold, name in ENERGY_GROOVE:
        if energy < threshold:
            return name
    return "rock"  # pragma: no cover - ENERGY_GROOVE 覆盖到 1.01


def _scale_hits(groove: Groove, bar_length: float) -> list[Hit]:
    """把图案缩放到目标小节长度（3/4 图案套 4/4 时按比例抻开）。"""
    if abs(groove.beats - bar_length) < 1e-9:
        return list(groove.hits)
    ratio = bar_length / groove.beats
    return [Hit(hit.offset * ratio, hit.pitch, hit.velocity) for hit in groove.hits]


def _fill(bar_length: float, rng) -> list[Hit]:
    """末小节 fill：军鼓十六分 + 通鼓下行（两选一）。"""
    if rng.random() < 0.5:
        return [
            Hit(bar_length - 1.0 + index * 0.25, SNARE, 70 + index * 8)
            for index in range(4)
        ]
    return [
        Hit(bar_length - 1.0, TOM_HIGH, 88),
        Hit(bar_length - 0.6667, TOM_MID, 84),
        Hit(bar_length - 0.3333, TOM_LOW, 90),
    ]


def bar_hits(
    groove_name: str,
    *,
    bar_length: float = 4.0,
    fill: bool = False,
    crash: bool = False,
    rng=None,
) -> list[Hit]:
    """一小节的鼓击列表（可加 crash 首拍与末拍 fill）。"""
    groove = GROOVES.get(groove_name) or GROOVES["pop"]
    hits = _scale_hits(groove, bar_length)
    if crash:
        hits = [Hit(0.0, CRASH, 100), *hits]
    if fill and rng is not None:
        hits = [hit for hit in hits if hit.offset < bar_length - 1.0] + _fill(bar_length, rng)
    return hits


def section_hits(
    groove_name: str,
    *,
    start: float,
    beats: float,
    bar_length: float = 4.0,
    seed: int = 0,
    section_id: str = "",
    track_id: str = "drums",
    fill_last: bool = True,
    crash_first: bool = True,
    energy: float = 0.5,
) -> list[NoteEvent]:
    """一个段落的完整鼓谱（绝对拍，音符时值取 0.25 拍）。

    Args:
        groove_name: :data:`GROOVES` 的键（未知名称回退 pop）。
        start: 段落起始绝对拍。
        beats: 段落总拍数。
        bar_length: 小节长度（拍）。
        seed / section_id / track_id: 确定性随机派生参数。
        fill_last: 末小节加 fill。
        crash_first: 首拍加吊镲。
        energy: 段落能量（决定力度缩放与 fill 概率）。
    """
    rng = rng_for(seed, section_id, track_id, "drums")
    bars = int(round(beats / bar_length)) if bar_length else 0
    events: list[NoteEvent] = []
    gain = 0.8 + 0.2 * max(0.0, min(1.0, energy))
    for bar in range(bars):
        is_last = bar == bars - 1
        do_fill = fill_last and is_last and bars >= 2 and rng.random() < 0.45 + 0.4 * energy
        hits = bar_hits(
            groove_name,
            bar_length=bar_length,
            fill=do_fill,
            crash=crash_first and bar == 0,
            rng=rng,
        )
        for hit in hits:
            velocity = max(1, min(127, int(round(hit.velocity * gain))))
            events.append(NoteEvent(
                pitch=hit.pitch,
                start=start + bar * bar_length + hit.offset,
                duration=0.25,
                velocity=velocity,
            ))
    events.sort(key=lambda note: (note.start, note.pitch))
    return events
