"""编曲领域模型（2.0 PR-B）：Song / Section / Track / Clip / MixStrip。

定位：``Song`` 是 2.0 的唯一真源（Single Source of Truth），承载"一首曲子"的
全部可编辑信息；``midi`` 包是它的交换格式，``compose.arranger``（PR-D）负责从
清单生成它，``render``（PR-E）负责把它变成音频。

时间约定与 ``midi.document`` 一致：所有 ``beat`` 为拍（float，四分音符=1），
秒换算只能通过速度表积分。

与旧模型的关系：``models.notes.NoteSequence`` 与 ``models.midi.MidiDocument``
保持不变，通过 ``compose.adapters`` 单向转换进来。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from sunoauxtool.exceptions import ParameterError
from sunoauxtool.midi.channel import ChannelRequest, allocate_channels
from sunoauxtool.midi.document import (
    DRUM_CHANNEL,
    KeyPoint,
    MeterPoint,
    MidiDocumentV2,
    MidiTrackV2,
    NoteEvent,
    TempoPoint,
    beat_to_seconds,
    total_bars,
    validate_timeline,
)

#: 编曲角色（决定生成策略；instrument 只决定音色）
KNOWN_ROLES = frozenset({
    "melody",      # 主旋律
    "counter",     # 对位/副旋律
    "harmony",     # 和弦/和声层
    "arpeggio",    # 分解和弦
    "pad",         # 长音铺底
    "bass",        # 低音
    "drums",       # 鼓组（固定通道 9）
    "percussion",  # 旋律性打击（定音鼓等，走旋律通道）
})

_EPS = 1e-6


def _finite(value: float, name: str) -> float:
    """校验有限数值。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ParameterError(f"{name} 必须是数值: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ParameterError(f"{name} 必须是有限数值: {value!r}")
    return number


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


@dataclass
class MixStrip:
    """调音台通道条。

    Attributes:
        gain_db: 增益（dB，-60..+12）。
        pan: 声像（-1.0 左 .. +1.0 右）。
        mute / solo: 静音/独奏（solo 优先）。
        reverb_send / chorus_send: 发送量（0..1）。
    """

    gain_db: float = 0.0
    pan: float = 0.0
    mute: bool = False
    solo: bool = False
    reverb_send: float = 0.0
    chorus_send: float = 0.0

    def __post_init__(self) -> None:
        _finite(self.gain_db, "MixStrip.gain_db")
        _finite(self.pan, "MixStrip.pan")
        if not -60.0 <= self.gain_db <= 12.0:
            raise ParameterError(f"gain_db 超出范围 (-60..+12): {self.gain_db}")
        if not -1.0 <= self.pan <= 1.0:
            raise ParameterError(f"pan 超出范围 (-1..+1): {self.pan}")
        for label, value in (("reverb_send", self.reverb_send),
                             ("chorus_send", self.chorus_send)):
            _finite(value, f"MixStrip.{label}")
            if not 0.0 <= value <= 1.0:
                raise ParameterError(f"{label} 超出范围 (0..1): {value}")

    @property
    def volume_cc(self) -> int:
        """增益 → CC7（0 dB = 100，线性幅度，钳制 0..127）。"""
        value = round(100.0 * (10.0 ** (self.gain_db / 20.0)))
        return _clamp(value, 0, 127)

    @property
    def pan_cc(self) -> int:
        """声像 → CC10（-1→0，0→64，+1→127）。"""
        return _clamp(round((self.pan + 1.0) * 63.5), 0, 127)

    def to_dict(self) -> dict:
        return {
            "gain_db": self.gain_db,
            "pan": self.pan,
            "mute": self.mute,
            "solo": self.solo,
            "reverb_send": self.reverb_send,
            "chorus_send": self.chorus_send,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MixStrip":
        return cls(**{key: data.get(key, default) for key, default in (
            ("gain_db", 0.0), ("pan", 0.0), ("mute", False), ("solo", False),
            ("reverb_send", 0.0), ("chorus_send", 0.0),
        )})


@dataclass(frozen=True)
class InstrumentRef:
    """音色引用：逻辑库 + 预设/显式 GM 音色号。

    Attributes:
        bank: 音源库 id（2.1 注册表键；当前默认 ``"gm"``）。
        preset: 逻辑预设名（如 ``"piano"``）；2.1 起由注册表解析。
        program: 显式 GM 音色号 0-127（优先于 preset）。
        drum_kit: 鼓组名（鼓轨可选）。
    """

    bank: str = "gm"
    preset: str | None = None
    program: int | None = None
    drum_kit: str | None = None

    def __post_init__(self) -> None:
        if not str(self.bank).strip():
            raise ParameterError("instrument.bank 不能为空")
        if self.program is not None and not 0 <= int(self.program) <= 127:
            raise ParameterError(f"instrument.program 超出范围 (0-127): {self.program}")
        for label, value in (("preset", self.preset), ("drum_kit", self.drum_kit)):
            if value is not None and not str(value).strip():
                raise ParameterError(f"instrument.{label} 不能为空字符串")

    def resolved_program(self) -> int:
        """当前可用的 GM 音色号。

        2.0 阶段没有音源库注册表：显式 ``program`` 优先；仅给 ``preset`` 时
        暂回退到 0（PR-G 接入注册表后按预设解析）。调用方可用
        ``has_explicit_program`` 判断是否需要提示用户。
        """
        return int(self.program) if self.program is not None else 0

    @property
    def has_explicit_program(self) -> bool:
        return self.program is not None

    def to_dict(self) -> dict:
        return {
            "bank": self.bank,
            "preset": self.preset,
            "program": self.program,
            "drum_kit": self.drum_kit,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "InstrumentRef":
        return cls(
            bank=data.get("bank", "gm"),
            preset=data.get("preset"),
            program=data.get("program"),
            drum_kit=data.get("drum_kit"),
        )


@dataclass
class Clip:
    """轨道上的一段音符（通常对应一个段落）。

    Attributes:
        start / length: 相对全曲的起始拍与长度（拍）。
        notes: 音符事件（``start`` 为全曲绝对拍）。
        section_id: 所属段落（空表示未归属）。
        label: 片段标签（审计用）。
    """

    start: float
    length: float
    notes: list[NoteEvent] = field(default_factory=list)
    section_id: str = ""
    label: str = ""

    def __post_init__(self) -> None:
        _finite(self.start, "Clip.start")
        _finite(self.length, "Clip.length")
        if self.start < 0:
            raise ParameterError(f"片段起始拍不能为负: {self.start}")
        if self.length <= 0:
            raise ParameterError(f"片段长度必须为正: {self.length}")

    @property
    def end(self) -> float:
        return self.start + self.length

    def transposed(self, semitones: int) -> "Clip":
        """返回整体移调后的新片段（不修改自身）。"""
        notes = [
            NoteEvent(
                pitch=note.pitch + int(semitones),
                start=note.start,
                duration=note.duration,
                velocity=note.velocity,
                release_velocity=note.release_velocity,
                articulation=note.articulation,
                offset_ticks=note.offset_ticks,
            )
            for note in self.notes
        ]
        return Clip(start=self.start, length=self.length, notes=notes,
                    section_id=self.section_id, label=self.label)


@dataclass
class Track:
    """一条乐器轨。

    Attributes:
        id / name: 唯一标识与显示名。
        role: 编曲角色（见 ``KNOWN_ROLES``）。
        instrument: 音色引用。
        channel: 显式 MIDI 通道；None 表示构建 MIDI 时自动分配。
        clips: 片段列表。
        mix: 调音台通道条。
        octave: 生成时的基准八度（arranger 使用）。
        range: 允许音域 (low, high)；None 表示不限制。
        muted: 轨道静音（arranger/渲染参考）。
    """

    id: str
    name: str
    role: str
    instrument: InstrumentRef
    channel: int | None = None
    clips: list[Clip] = field(default_factory=list)
    mix: MixStrip = field(default_factory=MixStrip)
    octave: int = 4
    range: tuple[int, int] | None = None
    muted: bool = False

    def __post_init__(self) -> None:
        if not str(self.id).strip():
            raise ParameterError("track.id 不能为空")
        if not str(self.name).strip():
            raise ParameterError(f"track.name 不能为空（id={self.id!r}）")
        if self.role not in KNOWN_ROLES:
            raise ParameterError(
                f"未知编曲角色 {self.role!r}（track={self.id!r}）；"
                f"可用: {', '.join(sorted(KNOWN_ROLES))}"
            )
        if self.channel is not None and not 0 <= int(self.channel) <= 15:
            raise ParameterError(f"channel 超出范围 (0-15): {self.channel}")
        if self.role == "drums":
            if self.channel is not None and self.channel != DRUM_CHANNEL:
                raise ParameterError(
                    f"鼓轨必须使用通道 {DRUM_CHANNEL}，track={self.id!r} channel={self.channel}"
                )
        elif self.channel == DRUM_CHANNEL:
            raise ParameterError(
                f"通道 {DRUM_CHANNEL} 保留给鼓轨，track={self.id!r} role={self.role!r}"
            )
        if not 0 <= int(self.octave) <= 9:
            raise ParameterError(f"octave 超出范围 (0-9): {self.octave}")
        if self.range is not None:
            low, high = self.range
            if not 0 <= low <= high <= 127:
                raise ParameterError(f"音域非法 (low, high): {self.range}")

    @property
    def is_drum(self) -> bool:
        return self.role == "drums"

    @property
    def end_beat(self) -> float:
        return max((clip.end for clip in self.clips), default=0.0)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "instrument": self.instrument.to_dict(),
            "channel": self.channel,
            "octave": self.octave,
            "range": list(self.range) if self.range else None,
            "muted": self.muted,
            "mix": self.mix.to_dict(),
            "clips": [
                {
                    "start": clip.start,
                    "length": clip.length,
                    "section_id": clip.section_id,
                    "label": clip.label,
                    "notes": [
                        {
                            "pitch": note.pitch,
                            "start": note.start,
                            "duration": note.duration,
                            "velocity": note.velocity,
                            "release_velocity": note.release_velocity,
                            "articulation": note.articulation,
                            "offset_ticks": note.offset_ticks,
                        }
                        for note in clip.notes
                    ],
                }
                for clip in self.clips
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Track":
        return cls(
            id=data["id"],
            name=data["name"],
            role=data["role"],
            instrument=InstrumentRef.from_dict(data.get("instrument", {})),
            channel=data.get("channel"),
            octave=data.get("octave", 4),
            range=tuple(data["range"]) if data.get("range") else None,
            muted=bool(data.get("muted", False)),
            mix=MixStrip.from_dict(data.get("mix", {})),
            clips=[
                Clip(
                    start=clip["start"],
                    length=clip["length"],
                    section_id=clip.get("section_id", ""),
                    label=clip.get("label", ""),
                    notes=[NoteEvent(**note) for note in clip.get("notes", [])],
                )
                for clip in data.get("clips", [])
            ],
        )


@dataclass
class Section:
    """段落（前奏/主歌/副歌/间奏/尾奏）。

    Attributes:
        id / name: 唯一标识与显示名。
        start: 起始拍（必须落在小节线）。
        bars: 小节数（>= 1）。
        energy: 能量 0..1，驱动配器密度。
        progression: 每小节一个和弦（如 ``"Dm"`` / ``"F/A"``）。
        label: 排练记号/显示标签。
    """

    id: str
    name: str
    start: float
    bars: int
    energy: float = 0.5
    progression: list[str] = field(default_factory=list)
    label: str = ""

    def __post_init__(self) -> None:
        if not str(self.id).strip():
            raise ParameterError("section.id 不能为空")
        _finite(self.start, "Section.start")
        if self.start < 0:
            raise ParameterError(f"段落起始拍不能为负: {self.start}")
        if not isinstance(self.bars, int) or isinstance(self.bars, bool) or self.bars < 1:
            raise ParameterError(f"段落小节数必须是 >=1 的整数: {self.bars}")
        _finite(self.energy, "Section.energy")
        if not 0.0 <= self.energy <= 1.0:
            raise ParameterError(f"段落能量超出范围 (0..1): {self.energy}")
        for chord in self.progression:
            if not str(chord).strip():
                raise ParameterError(f"段落 {self.id!r} 的和弦不能为空")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "start": self.start,
            "bars": self.bars,
            "energy": self.energy,
            "progression": list(self.progression),
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Section":
        return cls(
            id=data["id"],
            name=data.get("name", ""),
            start=data["start"],
            bars=data["bars"],
            energy=data.get("energy", 0.5),
            progression=list(data.get("progression", [])),
            label=data.get("label", ""),
        )


@dataclass
class Song:
    """一首曲子（编曲唯一真源）。

    Attributes:
        title: 曲名。
        tempo_map / meter_map / key_map: 变化表（首点 beat=0）。
        sections: 段落（首尾相接、按小节对齐）。
        tracks: 乐器轨。
        master: 总线（增益/限幅前的整体设置）。
        seed: 编曲随机种子。
        pickup_beats: 弱起拍数（0 表示无弱起）。
        schema_version: 序列化版本（arrangement.json）。
    """

    title: str = "Untitled"
    tempo_map: list[TempoPoint] = field(
        default_factory=lambda: [TempoPoint(beat=0.0, bpm=120.0)]
    )
    meter_map: list[MeterPoint] = field(
        default_factory=lambda: [MeterPoint(beat=0.0, numerator=4, denominator=4)]
    )
    key_map: list[KeyPoint] = field(
        default_factory=lambda: [KeyPoint(beat=0.0, key="C major")]
    )
    sections: list[Section] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    master: MixStrip = field(default_factory=MixStrip)
    seed: int = 0
    pickup_beats: float = 0.0
    schema_version: int = 1

    # -- 时间与段落 -------------------------------------------------------

    def bar_length_at(self, beat: float) -> float:
        """返回 ``beat`` 所在拍号段的小节长度（拍）。"""
        active = self.meter_map[0]
        for point in self.meter_map:
            if point.beat <= beat + _EPS:
                active = point
            else:
                break
        return active.bar_length_beats

    def section_end(self, section: Section) -> float:
        """段落结束拍 = 起始拍 + 小节数 × 所在小节长度。"""
        return section.start + section.bars * self.bar_length_at(section.start)

    def section_by_id(self, section_id: str) -> Section | None:
        return next((section for section in self.sections if section.id == section_id), None)

    @property
    def total_bars(self) -> int:
        return sum(section.bars for section in self.sections)

    @property
    def duration_beats(self) -> float:
        """全曲结束拍：优先取最后一段，否则取片段最大值（空曲为弱起）。"""
        if self.sections:
            return self.section_end(self.sections[-1])
        return max((track.end_beat for track in self.tracks), default=self.pickup_beats)

    def duration_seconds(self) -> float:
        return beat_to_seconds(self.duration_beats, self.tempo_map)

    def total_bars_measured(self) -> float:
        """按拍号表实测的小节数（含弱起，便于交叉校验）。"""
        return total_bars(self.meter_map, self.duration_beats)

    # -- 构建辅助 ---------------------------------------------------------

    def add_section(
        self,
        section_id: str,
        *,
        name: str = "",
        bars: int = 4,
        energy: float = 0.5,
        progression: Sequence[str] | None = None,
        label: str = "",
    ) -> Section:
        """追加段落（起始拍自动接在上一段之后）。"""
        if self.section_by_id(section_id) is not None:
            raise ParameterError(f"段落 id 重复: {section_id!r}")
        start = self.section_end(self.sections[-1]) if self.sections else self.pickup_beats
        section = Section(
            id=section_id, name=name or section_id, start=start, bars=bars,
            energy=energy, progression=list(progression or []), label=label,
        )
        self.sections.append(section)
        return section

    def add_track(self, track: Track) -> Track:
        """追加轨道（id 重复立即报错）。"""
        if any(existing.id == track.id for existing in self.tracks):
            raise ParameterError(f"轨道 id 重复: {track.id!r}")
        self.tracks.append(track)
        return track

    def track_by_id(self, track_id: str) -> Track | None:
        return next((track for track in self.tracks if track.id == track_id), None)

    # -- 校验 -------------------------------------------------------------

    def validate(self) -> None:
        """校验整首曲子；失败抛 ``ParameterError``（PR-C 将映射为错误码 40）。

        检查项：
        1. 三张时间表首点 0、严格递增；
        2. 段落 id 唯一、首尾相接、起始拍在小节线、拍号变化不落在段落内部；
        3. 轨道 id 唯一、显式通道不冲突、鼓轨唯一且固定通道 9；
        4. 片段归属与边界、音符落在片段内、音符在轨道音域内；
        5. 弱起拍非负且不超过一个小节。
        """
        _finite(self.pickup_beats, "pickup_beats")
        if self.pickup_beats < 0:
            raise ParameterError(f"弱起拍不能为负: {self.pickup_beats}")
        if self.pickup_beats > self.bar_length_at(0):
            raise ParameterError(
                f"弱起拍 {self.pickup_beats} 超过一小节 {self.bar_length_at(0)}"
            )
        validate_timeline(self.tempo_map, self.meter_map, self.key_map)

        self._validate_sections()
        self._validate_tracks()

    def _validate_sections(self) -> None:
        seen: set[str] = set()
        expected_start = self.pickup_beats
        for section in self.sections:
            if section.id in seen:
                raise ParameterError(f"段落 id 重复: {section.id!r}")
            seen.add(section.id)
            if abs(section.start - expected_start) > _EPS:
                raise ParameterError(
                    f"段落 {section.id!r} 起点 {section.start} 与上一段结束 "
                    f"{expected_start} 不相接（段落必须首尾相接）"
                )
            if not self._is_bar_boundary(section.start):
                raise ParameterError(
                    f"段落 {section.id!r} 起点 {section.start} 不在小节线上"
                )
            expected_start = self.section_end(section)

        # 拍号变化只能发生在段落边界（否则"小节数"语义会漂移）
        boundaries = {round(section.start, 9) for section in self.sections}
        boundaries.add(round(self.pickup_beats, 9))
        boundaries.add(round(expected_start, 9))
        for point in self.meter_map:
            if point.beat == 0:
                continue
            if round(point.beat, 9) not in boundaries:
                raise ParameterError(
                    f"拍号变化点 {point.beat} 不在段落边界上；请把变拍放在段落开头"
                )

    def _validate_tracks(self) -> None:
        ids: set[str] = set()
        channels: dict[int, str] = {}
        drum_count = 0
        for track in self.tracks:
            if track.id in ids:
                raise ParameterError(f"轨道 id 重复: {track.id!r}")
            ids.add(track.id)
            if track.channel is not None:
                if track.channel in channels:
                    raise ParameterError(
                        f"通道 {track.channel} 被 {channels[track.channel]!r} 与 "
                        f"{track.id!r} 同时占用"
                    )
                channels[track.channel] = track.id
            if track.is_drum:
                drum_count += 1
            self._validate_track_clips(track)
        if drum_count > 1:
            raise ParameterError("一首曲子最多允许一条鼓轨（role='drums'）")

    def _validate_track_clips(self, track: Track) -> None:
        for clip in track.clips:
            if clip.section_id:
                section = self.section_by_id(clip.section_id)
                if section is None:
                    raise ParameterError(
                        f"轨道 {track.id!r} 的片段引用了不存在的段落 {clip.section_id!r}"
                    )
                section_end = self.section_end(section)
                if clip.start < section.start - _EPS or clip.end > section_end + _EPS:
                    raise ParameterError(
                        f"轨道 {track.id!r} 的片段 [{clip.start}, {clip.end}] "
                        f"超出段落 {section.id!r} [{section.start}, {section_end}]"
                    )
            for note in clip.notes:
                if note.start < clip.start - _EPS or note.end > clip.end + _EPS:
                    raise ParameterError(
                        f"轨道 {track.id!r} 的音符 {note.pitch}@{note.start} "
                        f"超出片段 [{clip.start}, {clip.end}]"
                    )
                if track.range and not track.range[0] <= note.pitch <= track.range[1]:
                    raise ParameterError(
                        f"轨道 {track.id!r} 的音符 {note.pitch} 超出音域 {track.range}"
                    )

    def _is_bar_boundary(self, beat: float) -> bool:
        """判断 beat 是否落在拍号表的小节线上。

        弱起（``pickup_beats > 0``）时首段的小节网格以**弱起起点**为锚——
        弱起是一小节之前的不完整小节，第 1 小节紧跟其后（否则弱起曲永远
        无法通过"段落起点在小节线上"的校验）。
        """
        for index, point in enumerate(self.meter_map):
            anchor = self.pickup_beats if index == 0 else point.beat
            segment_end = (
                self.meter_map[index + 1].beat
                if index + 1 < len(self.meter_map)
                else float("inf")
            )
            if beat < anchor - _EPS:
                return False
            if beat <= segment_end + _EPS:
                bars = (beat - anchor) / point.bar_length_beats
                return abs(bars - round(bars)) <= 1e-6
        return False

    # -- 导出 MIDI --------------------------------------------------------

    def to_midi(self) -> MidiDocumentV2:
        """构建全保真 MIDI 文档（通道自动分配、混音映射为 CC7/CC10）。

        Raises:
            ParameterError: 模型不合法。
            ChannelAllocationError: 通道冲突或旋律轨超过 15 条（错误码 45）。
        """
        self.validate()
        requests = [
            ChannelRequest(track.id, track.channel, is_drum=track.is_drum)
            for track in self.tracks
        ]
        channels = allocate_channels(requests)

        midi_tracks: list[MidiTrackV2] = []
        for track in self.tracks:
            notes = [note for clip in track.clips for note in clip.notes]
            midi_tracks.append(MidiTrackV2(
                name=track.name,
                channel=channels[track.id],
                notes=sorted(notes, key=lambda note: (note.start, note.pitch)),
                program=track.instrument.resolved_program(),
                is_drum=track.is_drum,
                volume=track.mix.volume_cc,
                pan=track.mix.pan_cc,
            ))

        document = MidiDocumentV2(
            tracks=midi_tracks,
            tempo_map=list(self.tempo_map),
            meter_map=list(self.meter_map),
            key_map=list(self.key_map),
            markers=[(section.start, section.label or section.name) for section in self.sections],
            ppq=960,
            title=self.title,
            end_beat=self.duration_beats,
        )
        document.validate()
        return document

    # -- 序列化 -----------------------------------------------------------

    def to_dict(self) -> dict:
        """展开为可 JSON 序列化的快照（arrangement.json）。"""
        return {
            "schema_version": self.schema_version,
            "title": self.title,
            "seed": self.seed,
            "pickup_beats": self.pickup_beats,
            "tempo_map": [
                {"beat": point.beat, "bpm": point.bpm, "curve": point.curve}
                for point in self.tempo_map
            ],
            "meter_map": [
                {"beat": point.beat, "numerator": point.numerator,
                 "denominator": point.denominator}
                for point in self.meter_map
            ],
            "key_map": [{"beat": point.beat, "key": point.key} for point in self.key_map],
            "sections": [section.to_dict() for section in self.sections],
            "tracks": [track.to_dict() for track in self.tracks],
            "master": self.master.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict, *, validate: bool = True) -> "Song":
        """从快照重建（``validate=True`` 时执行完整校验）。"""
        song = cls(
            title=data.get("title", "Untitled"),
            tempo_map=[TempoPoint(**point) for point in data.get("tempo_map", [])]
            or [TempoPoint(beat=0.0, bpm=120.0)],
            meter_map=[MeterPoint(**point) for point in data.get("meter_map", [])]
            or [MeterPoint(beat=0.0, numerator=4, denominator=4)],
            key_map=[KeyPoint(**point) for point in data.get("key_map", [])]
            or [KeyPoint(beat=0.0, key="C major")],
            sections=[Section.from_dict(item) for item in data.get("sections", [])],
            tracks=[Track.from_dict(item) for item in data.get("tracks", [])],
            master=MixStrip.from_dict(data.get("master", {})),
            seed=int(data.get("seed", 0)),
            pickup_beats=float(data.get("pickup_beats", 0.0)),
            schema_version=int(data.get("schema_version", 1)),
        )
        if validate:
            song.validate()
        return song


def flatten_notes(song: Song) -> Iterable[NoteEvent]:
    """跨轨遍历全部音符（统计/渲染辅助）。"""
    for track in song.tracks:
        for clip in track.clips:
            yield from clip.notes
