"""配器规则（2.0 PR-D）：角色 → 乐器 / 音域 / 力度 / 能量矩阵。

这里是"编曲常识"的唯一定义处：改这里就等于改默认风格，不必碰生成算法。

- :data:`ROLE_INSTRUMENTS`：角色默认 GM 音色（2.1 由 SoundBank 注册表覆盖）；
- :data:`ROLE_RANGES`：角色默认音域（超界折八度，见 :func:`fit_range`）；
- :func:`roles_for_energy`：段落能量 → 参与的编曲角色（配器密度）；
- :func:`effective_range`：叠加轨道的 ``octave`` 与 ``range`` 覆盖。
"""
from __future__ import annotations

from sunoauxtool.compose.song import InstrumentRef

#: 角色 → 默认 GM 音色号
ROLE_PROGRAMS: dict[str, int] = {
    "melody": 73,      # Flute
    "counter": 68,     # Oboe
    "harmony": 0,      # Acoustic Grand Piano
    "arpeggio": 46,    # Harp
    "pad": 89,         # Pad 2 (warm)
    "bass": 33,        # Electric Bass (finger)
    "drums": 0,        # 打击乐（按通道识别，音色号无意义）
    "percussion": 47,  # Timpani
}

#: 角色 → 默认音域（MIDI，含端点）
ROLE_RANGES: dict[str, tuple[int, int]] = {
    "melody": (60, 84),
    "counter": (55, 79),
    "harmony": (48, 72),
    "arpeggio": (55, 84),
    "pad": (48, 76),
    "bass": (28, 52),
    "drums": (35, 59),
    "percussion": (40, 60),
}

#: 角色 → 基准力度
ROLE_VELOCITY: dict[str, int] = {
    "melody": 88,
    "counter": 70,
    "harmony": 74,
    "arpeggio": 68,
    "pad": 58,
    "bass": 86,
    "drums": 96,
    "percussion": 80,
}

#: 能量 → 参与角色（升序阈值；低能量只留铺底，高能量加对位与琶音）
ENERGY_TIERS: tuple[tuple[float, tuple[str, ...]], ...] = (
    (0.30, ("pad",)),
    (0.60, ("harmony", "bass", "drums")),
    (0.85, ("harmony", "bass", "drums", "melody")),
    (1.01, ("harmony", "bass", "drums", "melody", "counter", "arpeggio")),
)

#: 双奏（同度加倍）触发能量
DOUBLING_ENERGY = 0.85


def roles_for_energy(energy: float) -> tuple[str, ...]:
    """段落能量 → 应参与的角色（按 :data:`ENERGY_TIERS` 取第一档）。"""
    value = max(0.0, min(1.0, float(energy)))
    for threshold, roles in ENERGY_TIERS:
        if value < threshold:
            return roles
    return ENERGY_TIERS[-1][1]  # pragma: no cover - 末档阈值 1.01


def default_instrument(role: str, *, bank: str = "gm") -> InstrumentRef:
    """角色默认音色（未知角色回退 harmony 的音色；保留调用方的音源库）。"""
    program = ROLE_PROGRAMS.get(role, ROLE_PROGRAMS["harmony"])
    return InstrumentRef(bank=bank or "gm", preset=role, program=program)


def effective_range(
    role: str, *, octave: int | None = None, explicit: tuple[int, int] | None = None
) -> tuple[int, int]:
    """轨道实际音域：默认音域 → 叠加 ``octave`` 位移 → ``range`` 覆盖。"""
    if explicit is not None:
        return (int(explicit[0]), int(explicit[1]))
    low, high = ROLE_RANGES.get(role, ROLE_RANGES["harmony"])
    if octave is not None and role != "drums":
        shift = (int(octave) - 4) * 12
        return (max(0, low + shift), min(127, high + shift))
    return (low, high)


def base_velocity(role: str, energy: float = 0.5) -> int:
    """角色基准力度，按段落能量缩放（0.8–1.0 倍）。"""
    value = ROLE_VELOCITY.get(role, 74)
    gain = 0.8 + 0.2 * max(0.0, min(1.0, float(energy)))
    return max(1, min(127, int(round(value * gain))))
