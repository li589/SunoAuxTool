"""SMF（Standard MIDI File）format 1 读写：mido 实现，全保真。

设计要点：

- **写入**：format 1，track 0 为指挥轨（track_name / tempo / meter / key / marker），
  之后每轨一条；轨道内先 bank select（CC0/CC32）再 program_change，初始 CC7/CC10
  仅在偏离 MIDI 默认值（100/64）时写入。
- **读取**：按通道拆分（一个 mido track 含多通道时也能正确还原），速度/拍号/调性
  收集为变化表，未知事件（sysex / pitchwheel / aftertouch / 未知 meta）原样保留。
- **文本**：mido 默认 latin1；写入时用 ``meta_charset("utf-8")``，读取时用
  latin1→UTF-8 还原（中文曲名/轨名可往返）。
- **确定性**：事件按 (tick, 优先级, 插入序) 稳定排序；同输入重复写入字节一致。
"""

from __future__ import annotations

from pathlib import Path

import mido

from sunoauxtool.exceptions import ExportError, InputFileError, ParameterError
from sunoauxtool.midi.document import (
    DRUM_CHANNEL,
    ControlChange,
    KeyPoint,
    MeterPoint,
    MidiDocumentV2,
    MidiTrackV2,
    NoteEvent,
    RawEvent,
    TempoPoint,
)

# 同一 tick 内的事件顺序（值越小越先写）：
# 音色库选择 → 音色 → 初始音量/声像 → 音符，避免合成器在 bank 切换前就发声
_PRIO_META = 0
_PRIO_NOTE_OFF = 1
_PRIO_BANK = 2
_PRIO_PROGRAM = 3
_PRIO_CC = 4
_PRIO_NOTE_ON = 5
_PRIO_RAW = 6

#: 读入时归类为"指挥信息"的 meta 类型（其余 meta 进 raw_events）
_CONDUCTOR_META = frozenset({"set_tempo", "time_signature", "key_signature", "marker"})

#: 通道消息类型（其余视为通道无关的原始事件）
_CHANNEL_MESSAGES = frozenset(
    {"note_on", "note_off", "control_change", "program_change",
     "pitchwheel", "aftertouch", "polytouch"}
)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _recover_text(value: str) -> str:
    """把 mido 以 latin1 解出的字符串还原为 UTF-8（失败则原样返回）。"""
    try:
        return value.encode("latin1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def _key_to_mido(key: str) -> str:
    """项目写法（``"D minor"``）→ mido 写法（``"Dm"``）。"""
    text = str(key).strip()
    if not text:
        raise ParameterError("调性不能为空")
    parts = text.split()
    if len(parts) == 1:
        return text
    tonic, mode = parts[0], parts[1].lower()
    if mode.startswith("min"):
        return f"{tonic}m"
    if mode.startswith("maj"):
        return tonic
    raise ParameterError(f"无法识别的调式写法: {key!r}（应为 'D minor' / 'Bb major'）")


def _key_from_mido(key: str) -> str:
    """mido 写法（``"Dm"``）→ 项目写法（``"D minor"``）。"""
    text = str(key)
    if text.endswith("m"):
        return f"{text[:-1]} minor"
    return f"{text} major"


def _tick(beat: float, ppq: int) -> int:
    """拍 → tick（独立取整，避免长曲累计漂移）。"""
    return int(round(float(beat) * int(ppq)))


def _message_dict(message) -> dict:
    """mido 消息 → 可序列化字典（去掉 time）。"""
    data = message.dict()
    data.pop("time", None)
    return data


def _rebuild_message(data: dict):
    """字典 → mido 消息（通道消息优先，失败回落 meta）。"""
    payload = {key: value for key, value in data.items() if key != "time"}
    try:
        return mido.Message.from_dict(payload)
    except (LookupError, ValueError, TypeError):
        return mido.MetaMessage.from_dict(payload)


def _assemble(events: list[tuple[int, int, int, object]]) -> mido.MidiTrack:
    """按 (tick, 优先级, 插入序) 排序并转成 delta-time 的 mido 轨道。"""
    events.sort(key=lambda item: (item[0], item[1], item[2]))
    track = mido.MidiTrack()
    previous_tick = 0
    for tick, _priority, _sequence, message in events:
        track.append(message.copy(time=tick - previous_tick))
        previous_tick = tick
    return track


def _dedupe_by_beat(points: list) -> list:
    """同一 beat 的多个变化点只保留最后一个（读入外部文件时的容错）。"""
    ordered = sorted(points, key=lambda point: point.beat)
    result: list = []
    for point in ordered:
        if result and abs(point.beat - result[-1].beat) < 1e-9:
            result[-1] = point
        else:
            result.append(point)
    return result


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def _conductor_track(doc: MidiDocumentV2) -> mido.MidiTrack:
    """指挥轨：曲名 + 速度表 + 拍号表 + 调性表 + 段落标记 + 文档级原始事件。"""
    events: list[tuple[int, int, int, object]] = []
    sequence = 0

    def add(tick: int, priority: int, message) -> None:
        nonlocal sequence
        events.append((tick, priority, sequence, message))
        sequence += 1

    add(0, _PRIO_META, mido.MetaMessage("track_name", name=doc.title.strip() or "Conductor"))
    for point in doc.tempo_map:
        add(
            _tick(point.beat, doc.ppq),
            _PRIO_META,
            mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(point.bpm)),
        )
    for point in doc.meter_map:
        add(
            _tick(point.beat, doc.ppq),
            _PRIO_META,
            mido.MetaMessage(
                "time_signature",
                numerator=point.numerator,
                denominator=point.denominator,
            ),
        )
    for point in doc.key_map:
        add(
            _tick(point.beat, doc.ppq),
            _PRIO_META,
            mido.MetaMessage("key_signature", key=_key_to_mido(point.key)),
        )
    for beat, text in doc.markers:
        add(_tick(beat, doc.ppq), _PRIO_META, mido.MetaMessage("marker", text=text))
    for raw in doc.raw_events:
        add(_tick(raw.beat, doc.ppq), _PRIO_RAW, _rebuild_message(raw.message))
    return _assemble(events)


def _resolve_note_spans(
    notes: list[NoteEvent], ppq: int
) -> list[tuple[int, int, int, int, int]]:
    """把音符转成 (pitch, start_tick, end_tick, velocity, release)。

    同通道同音高的重叠处理（确定性）：先按 (pitch, start, -end) 排序；完全同起点
    的重复音只保留最长的一个；后音早于前音结束时，前音截断到后音起点。
    """
    spans: list[list[int]] = []
    for note in notes:
        start = _tick(note.start, ppq) + int(note.offset_ticks)
        end = _tick(note.end, ppq) + int(note.offset_ticks)
        start = max(0, start)
        end = max(start + 1, end)
        spans.append([int(note.pitch), start, end, int(note.velocity),
                      int(note.release_velocity)])

    spans.sort(key=lambda span: (span[0], span[1], -span[2]))
    resolved: list[list[int]] = []
    for span in spans:
        if resolved and resolved[-1][0] == span[0]:
            previous = resolved[-1]
            if span[1] <= previous[1]:
                continue
            if span[1] < previous[2]:
                previous[2] = span[1]
        resolved.append(span)
    return [
        (pitch, start, end, velocity, release)
        for pitch, start, end, velocity, release in resolved
        if end > start
    ]


def _instrument_track(track: MidiTrackV2, ppq: int) -> mido.MidiTrack:
    """乐器轨：音色库/音色/初始音量声像 + CC + 音符 + 原始事件。"""
    events: list[tuple[int, int, int, object]] = []
    sequence = 0

    def add(tick: int, priority: int, message) -> None:
        nonlocal sequence
        events.append((tick, priority, sequence, message))
        sequence += 1

    add(0, _PRIO_META, mido.MetaMessage("track_name", name=track.name))
    if track.bank_msb or track.bank_lsb:
        add(0, _PRIO_BANK, mido.Message(
            "control_change", channel=track.channel, control=0, value=track.bank_msb))
        add(0, _PRIO_BANK, mido.Message(
            "control_change", channel=track.channel, control=32, value=track.bank_lsb))
    add(0, _PRIO_PROGRAM, mido.Message(
        "program_change", channel=track.channel, program=track.program))
    if track.volume != 100:
        add(0, _PRIO_CC, mido.Message(
            "control_change", channel=track.channel, control=7, value=track.volume))
    if track.pan != 64:
        add(0, _PRIO_CC, mido.Message(
            "control_change", channel=track.channel, control=10, value=track.pan))

    for change in track.control_changes:
        add(
            _tick(change.beat, ppq),
            _PRIO_CC,
            mido.Message(
                "control_change",
                channel=track.channel,
                control=change.controller,
                value=change.value,
            ),
        )
    for raw in track.raw_events:
        add(_tick(raw.beat, ppq), _PRIO_RAW, _rebuild_message(raw.message))

    for pitch, start, end, velocity, release in _resolve_note_spans(track.notes, ppq):
        add(start, _PRIO_NOTE_ON, mido.Message(
            "note_on", channel=track.channel, note=pitch, velocity=velocity))
        add(end, _PRIO_NOTE_OFF, mido.Message(
            "note_off", channel=track.channel, note=pitch, velocity=release))

    return _assemble(events)


def write_smf(doc: MidiDocumentV2, path: str | Path) -> Path:
    """把文档写为 SMF format 1。

    Args:
        doc: 已构建的文档（内部会先 ``validate()``）。
        path: 输出路径（自动创建父目录）。

    Returns:
        解析后的绝对路径。

    Raises:
        ParameterError: 文档不合法或调性写法非法。
        ExportError: 文件写入失败（错误码 5）。
    """
    doc.validate()
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    # charset 必须在构造期指定：MidiFile.save() 内部会用 self.charset 再包一层
    # meta_charset，外部 with meta_charset(...) 会被覆盖（mido 1.3.3 行为）。
    midi = mido.MidiFile(type=1, ticks_per_beat=int(doc.ppq), charset="utf-8")
    midi.tracks.append(_conductor_track(doc))
    for track in doc.tracks:
        midi.tracks.append(_instrument_track(track, doc.ppq))

    try:
        midi.save(str(target))
    except (OSError, ValueError, TypeError) as exc:
        raise ExportError(f"MIDI 写入失败: {target} ({exc})", code=5) from exc
    return target


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def _new_state(name: str) -> dict:
    """单个通道的读入累积器。"""
    return {
        "name": name,
        "notes": [],
        "program": 0,
        "program_seen": False,
        "bank_msb": 0,
        "bank_lsb": 0,
        "volume": 100,
        "pan": 64,
        "ccs": [],
        "raw": [],
        "pending": {},
    }


def read_smf(path: str | Path) -> MidiDocumentV2:
    """读取 SMF（format 0/1）为文档对象。

    Args:
        path: .mid 路径。

    Returns:
        ``MidiDocumentV2``（保留文件 PPQ；缺失的时间表补默认值）。

    Raises:
        InputFileError: 文件不存在、无法解析，或为不支持的 format 2（错误码 3）。
    """
    target = Path(path).expanduser().resolve()
    if not target.is_file():
        raise InputFileError(f"MIDI 文件不存在: {target}", code=3)
    try:
        midi = mido.MidiFile(str(target))
    except Exception as exc:  # mido 对损坏文件抛多种异常
        raise InputFileError(f"无法解析 MIDI 文件: {target} ({exc})", code=3) from exc

    if midi.type == 2:
        raise InputFileError(
            f"不支持 SMF format 2（独立序列）: {target}；请先转换为 format 0/1", code=3
        )
    ppq = int(midi.ticks_per_beat)
    if ppq <= 0:
        raise InputFileError(f"MIDI 文件 PPQ 非法: {ppq}", code=3)

    tempo_points: list[TempoPoint] = []
    meter_points: list[MeterPoint] = []
    key_points: list[KeyPoint] = []
    markers: list[tuple[float, str]] = []
    doc_raw: list[RawEvent] = []
    states: dict[int, dict] = {}
    title = ""

    for mido_track in midi.tracks:
        absolute_tick = 0
        pending_name = ""
        touched: set[int] = set()
        for message in mido_track:
            absolute_tick += int(message.time)
            beat = absolute_tick / ppq

            if message.is_meta:
                if message.type == "track_name":
                    text = _recover_text(message.name)
                    if text:
                        pending_name = text
                        if not title and text.lower() != "conductor":
                            title = text
                elif message.type == "set_tempo":
                    tempo_points.append(TempoPoint(beat=beat, bpm=mido.tempo2bpm(message.tempo)))
                elif message.type == "time_signature":
                    meter_points.append(MeterPoint(
                        beat=beat,
                        numerator=int(message.numerator),
                        denominator=int(message.denominator),
                    ))
                elif message.type == "key_signature":
                    key_points.append(KeyPoint(beat=beat, key=_key_from_mido(message.key)))
                elif message.type == "marker":
                    markers.append((beat, _recover_text(message.text)))
                elif message.type != "end_of_track":
                    doc_raw.append(RawEvent(beat=beat, message=_message_dict(message)))
                continue

            if message.type == "sysex":
                doc_raw.append(RawEvent(beat=beat, message=_message_dict(message)))
                continue

            channel = getattr(message, "channel", None)
            if channel is None or message.type not in _CHANNEL_MESSAGES:
                doc_raw.append(RawEvent(beat=beat, message=_message_dict(message)))
                continue

            state = states.get(channel)
            if state is None:
                state = _new_state(pending_name)
                states[channel] = state
            elif pending_name and not state["name"]:
                state["name"] = pending_name
            touched.add(channel)

            if message.type == "program_change":
                if not state["program_seen"]:
                    state["program"] = int(message.program)
                    state["program_seen"] = True
                else:
                    state["raw"].append(RawEvent(beat=beat, message=_message_dict(message)))
            elif message.type == "control_change":
                if message.control == 0:
                    state["bank_msb"] = int(message.value)
                elif message.control == 32:
                    state["bank_lsb"] = int(message.value)
                elif message.control == 7 and absolute_tick == 0:
                    state["volume"] = int(message.value)
                elif message.control == 10 and absolute_tick == 0:
                    state["pan"] = int(message.value)
                else:
                    state["ccs"].append(ControlChange(
                        beat=beat, controller=int(message.control), value=int(message.value)))
            elif message.type == "note_on" and message.velocity > 0:
                state["pending"].setdefault(int(message.note), []).append(
                    (absolute_tick, int(message.velocity)))
            elif message.type in ("note_on", "note_off"):
                queue = state["pending"].get(int(message.note))
                if queue:
                    start_tick, velocity = queue.pop(0)
                    if absolute_tick > start_tick:
                        state["notes"].append(NoteEvent(
                            pitch=int(message.note),
                            start=start_tick / ppq,
                            duration=(absolute_tick - start_tick) / ppq,
                            velocity=velocity,
                            release_velocity=int(message.velocity),
                        ))
            else:  # pitchwheel / aftertouch / polytouch
                state["raw"].append(RawEvent(beat=beat, message=_message_dict(message)))

        # 收尾：本轨内未闭合的 note_on 用轨道末尾闭合
        for channel in touched:
            state = states[channel]
            for pitch, queue in state["pending"].items():
                for start_tick, velocity in queue:
                    if absolute_tick > start_tick:
                        state["notes"].append(NoteEvent(
                            pitch=pitch,
                            start=start_tick / ppq,
                            duration=(absolute_tick - start_tick) / ppq,
                            velocity=velocity,
                        ))
                queue.clear()
        if pending_name:
            for channel in touched:
                if not states[channel]["name"]:
                    states[channel]["name"] = pending_name

    tracks: list[MidiTrackV2] = []
    for channel, state in states.items():
        tracks.append(MidiTrackV2(
            name=state["name"] or f"Track {channel}",
            channel=channel,
            notes=sorted(state["notes"], key=lambda note: (note.start, note.pitch)),
            program=state["program"],
            bank_msb=state["bank_msb"],
            bank_lsb=state["bank_lsb"],
            is_drum=(channel == DRUM_CHANNEL),
            volume=state["volume"],
            pan=state["pan"],
            control_changes=sorted(state["ccs"], key=lambda change: change.beat),
            raw_events=sorted(state["raw"], key=lambda event: event.beat),
        ))

    tempo_map = _dedupe_by_beat(tempo_points)
    if not tempo_map or tempo_map[0].beat > 0:
        tempo_map.insert(0, TempoPoint(beat=0.0, bpm=120.0))
    meter_map = _dedupe_by_beat(meter_points)
    if not meter_map or meter_map[0].beat > 0:
        meter_map.insert(0, MeterPoint(beat=0.0, numerator=4, denominator=4))
    key_map = _dedupe_by_beat(key_points)
    if not key_map or key_map[0].beat > 0:
        key_map.insert(0, KeyPoint(beat=0.0, key="C major"))

    return MidiDocumentV2(
        tracks=tracks,
        tempo_map=tempo_map,
        meter_map=meter_map,
        key_map=key_map,
        markers=sorted(markers, key=lambda item: item[0]),
        raw_events=sorted(doc_raw, key=lambda event: event.beat),
        ppq=ppq,
        title=title or target.stem,
    )
