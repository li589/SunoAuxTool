"""MusicXML 4.0 导出（partwise），纯标准库、零第三方依赖。

为什么不用 music21
------------------
本项目的 ``generators/base.py`` 与 ``models/chords.py`` 确实依赖 music21，但导出层**不**
沿用它，理由有三：

1. **二次映射会丢信息**。music21 的时值模型是 ``quarterLength`` 浮点 + 写出时量化；
   而本项目的时值在 ``theory.duration_components`` 里已按记谱惯例**精确分解**
   （5 拍 = 全音符 + 四分音符，两者用延音线相连）。绕道 music21 等于把这份结论
   再猜一遍，量化误差会直接变成记谱错误。
2. **拼写/调号/谱号/延音线的决定权在本项目 IR 里**（``model`` + ``theory``）。
   直接序列化这些既成结论是无损的；重建一套 music21 对象树则是第二份事实来源。
3. **导入成本**。``music21`` 首次 import 约 1–3 秒。``png.py`` 已为 Pillow 立了
   「渲染器不该无条件拖重依赖」的规矩，导出器同理。

客观性说明：本模块只保证**结构合法 + 语义忠实**，不承诺通过外部 DTD/xsd 校验
（离线环境取不到 DTD）；``validate_musicxml`` 只做良构性与关键结构断言。

时间单位
--------
MusicXML 的 ``<duration>`` 是**整数**且以 ``<divisions>``（每四分音符的分割数）为单位。
本模块固定 ``divisions=16``：``DURATION_TABLE`` 里最短的 64 分音符（0.0625 拍）
恰好等于 1 个单位，故所有可记谱时值都是整数，不需要额外求 LCM。

输出确定性
----------
``encoding_date`` 默认为 None（不写 ``<encoding-date>``），以便同一输入两次导出
字节一致、测试可直接断言；需要时间戳时显式传入。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from sunoauxtool import __version__
from sunoauxtool.score.model import Score, ScoreMeasure, ScoreNote, ScoreTrack
from sunoauxtool.score.theory import (
    LETTERS,
    accidental_glyph,
    duration_components,
    key_accidentals,
    normalize_key,
    parse_time_signature,
    prefer_flats,
    spell_pitch,
)

__all__ = [
    "DIVISIONS",
    "MusicXmlOptions",
    "render_musicxml",
    "validate_musicxml",
    "write_musicxml",
]

#: 每四分音符的分割数；16 恰能整除 64 分音符（0.0625 拍 → 1 单位）
DIVISIONS = 16

#: MusicXML 版本与本模块声明的外部 DTD
#: 常见移调乐器的 diatonic 步数（MusicXML ``<transpose><diatonic>``）
_DIATONIC_STEPS = {2: 1, -2: -1, 3: 2, -3: -2, 5: 4, -5: -4, 7: 4, -7: -4,
                   12: 7, -12: -7}

MUSICXML_VERSION = "4.0"
_DOCTYPE = (
    '<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN" '
    '"http://www.musicxml.org/dtds/partwise.dtd">'
)

#: 变音记号数值 -> MusicXML ``<accidental>`` 文本
_ACCIDENTAL_NAMES: Dict[int, str] = {-1: "flat", 0: "natural", 1: "sharp"}

#: 谱号名 -> ``(sign, line)``；未列入者回落高音谱号
_CLEF_SIGNS: Dict[str, Tuple[str, str]] = {
    "treble": ("G", "2"),
    "bass": ("F", "4"),
    "alto": ("C", "3"),
    "tenor": ("C", "4"),
    "percussion": ("percussion", "2"),
}
_FALLBACK_CLEF: Tuple[str, str] = ("G", "2")

_EPS = 1e-6


# ---------------------------------------------------------------------------
# 选项
# ---------------------------------------------------------------------------

@dataclass
class MusicXmlOptions:
    """导出开关。"""

    include_metadata: bool = True
    """写 ``<work>/<identification>``（标题、作曲者、软件署名）。"""

    include_tempo: bool = True
    """首小节写速度记号（``<metronome>`` + ``<sound tempo>``）。"""

    include_midi_instruments: bool = True
    """``<part-list>`` 里写 ``<midi-instrument>``（通道/音色，1 基）。"""

    include_part_groups: bool = True
    """按 ``ScoreTrack.part_group`` 输出 ``<part-group>`` 括号（分谱组）。"""

    encoding_date: Optional[str] = None
    """``<encoding-date>``（``YYYY-MM-DD``）；None 表示不写，保证输出确定性。"""

    software: str = field(default_factory=lambda: f"SmartNoteGen {__version__}")
    """``<software>`` 文本。"""

    def __post_init__(self) -> None:
        if self.encoding_date is None:
            return  # 默认：不写日期，保证输出确定性
        text = str(self.encoding_date).strip()
        parts = text.split("-")
        if not text or len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise ValueError(f"非法 encoding_date: {self.encoding_date!r}（期望 YYYY-MM-DD）")
        self.encoding_date = text


# ---------------------------------------------------------------------------
# 顶层入口
# ---------------------------------------------------------------------------

def render_musicxml(score: Score, options: Optional[MusicXmlOptions] = None) -> str:
    """把 ``Score`` 导出为 MusicXML 4.0 partwise 文档字符串。

    Args:
        score: 已装配的乐谱（``Score.from_midi`` / ``from_sequence`` 等入口产出）。
        options: 导出开关；None 用默认值。

    Returns:
        完整 MusicXML 文本（声明 + DOCTYPE + 文档体，``\\n`` 换行）。

    Raises:
        ValueError: 拍号非法（由 theory 层抛出）。
    """
    opts = options or MusicXmlOptions()
    root = _build_root(score, opts)
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{_DOCTYPE}\n{body}\n'


def write_musicxml(
    score: Score, path: str | Path, options: Optional[MusicXmlOptions] = None
) -> str:
    """导出并落盘 ``.musicxml`` / ``.xml``，返回绝对路径。

    显式用 ``newline="\\n"`` 打开：``Path.write_text`` 在 Windows 上会把 ``\\n``
    翻译成 ``\\r\\n``，会让同一份乐谱在两平台产出不同字节。
    """
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    text = render_musicxml(score, options)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return str(target)


def validate_musicxml(text: str) -> ET.Element:
    """校验 MusicXML 文本良构且关键结构齐全，返回根元素。

    只做**离线可判定**的检查（真 DTD/XSD 校验需要网络取 DTD，不适合放进测试）：
    良构性、根标签与 ``version``、``<part-list>`` 声明的 part 数与实际 ``<part>``
    数量一致、每个 ``<part>`` 至少一个小节、每小节音符时值合计等于 ``<divisions>``
    与小节容量的乘积。

    Args:
        text: ``render_musicxml`` 的产物（或任何 MusicXML 文本）。

    Returns:
        解析后的根元素（``<score-partwise>``）。

    Raises:
        ValueError: 非良构、根标签不对、结构缺失或小节时值不闭合。
    """
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as exc:  # pragma: no cover - 正常渲染不会触发
        raise ValueError(f"MusicXML 不是良构 XML: {exc}") from exc

    if root.tag != "score-partwise":
        raise ValueError(f"MusicXML 根标签应为 'score-partwise'，实为 {root.tag!r}")

    version = root.get("version")
    if version != MUSICXML_VERSION:
        raise ValueError(f"MusicXML version 应为 {MUSICXML_VERSION!r}，实为 {version!r}")

    part_list = root.find("part-list")
    if part_list is None:
        raise ValueError("MusicXML 缺少 <part-list>")
    declared = [p.get("id") for p in part_list.findall("score-part")]
    parts = root.findall("part")
    actual = [p.get("id") for p in parts]
    if declared != actual:
        raise ValueError(f"<part-list> 声明 {declared} 与实际 <part> {actual} 不一致")

    for part in parts:
        measures = part.findall("measure")
        if not measures:
            raise ValueError(f"part {part.get('id')!r} 没有任何小节")
        divisions = _part_divisions(measures[0])
        if divisions <= 0:
            raise ValueError(f"part {part.get('id')!r} 的 <divisions> 非正: {divisions}")
        # 拍号只在首小节 <attributes> 里声明一次，之后沿用 —— 校验时必须跨小节携带
        ts = "4/4"
        for measure in measures:
            ts = _measure_ts(measure, default=ts)
            _check_measure_closes(measure, divisions, ts, part.get("id", ""))
    return root


# ---------------------------------------------------------------------------
# 文档骨架
# ---------------------------------------------------------------------------

def _build_root(score: Score, opts: MusicXmlOptions) -> ET.Element:
    """构建 ``<score-partwise>`` 根（含 work/identification/part-list/part）。"""
    root = ET.Element("score-partwise", {"version": MUSICXML_VERSION})

    if opts.include_metadata:
        work = ET.SubElement(root, "work")
        ET.SubElement(work, "work-title").text = score.title or "Untitled"
        identification = ET.SubElement(root, "identification")
        if score.composer:
            ET.SubElement(identification, "creator", {"type": "composer"}).text = score.composer
        encoding = ET.SubElement(identification, "encoding")
        ET.SubElement(encoding, "software").text = opts.software
        if opts.encoding_date:
            ET.SubElement(encoding, "encoding-date").text = opts.encoding_date

    part_list = ET.SubElement(root, "part-list")
    spans = _part_group_spans(score) if opts.include_part_groups else []
    starts = {start: (number, name) for number, (start, _end, name)
              in enumerate(spans, start=1)}
    ends = {end: number for number, (_start, end, _name) in enumerate(spans, start=1)}
    for index, track in enumerate(score.tracks):
        if index in starts:
            number, name = starts[index]
            marker = ET.SubElement(part_list, "part-group",
                                   {"type": "start", "number": str(number)})
            ET.SubElement(marker, "group-name").text = name
            ET.SubElement(marker, "group-symbol").text = "bracket"
        _build_score_part(part_list, track, index, opts)
        if index in ends:
            ET.SubElement(part_list, "part-group",
                          {"type": "stop", "number": str(ends[index])})

    for index, track in enumerate(score.tracks):
        root.append(_build_part(score, track, index, opts))
    return root


def _part_group_spans(score: Score) -> List[tuple[int, int, str]]:
    """扫描轨道序列，返回 ``[(首个下标, 末个下标, 组名)]``（只认连续同名）。"""
    spans: List[tuple[int, int, str]] = []
    start: int | None = None
    current = ""
    for index, track in enumerate(score.tracks):
        name = str(getattr(track, "part_group", "") or "")
        if name and name == current and start is not None:
            spans[-1] = (spans[-1][0], index, name)
            continue
        if name:
            start = index
            current = name
            spans.append((index, index, name))
        else:
            start = None
            current = ""
    return spans


def _build_score_part(
    part_list: ET.Element, track: ScoreTrack, index: int, opts: MusicXmlOptions
) -> None:
    """``<score-part>``：part-name / part-abbreviation / midi-instrument。"""
    pid = _part_id(index)
    node = ET.SubElement(part_list, "score-part", {"id": pid})
    name = track.name or f"Part {index + 1}"
    ET.SubElement(node, "part-name").text = name
    ET.SubElement(node, "part-abbreviation").text = _abbreviate(name)
    if opts.include_midi_instruments:
        instrument = ET.SubElement(node, "midi-instrument", {"id": f"{pid}-I1"})
        # MusicXML 的通道/音色是 1 基，本项目内部是 0 基
        ET.SubElement(instrument, "midi-channel").text = str(_clamp(track.channel + 1, 1, 16))
        ET.SubElement(instrument, "midi-program").text = str(_clamp(track.program + 1, 1, 128))


def _build_part(
    score: Score, track: ScoreTrack, index: int, opts: MusicXmlOptions
) -> ET.Element:
    """``<part>``：逐小节输出属性（仅首小节）与音符。"""
    part = ET.Element("part", {"id": _part_id(index)})
    flats = prefer_flats(score.key)
    key_acc = key_accidentals(score.fifths)

    measures = track.measures or [
        ScoreMeasure(index=1, start_beat=0.0, duration_beats=score.measure_capacity)
    ]
    for measure in measures:
        node = ET.SubElement(part, "measure", {"number": str(measure.index)})
        if measure.index == 1:
            _emit_attributes(node, score, track)
            if opts.include_tempo:
                _emit_tempo(node, score.bpm)
        _emit_expression(node, track, measure.index, _to_divisions(measure.duration_beats))
        _emit_measure_notes(node, measure, track, key_acc=key_acc, flats=flats)
    return part


# ---------------------------------------------------------------------------
# 属性 / 速度
# ---------------------------------------------------------------------------

def _emit_attributes(node: ET.Element, score: Score, track: ScoreTrack) -> None:
    """首小节的 ``<attributes>``：divisions / key / time / clef。"""
    attributes = ET.SubElement(node, "attributes")
    ET.SubElement(attributes, "divisions").text = str(DIVISIONS)

    _tonic, mode = normalize_key(score.key)
    key = ET.SubElement(attributes, "key")
    ET.SubElement(key, "fifths").text = str(score.fifths)
    ET.SubElement(key, "mode").text = mode

    num, den = parse_time_signature(score.time_signature)
    time = ET.SubElement(attributes, "time")
    ET.SubElement(time, "beats").text = str(num)
    ET.SubElement(time, "beat-type").text = str(den)

    sign, line = _clef_signs(track)
    clef = ET.SubElement(attributes, "clef")
    ET.SubElement(clef, "sign").text = sign
    ET.SubElement(clef, "line").text = line

    transpose = int(getattr(track, "transpose", 0) or 0)
    if transpose:
        node = ET.SubElement(attributes, "transpose")
        if _DIATONIC_STEPS.get(transpose) is not None:
            ET.SubElement(node, "diatonic").text = str(_DIATONIC_STEPS[transpose])
        ET.SubElement(node, "chromatic").text = str(transpose)


def _emit_tempo(node: ET.Element, bpm: int) -> None:
    """首小节的 ``<direction>``：节拍器记号 + ``<sound tempo>``。"""
    direction = ET.SubElement(node, "direction", {"placement": "above"})
    direction_type = ET.SubElement(direction, "direction-type")
    metronome = ET.SubElement(direction_type, "metronome")
    ET.SubElement(metronome, "beat-unit").text = "quarter"
    ET.SubElement(metronome, "per-minute").text = str(int(bpm))
    ET.SubElement(direction, "sound", {"tempo": str(int(bpm))})


def _emit_expression(
    node: ET.Element, track: ScoreTrack, measure_index: int, divisions: int
) -> None:
    """小节开头的力度记号与渐变楔形（``<direction>``）。

    位置约定：``<direction>`` 出现在小节第一个 ``<note>`` 之前，即"小节起点"。
    唯一例外是**楔形收口**——它必须落在段落最后一小节的**小节末**，因此带
    ``<offset>=divisions``（``<direction>`` 自身不推进时间游标，offset 是相对
    当前位置的延后量）。若把 stop 放在下一小节开头，导入器会把记号画到下一小节里，
    跨段落时就会串到别人的谱面上。
    """
    dynamic = track.dynamics.get(measure_index)
    if dynamic:
        direction = ET.SubElement(node, "direction", {"placement": "below"})
        direction_type = ET.SubElement(direction, "direction-type")
        marks = ET.SubElement(direction_type, "dynamics")
        ET.SubElement(marks, dynamic)  # <mf/> / <p/> …（元素名即力度记号）

    wedge = track.wedges.get(measure_index)
    if wedge:
        direction = ET.SubElement(node, "direction", {"placement": "below"})
        direction_type = ET.SubElement(direction, "direction-type")
        ET.SubElement(direction_type, "wedge", {"type": wedge, "number": "1"})

    if track.wedge_ends.get(measure_index):
        direction = ET.SubElement(node, "direction", {"placement": "below"})
        direction_type = ET.SubElement(direction, "direction-type")
        ET.SubElement(direction_type, "wedge", {"type": "stop", "number": "1"})
        ET.SubElement(direction, "offset").text = str(int(divisions))


def _clef_signs(track: ScoreTrack) -> Tuple[str, str]:
    """轨道的 MusicXML 谱号；打击轨恒用 percussion 谱号。"""
    if track.is_drum:
        return _CLEF_SIGNS["percussion"]
    return _CLEF_SIGNS.get(track.clef, _FALLBACK_CLEF)


# ---------------------------------------------------------------------------
# 小节内音符
# ---------------------------------------------------------------------------

def _emit_measure_notes(
    node: ET.Element,
    measure: ScoreMeasure,
    track: ScoreTrack,
    *,
    key_acc: Dict[int, int],
    flats: bool,
) -> None:
    """把一个小节的所有声部写成 ``<note>`` 序列（含 ``<backup>`` / ``<forward>`` / ``<chord/>``）。

    两个关键约定：

    **① 声部游标**。每个声部都从**小节起点**写起，声部之间用 ``<backup>`` 退回起点；
    声部自身不足一小节时用 ``<forward>`` 补齐到小节容量。退避量取「本声部已写时长」
    而非小节容量 —— 多声部轨不补休止符，用容量硬退会把游标推到小节起点之前
    （非法且被导入器拒绝）。

    **② 同时发声组（和弦）必须先按时值分量铺开**。MusicXML 的 ``<note>`` 序列是严格的
    时间序；和弦的 ``<chord/>`` 成员「不推进时间」是相对**紧邻的前一个非和弦音**而言的。
    因此不能「逐个音符输出它的全部分量」——那会把第二个和弦成员的分量挂到第一个成员的
    末段上（音区错位、时值溢出小节）。正确做法见 :func:`_emit_group`：先按时值分量
    切片，再在每一片里输出该组的所有成员。
    """
    by_voice: Dict[int, List[ScoreNote]] = {}
    for note in measure.notes:
        by_voice.setdefault(note.voice, []).append(note)

    capacity_beats = measure.duration_beats
    if not by_voice:
        # 空小节（多声部轨不补休止符，或整轨无音符）：必须补一个整小节休止符。
        # 否则该小节一个 <note> 都没有，时间游标恒为 0 —— 小节不闭合，导入器会报错。
        silent = ScoreNote(
            pitch=0,
            start=measure.start_beat,
            duration=capacity_beats,
            voice=0,
            measure=measure.index,
            is_rest=True,
        )
        _emit_group(
            node, group=[silent], voice=1, track=track, key_acc=key_acc, flats=flats
        )
        return

    capacity = _to_divisions(capacity_beats)
    cursor = 0  # 本小节游标（divisions），从起点算起
    for order, voice in enumerate(sorted(by_voice)):
        if order > 0:
            backup = ET.SubElement(node, "backup")
            ET.SubElement(backup, "duration").text = str(cursor)
        cursor = 0
        for group in _voice_groups(by_voice[voice]):
            cursor += _emit_group(
                node,
                group=group,
                voice=voice + 1,
                track=track,
                key_acc=key_acc,
                flats=flats,
            )
        if cursor < capacity:
            forward = ET.SubElement(node, "forward")
            ET.SubElement(forward, "duration").text = str(capacity - cursor)
            cursor = capacity


def _voice_groups(notes: Sequence[ScoreNote]) -> List[List[ScoreNote]]:
    """按起音把同声部音符聚成「同时发声组」。

    组内成员共享起音（和弦，同一符干）；休止符不与实音同组（记谱上休止符不可能
    是和弦成员）。组的时值取组首成员——``model._assign_voices`` 是按
    「同起音 + 同时值」聚组再分声部的，故同组时值必然一致。
    """
    ordered = sorted(notes, key=lambda n: (n.start, n.is_rest, n.pitch))
    groups: List[List[ScoreNote]] = []
    for note in ordered:
        if groups:
            head = groups[-1][0]
            if abs(note.start - head.start) <= _EPS and note.is_rest == head.is_rest:
                groups[-1].append(note)
                continue
        groups.append([note])
    groups.sort(key=lambda g: (g[0].start, g[0].is_rest))
    return groups


def _emit_group(
    node: ET.Element,
    *,
    group: Sequence[ScoreNote],
    voice: int,
    track: ScoreTrack,
    key_acc: Dict[int, int],
    flats: bool,
) -> int:
    """输出一个同时发声组，返回推进的时长（divisions）。

    先把组的时值用 ``theory.duration_components`` 分解为可记谱分量（相邻分量需延音
    线相连），再**分量外层、成员内层**地输出：第 i 片的每个成员各自成一个 ``<note>``，
    除首成员外都带 ``<chord/>``（故不推进时间），整组只在片末推进一次。
    """
    head = group[0]
    components = duration_components(head.duration)
    if not components:
        return 0

    last = len(components) - 1
    advanced = 0
    for index, (note_type, dots, value) in enumerate(components):
        # 分量边界一律连延音线；首片接上游、末片接下家
        tie_stop = index > 0 or head.tie_from_prev
        tie_start = index < last or head.tie_to_next
        duration = _to_divisions(value)
        for member_index, member in enumerate(group):
            _emit_note(
                node,
                note=member,
                note_type=note_type,
                dots=dots,
                duration=duration,
                voice=voice,
                is_chord=member_index > 0,
                tie_start=tie_start,
                tie_stop=tie_stop,
                track=track,
                key_acc=key_acc,
                flats=flats,
            )
        advanced += duration
    return advanced


def _emit_note(
    node: ET.Element,
    *,
    note: ScoreNote,
    note_type: str,
    dots: int,
    duration: int,
    voice: int,
    is_chord: bool,
    tie_start: bool,
    tie_stop: bool,
    track: ScoreTrack,
    key_acc: Dict[int, int],
    flats: bool,
) -> None:
    """写一个 ``<note>`` 元素（元素次序遵循 MusicXML 的 note 复合类型序列）。"""
    step, octave, accidental = _spell(note, flats, track.transpose)
    # 变音记号按「调号是否已覆盖」决定：调号覆盖时不写 <accidental>（<alter> 仍照写）
    glyph = (
        accidental_glyph(LETTERS.index(step), accidental, key_acc)
        if not note.is_rest and not track.is_drum
        else None
    )

    element = ET.SubElement(node, "note")
    if is_chord:
        ET.SubElement(element, "chord")
    if note.is_rest:
        ET.SubElement(element, "rest")
    elif track.is_drum:
        unpitched = ET.SubElement(element, "unpitched")
        ET.SubElement(unpitched, "display-step").text = step
        ET.SubElement(unpitched, "display-octave").text = str(octave)
    else:
        pitch = ET.SubElement(element, "pitch")
        ET.SubElement(pitch, "step").text = step
        if accidental:
            ET.SubElement(pitch, "alter").text = str(accidental)
        ET.SubElement(pitch, "octave").text = str(octave)

    ET.SubElement(element, "duration").text = str(duration)
    # <tie> 顺序（stop 在前）与 MuseScore 等主流写出器一致
    if tie_stop:
        ET.SubElement(element, "tie", {"type": "stop"})
    if tie_start:
        ET.SubElement(element, "tie", {"type": "start"})
    ET.SubElement(element, "voice").text = str(voice)
    ET.SubElement(element, "type").text = note_type
    for _ in range(dots):
        ET.SubElement(element, "dot")
    if glyph is not None:
        ET.SubElement(element, "accidental").text = _ACCIDENTAL_NAMES[glyph]
    mark = "" if note.is_rest else _articulation_element(note.articulation)
    if tie_stop or tie_start or mark:
        notations = ET.SubElement(element, "notations")
        if tie_stop:
            ET.SubElement(notations, "tied", {"type": "stop"})
        if tie_start:
            ET.SubElement(notations, "tied", {"type": "start"})
        if mark:
            ET.SubElement(
                ET.SubElement(notations, "articulations"), mark
            )


#: 演奏法 → MusicXML ``<articulations>`` 元素名（首次使用时从 compose.expression 同步）
_ARTICULATION_ELEMENTS: Optional[Dict[str, str]] = None


def _articulation_element(name: str) -> str:
    """演奏法名 → ``<articulations>`` 子元素名（未知/无记号返回空串）。

    映射表的唯一真源是 ``compose.expression.ARTICULATIONS``（含 marcato→strong-accent
    这类义变名）；这里只在首次调用时读一次并缓存，避免逐音符跨包 import。
    """
    global _ARTICULATION_ELEMENTS
    if _ARTICULATION_ELEMENTS is None:
        from sunoauxtool.compose.expression import ARTICULATIONS

        _ARTICULATION_ELEMENTS = {
            key: spec.musicxml for key, spec in ARTICULATIONS.items() if spec.musicxml
        }
    return _ARTICULATION_ELEMENTS.get(str(name or "").strip().lower(), "")


def _spell(note: ScoreNote, flats: bool, transpose: int = 0) -> Tuple[str, int, int]:
    """音高 -> ``(step, octave, alter)``。

    移调乐器按**记谱音高**写出（实音 + ``transpose``），休止符返回占位 ``("C", 4, 0)``。
    """
    if note.is_rest:
        return "C", 4, 0
    letter, diatonic, accidental = spell_pitch(note.pitch + int(transpose), flats)
    return LETTERS[letter], diatonic // 7, accidental


def _to_divisions(quarters: float) -> int:
    """拍数 -> MusicXML ``<duration>`` 整数（四舍五入到最近单位）。"""
    return int(round(quarters * DIVISIONS))


# ---------------------------------------------------------------------------
# 校验辅助
# ---------------------------------------------------------------------------

def _part_id(index: int) -> str:
    """0 基轨序号 -> ``"P1"`` / ``"P2"``（MusicXML id 惯例）。"""
    return f"P{index + 1}"


def _abbreviate(name: str) -> str:
    """part-abbreviation：取首字母（中文名原样，长度 > 1 时也不截断成空）。"""
    text = (name or "").strip()
    if not text:
        return "P"
    return text if not text[0].isascii() else text[0].upper()


def _clamp(value: int, low: int, high: int) -> int:
    """夹到 ``[low, high]``（MIDI 通道/音色越界时不让 XML 非法）。"""
    return max(low, min(high, int(value)))


def _part_divisions(first_measure: ET.Element) -> int:
    """从小节里取 ``<divisions>``；缺失返回 0（由调用方判为非正）。"""
    attributes = first_measure.find("attributes")
    if attributes is None:
        return 0
    node = attributes.find("divisions")
    if node is None or node.text is None or not node.text.strip().isdigit():
        return 0
    return int(node.text.strip())


def _check_measure_closes(
    measure: ET.Element, divisions: int, time_signature: str, part_id: str
) -> None:
    """校验小节「时间游标」闭合：净推进量 = 小节容量（divisions 单位）。

    游标规则与 MusicXML 一致：``<note>`` 推进 ``<duration>``（``<chord/>`` 成员不推进），
    ``<backup>`` 回退 ``<duration>``，``<forward>`` 推进 ``<duration>``。
    """
    cursor = 0
    max_cursor = 0
    for child in measure:
        if child.tag == "note":
            if child.find("chord") is not None:
                continue
            cursor += _duration_of(child)
        elif child.tag == "backup":
            cursor -= _duration_of(child)
            if cursor < 0:
                raise ValueError(
                    f"part {part_id!r} 第 {measure.get('number')} 小节 <backup> 越界（游标为负）"
                )
        elif child.tag == "forward":
            cursor += _duration_of(child)
        max_cursor = max(max_cursor, cursor)

    if cursor != max_cursor:
        raise ValueError(
            f"part {part_id!r} 第 {measure.get('number')} 小节时间游标未回到最远处"
            f"（末 {cursor} / 峰 {max_cursor}）"
        )
    if max_cursor <= 0:
        raise ValueError(f"part {part_id!r} 第 {measure.get('number')} 小节没有任何音符时值")

    num, den = parse_time_signature(time_signature)
    expected = _to_divisions(num * 4.0 / den)
    if expected != max_cursor:
        raise ValueError(
            f"part {part_id!r} 第 {measure.get('number')} 小节时值合计 {max_cursor} "
            f"≠ 容量 {expected}（divisions，拍号 {time_signature}）"
        )


def _duration_of(node: ET.Element) -> int:
    """读取 ``<note>/<backup>/<forward>`` 的 ``<duration>``；缺失按 0。"""
    child = node.find("duration")
    if child is None or child.text is None or not child.text.strip().lstrip("-").isdigit():
        return 0
    return int(child.text.strip())


def _measure_ts(measure: ET.Element, *, default: str = "4/4") -> str:
    """本小节生效的拍号：``<attributes><time>`` 优先，否则沿用 ``default``。"""
    attributes = measure.find("attributes")
    if attributes is not None:
        time = attributes.find("time")
        if time is not None:
            beats = time.find("beats")
            beat_type = time.find("beat-type")
            if beats is not None and beat_type is not None:
                return f"{beats.text}/{beat_type.text}"
    return default
