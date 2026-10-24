"""表现力（2.2 PR-M3）：演奏法、力度记号、摇摆。

三者的落地位置刻意分开：

- **演奏法**：改音符的**时值/力度**（`gate` × duration、`velocity` + Δ），
  同时留下 ``articulation`` 标签给谱面（MusicXML ``<articulations>``）用；
- **力度**：段落级 `dynamic`（pp..ff）缩放力度，`cresc`/`dim` 在段落内线性渐变；
- **摇摆**：只改 ``offset_ticks``（不动 `start`），因此节拍网格、和声、谱面都不受影响，
  但 MIDI 听感是摇摆的——这是"网格可读 + 演奏人性化"的折中。

pizz/arco 这类**换音色**的技法需要 keyswitch/换 program，SF2 并不通用，
因此这里只做"时值/力度"层面的近似，并在文档里注明。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from sunoauxtool.midi.document import NoteEvent


@dataclass(frozen=True)
class Articulation:
    """一种演奏法的机读语义。"""

    name: str
    gate: float = 1.0
    """时值倍率（0.3 = 断奏到三成）。"""

    velocity_delta: int = 0
    """力度偏移（写到 NoteEvent.velocity）。"""

    musicxml: str = ""
    """MusicXML ``<articulations>`` 里的元素名（空 = 不写记号）。"""

    def describe(self) -> str:
        return f"{self.name}: gate×{self.gate:g} vel{self.velocity_delta:+d}"


#: 支持的演奏法（键为清单里的写法）
ARTICULATIONS: dict[str, Articulation] = {
    "legato": Articulation("legato", gate=1.0, velocity_delta=0),
    "tenuto": Articulation("tenuto", gate=1.0, velocity_delta=2, musicxml="tenuto"),
    "staccato": Articulation("staccato", gate=0.5, velocity_delta=0, musicxml="staccato"),
    "staccatissimo": Articulation(
        "staccatissimo", gate=0.3, velocity_delta=2, musicxml="staccatissimo"
    ),
    "accent": Articulation("accent", gate=1.0, velocity_delta=14, musicxml="accent"),
    "marcato": Articulation("marcato", gate=0.7, velocity_delta=18, musicxml="strong-accent"),
    "spiccato": Articulation("spiccato", gate=0.35, velocity_delta=6, musicxml="spiccato"),
    # 弦乐技法：只做时值/力度近似（换音色需 keyswitch，SF2 不通用）
    "pizz": Articulation("pizz", gate=0.55, velocity_delta=6, musicxml="staccato"),
    "arco": Articulation("arco", gate=1.0, velocity_delta=0),
}


#: 合法演奏法名（校验用）
ARTICULATION_NAMES = tuple(ARTICULATIONS)

#: 合法的段内渐变
RAMPS = ("cresc", "dim")


def articulation_or_none(name: str | None) -> Articulation | None:
    """按名字取演奏法（未知/空返回 None，调用方决定是否报错）。"""
    if not name:
        return None
    return ARTICULATIONS.get(str(name).strip().lower())


def apply_articulation(
    notes: Sequence[NoteEvent], articulation: str | None, *, min_duration: float = 0.05
) -> list[NoteEvent]:
    """给一组音符施加同一种演奏法（返回新列表，不改入参）。"""
    spec = articulation_or_none(articulation)
    if spec is None:
        return list(notes)
    result: list[NoteEvent] = []
    for note in notes:
        duration = note.duration * spec.gate
        duration = max(min_duration, duration)
        velocity = max(1, min(127, int(note.velocity) + spec.velocity_delta))
        result.append(NoteEvent(
            pitch=note.pitch,
            start=note.start,
            duration=min(duration, note.duration),
            velocity=velocity,
            release_velocity=note.release_velocity,
            articulation=spec.name,
            offset_ticks=note.offset_ticks,
        ))
    return result


def apply_articulations(
    notes: Sequence[NoteEvent], default: str | None = None, *, min_duration: float = 0.05
) -> list[NoteEvent]:
    """逐音符施加演奏法：音符自带 ``articulation`` 优先，否则用轨道默认。"""
    result: list[NoteEvent] = []
    for note in notes:
        name = note.articulation or default
        spec = articulation_or_none(name)
        if spec is None:
            result.append(note)
            continue
        duration = max(min_duration, note.duration * spec.gate)
        result.append(NoteEvent(
            pitch=note.pitch,
            start=note.start,
            duration=min(duration, note.duration),
            velocity=max(1, min(127, int(note.velocity) + spec.velocity_delta)),
            release_velocity=note.release_velocity,
            articulation=spec.name,
            offset_ticks=note.offset_ticks,
        ))
    return result


# ---------------------------------------------------------------------------
# 力度
# ---------------------------------------------------------------------------

#: 力度记号 → 力度倍率（相对轨道基准力度）
DYNAMICS: dict[str, float] = {
    "ppp": 0.34, "pp": 0.42, "p": 0.55, "mp": 0.68,
    "mf": 0.80, "f": 0.92, "ff": 1.05, "fff": 1.15,
}

#: 力度记号从弱到强（渐变插值用）
DYNAMIC_ORDER: tuple[str, ...] = ("ppp", "pp", "p", "mp", "mf", "f", "ff", "fff")


def dynamic_for_energy(energy: float) -> str:
    """段落能量 → 默认力度记号（未显式指定 ``dynamic`` 时）。"""
    value = max(0.0, min(1.0, float(energy)))
    if value < 0.20:
        return "pp"
    if value < 0.35:
        return "p"
    if value < 0.50:
        return "mp"
    if value < 0.70:
        return "mf"
    if value < 0.85:
        return "f"
    return "ff"


def _level(name: str) -> float:
    return DYNAMICS.get(str(name).strip().lower(), DYNAMICS["mf"])


def apply_dynamics(
    notes: Sequence[NoteEvent],
    *,
    dynamic: str = "mf",
    ramp: str = "",
    ramp_to: str | None = None,
) -> list[NoteEvent]:
    """按力度记号缩放力度；``cresc``/``dim`` 在整组内线性渐变。

    Args:
        notes: 同一段落（片段）内的音符，按 ``start`` 升序最佳。
        dynamic: 起始力度记号（pp..fff）。
        ramp: ``""``（不变）/ ``"cresc"`` / ``"dim"``。
        ramp_to: 渐变终点力度；None 时按方向推一档。

    Returns:
        新的音符列表（力度被缩放，其它字段不变）。
    """
    if not notes:
        return []
    start_level = _level(dynamic)
    target = ramp_to
    if ramp and target is None:
        index = DYNAMIC_ORDER.index(dynamic) if dynamic in DYNAMIC_ORDER else 3
        step = 1 if ramp == "cresc" else -1
        index = max(0, min(len(DYNAMIC_ORDER) - 1, index + step))
        target = DYNAMIC_ORDER[index]
    end_level = _level(target) if ramp and target else start_level

    ordered = sorted(notes, key=lambda note: note.start)
    first, last = ordered[0].start, ordered[-1].start
    span = max(last - first, 1e-9)
    result: list[NoteEvent] = []
    for note in notes:
        if end_level == start_level:
            factor = start_level
        else:
            ratio = (note.start - first) / span
            factor = start_level + (end_level - start_level) * max(0.0, min(1.0, ratio))
        result.append(NoteEvent(
            pitch=note.pitch,
            start=note.start,
            duration=note.duration,
            velocity=max(1, min(127, int(round(note.velocity * factor)))),
            release_velocity=note.release_velocity,
            articulation=note.articulation,
            offset_ticks=note.offset_ticks,
        ))
    return result


# ---------------------------------------------------------------------------
# 摇摆
# ---------------------------------------------------------------------------

#: 摇摆比例上限（0.75 已是三连音极限）
MAX_SWING = 0.75


def swing_offset_ticks(
    start: float, ratio: float, *, ppq: int = 960, grid: float = 0.5
) -> int:
    """处于反拍的音符应额外延后的 tick 数（其余音符为 0）。

    摇摆把"每对八分"里的**第二个**往后推：``ratio=0.5`` 是平直，``0.66`` 接近三连音。
    ``grid`` 是前半拍的时值（默认八分 = 0.5 拍）。
    """
    if not ratio or abs(ratio - 0.5) < 1e-9:
        return 0
    pair = grid * 2.0
    position = start % pair
    if abs(position - grid) > 1e-6:
        return 0
    delay = (float(ratio) - 0.5) * pair
    return int(round(delay * ppq))


def apply_swing(
    notes: Sequence[NoteEvent], ratio: float, *, ppq: int = 960, grid: float = 0.5
) -> list[NoteEvent]:
    """给反拍音符加摇摆偏移（写 ``offset_ticks``，不动网格起点）。"""
    if not ratio or abs(ratio - 0.5) < 1e-9:
        return list(notes)
    clamped = max(0.5, min(MAX_SWING, float(ratio)))
    result: list[NoteEvent] = []
    for note in notes:
        delta = swing_offset_ticks(note.start, clamped, ppq=ppq, grid=grid)
        result.append(NoteEvent(
            pitch=note.pitch,
            start=note.start,
            duration=note.duration,
            velocity=note.velocity,
            release_velocity=note.release_velocity,
            articulation=note.articulation,
            offset_ticks=note.offset_ticks + delta,
        ))
    return result
