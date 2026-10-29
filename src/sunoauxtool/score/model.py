"""乐谱中间表示（IR）：把 MidiDocument / NoteSequence 解析为可排版的多轨谱面数据。

设计要点
--------
1. **时间单位统一为「拍」**（四分音符），与 ``models/notes.py`` 一致；
   一小节容量 = ``numerator * 4 / denominator`` 拍。
2. **构建期即按小节线切开**：跨小节的长音被拆成若干带延音线（tie）的片段，
   排版层因此只需处理「完全落在单一小节内」的音符，不必重复处理小节线逻辑。
3. **每轨独立做声部指派**：按起音时间贪心分层（低音先入 0 号声部），
   供排版层决定符干方向；单声部轨则按音高与中线关系决定。
4. **单声部轨自动补休止符**：多声部轨不补（避免与另一声部视觉互相穿插）。
5. 调号/拼写/时值分解全部委托 ``score/theory.py``，本层只消费结论。

典型用法::

    score = Score.from_midi("x.mid", key="a minor", time_signature="4/4")
    layout = layout_score(score)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from sunoauxtool.score.theory import (
    beats_per_measure,
    diatonic_number,
    duration_components,
    key_fifths,
    normalize_key,
)

_EPS = 1e-6

#: 补休止符的最小空隙（拍）；小于此值视为演奏噪声，不补
REST_MIN_GAP = 0.125


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class ScoreNote:
    """谱面上的一个音符（或休止符）。

    Attributes:
        pitch: MIDI 音高；休止符该值无意义（置 0）。
        start: 全曲起始拍（四分音符计）。
        duration: 时值（拍）。
        velocity: 力度（用于 MusicXML 的 dynamics）。
        voice: 轨内声部号（0 起，偶数偏下、奇数偏上）。
        measure: 所属小节号（1 起）。
        beat_in_measure: 小节内起始拍。
        tie_from_prev / tie_to_next: 是否与相邻片段用延音线相连（小节切分产物）。
        is_rest: 是否休止符。
    """

    pitch: int
    start: float
    duration: float
    velocity: int = 64
    voice: int = 0
    measure: int = 1
    beat_in_measure: float = 0.0
    tie_from_prev: bool = False
    tie_to_next: bool = False
    is_rest: bool = False
    articulation: str = ""
    """演奏法（staccato/accent/…），空表示无记号。"""

    @property
    def beat_end(self) -> float:
        """结束拍。"""
        return self.start + self.duration

    def overlaps(self, other: "ScoreNote") -> bool:
        """两音是否在时间上重叠（休止符不参与）。"""
        if self.is_rest or other.is_rest:
            return False
        return self.start < other.beat_end - _EPS and other.start < self.beat_end - _EPS


@dataclass
class ScoreMeasure:
    """一个小节（承载该轨落在本小节内的音符）。"""

    index: int
    start_beat: float
    duration_beats: float
    notes: List[ScoreNote] = field(default_factory=list)

    def notes_of_voice(self, voice: int) -> List[ScoreNote]:
        """取某声部的音符。"""
        return [n for n in self.notes if n.voice == voice]

    @property
    def sounding(self) -> List[ScoreNote]:
        """非休止音符。"""
        return [n for n in self.notes if not n.is_rest]


@dataclass
class ScoreTrack:
    """一条谱表（一个声部／乐器）。"""

    name: str
    program: int = 0
    channel: int = 0
    is_drum: bool = False
    clef: str = "treble"
    voices: int = 1
    transpose: int = 0
    """移调（半音）。正值表示记谱音高比实音高（如 Bb 单簧管 +2）。"""
    part_group: str = ""
    """分谱组名（同名的连续声部在 MusicXML 里用括号连成一个 part-group）。"""
    brace: str = ""
    """大谱表分组标记（2.6）：非空表示与**相邻同标记**的谱表组成一件乐器
    （如钢琴两行谱）——SVG/PNG 画花括号、MusicXML 用 ``symbol="brace"`` 的
    part-group 连接。当前只支持相邻两行。"""
    notes: List[ScoreNote] = field(default_factory=list)
    measures: List[ScoreMeasure] = field(default_factory=list)
    dynamics: Dict[int, str] = field(default_factory=dict)
    """力度记号：``{小节号: "mf"}``（在对应小节开头写 ``<dynamics>``）。"""
    wedges: Dict[int, str] = field(default_factory=dict)
    """渐变楔形：``{小节号: "crescendo"|"diminuendo"}``（起点）。"""
    wedge_ends: Dict[int, bool] = field(default_factory=dict)
    """楔形收口小节：``{小节号: True}``。"""

    @property
    def is_transposing(self) -> bool:
        """是否为移调乐器。"""
        return int(self.transpose) != 0

    def written_pitch(self, sounding: int) -> int:
        """实音 → 记谱音高（移调乐器记谱更高）。"""
        return int(sounding) + int(self.transpose)

    @property
    def pitch_range(self) -> Tuple[int, int]:
        """(最低音, 最高音)；空轨返回 (60, 60)。"""
        pitches = [n.pitch for n in self.notes if not n.is_rest]
        return (min(pitches), max(pitches)) if pitches else (60, 60)


@dataclass
class Score:
    """整首乐谱。"""

    title: str = "Untitled"
    composer: str = ""
    bpm: int = 120
    key: str = "C major"
    time_signature: str = "4/4"
    bars: int = 0
    tracks: List[ScoreTrack] = field(default_factory=list)

    # -- 派生属性 ---------------------------------------------------------

    @property
    def measure_capacity(self) -> float:
        """小节容量（拍）。"""
        return beats_per_measure(self.time_signature)

    @property
    def fifths(self) -> int:
        """调号升降号数。"""
        return key_fifths(self.key)

    @property
    def total_beats(self) -> float:
        """整曲总拍数。"""
        return self.bars * self.measure_capacity

    def duration_seconds(self) -> float:
        """整曲时长（秒）。"""
        return self.total_beats * 60.0 / self.bpm

    @property
    def note_count(self) -> int:
        """全部实音（不含休止符）数量。"""
        return sum(1 for t in self.tracks for n in t.notes if not n.is_rest)

    def measure_labels(self) -> List[int]:
        """小节号序列。"""
        return list(range(1, self.bars + 1))

    def to_dict(self) -> Dict[str, object]:
        """转为可 JSON 序列化的摘要（不展开逐音符）。"""
        return {
            "title": self.title,
            "composer": self.composer,
            "bpm": self.bpm,
            "key": self.key,
            "time_signature": self.time_signature,
            "bars": self.bars,
            "fifths": self.fifths,
            "note_count": self.note_count,
            "tracks": [
                {
                    "name": t.name,
                    "clef": t.clef,
                    "program": t.program,
                    "is_drum": t.is_drum,
                    "voices": t.voices,
                    "notes": len([n for n in t.notes if not n.is_rest]),
                    "range": list(t.pitch_range),
                }
                for t in self.tracks
            ],
        }

    # -- 构造入口 ---------------------------------------------------------

    @classmethod
    def from_midi(
        cls,
        path: str | Path,
        *,
        title: Optional[str] = None,
        composer: str = "",
        key: str = "C major",
        time_signature: str = "4/4",
        bars: Optional[int] = None,
        clefs: Optional[Dict[str, str]] = None,
    ) -> "Score":
        """从 .mid 文件构建乐谱。

        Args:
            path: .mid 路径。
            title: 标题；None 时取文件名主干。
            composer: 作曲者署名。
            key: 调式（任意写法，内部归一化）。
            time_signature: 拍号，如 ``"4/4"``。
            bars: 小节数；None 时按最后一个音自动推算。
            clefs: 可选，``{轨道名: "treble"|"bass"}`` 覆盖自动谱号判定。

        Returns:
            Score 实例。

        Raises:
            InputFileError: 文件缺失或无法解析（错误码 3）。
        """
        from sunoauxtool.models.midi import MidiDocument

        target = Path(path).expanduser().resolve()
        doc = MidiDocument.load(target)
        return cls.from_document(
            doc,
            title=title if title is not None else target.stem,
            composer=composer,
            key=key,
            time_signature=time_signature,
            bars=bars,
            clefs=clefs,
        )

    @classmethod
    def from_document(
        cls,
        doc,
        *,
        title: str = "Untitled",
        composer: str = "",
        key: str = "C major",
        time_signature: str = "4/4",
        bars: Optional[int] = None,
        clefs: Optional[Dict[str, str]] = None,
    ) -> "Score":
        """从 MidiDocument 构建乐谱。

        Args:
            doc: ``models.midi.MidiDocument``。
            title / composer / key / time_signature / bars / clefs: 同 ``from_midi``。

        Returns:
            Score 实例。

        Raises:
            ValueError: 拍号非法。
        """
        raw = [
            (
                tr.name or f"track{t_idx + 1}",
                int(getattr(tr, "program", 0)),
                int(getattr(tr, "channel", 0)),
                bool(getattr(tr, "is_drum", False)),
                list(tr.notes),
            )
            for t_idx, tr in enumerate(doc.tracks)
        ]
        last_beat = 0.0
        for *_meta, notes in raw:
            for n in notes:
                last_beat = max(last_beat, float(n.start) + float(n.duration))
        return cls._assemble(
            raw_tracks=raw,
            title=title,
            composer=composer,
            key=key,
            time_signature=time_signature,
            bars=bars,
            bpm=int(getattr(doc, "bpm", 120) or 120),
            clefs=clefs,
            last_beat=last_beat,
        )

    @classmethod
    def from_song(
        cls,
        song,
        *,
        title: Optional[str] = None,
        composer: str = "",
        bars: Optional[int] = None,
        clefs: Optional[Dict[str, str]] = None,
        part_groups: bool = True,
        grand_staff: bool = True,
    ) -> "Score":
        """从 2.0 的 ``compose.Song`` 构建乐谱（多轨 + 移调 + 分谱组 + 大谱表）。

        与 ``from_document`` 的区别：Song 自带速度表/拍号表/调性/段落，
        且每轨有 ``role``/``transpose``，因此这里能直出**记谱用**的谱面元数据。

        Args:
            song: ``compose.song.Song``（已编排）。
            title: 标题；None 用 ``song.title``。
            composer: 作曲者署名。
            bars: 小节数；None 用 Song 的小节数（含弱起前的段首对齐）。
            clefs: ``{轨道名: "treble"|"bass"}`` 覆盖自动谱号判定。
            part_groups: 是否按角色把同族乐器归入同一 part-group（弦乐组/节奏组）。
            grand_staff: 钢琴/键盘家族轨（harmony/arpeggio + GM 0..11）自动拆成
                两行大谱表（右手 ≥60 / 左手 <60，brace 连接）；2.6 起默认开。

        Returns:
            Score 实例（``transpose``/``part_group``/``brace`` 已填好）。
        """
        from sunoauxtool.score.theory import beats_per_measure

        song.validate()  # 谱面只接受已编排完成的曲子（空曲/半成品应尽早失败）
        meter = song.meter_map[0]
        time_signature = f"{meter.numerator}/{meter.denominator}"
        raw: List[Tuple[str, int, int, bool, List[object]]] = []
        owners: List[object] = []      # raw[i] 来自哪个 song 轨（大谱表拆分后共享）
        brace_marks: List[str] = []    # raw[i] 的大谱表标记（"" = 独行谱表）
        for track in song.tracks:
            notes = [note for clip in track.clips for note in clip.notes]
            program = int(track.instrument.resolved_program())
            name = track.name or track.id
            if grand_staff and _is_piano_family(track, program) and notes:
                upper, lower = _split_hands(notes)
                raw.append((name, program, int(track.channel or 0), False, upper))
                brace_marks.append("piano")
                owners.append(track)
                raw.append((name, program, int(track.channel or 0), False, lower))
                brace_marks.append("piano")
                owners.append(track)
            else:
                raw.append((name, program, int(track.channel or 0),
                            bool(track.is_drum), notes))
                brace_marks.append("")
                owners.append(track)
        last_beat = 0.0
        for *_meta, notes in raw:
            for note in notes:
                last_beat = max(last_beat, float(note.start) + float(note.duration))
        score = cls._assemble(
            raw_tracks=raw,
            title=title if title is not None else (song.title or "Untitled"),
            composer=composer,
            key=song.key_map[0].key,
            time_signature=time_signature,
            bars=bars,
            bpm=int(round(song.tempo_map[0].bpm)),
            clefs=clefs,
            last_beat=last_beat,
            capacity=beats_per_measure(time_signature),
        )
        for score_track, track, brace in zip(score.tracks, owners, brace_marks):
            score_track.transpose = int(getattr(track, "transpose", 0) or 0)
            if part_groups:
                score_track.part_group = _group_of(track.role)
            if brace:
                score_track.brace = brace
            _apply_expression_marks(
                score_track, song, beats_per_measure(time_signature)
            )
        return score

    def parts(self) -> List["Score"]:
        """拆成单声部分谱（保留标题/调号/拍号/小节数/移调）。"""
        result: List[Score] = []
        for track in self.tracks:
            result.append(Score(
                title=f"{self.title} - {track.name}",
                composer=self.composer,
                bpm=self.bpm,
                key=self.key,
                time_signature=self.time_signature,
                bars=self.bars,
                tracks=[track],
            ))
        return result

    @classmethod
    def from_sequence(
        cls,
        seq,
        *,
        title: str = "Untitled",
        composer: str = "",
        bars: Optional[int] = None,
        clefs: Optional[Dict[str, str]] = None,
    ) -> "Score":
        """从领域层 NoteSequence 构建乐谱（bpm/key/拍号/小节数直接沿用）。

        Args:
            seq: ``models.notes.NoteSequence``。
            title / composer / bars / clefs: 同上。

        Returns:
            Score 实例。
        """
        capacity = beats_per_measure(seq.time_signature)
        raw = [
            (
                t.name or f"track{i + 1}",
                int(t.program),
                int(t.channel),
                int(t.channel) == 9,
                list(t.notes),
            )
            for i, t in enumerate(seq.tracks)
        ]
        return cls._assemble(
            raw_tracks=raw,
            title=title,
            composer=composer,
            key=seq.key,
            time_signature=seq.time_signature,
            bars=bars if bars is not None else int(seq.bars),
            bpm=int(seq.bpm),
            clefs=clefs,
            last_beat=float(seq.total_beats()),
            capacity=capacity,
        )

    # -- 内部装配 ---------------------------------------------------------

    @classmethod
    def _assemble(
        cls,
        *,
        raw_tracks: Sequence[Tuple[str, int, int, bool, List[object]]],
        title: str,
        composer: str,
        key: str,
        time_signature: str,
        bars: Optional[int],
        bpm: int,
        clefs: Optional[Dict[str, str]],
        last_beat: float,
        capacity: Optional[float] = None,
    ) -> "Score":
        """把「原始轨列表」装配为完整 Score（各入口共用）。"""
        cap = capacity if capacity is not None else beats_per_measure(time_signature)
        if cap <= 0:  # pragma: no cover - theory 层已拒绝非正拍号
            raise ValueError(f"非法小节容量: {cap}")

        # 小节数：显式传入优先；否则按最后落音向上取整，至少 1 小节
        if bars is None or bars <= 0:
            bars = max(1, int(math.ceil(last_beat / cap - _EPS)))

        tracks: List[ScoreTrack] = []
        for name, program, channel, is_drum, notes in raw_tracks:
            track = _build_track(
                name=name,
                program=program,
                channel=channel,
                is_drum=is_drum,
                notes=notes,
                capacity=cap,
                bars=bars,
                clefs=clefs,
            )
            tracks.append(track)

        tonic, mode = normalize_key(key)
        normalized_key = f"{tonic} {mode}"
        return cls(
            title=title,
            composer=composer,
            bpm=bpm,
            key=normalized_key,
            time_signature=time_signature,
            bars=bars,
            tracks=tracks,
        )


# ---------------------------------------------------------------------------
# 单轨构建
# ---------------------------------------------------------------------------

#: 编曲角色 → 分谱组（MusicXML part-group 括号）
_GROUP_BY_ROLE = {
    "harmony": "键盘组",
    "pad": "弦乐组",
    "arpeggio": "键盘组",
    "bass": "低音组",
    "melody": "旋律组",
    "counter": "旋律组",
    "drums": "节奏组",
    "percussion": "节奏组",
}


def _group_of(role: str) -> str:
    """角色 → 分谱组名（未知角色不分组）。"""
    return _GROUP_BY_ROLE.get(str(role), "")


#: 大谱表适用角色：只有"键盘式织体"的轨才拆两行（旋律/贝斯有各自谱表）
_GRAND_STAFF_ROLES = frozenset({"harmony", "arpeggio"})
#: GM 钢琴/键盘家族音色号（0 大钢琴 .. 11 电颤琴）
_GRAND_STAFF_PROGRAM_MAX = 11
#: 左右手分界（中央 C 归右手）
_GRAND_STAFF_SPLIT_PITCH = 60


def _is_piano_family(track, program: int) -> bool:
    """键盘家族判定：织体角色 + GM 钢琴音色段 + 非鼓轨。"""
    if getattr(track, "is_drum", False):
        return False
    if str(getattr(track, "role", "")) not in _GRAND_STAFF_ROLES:
        return False
    return 0 <= program <= _GRAND_STAFF_PROGRAM_MAX


def _split_hands(notes: Sequence[object], split_pitch: int = _GRAND_STAFF_SPLIT_PITCH
                 ) -> Tuple[List[object], List[object]]:
    """按音高把音符分到右手（≥split_pitch）/左手（<split_pitch）。

    顺序保持各自稳定（上下两行都按时间排序交给布局层）；空手在记谱侧
    会自动补整小节休止符。
    """
    upper = [n for n in notes if int(n.pitch) >= split_pitch]
    lower = [n for n in notes if int(n.pitch) < split_pitch]
    return upper, lower


#: 段落渐变 → MusicXML ``<wedge>`` 类型
_WEDGE_TYPE = {"cresc": "crescendo", "dim": "diminuendo"}


def _apply_expression_marks(track: ScoreTrack, song, capacity: float) -> None:
    """把段落级力度/渐变落成谱面记号（按小节号登记到 ``ScoreTrack``）。

    两条刻意保留的克制：

    1. **只在该轨真正发声的段落打记号**。段落是全局的，但某轨可能整段休息
       （如 pad 只在副歌进），在空白小节上写 ``<dynamics>`` 会悬在空中、
       还会让 crescendo 楔形跨过一堆休止符；
    2. **打击轨不写**。鼓谱的力度靠节奏层表达，写 p/ff 既不合惯例也无音高意义。
    """
    from sunoauxtool.compose.expression import dynamic_for_energy

    if track.is_drum:
        return
    sounding = {n.measure for n in track.notes if not n.is_rest}
    for section in getattr(song, "sections", ()) or ():
        first = int(round(float(section.start) / capacity)) + 1
        last = first + int(section.bars) - 1
        if first < 1 or not any(m in sounding for m in range(first, last + 1)):
            continue
        track.dynamics[first] = section.dynamic or dynamic_for_energy(section.energy)
        ramp = str(getattr(section, "ramp", "") or "")
        wedge = _WEDGE_TYPE.get(ramp)
        if wedge:
            track.wedges[first] = wedge
            track.wedge_ends[last] = True


def _build_track(
    *,
    name: str,
    program: int,
    channel: int,
    is_drum: bool,
    notes: Sequence[object],
    capacity: float,
    bars: int,
    clefs: Optional[Dict[str, str]],
) -> ScoreTrack:
    """把一条 MIDI 轨的原始音符列表装配为 ScoreTrack。"""
    # 1) 原始音符 -> ScoreNote（尚未分小节）
    raw_notes: List[ScoreNote] = []
    for n in notes:
        duration = float(n.duration)
        if duration <= _EPS:
            continue  # 零时值音符在记谱上无意义，丢弃
        raw_notes.append(
            ScoreNote(
                pitch=int(n.pitch),
                start=float(n.start),
                duration=duration,
                velocity=int(getattr(n, "velocity", 64)),
                # 演奏法只影响记谱记号；旧模型（NoteSequence）没有该字段，故用 getattr
                articulation=str(getattr(n, "articulation", "") or ""),
            )
        )

    # 2) 声部指派（重叠分层）——先于小节切分，保证同一音的片段声部一致
    voice_count = _assign_voices(raw_notes)

    # 3) 按小节线切分，并打上小节号 / 小节内拍位 / 延音线标记
    fragments: List[ScoreNote] = []
    for note in raw_notes:
        fragments.extend(_split_at_barlines(note, capacity, bars))

    # 4) 单声部轨补休止符（多声部不补，避免与另一声部视觉穿插）
    if voice_count <= 1:
        fragments = _fill_rests(fragments, capacity, bars)

    # 5) 归入小节
    measures: List[ScoreMeasure] = []
    by_measure: Dict[int, List[ScoreNote]] = {}
    for frag in fragments:
        by_measure.setdefault(frag.measure, []).append(frag)
    for idx in range(1, bars + 1):
        measure_notes = sorted(
            by_measure.get(idx, []),
            key=lambda n: (n.voice, n.is_rest, n.start, n.pitch),
        )
        measures.append(
            ScoreMeasure(
                index=idx,
                start_beat=(idx - 1) * capacity,
                duration_beats=capacity,
                notes=measure_notes,
            )
        )

    clef = _infer_clef(name, raw_notes, clefs)
    return ScoreTrack(
        name=name,
        program=program,
        channel=channel,
        is_drum=is_drum,
        clef=clef,
        voices=max(1, voice_count),
        notes=sorted(fragments, key=lambda n: (n.start, n.voice, n.pitch)),
        measures=measures,
    )


def _assign_voices(notes: Sequence[ScoreNote]) -> int:
    """就地给音符打上声部号，返回声部数量。

    贪心策略：先按「同起音 + 同时值」聚成**和弦组**（同符干，必须同声部），
    再按 (起音, 最低音) 升序把和弦组放进第一个「此刻无音在响」的声部；
    都占满则新开一个声部。低音先入 0 号声部，与「0 号在下、1 号在上」的
    排版约定一致。

    先聚和弦组再分层很关键：否则一个三音和弦会被当成三个互相重叠的音，
    拆成三个声部，符干方向随即自相矛盾。
    """
    # 量化到 1/64 拍，吸收浮点噪声；tolerance 约 ±1/128 拍
    groups: Dict[Tuple[int, int], List[ScoreNote]] = {}
    for note in notes:
        key = (round(note.start * 64), round(note.duration * 64))
        groups.setdefault(key, []).append(note)

    ordered = sorted(
        groups.values(),
        key=lambda g: (round(g[0].start, 6), min(n.pitch for n in g)),
    )

    voices: List[List[ScoreNote]] = []
    for group in ordered:
        for idx, voice in enumerate(voices):
            if not any(a.overlaps(b) for a in group for b in voice):
                voice.extend(group)
                for n in group:
                    n.voice = idx
                break
        else:
            for n in group:
                n.voice = len(voices)
            voices.append(list(group))
    return max(1, len(voices))


def _split_at_barlines(note: ScoreNote, capacity: float, bars: int) -> List[ScoreNote]:
    """把跨小节的长音切为若干片段，并标注延音线方向。

    同音高片段之间才叫「延音线」（tie）；这里只处理同音切分，故一定是 tie。
    音符越过声明小节数时按记谱惯例**截断**，并把末片的 ``tie_to_next`` 清掉 ——
    延续端不存在，留着它会外泄成孤立的「tie start」（MusicXML 里是非法的开弧）。
    """
    out: List[ScoreNote] = []
    cursor = note.start
    remaining = note.duration
    total = note.duration
    guard = 0
    while remaining > _EPS and guard < 4096:
        guard += 1
        measure = int(cursor // capacity) + 1
        if measure > bars:
            break  # 超出声明的小节数，截断（记谱上无容身处）
        measure_end = measure * capacity
        span = min(remaining, measure_end - cursor)
        out.append(
            ScoreNote(
                pitch=note.pitch,
                start=cursor,
                duration=span,
                velocity=note.velocity,
                voice=note.voice,
                measure=measure,
                beat_in_measure=cursor - (measure - 1) * capacity,
                tie_from_prev=bool(out),                       # 非首片
                tie_to_next=remaining - span > _EPS,           # 后面还有
                # 记号只挂在音头所在片段上：延音线之后的片段不该重复写 staccato
                articulation=note.articulation if not out else "",
            )
        )
        cursor += span
        remaining -= span
    if out and remaining > _EPS:
        out[-1].tie_to_next = False   # 被截断：没有延续端，不留悬空的延音线起点
    if not out:  # 完全越界（起点已在曲末之外）——退化为一个最短片段，避免丢音
        out.append(
            ScoreNote(
                pitch=note.pitch,
                start=note.start,
                duration=min(total, capacity),
                velocity=note.velocity,
                voice=note.voice,
                measure=bars,
                beat_in_measure=0.0,
                articulation=note.articulation,
            )
        )
    return out


def _fill_rests(
    notes: Sequence[ScoreNote], capacity: float, bars: int
) -> List[ScoreNote]:
    """为单声部轨补休止符（小节内空隙 + 整小节空小节）。"""
    by_measure: Dict[int, List[ScoreNote]] = {}
    for n in notes:
        by_measure.setdefault(n.measure, []).append(n)

    out: List[ScoreNote] = []
    for idx in range(1, bars + 1):
        measure_start = (idx - 1) * capacity
        sounding = sorted(
            (n for n in by_measure.get(idx, []) if not n.is_rest),
            key=lambda n: n.start,
        )
        if not sounding:
            # 整小节休止：一个全小节休止符（时值 = 小节容量，由排版层转成正/全休止）
            out.append(_make_rest(measure_start, capacity, idx, capacity))
            continue
        cursor = measure_start
        for n in sounding:
            if n.start - cursor > REST_MIN_GAP - _EPS:
                out.extend(_rests_for_gap(cursor, n.start, idx, capacity))
            cursor = max(cursor, n.beat_end)
        if measure_start + capacity - cursor > REST_MIN_GAP - _EPS:
            out.extend(_rests_for_gap(cursor, measure_start + capacity, idx, capacity))
    return list(notes) + out


def _rests_for_gap(
    start: float, end: float, measure: int, capacity: float
) -> List[ScoreNote]:
    """把一段空隙分解为若干可记谱休止符。"""
    rests: List[ScoreNote] = []
    cursor = start
    for _note_type, _dots, value in duration_components(end - start):
        rests.append(_make_rest(cursor, value, measure, capacity))
        cursor += value
    return rests


def _make_rest(
    start: float, duration: float, measure: int, capacity: float
) -> ScoreNote:
    """构造一个休止符（``is_rest=True``，pitch 置 0 无意义）。"""
    return ScoreNote(
        pitch=0,
        start=start,
        duration=duration,
        velocity=0,
        voice=0,
        measure=measure,
        beat_in_measure=start - (measure - 1) * capacity,
        is_rest=True,
    )


#: 谱表最下线音级序号：高音谱号 E4=30，低音谱号 G2=18（相邻线差 2 个音级）
_TREBLE_REF = 30
_BASS_REF = 18
#: 五线谱覆盖的音级跨度（最下线至最上线）
_STAFF_SPAN = 8


def _clef_overflow(ref: int, pitches: Sequence[int]) -> int:
    """该套谱号下，全部音符超出五线范围的总音级距离（越小越合适）。"""
    total = 0
    for pitch in pitches:
        step = diatonic_number(pitch) - ref
        if step < 0:
            total += -step
        elif step > _STAFF_SPAN:
            total += step - _STAFF_SPAN
    return total


def _infer_clef(
    name: str, notes: Sequence[ScoreNote], clefs: Optional[Dict[str, str]]
) -> str:
    """推断谱号：显式覆盖 > 轨道名约定 > 溢出距离取小。

    为什么不用中位音高：钢琴/键盘轨常跨三个八度，中位数会被多数音拖到中间，
    一个含低音持续音的高音域轨会被判成低音谱号。改为「哪个谱号的越界距离总和更小」
    后，判定由整条音高分布决定，对宽音域轨同样稳健。
    """
    if clefs and name in clefs:
        return clefs[name]

    lowered = (name or "").lower()
    if any(tag in lowered for tag in ("bass", "bassline", "低音")):
        return "bass"
    if any(tag in lowered for tag in ("melody", "lead", "旋律", "solo")):
        return "treble"

    pitches = [n.pitch for n in notes if not n.is_rest]
    if not pitches:
        return "treble"
    return (
        "bass"
        if _clef_overflow(_BASS_REF, pitches) < _clef_overflow(_TREBLE_REF, pitches)
        else "treble"
    )
