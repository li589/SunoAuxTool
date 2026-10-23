"""MIDI 领域模型 v2：全保真、可校验的编曲数据交换层。

与旧 ``models/midi.py`` 的关系（PR-A 原则：旧路径零改动）：

- 旧 ``MidiDocument`` 基于 pretty_midi，写入时只保留单一速度、不写 CC / 音色库选择，
  通道由 pretty_midi 顺序分配——足够"出个能听的 MIDI"，但不足以承载多轨编曲。
- 本模块是 2.0 的交换层：速度/拍号/调性变化表、每轨通道/音色库/音量/声像/CC、
  未知事件原样保留，均由 ``midi.io`` 用 mido 读写 SMF format 1。

时间约定：所有 ``beat`` 均为"拍"（float，四分音符 = 1 拍），与 ``models.notes.Note``
一致；秒换算只能通过 ``beat_to_seconds`` 按速度表积分，不得再用全局 60/bpm。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from sunoauxtool.exceptions import ParameterError

#: 鼓轨固定通道（GM 约定）
DRUM_CHANNEL = 9


def _require_finite(value: float, name: str) -> float:
    """校验数值有限，否则抛参数错误。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ParameterError(f"{name} 必须是数值: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ParameterError(f"{name} 必须是有限数值: {value!r}")
    return number


@dataclass(frozen=True)
class TempoPoint:
    """速度变化点。

    Attributes:
        beat: 生效起点（拍，全曲累计）。
        bpm: 每分钟拍数（允许小数）。
        curve: 到下一个速度点的过渡方式：``step`` 阶跃 / ``linear`` 线性渐变。
    """

    beat: float
    bpm: float
    curve: str = "step"

    def __post_init__(self) -> None:
        _require_finite(self.beat, "TempoPoint.beat")
        if self.beat < 0:
            raise ParameterError(f"速度点 beat 不能为负: {self.beat}")
        _require_finite(self.bpm, "TempoPoint.bpm")
        if not 0 < self.bpm <= 400:
            raise ParameterError(f"速度点 bpm 超出范围 (0, 400]: {self.bpm}")
        if self.curve not in ("step", "linear"):
            raise ParameterError(f"速度曲线必须是 step/linear: {self.curve!r}")


@dataclass(frozen=True)
class MeterPoint:
    """拍号变化点。"""

    beat: float
    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        _require_finite(self.beat, "MeterPoint.beat")
        if self.beat < 0:
            raise ParameterError(f"拍号点 beat 不能为负: {self.beat}")
        if self.numerator < 1:
            raise ParameterError(f"拍号分子必须 >= 1: {self.numerator}")
        if self.denominator not in (1, 2, 4, 8, 16, 32, 64):
            raise ParameterError(f"拍号分母必须是 2 的幂 (1..64): {self.denominator}")

    @property
    def bar_length_beats(self) -> float:
        """一小节的拍数（四分音符计）。"""
        return 4.0 * self.numerator / self.denominator


@dataclass(frozen=True)
class KeyPoint:
    """调性变化点；``key`` 采用项目约定写法（如 ``"D minor"`` / ``"Bb major"``）。"""

    beat: float
    key: str

    def __post_init__(self) -> None:
        _require_finite(self.beat, "KeyPoint.beat")
        if self.beat < 0:
            raise ParameterError(f"调性点 beat 不能为负: {self.beat}")
        if not str(self.key).strip():
            raise ParameterError("调性不能为空")


@dataclass
class NoteEvent:
    """带演奏信息的音符事件（``models.notes.Note`` 的超集）。

    Attributes:
        pitch: MIDI 音高 0-127（C4=60）。
        start: 起始拍（全曲累计）。
        duration: 持续拍数（> 0）。
        velocity: 击键力度 1-127。
        release_velocity: 释键力度 0-127。
        articulation: 演奏法标记（M3 启用；当前仅存储，写盘时按需映射）。
        offset_ticks: 人性化微时序偏移（tick，写入时叠加到起始位置）。
    """

    pitch: int
    start: float
    duration: float
    velocity: int = 64
    release_velocity: int = 64
    articulation: str | None = None
    offset_ticks: int = 0

    def __post_init__(self) -> None:
        if not 0 <= int(self.pitch) <= 127:
            raise ParameterError(f"pitch 超出 MIDI 范围 (0-127): {self.pitch}")
        _require_finite(self.start, "NoteEvent.start")
        _require_finite(self.duration, "NoteEvent.duration")
        if self.start < 0:
            raise ParameterError(f"音符起始拍不能为负: {self.start}")
        if self.duration <= 0:
            raise ParameterError(f"音符时值必须为正: {self.duration}")
        if not 1 <= int(self.velocity) <= 127:
            raise ParameterError(f"velocity 超出范围 (1-127): {self.velocity}")
        if not 0 <= int(self.release_velocity) <= 127:
            raise ParameterError(f"release_velocity 超出范围 (0-127): {self.release_velocity}")
        if not isinstance(self.offset_ticks, int) or isinstance(self.offset_ticks, bool):
            raise ParameterError(f"offset_ticks 必须是整数: {self.offset_ticks!r}")

    @property
    def end(self) -> float:
        """结束拍。"""
        return self.start + self.duration


@dataclass(frozen=True)
class ControlChange:
    """轨道内控制器事件（通道由所属轨道决定）。"""

    beat: float
    controller: int
    value: int

    def __post_init__(self) -> None:
        _require_finite(self.beat, "ControlChange.beat")
        if self.beat < 0:
            raise ParameterError(f"CC 事件 beat 不能为负: {self.beat}")
        if not 0 <= int(self.controller) <= 127:
            raise ParameterError(f"controller 超出范围 (0-127): {self.controller}")
        if not 0 <= int(self.value) <= 127:
            raise ParameterError(f"controller 值超出范围 (0-127): {self.value}")


@dataclass(frozen=True)
class RawEvent:
    """读入时无法归类的事件（sysex / pitchwheel / 未知 meta 等），写回时原样重发。

    ``message`` 为 ``mido.Message.dict()`` 的结果（不含 ``time``）。
    """

    beat: float
    message: dict

    def __post_init__(self) -> None:
        _require_finite(self.beat, "RawEvent.beat")
        if self.beat < 0:
            raise ParameterError(f"原始事件 beat 不能为负: {self.beat}")
        if not isinstance(self.message, dict) or "type" not in self.message:
            raise ParameterError("原始事件必须是含 type 的 mido 消息字典")


@dataclass
class MidiTrackV2:
    """一条通道化的 MIDI 轨道。

    Attributes:
        name: 轨道名。
        channel: MIDI 通道 0-15；鼓轨固定 9。
        notes: 音符事件。
        program: GM 音色号 0-127。
        bank_msb / bank_lsb: 音色库选择（CC0 / CC32）。
        is_drum: 是否鼓轨；与 ``channel == 9`` 强绑定。
        volume / pan: 初始 CC7 / CC10（0-127；默认 100 / 64 = MIDI 默认值）。
        control_changes: 其余控制器事件（不含 CC0/32/7/10 的初始值）。
        raw_events: 读入时保留的未知事件。
    """

    name: str
    channel: int
    notes: list[NoteEvent] = field(default_factory=list)
    program: int = 0
    bank_msb: int = 0
    bank_lsb: int = 0
    is_drum: bool = False
    volume: int = 100
    pan: int = 64
    control_changes: list[ControlChange] = field(default_factory=list)
    raw_events: list[RawEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not 0 <= int(self.channel) <= 15:
            raise ParameterError(f"channel 超出范围 (0-15): {self.channel}")
        if not 0 <= int(self.program) <= 127:
            raise ParameterError(f"program 超出范围 (0-127): {self.program}")
        for label, value in (("bank_msb", self.bank_msb), ("bank_lsb", self.bank_lsb),
                             ("volume", self.volume), ("pan", self.pan)):
            if not 0 <= int(value) <= 127:
                raise ParameterError(f"{label} 超出范围 (0-127): {value}")
        if int(self.channel) == DRUM_CHANNEL:
            self.is_drum = True
        elif self.is_drum:
            raise ParameterError(
                f"鼓轨必须使用通道 {DRUM_CHANNEL}，当前 channel={self.channel}"
            )

    @property
    def end_beat(self) -> float:
        """轨道最后一个音符的结束拍（空轨为 0）。"""
        return max((note.end for note in self.notes), default=0.0)


@dataclass
class MidiDocumentV2:
    """一首曲子的 MIDI 交换文档（SMF format 1）。

    Attributes:
        tracks: 通道化轨道列表（不含指挥轨；指挥轨由写入器生成）。
        tempo_map / meter_map / key_map: 全曲变化表（首点 beat 必须为 0）。
        markers: 段落标记（beat, text），写入 meta marker。
        ppq: 每拍 tick 数（新建默认 960；读入时保留文件原值）。
        title: 曲名（写入指挥轨 track_name）。
    """

    tracks: list[MidiTrackV2] = field(default_factory=list)
    tempo_map: list[TempoPoint] = field(
        default_factory=lambda: [TempoPoint(beat=0.0, bpm=120.0)]
    )
    meter_map: list[MeterPoint] = field(
        default_factory=lambda: [MeterPoint(beat=0.0, numerator=4, denominator=4)]
    )
    key_map: list[KeyPoint] = field(
        default_factory=lambda: [KeyPoint(beat=0.0, key="C major")]
    )
    markers: list[tuple[float, str]] = field(default_factory=list)
    raw_events: list[RawEvent] = field(default_factory=list)
    ppq: int = 960
    title: str = ""

    # -- 校验与度量 -------------------------------------------------------

    def validate(self) -> None:
        """校验整篇文档；失败抛 ``ParameterError``（M1 阶段由编排层映射为 40）。

        校验项：PPQ、三张时间表、通道唯一性、鼓轨唯一性、音符边界。
        """
        if not 1 <= int(self.ppq) <= 32767:
            raise ParameterError(f"ppq 超出范围 (1-32767): {self.ppq}")
        validate_timeline(self.tempo_map, self.meter_map, self.key_map)

        seen: dict[int, str] = {}
        drum_count = 0
        for track in self.tracks:
            if track.channel in seen:
                raise ParameterError(
                    f"通道 {track.channel} 被多条轨道占用: "
                    f"{seen[track.channel]!r} 与 {track.name!r}"
                )
            seen[track.channel] = track.name
            if track.is_drum:
                drum_count += 1
        if drum_count > 1:
            raise ParameterError("一条曲子最多允许一条鼓轨（通道 9）")
        for beat, _text in self.markers:
            _require_finite(beat, "marker.beat")

    @property
    def duration_beats(self) -> float:
        """全曲结束拍（取轨道/标记最大值）。"""
        ends = [track.end_beat for track in self.tracks]
        ends.extend(beat for beat, _ in self.markers)
        return max(ends, default=0.0)

    def duration_seconds(self) -> float:
        """全曲时长（秒），按速度表积分。"""
        return beat_to_seconds(self.duration_beats, self.tempo_map)

    def track_by_channel(self, channel: int) -> MidiTrackV2 | None:
        """按通道查找轨道。"""
        return next((t for t in self.tracks if t.channel == channel), None)


# -- 时间表工具 ---------------------------------------------------------------


def validate_timeline(
    tempo_map: Sequence[TempoPoint],
    meter_map: Sequence[MeterPoint],
    key_map: Sequence[KeyPoint],
) -> None:
    """校验三张变化表：非空、首点 0、beat 严格递增。"""
    for label, points in (("速度表", tempo_map), ("拍号表", meter_map), ("调性表", key_map)):
        if not points:
            raise ParameterError(f"{label}不能为空")
        if points[0].beat != 0:
            raise ParameterError(f"{label}首点 beat 必须为 0，实为 {points[0].beat}")
        previous = -1.0
        for point in points:
            if point.beat <= previous:
                raise ParameterError(f"{label}的 beat 必须严格递增（{previous} -> {point.beat}）")
            previous = point.beat


def beat_to_seconds(beat: float, tempo_map: Sequence[TempoPoint]) -> float:
    """把"拍"换算为秒（按速度表分段积分）。

    ``step`` 段按常量速度；``linear`` 段按 BPM 随拍线性变化积分：

        ∫ 60 / bpm(b) db = 60 / k * ln((bpm0 + k·Δ) / bpm0)

    Args:
        beat: 目标拍（>= 0）。
        tempo_map: 速度表（首点 beat=0，严格递增）。

    Returns:
        秒数。

    Raises:
        ParameterError: 速度表为空或目标拍早于首点。
    """
    target = _require_finite(beat, "beat")
    if not tempo_map:
        raise ParameterError("速度表不能为空")
    if target < tempo_map[0].beat:
        raise ParameterError(f"目标拍 {target} 早于速度表首点 {tempo_map[0].beat}")

    seconds = 0.0
    for index, point in enumerate(tempo_map):
        if target <= point.beat:
            break
        next_beat = tempo_map[index + 1].beat if index + 1 < len(tempo_map) else target
        segment_end = min(target, next_beat)
        span = segment_end - point.beat
        if span <= 0:
            continue
        if (
            point.curve == "linear"
            and index + 1 < len(tempo_map)
            and tempo_map[index + 1].bpm != point.bpm
        ):
            b0 = point.bpm
            b1 = tempo_map[index + 1].bpm
            slope = (b1 - b0) / (next_beat - point.beat)
            seconds += 60.0 * math.log((b0 + slope * span) / b0) / slope
        else:
            seconds += span * 60.0 / point.bpm
        if target <= next_beat:
            break
    return seconds


def seconds_between(
    start_beat: float, end_beat: float, tempo_map: Sequence[TempoPoint]
) -> float:
    """两个拍位置之间的秒数。"""
    if end_beat < start_beat:
        raise ParameterError(f"结束拍 {end_beat} 早于起始拍 {start_beat}")
    return beat_to_seconds(end_beat, tempo_map) - beat_to_seconds(start_beat, tempo_map)


def total_bars(
    meter_map: Sequence[MeterPoint], end_beat: float
) -> float:
    """按拍号表累计到 ``end_beat`` 的小节数（可为小数，便于校验弱起）。"""
    target = _require_finite(end_beat, "end_beat")
    if not meter_map:
        raise ParameterError("拍号表不能为空")
    bars = 0.0
    for index, point in enumerate(meter_map):
        if target <= point.beat:
            break
        next_beat = meter_map[index + 1].beat if index + 1 < len(meter_map) else target
        segment_end = min(target, next_beat)
        bars += (segment_end - point.beat) / point.bar_length_beats
        if target <= next_beat:
            break
    return bars


def iter_notes(tracks: Iterable[MidiTrackV2]) -> Iterable[NoteEvent]:
    """跨轨遍历全部音符（统计/校验辅助）。"""
    for track in tracks:
        yield from track.notes
