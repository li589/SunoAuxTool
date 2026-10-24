"""谱面排版引擎：把 Score 摊成可直接绘制的几何布局（纯 Python，零第三方依赖）。

排版分四步
----------
1. **记谱展开**：把每个 ScoreNote 按时值分解成「音符类型 + 附点数」，
   分解出的多段用延音线相连；跨小节已由 model 层切好，这里不再处理小节线。
2. **时间点归并**：同起音的所有事件（含多声部）合并为一个时间点，
   共享同一个 x —— 否则和弦/对位音的符头会横向错开。
3. **行断开**：对每小节的「自然宽度」跑一次动态规划（TeX 式 badness 最小化），
   目标不是塞满而是**各行疏密均衡**，避免出现「前几行挤满、末行只剩两小节」。
4. **行内分配**：小节宽度按自然宽度比例摊到该行可用宽度（上限 1.35 倍，防拉伸失真）；
   音符 x 用弹簧模型 ``gap = 0.55 + 1.25 * √间隔`` 分配，符合光学间距直觉。

坐标系约定
----------
- 页面坐标，原点左上，y 向下为正。
- ``space`` = 相邻谱线间距。五线谱总高 ``4 * space``。
- 音级偏移 ``step_offset`` 以**最下线**为 0，单位为「半个 space」（线=偶数，间=奇数），
  故 ``y = 谱表最下线 y - step_offset * space / 2``。

局限（有意为之，已在 docs 记录）
--------------------------------
- 鼓轨按普通有音高谱表渲染（不做打击乐谱号 / 鼓位映射）。
- 多声部轨不补休止符（只对单声部轨补），避免两个声部的休止符视觉穿插。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from sunoauxtool.score.model import Score, ScoreMeasure, ScoreNote, ScoreTrack
from sunoauxtool.score.theory import (
    accidental_glyph,
    beat_unit,
    duration_components,
    key_accidentals,
    note_beams,
    parse_time_signature,
    prefer_flats,
    spell_pitch,
)

_EPS = 1e-6

#: 五线谱的线数 / 线间格数
STAFF_LINES = 5
STAFF_SPACES = STAFF_LINES - 1

# ---------------------------------------------------------------------------
# 签名区（谱号 + 调号 + 拍号）横向占位
# ---------------------------------------------------------------------------
# 这些常量属于**排版**职责而非渲染职责：它们决定每个系统的缩进，
# 缩进不够时第一小节的变音记号会压到谱号上。svg 层直接复用同一组常量。

#: 谱号横向占位（单位 space）
CLEF_WIDTH: Dict[str, float] = {"treble": 3.05, "bass": 3.35}
#: 单个调号变音记号的横向步进（单位 space）。取 1.0 而非 0.8：
#: 升号字形宽约 0.90、降号约 0.75，步进 0.8 会让相邻记号相贴。
ACCIDENTAL_ADVANCE = 1.00
#: 谱号 / 调号 / 拍号之间的间隙（单位 space）
SIGNATURE_PAD = 0.35
#: 拍号占位（单位 space）
TIME_SIG_WIDTH = 2.15
#: 签名区与第一小节之间的最小间隙（单位 space）
SIGNATURE_GUTTER = 1.10


def signature_area_width(
    clef: str, fifths: int, space: float, *, with_time_signature: bool
) -> float:
    """谱号 + 调号 [+ 拍号] 区的横向占位（像素）。

    Args:
        clef: ``"treble"`` / ``"bass"``。
        fifths: 调号升降号数。
        space: 谱线间距。
        with_time_signature: 是否含拍号（仅首系统需要）。

    Returns:
        占位宽度（像素）。
    """
    width = CLEF_WIDTH.get(clef, CLEF_WIDTH["treble"]) * space
    width += abs(fifths) * ACCIDENTAL_ADVANCE * space
    if with_time_signature:
        width += TIME_SIG_WIDTH * space
    return width + SIGNATURE_GUTTER * space


def required_indent(score: Score, space: float, *, with_time_signature: bool) -> float:
    """该乐谱需要的系统缩进 = 各谱表签名区占位的最大值。"""
    if not score.tracks:
        return 0.0
    return max(
        signature_area_width(
            t.clef, score.fifths, space, with_time_signature=with_time_signature
        )
        for t in score.tracks
    )


# ---------------------------------------------------------------------------
# 排版选项
# ---------------------------------------------------------------------------

@dataclass
class LayoutOptions:
    """排版参数（改这里就能整体调版式，不必碰算法）。"""

    page_width: float = 1240.0
    margin: float = 52.0
    #: 相邻谱线间距（全部纵向尺寸的基准单位）
    space: float = 9.0
    #: 系统（一行谱表组）之间的额外间距
    system_gap: float = 26.0
    #: 同一系统内相邻谱表之间的间距
    staff_gap: float = 22.0
    #: 首行缩进（留给谱号 + 调号 + 拍号）
    first_indent: float = 86.0
    #: 后续行的缩进（只需留谱号）
    system_indent: float = 62.0
    #: 小节自然宽度下限
    min_measure_width: float = 0.0  # 0 表示按 7.6 * space 计算
    #: 单小节最大拉伸倍数
    max_stretch: float = 1.35
    #: 预留的标题区高度
    title_height: float = 118.0

    @property
    def content_width(self) -> float:
        """单行可用宽度（不含首行缩进）。"""
        return self.page_width - 2 * self.margin

    def measure_floor(self) -> float:
        """小节自然宽度下限。"""
        return self.min_measure_width if self.min_measure_width > 0 else 7.6 * self.space


# ---------------------------------------------------------------------------
# 布局产物
# ---------------------------------------------------------------------------

@dataclass
class LaidNote:
    """谱面上单个符头（含休止符）。"""

    note: ScoreNote
    step_offset: int
    accidental: Optional[int] = None
    ledger_steps: List[int] = field(default_factory=list)

    @property
    def is_rest(self) -> bool:
        """是否休止符（无符头）。"""
        return self.note.is_rest


@dataclass
class LaidCluster:
    """同一时间点、同一声部的符头集合（和弦共用一个符干）。"""

    x: float
    start: float
    duration: float
    measure: int
    voice: int
    note_type: str
    dots: int
    beams: int
    stem_up: bool
    stem_len: float
    notes: List[LaidNote] = field(default_factory=list)
    is_rest: bool = False
    full_measure: bool = False
    beam_group: Optional[int] = None
    tie_from_prev: bool = False
    tie_to_next: bool = False

    @property
    def flags(self) -> int:
        """未被连杠时需要的符尾数。"""
        return self.beams if self.beam_group is None else 0

    @property
    def step_offsets(self) -> List[int]:
        """全部符头的音级偏移。"""
        return [n.step_offset for n in self.notes]


@dataclass
class LaidMeasure:
    """一个小节在某一谱表上的全部内容与槽位几何。"""

    index: int                       # 1 起
    x: float
    width: float
    natural_width: float
    start_beat: float
    is_system_start: bool = False
    clusters: List[LaidCluster] = field(default_factory=list)

    @property
    def right(self) -> float:
        """小节右边界 x（即该小节结束的小节线）。"""
        return self.x + self.width


@dataclass
class LaidStaff:
    """一个系统内的一条谱表。"""

    track_index: int
    name: str
    clef: str
    y: float                     # 最下线 y
    measures: List[LaidMeasure] = field(default_factory=list)

    def line_y(self, index: int, space: float) -> float:
        """第 index 条线（0 = 最上线）的 y。"""
        return self.y - (STAFF_SPACES - index) * space


@dataclass
class System:
    """一个系统：若干小节，纵向叠放全部谱表。"""

    index: int
    y: float                     # 系统内容顶部
    height: float
    space: float
    staffs: List[LaidStaff] = field(default_factory=list)

    @property
    def first_measure(self) -> int:
        """本系统首个小节号。"""
        return self.staffs[0].measures[0].index if self.staffs and self.staffs[0].measures else 0

    @property
    def last_measure(self) -> int:
        """本系统末个小节号。"""
        return self.staffs[0].measures[-1].index if self.staffs and self.staffs[0].measures else 0

    @property
    def staff_count(self) -> int:
        return len(self.staffs)


@dataclass
class ScoreLayout:
    """整份乐谱的排版结果（渲染层的唯一输入）。"""

    score: Score
    options: LayoutOptions
    systems: List[System] = field(default_factory=list)
    page_width: float = 0.0
    page_height: float = 0.0
    fifths: int = 0
    key_acc: Dict[int, int] = field(default_factory=dict)
    prefer_flats: bool = False
    note_count: int = 0

    @property
    def space(self) -> float:
        """谱线间距。"""
        return self.options.space

    @property
    def measure_slots(self) -> List[Tuple[int, float, float]]:
        """``[(小节号, x, 宽度)]``（以第一个谱表为准，各谱表槽位一致）。"""
        out: List[Tuple[int, float, float]] = []
        for system in self.systems:
            if not system.staffs:
                continue
            for m in system.staffs[0].measures:
                out.append((m.index, m.x, m.width))
        return out

    def cluster_at(self, measure: int, beat: float, tol: float = 1e-6) -> Optional[LaidCluster]:
        """按小节号 + 起拍定位簇（供 sunoauxtool.video 滚动谱面 / 测试断言用）。"""
        for system in self.systems:
            for staff in system.staffs:
                for m in staff.measures:
                    if m.index != measure:
                        continue
                    for c in m.clusters:
                        if abs(c.start - beat) <= tol:
                            return c
        return None

    def to_dict(self) -> Dict[str, object]:
        """轻量摘要（不含逐音符几何），供 JSON 与预览页使用。"""
        return {
            "title": self.score.title,
            "key": self.score.key,
            "time_signature": self.score.time_signature,
            "bpm": self.score.bpm,
            "bars": self.score.bars,
            "systems": len(self.systems),
            "page": [round(self.page_width, 1), round(self.page_height, 1)],
            "tracks": [t.name for t in self.score.tracks],
            "note_count": self.note_count,
        }


# ---------------------------------------------------------------------------
# 中间草稿结构（排版第一步的产物）
# ---------------------------------------------------------------------------

@dataclass
class _Event:
    """一个「已记谱」的事件：可能是和弦，也可能是休止符。"""

    start: float
    duration: float
    note_type: str
    dots: int
    voice: int
    notes: List[ScoreNote]
    is_rest: bool = False
    tie_from_prev: bool = False
    tie_to_next: bool = False

    @property
    def beams(self) -> int:
        """符杠/符尾数；休止符不参与连杠，恒为 0。"""
        return 0 if self.is_rest else note_beams(self.note_type)


@dataclass
class _TimePoint:
    """同一时刻的全部事件（跨声部合并）。"""

    start: float
    events: List[_Event] = field(default_factory=list)
    gap: float = 0.0          # 到下一个时间点的间隔（拍），用于弹簧模型


@dataclass
class _MeasureDraft:
    """单轨单小节的排版草稿。"""

    index: int
    start_beat: float
    duration_beats: float
    time_points: List[_TimePoint] = field(default_factory=list)
    natural_width: float = 0.0

    @property
    def is_empty(self) -> bool:
        return not self.time_points


# ---------------------------------------------------------------------------
# 第一步：记谱展开
# ---------------------------------------------------------------------------

def _expand_note(note: ScoreNote) -> List[_Event]:
    """把一个 ScoreNote 按时值分解为若干带延音线的事件。"""
    parts = duration_components(note.duration)
    if not parts:  # pragma: no cover - model 层已过滤非正时值
        return []
    out: List[_Event] = []
    cursor = note.start
    for i, (note_type, dots, value) in enumerate(parts):
        out.append(
            _Event(
                start=cursor,
                duration=value,
                note_type=note_type,
                dots=dots,
                voice=note.voice,
                notes=[note],
                is_rest=note.is_rest,
                # 片段自身已标记的延音线，或分解出的后续片段 → 都要连
                tie_from_prev=note.tie_from_prev or i > 0,
                tie_to_next=note.tie_to_next or i < len(parts) - 1,
            )
        )
        cursor += value
    return out


def _draft_measure(track: ScoreTrack, measure: ScoreMeasure, space: float) -> _MeasureDraft:
    """把一个小节展开为时间点序列，并估算自然宽度。"""
    draft = _MeasureDraft(
        index=measure.index,
        start_beat=measure.start_beat,
        duration_beats=measure.duration_beats,
    )

    # 整小节休止（唯一事件且占满小节）单独标记，渲染成整个小节休止符
    sounding = [n for n in measure.notes if not n.is_rest]
    rests = [n for n in measure.notes if n.is_rest]
    full_rest = (
        not sounding
        and len(rests) == 1
        and abs(rests[0].duration - measure.duration_beats) < 1e-3
    )

    events: List[_Event] = []
    for note in measure.notes:
        if note.is_rest and full_rest:
            events.append(
                _Event(
                    start=note.start,
                    duration=note.duration,
                    note_type="whole",
                    dots=0,
                    voice=note.voice,
                    notes=[note],
                    is_rest=True,
                )
            )
        else:
            events.extend(_expand_note(note))

    if not events:
        draft.natural_width = _measure_floor_width(space)
        return draft

    # 按起音归并为时间点
    buckets: Dict[int, _TimePoint] = {}
    for ev in events:
        key = round(ev.start * 64)
        tp = buckets.get(key)
        if tp is None:
            tp = _TimePoint(start=ev.start)
            buckets[key] = tp
        tp.events.append(ev)
    points = [buckets[k] for k in sorted(buckets)]

    # 事件内部排序：休止符在前，再按声部、音高
    for tp in points:
        tp.events.sort(
            key=lambda e: (
                e.is_rest,
                e.voice,
                min((n.pitch for n in e.notes if not n.is_rest), default=0),
            )
        )

    # 时间间隔（末尾间隔 = 小节容量 - 末起拍）
    for i, tp in enumerate(points):
        nxt = points[i + 1].start if i + 1 < len(points) else measure.duration_beats
        tp.gap = max(0.0, nxt - tp.start)

    draft.time_points = points
    draft.natural_width = _natural_width(points, space)
    return draft


def _measure_floor_width(space: float) -> float:
    """小节宽度下限。"""
    return 7.6 * space


def _spring_weight(gap: float, space: float) -> float:
    """弹簧权重：间隔越长占位越多，但按平方根收敛（光学间距经验式）。"""
    return space * (0.55 + 1.25 * math.sqrt(max(gap, 0.0) + 1e-9))


def _natural_width(points: Sequence[_TimePoint], space: float) -> float:
    """小节自然宽度 = 左右内边距 + 各时间点弹簧权重之和。"""
    total = 1.7 * space + 1.15 * space  # 左边（含变音记号位）+ 右边（符尾余量）
    for tp in points:
        total += _spring_weight(tp.gap, space)
    return max(total, _measure_floor_width(space))


# ---------------------------------------------------------------------------
# 第三步：行断开（DP）
# ---------------------------------------------------------------------------

def _break_systems(
    widths: Sequence[float],
    available: float,
    first_indent: float,
) -> List[Tuple[int, int]]:
    """动态规划求「疏密最均衡」的行断开方案。

    代价函数（TeX 式 badness）：
      - 非末行：``1000 * ((avail - used)/avail)^3`` —— 三次方让极空行代价迅速升高
      - 末行：``200 * ((avail - used)/avail)^2`` —— 末行宽松是常规版式，惩罚更轻
      - 超宽行：叠加 5000 常量惩罚；但**允许**单小节自成一行（否则无解）

    Args:
        widths: 各小节自然宽度。
        available: 单行可用宽度（不含缩进）。
        first_indent: 首行缩进（首行少这么多宽度）。

    Returns:
        ``[(起, 止), ...]`` 半开区间列表；无小节时返回空列表。
    """
    n = len(widths)
    if n == 0:
        return []

    prefix = [0.0] * (n + 1)
    for i, w in enumerate(widths):
        prefix[i + 1] = prefix[i] + w

    inf = float("inf")
    best = [inf] * (n + 1)
    choice = [-1] * (n + 1)
    best[0] = 0.0

    for j in range(1, n + 1):
        for i in range(j - 1, -1, -1):
            avail = available - (first_indent if i == 0 else 0.0)
            if avail <= 0:  # pragma: no cover - 缩进大于页宽时才会出现
                avail = 1.0
            used = prefix[j] - prefix[i]
            overfull = used > avail + _EPS
            if overfull and i < j - 1:
                break  # 再多加小节只会更宽，无解

            ratio = (avail - used) / avail
            if overfull:
                cost = 5000.0 + 3000.0 * (-ratio)
            elif j == n:
                cost = 200.0 * ratio * ratio
            else:
                cost = 1000.0 * ratio ** 3

            total = best[i] + cost
            if total < best[j]:
                best[j] = total
                choice[j] = i

    spans: List[Tuple[int, int]] = []
    cursor = n
    guard = 0
    while cursor > 0 and guard < n + 2:
        guard += 1
        prev = choice[cursor]
        if prev < 0:  # pragma: no cover - DP 保证可达
            spans.append((0, cursor))
            break
        spans.append((prev, cursor))
        cursor = prev
    spans.reverse()
    return spans


def _distribute_widths(
    naturals: Sequence[float],
    available: float,
    stretch_cap: Optional[float],
) -> List[float]:
    """把 ``available`` 摊到各小节：按自然宽度比例放大，单小节封顶 ``stretch_cap``。

    Args:
        naturals: 各小节自然宽度。
        available: 该行可用宽度。
        stretch_cap: 单小节最大放大倍数；``None`` 或 ``<= 1`` 表示不拉伸
            （用于多行曲子的末行——末行齐左是常规版式）。

    Returns:
        与 ``naturals`` 等长的宽度列表。
    """
    if not naturals:
        return []
    if stretch_cap is None or stretch_cap <= 1.0:
        return list(naturals)

    total = sum(naturals)
    if total <= 0:  # pragma: no cover
        return list(naturals)

    slack = available - total
    if slack <= 0:
        # 超宽：等比例压缩（不断行时唯一选择）
        factor = available / total
        return [w * factor for w in naturals]

    widths = list(naturals)
    remaining = slack
    free = set(range(len(widths)))
    for _ in range(6):
        if remaining <= _EPS or not free:
            break
        weight_sum = sum(widths[i] for i in free)
        if weight_sum <= 0:  # pragma: no cover
            break
        progressed = False
        for i in list(free):
            share = remaining * (widths[i] / weight_sum)
            cap = naturals[i] * stretch_cap - widths[i]
            if share >= cap:
                widths[i] = naturals[i] * stretch_cap
                remaining -= cap
                free.discard(i)
                progressed = True
        if not progressed:
            for i in free:
                widths[i] += remaining * (widths[i] / weight_sum)
            remaining = 0.0
    return widths


# ---------------------------------------------------------------------------
# 第四步：行内定位 + 符干/连杠/变音记号
# ---------------------------------------------------------------------------

def _place_points(points: Sequence[_TimePoint], space: float, usable: float) -> List[float]:
    """弹簧模型求各时间点的相对 x（0 起，已缩放到 ``usable`` 内）。"""
    if not points:
        return []
    offsets = [0.0]
    for i in range(1, len(points)):
        offsets.append(
            offsets[-1] + (_spring_weight(points[i - 1].gap, space) + _spring_weight(points[i].gap, space)) / 2.0
        )
    span = offsets[-1]
    if span <= _EPS:
        return [0.0 for _ in points]
    scale = usable / span
    return [o * scale for o in offsets]


def _cluster_pitch_span(events: Sequence[_Event], note_map: Dict[int, int]) -> Tuple[int, int]:
    """一个时间点内实音的音级偏移范围；全休止返回 (4, 4)。"""
    steps = [note_map[id(n)] for e in events if not e.is_rest for n in e.notes]
    if not steps:
        return 4, 4
    return min(steps), max(steps)


def _beams_for_measure(
    clusters: Sequence[Tuple[float, int, int]],
    beat_size: float,
    counter: List[int],
) -> List[Optional[int]]:
    """给一个小时内已合并的簇分配连杠组号。

    规则：同一「拍」（复合拍为附点拍）内、**同声部同符尾数**的连续短音符成一组；
    声部或符尾数变化即断组（两 16 分 + 两 8 分 → 两组、各自符杠数正确）；
    休止符与长音符打断连杠。

    注意这里消费的是**已合并的簇**而非原始事件：一个和弦在簇层只有一个符干，
    若按事件计数会出现「组内只有 1 个符干」的空连杠。

    Args:
        clusters: ``[(相对小节的起拍, 声部, 符尾数)]``，按时间升序。
        beat_size: 拍长（四分音符数）。
        counter: 单元素列表，承载全局组号计数（就地自增）。

    Returns:
        与输入等长的组号列表，``None`` 表示未成组（应画符尾）。
    """
    out: List[Optional[int]] = [None] * len(clusters)
    if beat_size <= 0:  # pragma: no cover
        return out

    run: List[int] = []
    run_key: Optional[Tuple[int, int, int]] = None

    def flush() -> None:
        nonlocal run, run_key
        if run_key is not None and len(run) >= 2:
            counter[0] += 1
            for i in run:
                out[i] = counter[0]
        run = []
        run_key = None

    for i, (beat_pos, voice, beams) in enumerate(clusters):
        if beams <= 0:
            flush()
            continue
        group_key = (int(math.floor((beat_pos + 1e-9) / beat_size)), voice, beams)
        if run_key is not None and group_key != run_key:
            flush()
        if not run:
            run_key = group_key
        run.append(i)
    flush()
    return out


def _mono_stem_up(cluster: LaidCluster) -> bool:
    """单声部簇的符干方向：离中线（音级 4）更远的一侧决定；同距取朝下。"""
    steps = cluster.step_offsets
    if not steps:
        return True
    lo, hi = min(steps), max(steps)
    return (4 - lo) > (hi - 4)


def _ledger_steps(step_offset: int) -> List[int]:
    """该音级偏移需要画几条加线（返回加线的音级偏移）。"""
    out: List[int] = []
    if step_offset < 0:
        s = -2
        while s >= step_offset:
            out.append(s)
            s -= 2
    elif step_offset > 8:
        s = 10
        while s <= step_offset:
            out.append(s)
            s += 2
    return out


def _layout_measure_content(
    draft: _MeasureDraft,
    track: ScoreTrack,
    x: float,
    width: float,
    opts: LayoutOptions,
    key_acc: Dict[int, int],
    flats: bool,
    beam_counter: List[int],
    beat_size: float,
    is_system_start: bool,
) -> LaidMeasure:
    """把草稿落到具体槽位上，产出一条谱表在该小节的全部 LaidCluster。"""
    space = opts.space
    measure = LaidMeasure(
        index=draft.index,
        x=x,
        width=width,
        natural_width=draft.natural_width,
        start_beat=draft.start_beat,
        is_system_start=is_system_start,
    )
    if draft.is_empty:
        return measure

    left_pad = 1.7 * space
    right_pad = 1.15 * space
    usable = max(width - left_pad - right_pad, 1.0)
    rel_xs = _place_points(draft.time_points, space, usable)

    # 音级偏移 / 字母 / 变音记号缓存（同一 ScoreNote 会被分解成多个事件，算一次即可）
    step_of: Dict[int, int] = {}
    letter_of: Dict[int, int] = {}
    acc_of: Dict[int, int] = {}
    for tp in draft.time_points:
        for ev in tp.events:
            for n in ev.notes:
                if n.is_rest:
                    continue
                letter, diatonic, acc = spell_pitch(n.pitch, flats)
                step_of[id(n)] = diatonic - _staff_reference(track.clef)
                letter_of[id(n)] = letter
                acc_of[id(n)] = acc

    # 变音记号状态：(声部, 字母, 八度) -> 本小节已生效的记号
    acc_state: Dict[Tuple[int, int, int], int] = {}
    poly = track.voices > 1

    # --- 1) 建簇（符干方向稍后统一判定） ---
    built: List[LaidCluster] = []
    beam_input: List[Tuple[float, int, int]] = []
    for idx, tp in enumerate(draft.time_points):
        # 同一声部、同时值、同类型的事件合并为一个簇（和弦共用一个符干）
        merged: Dict[Tuple[int, int, str], List[_Event]] = {}
        order: List[Tuple[int, int, str]] = []
        for ev in tp.events:
            key = (ev.voice, round(ev.duration * 64), ev.note_type)
            if key not in merged:
                merged[key] = []
                order.append(key)
            merged[key].append(ev)

        for key in order:
            evs = merged[key]
            head = evs[0]
            is_rest = head.is_rest
            full = is_rest and _is_full_measure(draft, head)
            cluster = LaidCluster(
                x=x + left_pad + rel_xs[idx],
                start=head.start,
                duration=head.duration,
                measure=draft.index,
                voice=head.voice,
                note_type=head.note_type,
                dots=head.dots,
                beams=head.beams,
                stem_up=True,
                stem_len=0.0,
                is_rest=is_rest,
                full_measure=full,
                tie_from_prev=head.tie_from_prev,
                tie_to_next=head.tie_to_next,
            )
            if is_rest:
                for ev in evs:
                    for n in ev.notes:
                        cluster.notes.append(LaidNote(note=n, step_offset=6 if full else 4))
            else:
                for ev in evs:
                    for n in ev.notes:
                        step = step_of[id(n)]
                        cluster.notes.append(
                            LaidNote(
                                note=n,
                                step_offset=step,
                                accidental=_assign_accidental(
                                    acc_state, head.voice, letter_of[id(n)], step,
                                    acc_of[id(n)], key_acc, n,
                                ),
                                ledger_steps=_ledger_steps(step),
                            )
                        )
                cluster.notes.sort(key=lambda ln: ln.step_offset)
            built.append(cluster)
            beam_input.append((tp.start - draft.start_beat, head.voice, head.beams))

    # --- 2) 连杠分组的输入是「簇」而非事件 —— 和弦在簇层只有一个符干 ---
    for cluster, group in zip(built, _beams_for_measure(beam_input, beat_size, beam_counter)):
        cluster.beam_group = group

    # --- 3) 符干方向：组内取多数，保证同组符干同向（符杠才能连成一条线） ---
    natural_dir: List[bool] = []
    for c in built:
        if c.is_rest:
            natural_dir.append(True)
        elif poly:
            natural_dir.append(c.voice * 2 >= track.voices)
        else:
            natural_dir.append(_mono_stem_up(c))

    votes: Dict[int, List[bool]] = {}
    for c, up in zip(built, natural_dir):
        if c.beam_group is not None:
            votes.setdefault(c.beam_group, []).append(up)
    group_dir = {g: (sum(1 for v in vs if v) * 2 >= len(vs)) for g, vs in votes.items()}

    for c, up in zip(built, natural_dir):
        c.stem_up = group_dir.get(c.beam_group, up) if c.beam_group is not None else up

    # --- 4) 符干长度：3.5 格（= 7 个音级半步）起，且至少伸到中线 ---
    # 锚点必须是**中线（0）**，不能是谱表顶线（8）：后者会把低音的符干拉长到跨整条谱表。
    for c in built:
        if c.is_rest:
            c.stem_len = 0.0
            continue
        lo, hi = min(c.step_offsets), max(c.step_offsets)
        if c.stem_up:
            tip = max(hi + 7, 0)      # 3.5 格；低音至少到中线
            c.stem_len = (tip - lo) / 2.0
        else:
            tip = min(lo - 7, 0)
            c.stem_len = (hi - tip) / 2.0

    _align_beam_endpoints(built)
    measure.clusters = built
    return measure


def _is_full_measure(draft: _MeasureDraft, event: _Event) -> bool:
    """该事件是否为「整小节休止」。"""
    return (
        event.is_rest
        and len(draft.time_points) == 1
        and abs(event.duration - draft.duration_beats) < 1e-3
    )


def _staff_reference(clef: str) -> int:
    """谱表最下线对应的音级序号。高音谱号 E4=30，低音谱号 G2=18。"""
    return 18 if clef == "bass" else 30


def _assign_accidental(
    state: Dict[Tuple[int, int, int], int],
    voice: int,
    letter: int,
    step: int,
    note_acc: int,
    key_acc: Dict[int, int],
    note: ScoreNote,
) -> Optional[int]:
    """决定是否绘制变音记号，并维护小节内的记号延续状态。"""
    octave = step // 7
    key = (voice, letter, octave)
    key_value = key_acc.get(letter, 0)
    current = state.get(key, key_value)
    if note.tie_from_prev:
        # 跨小节延音线：记号由上一片携带，本片不重复（但声部状态同步）
        state[key] = note_acc
        return None
    glyph = accidental_glyph(letter, note_acc, key_acc)
    if glyph is None:
        return None
    if note_acc == current:
        return None
    state[key] = note_acc
    return note_acc


def _align_beam_endpoints(clusters: Sequence[LaidCluster]) -> None:
    """把同一连杠组的符干末端对齐到组内极值，使符杠能画成水平线。

    对齐只会**加长**符干（目标值是组内自然末端的最远者），因此不会违反
    「符干至少 3.5 格」的下限，无需 clamp。
    """
    groups: Dict[int, List[LaidCluster]] = {}
    for c in clusters:
        if c.beam_group is not None and not c.is_rest and c.notes:
            groups.setdefault(c.beam_group, []).append(c)

    for members in groups.values():
        if len(members) < 2:  # pragma: no cover - 单成员不会分配组号
            continue
        if members[0].stem_up:
            target = max(max(c.step_offsets) + 2 * c.stem_len for c in members)
            for c in members:
                c.stem_len = (target - min(c.step_offsets)) / 2.0
        else:
            target = min(min(c.step_offsets) - 2 * c.stem_len for c in members)
            for c in members:
                c.stem_len = (max(c.step_offsets) - target) / 2.0


# ---------------------------------------------------------------------------
# 纵向：系统高度
# ---------------------------------------------------------------------------

def _staff_vertical_extent(
    staff_measures: Sequence[LaidMeasure], space: float, *, marks: bool = False
) -> Tuple[float, float]:
    """求一条谱表需要向上/向下预留的空间（单位 space）。

    取「加线范围」与「符干末端」的并集，并保底留出符干 + 符杠的常规高度，
    避免行距被压扁导致上行符杠与下行加线相撞。

    ``marks=True`` 表示该谱表下方还要画力度记号/楔形（约 3 space），需额外行距。
    """
    above_steps = 0
    below_steps = 0
    for m in staff_measures:
        for c in m.clusters:
            if c.is_rest or not c.notes:
                continue
            for ln in c.notes:
                above_steps = max(above_steps, ln.step_offset - 8)
                below_steps = max(below_steps, -ln.step_offset)
            if c.stem_len > 0:
                if c.stem_up:
                    above_steps = max(above_steps, max(c.step_offsets) + 2 * c.stem_len - 8)
                else:
                    below_steps = max(below_steps, -(min(c.step_offsets) - 2 * c.stem_len))
    above = max(above_steps / 2.0, 4.2)
    below = max(below_steps / 2.0, 3.6) + (3.0 if marks else 0.0)
    return above, below


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def layout_score(score: Score, options: Optional[LayoutOptions] = None) -> ScoreLayout:
    """把 Score 排版为可直接绘制的 ScoreLayout。

    Args:
        score: ``score.model.Score``。
        options: 排版参数；None 时用默认值。

    Returns:
        ScoreLayout；无轨道时返回仅含页面信息的空布局。

    Raises:
        ValueError: 拍号非法（由 theory 层抛出）。
    """
    opts = options or LayoutOptions()
    space = opts.space
    num, _den = parse_time_signature(score.time_signature)
    beat_size = beat_unit(score.time_signature)
    # 复合拍（6/8、9/8、12/8）以附点拍为连杠单位
    if num % 3 == 0 and num > 3:
        beat_size *= 3.0

    fifths = score.fifths
    key_acc = key_accidentals(fifths)
    flats = prefer_flats(score.key)

    # 缩进必须能容下最宽的那条谱表的签名区，否则首小节会与谱号/调号相撞
    first_indent = max(
        opts.first_indent, required_indent(score, space, with_time_signature=True)
    )
    system_indent = max(
        opts.system_indent, required_indent(score, space, with_time_signature=False)
    )

    layout = ScoreLayout(
        score=score,
        options=opts,
        page_width=opts.page_width,
        fifths=fifths,
        key_acc=key_acc,
        prefer_flats=flats,
        note_count=score.note_count,
    )

    if not score.tracks:
        layout.page_height = opts.title_height + 2 * opts.margin
        return layout

    beam_counter = [0]

    # --- 第一遍：逐轨逐小节出草稿，取各小节自然宽度的跨轨最大值 ---
    drafts: List[List[_MeasureDraft]] = []
    for track in score.tracks:
        per_track: List[_MeasureDraft] = []
        for measure in track.measures:
            per_track.append(_draft_measure(track, measure, space))
        drafts.append(per_track)

    naturals: List[float] = []
    for i in range(score.bars):
        width = max(
            (drafts[t][i].natural_width for t in range(len(drafts))),
            default=_measure_floor_width(space),
        )
        naturals.append(width)

    # --- 第二遍：行断开 ---
    spans = _break_systems(naturals, opts.content_width, first_indent)

    # --- 第三遍：逐行逐轨落位 ---
    for sys_idx, (start, end) in enumerate(spans):
        indent = first_indent if sys_idx == 0 else system_indent
        available = opts.content_width - indent
        naturals_slice = naturals[start:end]

        # 末行是否拉伸：单行曲子必须撑满（否则 4 小节会缩在页面左侧）；
        # 多行曲子的末行齐左不拉伸 —— 这是常规版式，也让末行小节宽度不被拉变形。
        if sys_idx < len(spans) - 1:
            stretch_cap: Optional[float] = opts.max_stretch
        elif len(spans) == 1:
            stretch_cap = max(
                opts.max_stretch, available / max(sum(naturals_slice), _EPS)
            )
        else:
            stretch_cap = None
        widths = _distribute_widths(naturals_slice, available, stretch_cap)

        # 小节左边界
        xs: List[float] = []
        cursor = opts.margin + indent
        for w in widths:
            xs.append(cursor)
            cursor += w

        staffs: List[LaidStaff] = []
        for t_idx, track in enumerate(score.tracks):
            staff_measures: List[LaidMeasure] = []
            for k in range(start, end):
                staff_measures.append(
                    _layout_measure_content(
                        draft=drafts[t_idx][k],
                        track=track,
                        x=xs[k - start],
                        width=widths[k - start],
                        opts=opts,
                        key_acc=key_acc,
                        flats=flats,
                        beam_counter=beam_counter,
                        beat_size=beat_size,
                        is_system_start=(k == start),
                    )
                )
            staffs.append(
                LaidStaff(
                    track_index=t_idx,
                    name=track.name,
                    clef=track.clef,
                    y=0.0,
                    measures=staff_measures,
                )
            )

        # --- 纵向排布 ---
        cursor_y = 0.0
        heights: List[float] = []
        tops: List[float] = []
        for t_idx, staff in enumerate(staffs):
            # 谱表下方还要放力度记号与渐变楔形时，多留一点行距（否则与下一行相撞）
            marks = False
            if 0 <= staff.track_index < len(score.tracks):
                track = score.tracks[staff.track_index]
                marks = bool(track.dynamics or track.wedges)
            above, below = _staff_vertical_extent(staff.measures, space, marks=marks)
            top = cursor_y + above * space + 6.0
            heights.append(above * space + STAFF_SPACES * space + below * space + 12.0)
            tops.append(top)
            cursor_y += heights[-1] + opts.staff_gap

        if cursor_y > 0:
            cursor_y -= opts.staff_gap  # 末个谱表后不加谱表间距

        system = System(
            index=sys_idx + 1,
            y=0.0,
            height=cursor_y,
            space=space,
            staffs=staffs,
        )
        for staff, top in zip(staffs, tops):
            staff.y = top + STAFF_SPACES * space  # 最下线 y（系统内相对坐标）
        layout.systems.append(system)

    # --- 纵向堆叠到页面 ---
    cursor_y = opts.margin + opts.title_height
    for system in layout.systems:
        system.y = cursor_y
        cursor_y += system.height + opts.system_gap
    if layout.systems:
        cursor_y -= opts.system_gap
    layout.page_height = cursor_y + opts.margin
    return layout
