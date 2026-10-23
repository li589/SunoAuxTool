"""旧模型 → ``Song`` 适配器（PR-B）。

两个入口：

- ``from_sequence(NoteSequence)``：把生成器（procedural / music21）的输出接进
  新编曲模型，旧 ``generate midi`` 路径不变；
- ``from_legacy_midi(MidiDocument)``：把旧 MIDI 读取结果接进来（旧模型通道信息
  不可靠，统一交给新模型自动分配）。

反方向 ``Song → MidiDocumentV2`` 在 ``Song.to_midi()``；旧 ``MidiDocument`` 的
写盘路径保持不变。
"""

from __future__ import annotations

import math

from sunoauxtool.compose.song import (
    Clip,
    InstrumentRef,
    Section,
    Song,
    Track,
)
from sunoauxtool.exceptions import ParameterError
from sunoauxtool.midi.document import DRUM_CHANNEL, KeyPoint, MeterPoint, NoteEvent, TempoPoint
from sunoauxtool.models.midi import MidiDocument
from sunoauxtool.models.notes import NoteSequence, Note

_EPS = 1e-9


def _parse_meter(text: str) -> tuple[int, int]:
    """解析 ``"4/4"`` / ``"3/4"`` 为 (分子, 分母)。"""
    parts = str(text).split("/")
    if len(parts) != 2:
        raise ParameterError(f"无法解析拍号: {text!r}（应为 '4/4'）")
    try:
        numerator, denominator = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ParameterError(f"无法解析拍号: {text!r}") from exc
    return numerator, denominator


def _infer_role(name: str, *, is_drum: bool) -> str:
    """按轨道名推断编曲角色（保守：识别不了按 harmony 处理）。"""
    if is_drum:
        return "drums"
    text = str(name).lower()
    if any(key in text for key in ("bass", "低音")):
        return "bass"
    if any(key in text for key in ("chord", "harm", "和声", "和弦")):
        return "harmony"
    if "pad" in text or "铺" in text:
        return "pad"
    if "arp" in text:
        return "arpeggio"
    if any(key in text for key in ("melod", "lead", "旋律")):
        return "melody"
    return "harmony"


def _bars_for(duration_beats: float, bar_length: float, minimum: int = 1) -> int:
    """把时长向上取整为小节数。"""
    if duration_beats <= 0:
        return minimum
    return max(minimum, int(math.ceil(duration_beats / bar_length - _EPS)))


def _copy_note(note: Note) -> NoteEvent:
    return NoteEvent(
        pitch=note.pitch,
        start=note.start,
        duration=note.duration,
        velocity=note.velocity,
    )


def from_sequence(seq: NoteSequence, *, title: str = "Untitled") -> Song:
    """把生成器输出 ``NoteSequence`` 转为 ``Song``。

    - 单速度/单拍号/单调性 → 三张表各一个点；
    - 全曲作为一个段落 ``full``（ID 固定，便于后续拆分）；
    - 非鼓轨保留可用的显式通道（重复的置 None 交给自动分配）；
    - 小节数取 ``max(seq.bars, 音符实际需要)``，避免片段越界。

    Args:
        seq: 生成器产出的音符序列。
        title: 曲名。

    Returns:
        校验通过的 ``Song``。
    """
    numerator, denominator = _parse_meter(seq.time_signature)
    meter = MeterPoint(beat=0.0, numerator=numerator, denominator=denominator)
    bar_length = meter.bar_length_beats
    max_note_end = max(
        (note.start + note.duration for note in seq.notes), default=0.0
    )
    bars = max(seq.bars, _bars_for(max_note_end, bar_length))
    total_beats = bars * bar_length

    song = Song(
        title=title,
        tempo_map=[TempoPoint(beat=0.0, bpm=float(seq.bpm))],
        meter_map=[meter],
        key_map=[KeyPoint(beat=0.0, key=seq.key)],
        sections=[Section(id="full", name="全曲", start=0.0, bars=bars)],
        seed=0,
    )

    used_channels: set[int] = set()
    for source in seq.tracks:
        is_drum = source.channel == DRUM_CHANNEL
        channel: int | None = None
        if is_drum:
            channel = DRUM_CHANNEL
        elif source.channel not in used_channels:
            channel = source.channel
            used_channels.add(source.channel)
        notes = [_copy_note(note) for note in source.notes]
        track = Track(
            id=source.name,
            name=source.name,
            role=_infer_role(source.name, is_drum=is_drum),
            instrument=InstrumentRef(program=source.program),
            channel=channel,
            clips=[Clip(start=0.0, length=total_beats, notes=notes, section_id="full")],
        )
        song.add_track(track)

    song.validate()
    return song


def from_legacy_midi(
    doc: MidiDocument,
    *,
    title: str = "Untitled",
    key: str = "C major",
    meter: str = "4/4",
) -> Song:
    """把旧 ``MidiDocument``（pretty_midi 读取结果）转为 ``Song``。

    旧模型的通道不可靠（非鼓一律 0），因此这里**不保留**其通道，统一自动分配；
    鼓轨按 ``is_drum`` 固定 9。旧模型没有拍号/调性，需由调用方给出（默认 4/4、
    C major），后续 PR-C 的导入命令会带分析结果覆盖。

    Args:
        doc: 旧 MIDI 文档。
        title: 曲名。
        key: 调性写法（``"D minor"``）。
        meter: 拍号（``"4/4"``）。

    Returns:
        校验通过的 ``Song``。
    """
    numerator, denominator = _parse_meter(meter)
    meter_point = MeterPoint(beat=0.0, numerator=numerator, denominator=denominator)
    bar_length = meter_point.bar_length_beats
    max_note_end = max(
        (note.start + note.duration for track in doc.tracks for note in track.notes),
        default=0.0,
    )
    bars = _bars_for(max_note_end, bar_length)
    total_beats = bars * bar_length

    song = Song(
        title=title,
        tempo_map=[TempoPoint(beat=0.0, bpm=float(doc.bpm))],
        meter_map=[meter_point],
        key_map=[KeyPoint(beat=0.0, key=key)],
        sections=[Section(id="full", name="全曲", start=0.0, bars=bars)],
    )

    for index, source in enumerate(doc.tracks):
        is_drum = source.is_drum
        notes = [_copy_note(note) for note in source.notes]
        track = Track(
            id=f"track{index + 1}",
            name=source.name or f"Track {index + 1}",
            role=_infer_role(source.name, is_drum=is_drum),
            instrument=InstrumentRef(program=source.program),
            channel=DRUM_CHANNEL if is_drum else None,
            clips=[Clip(start=0.0, length=total_beats, notes=notes, section_id="full")],
        )
        song.add_track(track)

    song.validate()
    return song
