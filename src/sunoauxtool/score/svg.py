"""五线谱 SVG 渲染（纯字符串拼装，零第三方依赖，离线可看）。

设计取舍
--------
- **不用音乐字体**：谱号 / 休止符 / 变音记号全部自绘路径。理由是本机与 CI 都没有
  Bravura / MuseScore 字体，``<text>𝄞</text>`` 在缺字体环境会渲染成方框；
  自绘路径在 Edge / Chrome / Firefox / Inkscape 上表现一致。
- **谱号是「示意级」而非书法级**：高音谱号卷曲锚在 G4 线（自下而上第二条线），
  低音谱号两点跨 F3 线 —— 功能上足以读谱，不追求雕版字形细节。
- **两套配色**：默认白纸黑墨（乐谱惯例）；``SvgTheme.dark()`` 供深色界面内嵌。

坐标系：页面坐标，y 向下为正。谱表最下线 y = ``system.y + staff.y``；
音符 y = 谱表最下线 y − ``step_offset * space / 2``（step_offset 以最下线为 0，单位半个 space）。

绘制顺序（决定叠放层次）：谱线 → 谱号/调号/拍号 → 加线 → 符头/变音记号/附点 →
符干/符尾 → 休止符 → 延音线 → 符杠 → 小节线（最上层）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sunoauxtool.score.drawing import Canvas, to_svg
from sunoauxtool.score.layout import (
    ACCIDENTAL_ADVANCE,
    CLEF_WIDTH,
    SIGNATURE_PAD,
    TIME_SIG_WIDTH,
    LaidCluster,
    LaidMeasure,
    LaidStaff,
    ScoreLayout,
    required_indent,
    signature_area_width,
)
from sunoauxtool.score.theory import parse_time_signature

__all__ = [
    "ACCIDENTAL_ADVANCE",
    "CLEF_WIDTH",
    "SIGNATURE_PAD",
    "TIME_SIG_WIDTH",
    "ScoreSvgRenderer",
    "SvgOptions",
    "SvgTheme",
    "draw_tempo_mark",
    "render_svg",
    "required_indent",
    "signature_area_width",
    "tempo_mark_width",
    "write_svg",
]

# ---------------------------------------------------------------------------
# 主题与常量
# ---------------------------------------------------------------------------


@dataclass
class SvgTheme:
    """配色。默认白纸黑墨，符合乐谱惯例。"""

    paper: str = "#ffffff"
    ink: str = "#141414"
    accent: str = "#8a1c1c"
    faint: str = "#9a9a9a"

    @classmethod
    def dark(cls) -> "SvgTheme":
        """深色主题：供深色界面内嵌（纸深墨浅，而非整体反色）。"""
        return cls(paper="#15171c", ink="#e9e7e4", accent="#f0b8b4", faint="#7d838d")


#: 谱号横向占位、调号步进、拍号占位等常量定义在**排版层**（它们决定系统缩进），
#: 此处直接复用，避免两处各写一份而漂移。

# --- 字形路径（1 单位 = space） --------------------------------------------

#: 高音谱号：两笔描边，坐标原点 = 谱表最上线、y 向下为正、1 单位 = 1 space。
#: G 线（自下第 2 线）在 y = 3。
#: 构造（照记谱惯例）：
#:   笔一 = 尾钩 → 竖直主轴 → 顶环（向右倾的水滴）→ 下行穿过主轴 → 交点 (1.30,1.25)；
#:   笔二 = 自交点向左下大弧（左翼）→ 绕底 → 沿右侧上行 → 圈的上弧 → 内卷小钩收在主轴左侧。
#: 两条铁律：**主轴必须穿过圈心**（x=1.30 与圈心同列，圈落在 G 线上），
#: 以及**每段接点的切向连续**（相邻控制点共线方向），否则会出现折点。
TREBLE_CLEF = (
    # 笔一：尾钩 + 主轴 + 顶环
    "M 0.60,5.00 "
    "C 0.40,5.34 0.76,5.64 1.12,5.46 "
    "C 1.24,5.40 1.30,5.20 1.30,5.00 "
    "L 1.30,0.10 "
    "C 1.30,-0.85 1.52,-1.42 1.92,-1.22 "
    "C 2.28,-1.04 2.34,-0.55 2.14,0.05 "
    "C 1.92,0.72 1.55,1.02 1.30,1.25 "
    # 笔二：左翼大弧 → 圈（下、右、上）→ 内卷
    "M 1.30,1.25 "
    "C 0.80,1.72 -0.15,2.42 -0.18,3.05 "
    "C -0.18,3.82 0.50,4.38 1.30,4.05 "
    "C 1.95,3.80 2.25,3.25 2.05,2.70 "
    "C 1.85,2.15 1.25,1.95 1.05,2.45 "
    "C 0.92,2.80 1.05,3.15 0.72,3.10"
)
#: 低音谱号主体（实心豆形，右侧收尖于 F3 线）
BASS_CLEF_BODY = (
    "M 1.45,0.30 "
    "C 1.20,-0.25 0.72,-0.62 0.05,-0.55 "
    "C -0.78,-0.48 -1.32,0.10 -1.32,0.98 "
    "C -1.32,1.84 -0.70,2.40 0.15,2.32 "
    "C 0.92,2.24 1.42,1.70 1.48,1.05 "
    "C 1.52,0.68 1.50,0.45 1.45,0.30 Z"
)
#: 低音谱号右上细尾
BASS_CLEF_TAIL = "M 1.38,0.28 C 1.72,0.06 2.00,0.30 2.05,0.72 C 2.09,1.06 1.98,1.36 1.80,1.54"
#: 低音谱号两点：F3 线上下的两个间（以**谱表最上线**为 0、向下为正的 space 单位）
BASS_CLEF_DOTS: Tuple[float, float] = (0.5, 1.5)
#: 低音谱号两点相对谱号原点的 x 偏移（单位 space）
BASS_CLEF_DOT_X = 2.60

#: 变音记号：``{记号: [(路径, 描边宽度或 None=填充), ...]}``，原点在符头中心
ACCIDENTAL_STROKES: Dict[int, List[Tuple[str, Optional[float]]]] = {
    1: [
        ("M -0.17,-0.95 L -0.17,0.95", 0.13),
        ("M 0.27,-1.05 L 0.27,0.85", 0.13),
        ("M -0.40,-0.30 L 0.50,-0.52", 0.30),
        ("M -0.40,0.32 L 0.50,0.10", 0.30),
    ],
    0: [
        ("M -0.17,-1.00 L -0.17,0.28", 0.14),
        ("M 0.25,-0.28 L 0.25,1.00", 0.14),
        ("M -0.17,-0.38 L 0.25,-0.38", 0.26),
        ("M -0.17,0.34 L 0.25,0.34", 0.26),
    ],
    -1: [
        ("M -0.15,-1.22 L -0.15,0.58", 0.14),
        (
            "M -0.15,0.58 C -0.15,0.10 0.10,-0.16 0.36,0.00 "
            "C 0.60,0.15 0.58,0.50 0.32,0.70 "
            "C 0.12,0.85 -0.05,0.88 -0.15,0.84 Z",
            None,
        ),
    ],
}
#: 变音记号沿纵向的附加偏移（单位 space）
ACCIDENTAL_DY: Dict[int, float] = {1: 0.0, 0: 0.0, -1: 0.05}
#: 变音记号相对符头中心的横向偏移（单位 space，负号在左）
ACCIDENTAL_DX = -1.05

#: 四分休止符（以中线为原点）
QUARTER_REST = (
    "M -0.18,-1.05 "
    "C 0.18,-0.62 0.44,-0.36 0.46,-0.06 "
    "C 0.48,0.20 0.28,0.40 0.06,0.48 "
    "C 0.34,0.36 0.52,0.52 0.46,0.78 "
    "C 0.40,1.02 0.10,1.16 -0.22,1.12 "
    "C 0.02,1.00 0.14,0.84 0.08,0.62 "
    "C 0.02,0.42 -0.22,0.32 -0.34,0.10 "
    "C -0.46,-0.14 -0.34,-0.52 -0.14,-0.92 Z"
)

#: 四分音符（速度记号用）的几何，均以**字号**为单位。
#: 不直接用 ``♩`` (U+2669)：Windows 的 msyh.ttc 没有该字形，PNG 光栅化没有字体回退，
#: 会渲染成豆腐块（与模块开头「不用音乐字体」的理由同源），故自绘。
TEMPO_NOTE_HEAD: Tuple[float, float, float] = (0.30, 0.215, -20.0)  # rx, ry, rotate
TEMPO_NOTE_STEM_DX = 0.27
TEMPO_NOTE_STEM_TOP = 1.30


def tempo_mark_width(size: float) -> float:
    """四分音符速度记号的横向占位（与 ``size`` 同单位）。"""
    rx = TEMPO_NOTE_HEAD[0]
    return size * (rx + 0.10)


def draw_tempo_mark(
    canvas: Canvas, x: float, baseline: float, size: float, color: str
) -> float:
    """在文字基线上画一个四分音符（速度记号），返回其右边界 x。

    五线谱与简谱共用：两边的 PNG 都要经过同一光栅化路径，字号单位也一致，
    所以放在这里而不是各自实现一份（否则又会出现 SVG / PNG 漂移）。

    Args:
        canvas: 绘制指令流。
        x: 记号左边界。
        baseline: 文字基线（音符下方与之相切）。
        size: 记号大小（字号）。
        color: 颜色。

    Returns:
        记号右边界 x，供后面接 ``= 96`` 之类的文字。
    """
    rx, ry, rotate = TEMPO_NOTE_HEAD
    head_x = x + size * rx
    head_y = baseline - size * 0.34
    canvas.ellipse(head_x, head_y, size * rx, size * ry, rotate, color)
    canvas.line(
        head_x + size * TEMPO_NOTE_STEM_DX, head_y - size * 0.05,
        head_x + size * TEMPO_NOTE_STEM_DX, baseline - size * TEMPO_NOTE_STEM_TOP,
        size * 0.078, color,
    )
    return x + tempo_mark_width(size)

# --- 线宽与尺寸（单位 space） ----------------------------------------------
STAFF_LINE_W = 0.115
BARLINE_W = 0.135
BARLINE_THICK_W = 0.46
STEM_W = 0.135
LEDGER_W = 0.145
BEAM_THICKNESS = 0.50
BEAM_GAP = 0.65
NOTEHEAD_RX = 0.615
NOTEHEAD_RY = 0.445
WHOLE_RX = 0.76
WHOLE_RY = 0.42
LEDGER_HALF = 0.62
TIE_W = 0.155
DOT_R = 0.135
REST_BAR_W = 0.52
REST_BAR_H = 0.44
FLAG_STEP = 0.86

#: 力度记号的基线（相对谱表最下线的 space 数）
DYNAMIC_DY = 2.95
#: 渐变楔形中线相对力度文字基线的偏移（负值 = 略高于基线，与文字共处一带）
WEDGE_DY = -0.40
#: 渐变楔形的最大张口宽度
WEDGE_GAP = 2.20
#: 力度文字宽度估算（每字符 space 数，用于让楔形避开同小节的力度记号）
DYNAMIC_CHAR_W = 0.78
#: 演奏法记号到符头中心的距离
ARTIC_DY = 1.12

#: 演奏法名 → 记号形状（未列出的画成符头外侧的点，与 staccato 同形）
_ARTIC_GLYPHS: Dict[str, str] = {
    "staccato": "dot",
    "spiccato": "dot",
    "pizz": "dot",
    "staccatissimo": "tick",
    "tenuto": "line",
    "accent": "accent",
    "marcato": "marcato",
}

#: 调号中各变音记号的音级偏移（**高音谱号**口径；低音谱号整体平移 BASS_KEY_SIG_SHIFT）
KEY_SIG_STEPS: Dict[int, Dict[int, int]] = {
    1: {3: 8, 0: 5, 4: 9, 1: 6, 5: 3, 2: 7, 6: 4},   # F C G D A E B
    -1: {6: 4, 2: 7, 5: 3, 1: 6, 4: 2, 0: 5, 3: 1},  # B E A D G C F
}
#: 调号记号的书写顺序（字母序号：0=C … 6=B）
KEY_SIG_ORDER: Dict[int, Tuple[int, ...]] = {
    1: (3, 0, 4, 1, 5, 2, 6),
    -1: (6, 2, 5, 1, 4, 0, 3),
}
#: 低音谱号调号相对高音谱号的音级平移（两谱表基准线相差 12 个音级，记谱移低两个八度 = 14）
BASS_KEY_SIG_SHIFT = -2


# ---------------------------------------------------------------------------
# 渲染选项
# ---------------------------------------------------------------------------


@dataclass
class SvgOptions:
    """渲染开关与文本覆盖。"""

    theme: SvgTheme = field(default_factory=SvgTheme)
    title: Optional[str] = None
    composer: Optional[str] = None
    show_title: bool = True
    show_tempo: bool = True
    show_measure_numbers: bool = True
    show_ties: bool = True
    font_family: str = "Georgia, 'Times New Roman', 'Songti SC', serif"



# ---------------------------------------------------------------------------
# 渲染器
# ---------------------------------------------------------------------------


class ScoreSvgRenderer:
    """把 ScoreLayout 画成 SVG。"""

    def __init__(self, layout: ScoreLayout, options: Optional[SvgOptions] = None) -> None:
        """初始化。

        Args:
            layout: ``layout_score()`` 的产物。
            options: 渲染选项；None 用默认（白纸黑墨）。
        """
        self.layout = layout
        self.opts = options or SvgOptions()
        self.theme = self.opts.theme
        self.space = layout.space
        self.canvas = Canvas()
        #: 「轨 -> 全曲簇序列」与「(系统序号, 轨) -> 谱表最下线」，供延音线跨小节/跨系统查找。
        self._track_clusters: Dict[int, List[Tuple[int, LaidMeasure, LaidCluster]]] = {}
        self._staff_bottom: Dict[Tuple[int, int], float] = {}
        #: 当前绘制上下文（由 ``render()`` 逐谱表刷新）。
        self._current_system = 0
        self._current_track = 0
        self._system_right = 0.0

    # -- 换算 ------------------------------------------------------------

    def _w(self, units: float) -> float:
        """把「单位 space」的线宽换算为像素。"""
        return units * self.space

    def _y(self, staff_bottom: float, step_offset: float) -> float:
        """音级偏移 -> 页面 y。"""
        return staff_bottom - step_offset * self.space / 2.0

    def _stem_x(self, cluster: LaidCluster) -> float:
        """符干 x：朝上的符干贴符头右侧，朝下的贴左侧。"""
        return cluster.x + (NOTEHEAD_RX * self.space if cluster.stem_up else -NOTEHEAD_RX * self.space)

    def _stem_tip_y(self, cluster: LaidCluster, staff_bottom: float) -> float:
        """符干末端 y。"""
        steps = cluster.step_offsets
        if not steps:  # pragma: no cover - 实音簇必有符头
            return self._y(staff_bottom, 4)
        if cluster.stem_up:
            return self._y(staff_bottom, max(steps)) - cluster.stem_len * self.space
        return self._y(staff_bottom, min(steps)) + cluster.stem_len * self.space

    # -- 顶层 ------------------------------------------------------------

    def draw(self) -> Canvas:
        """只往画布上画，不决定输出格式（PNG 复用同一指令流，避免两套几何漂移）。"""
        systems = self.layout.systems
        self._index_ties()
        for index, system in enumerate(systems):
            is_first = index == 0
            is_last = index == len(systems) - 1
            for staff_index, staff in enumerate(system.staffs):
                bottom = system.y + staff.y
                self._current_system = index
                self._current_track = staff.track_index
                self._system_right = staff.measures[-1].right if staff.measures else 0.0
                self._staff_lines(staff, bottom)
                self._signature(staff, bottom, with_time_signature=is_first)
                for measure in staff.measures:
                    self._measure_content(measure, bottom, show_number=staff_index == 0)
                self._draw_expression(staff, bottom)
                for measure in staff.measures:
                    self._beams(measure, bottom)
            self._barlines(system, final=is_last)
        self._page_text()
        return self.canvas

    def render(self) -> str:
        """渲染并返回 SVG 文档字符串。"""
        return to_svg(
            self.draw().ops, self.layout.page_width, self.layout.page_height, self.theme.paper
        )

    # -- 谱线 / 签名区 / 小节线 ------------------------------------------

    def _staff_lines(self, staff: LaidStaff, bottom: float) -> None:
        """五条谱线：从签名区左缘画到本谱表最后一个小的右边界。

        左端刻意比页边再向左伸 ``1.5 * space``：低音谱号主体比谱号锚点左伸最多约 1.4 格，
        否则谱线会从字形的中间「长出来」。
        """
        if not staff.measures:
            return
        x1 = self.layout.options.margin - 1.5 * self.space
        x2 = staff.measures[-1].right
        for i in range(5):
            y = bottom - (4 - i) * self.space
            self.canvas.line(x1, y, x2, y, self._w(STAFF_LINE_W), self.theme.ink)

    def _signature(self, staff: LaidStaff, bottom: float, *, with_time_signature: bool) -> None:
        """谱号 + 调号（每个系统都画）；拍号只在首个系统画（后续系统省略是常规做法）。"""
        space = self.space
        x = self.layout.options.margin
        self._draw_clef(staff.clef, x, bottom)
        x += CLEF_WIDTH.get(staff.clef, CLEF_WIDTH["treble"]) * space
        x += self._draw_key_signature(staff, bottom, x)
        if with_time_signature:
            self._draw_time_signature(bottom, x)

    def _draw_clef(self, clef: str, x: float, bottom: float) -> None:
        """在谱表起点画谱号字形。

        字形路径（``TREBLE_CLEF`` / ``BASS_CLEF_BODY``）以**谱表最上线**为原点、
        y 向下为正、单位为 space，故这里把组变换锚在最上线。
        高音谱号卷曲随之落在第 4 线（自下而上第 2 线 = G4），
        低音谱号主体与两点随之跨在第 2 线（F3）。
        """
        ink = self.theme.ink
        origin_y = self._y(bottom, 8)      # 最上线
        transform = f"translate({x:.2f},{origin_y:.2f}) scale({self.space:.4f})"
        if clef == "bass":
            with self.canvas.group(transform):
                self.canvas.path(BASS_CLEF_BODY, ink)
            with self.canvas.group(transform):
                self.canvas.path(BASS_CLEF_TAIL, "none", stroke=ink, stroke_width=0.24)
            for dy in BASS_CLEF_DOTS:
                self.canvas.circle(
                    x + BASS_CLEF_DOT_X * self.space, origin_y + dy * self.space,
                    0.25 * self.space, ink,
                )
            return
        with self.canvas.group(transform):
            self.canvas.path(TREBLE_CLEF, "none", stroke=ink, stroke_width=0.30)

    def _draw_key_signature(self, staff: LaidStaff, bottom: float, x: float) -> float:
        """画调号，返回其横向占用宽度。"""
        fifths = self.layout.fifths
        if fifths == 0:
            return 0.0
        kind = 1 if fifths > 0 else -1
        shift = BASS_KEY_SIG_SHIFT if staff.clef == "bass" else 0
        steps = KEY_SIG_STEPS[kind]
        cursor = x + SIGNATURE_PAD * self.space
        for letter in KEY_SIG_ORDER[kind][: abs(fifths)]:
            self._draw_accidental(self._y(bottom, steps[letter] + shift), cursor, kind)
            cursor += ACCIDENTAL_ADVANCE * self.space
        return cursor - x

    def _draw_time_signature(self, bottom: float, x: float) -> None:
        """拍号：上下两个数字，分别占据谱表的上半与下半（不越出谱表）。"""
        num, den = parse_time_signature(self.layout.score.time_signature)
        space = self.space
        cx = x + TIME_SIG_WIDTH * space / 2.0
        size = 2.20 * space
        # 数字基线：分子贴中线之上、分母贴最下线之上；字高约 0.7 * size，恰好不压线
        self.canvas.text(
            cx, self._y(bottom, 4) - 0.25 * space, str(num), size,
            self.theme.ink, self.opts.font_family, anchor="middle", weight="bold",
        )
        self.canvas.text(
            cx, self._y(bottom, 0) - 0.25 * space, str(den), size,
            self.theme.ink, self.opts.font_family, anchor="middle", weight="bold",
        )

    def _draw_accidental(self, y: float, x: float, kind: int) -> None:
        """在 (x, y) 处画一个变音记号（1 升 / 0 还原 / -1 降）。"""
        strokes = ACCIDENTAL_STROKES.get(kind)
        if not strokes:  # pragma: no cover - 拼写层只产出 -1/0/1
            return
        dy = ACCIDENTAL_DY.get(kind, 0.0) * self.space
        with self.canvas.group(f"translate({x:.2f},{y + dy:.2f}) scale({self.space:.4f})"):
            for d, width in strokes:
                if width is None:
                    self.canvas.path(d, self.theme.ink)
                else:
                    self.canvas.path(d, "none", stroke=self.theme.ink, stroke_width=width)

    def _barlines(self, system, *, final: bool) -> None:
        """小节线：每个边界从首谱表最上线连到末谱表最下线（钢琴式多谱表）。

        Args:
            system: 目标系统。
            final: 是否为全曲最后一个系统 —— 只有它才配终止粗细双线。
        """
        if not system.staffs:
            return
        slots = system.staffs[0].measures
        if not slots:
            return
        top = system.y + system.staffs[0].y - 4 * self.space
        bottom = system.y + system.staffs[-1].y
        last_right = slots[-1].right
        for measure in slots:
            self.canvas.line(
                measure.right, top, measure.right, bottom, self._w(BARLINE_W), self.theme.ink
            )
        if not final:
            return
        thick = self._w(BARLINE_THICK_W)
        self.canvas.line(
            last_right - thick, top, last_right - thick, bottom,
            self._w(BARLINE_THICK_W), self.theme.ink,
        )

    # -- 小节内容 --------------------------------------------------------

    def _measure_content(
        self, measure: LaidMeasure, bottom: float, *, show_number: bool
    ) -> None:
        """画一个小节的全部内容。

        Args:
            measure: 目标小节。
            bottom: 该谱表最下线的页面 y。
            show_number: 是否画小节号（只有系统首条谱表画，避免每条谱表都重复）。
        """
        for cluster in measure.clusters:
            if cluster.is_rest:
                self._draw_rest(cluster, bottom)
            else:
                self._draw_cluster(cluster, bottom)
        if self.opts.show_ties:
            self._draw_ties(measure, bottom)
        if show_number and self.opts.show_measure_numbers and measure.is_system_start:
            self.canvas.text(
                measure.x + 0.45 * self.space,
                bottom - 4 * self.space - 0.80 * self.space,
                str(measure.index), 0.92 * self.space, self.theme.faint,
                self.opts.font_family, weight="bold",
            )

    def _draw_cluster(self, cluster: LaidCluster, bottom: float) -> None:
        """画一个实音簇：加线 → 符头 → 变音记号 → 附点 → 符干 → 符尾。"""
        space = self.space
        for note in cluster.notes:
            for step in note.ledger_steps:
                y = self._y(bottom, step)
                self.canvas.line(
                    cluster.x - LEDGER_HALF * space, y, cluster.x + LEDGER_HALF * space, y,
                    self._w(LEDGER_W), self.theme.ink,
                )

        for note in cluster.notes:
            y = self._y(bottom, note.step_offset)
            if cluster.note_type == "whole":
                self.canvas.ellipse(
                    cluster.x, y, WHOLE_RX * space, WHOLE_RY * space, 0.0, self.theme.ink
                )
            elif cluster.note_type == "half":
                self.canvas.ellipse(
                    cluster.x, y, NOTEHEAD_RX * space, NOTEHEAD_RY * space, -20.0,
                    self.theme.paper, stroke=self.theme.ink, stroke_width=self._w(0.17),
                )
            else:
                self.canvas.ellipse(
                    cluster.x, y, NOTEHEAD_RX * space, NOTEHEAD_RY * space, -20.0, self.theme.ink
                )
            if note.accidental is not None:
                self._draw_accidental(y, cluster.x + ACCIDENTAL_DX * space, note.accidental)
            if cluster.dots:
                self._draw_dots(cluster, note, y, space)

        if cluster.note_type != "whole" and cluster.stem_len > 0:
            sx = self._stem_x(cluster)
            if cluster.stem_up:
                y1 = self._y(bottom, min(cluster.step_offsets))
            else:
                y1 = self._y(bottom, max(cluster.step_offsets))
            y2 = self._stem_tip_y(cluster, bottom)
            self.canvas.line(sx, y1, sx, y2, self._w(STEM_W), self.theme.ink)
            if cluster.beam_group is None and cluster.beams > 0:
                self._draw_flags(sx, y2, cluster)

        self._draw_articulations(cluster, bottom)

    def _draw_dots(self, cluster: LaidCluster, note, note_y: float, space: float) -> None:
        """附点：位于符头右侧的间上（线音符放到上一间）。"""
        y = note_y - space / 2.0 if note.step_offset % 2 == 0 else note_y
        for k in range(cluster.dots):
            self.canvas.circle(
                cluster.x + (1.06 + 0.42 * k) * space, y, DOT_R * space, self.theme.ink
            )

    def _draw_flags(self, sx: float, tip_y: float, cluster: LaidCluster) -> None:
        """符尾（未连杠的八分及更短音符）：符干朝上时向下垂，朝下时向上扬。"""
        space = self.space
        d = 1.0 if cluster.stem_up else -1.0
        for i in range(cluster.beams):
            oy = (tip_y + d * i * FLAG_STEP * space) / space
            bx = sx / space
            path = (
                f"M {bx:.3f},{oy:.3f} "
                f"C {bx + 0.58:.3f},{oy + d * 0.02:.3f} "
                f"{bx + 0.98:.3f},{oy + d * 0.42:.3f} "
                f"{bx + 0.82:.3f},{oy + d * 0.96:.3f} "
                f"C {bx + 0.56:.3f},{oy + d * 0.52:.3f} "
                f"{bx + 0.28:.3f},{oy + d * 0.32:.3f} "
                f"{bx:.3f},{oy + d * 0.30:.3f} Z"
            )
            self.canvas.path(path, self.theme.ink, transform=f"scale({space:.4f})")

    # -- 演奏法 / 力度 / 渐变 ---------------------------------------------

    def _draw_articulations(self, cluster: LaidCluster, bottom: float) -> None:
        """在符头外侧画演奏法记号（符干朝上画在上方，朝下画在下方）。

        整个簇只画一次：和弦各音的演奏法在语义上是「同一个音头」的标记，
        逐符头重复画会叠成一坨。
        """
        if cluster.is_rest:
            return
        names = {
            str(n.note.articulation).strip().lower()
            for n in cluster.notes
            if n.note.articulation
        }
        steps = cluster.step_offsets
        if not names or not steps:
            return
        space = self.space
        up = cluster.stem_up or cluster.note_type == "whole"
        anchor = self._y(bottom, max(steps) if up else min(steps))
        y = anchor - ARTIC_DY * space if up else anchor + ARTIC_DY * space
        for name in sorted(names):
            glyph = _ARTIC_GLYPHS.get(name)
            if glyph:
                self._draw_artic_glyph(glyph, cluster.x, y, up)

    def _draw_artic_glyph(self, glyph: str, x: float, y: float, up: bool) -> None:
        """画单个演奏法记号（全部以 space 为单位，避免线宽/尺寸随版面漂移）。"""
        space = self.space
        width = self._w(BARLINE_W)
        if glyph == "dot":
            self.canvas.circle(x, y, DOT_R * space, self.theme.ink)
            return
        if glyph == "line":  # tenuto：短横
            self.canvas.line(
                x - 0.55 * space, y, x + 0.55 * space, y, self._w(0.22), self.theme.ink
            )
            return
        if glyph == "tick":  # staccatissimo：短促的三角楔（尖端朝向符头）
            d = 1.0 if up else -1.0
            bx, by = x / space, y / space
            path = (
                f"M {bx - 0.22:.3f},{by - d * 0.05:.3f} "
                f"L {bx + 0.22:.3f},{by - d * 0.05:.3f} "
                f"L {bx:.3f},{by + d * 0.72:.3f} Z"
            )
            self.canvas.path(path, self.theme.ink, transform=f"scale({space:.4f})")
            return
        # accent ">"：尖端朝右；marcato "^"：尖端朝上（符干朝下时整体镜像）
        d = 1.0 if up else -1.0
        if glyph == "marcato":
            self.canvas.line(
                x - 0.42 * space, y + d * 0.34 * space, x, y - d * 0.30 * space,
                width, self.theme.ink,
            )
            self.canvas.line(
                x + 0.42 * space, y + d * 0.34 * space, x, y - d * 0.30 * space,
                width, self.theme.ink,
            )
            return
        self.canvas.line(
            x - 0.40 * space, y - 0.34 * space, x + 0.34 * space, y, width, self.theme.ink
        )
        self.canvas.line(
            x - 0.40 * space, y + 0.34 * space, x + 0.34 * space, y, width, self.theme.ink
        )

    def _track_of(self, staff: LaidStaff):
        """谱表 → ScoreTrack（轨序号越界时返回 None）。"""
        tracks = self.layout.score.tracks
        if 0 <= staff.track_index < len(tracks):
            return tracks[staff.track_index]
        return None

    def _expression_base(self, staff: LaidStaff) -> float:
        """力度文字基线（相对谱表下线的 space 数）。

        默认 ``DYNAMIC_DY``；谱表下方还有东西时整体让开：
        最低符头（含加线外的低音）让出 1.15 space，演奏法记号（符干朝下的
        staccato/tenuto 等）让出 1.55 space —— 否则力度文字会与它们叠在一起。
        """
        lowest_note = 0.0
        lowest_mark = 0.0
        for measure in staff.measures:
            for cluster in measure.clusters:
                if cluster.is_rest or not cluster.step_offsets:
                    continue
                bottom_step = min(cluster.step_offsets)
                lowest_note = max(lowest_note, -bottom_step / 2.0 + 0.60)
                if cluster.stem_up or cluster.note_type == "whole":
                    continue  # 记号画在上方，不占下方这一带
                if any(n.note.articulation for n in cluster.notes):
                    lowest_mark = max(lowest_mark, -bottom_step / 2.0 + ARTIC_DY)
        return max(DYNAMIC_DY, lowest_note + 1.15, lowest_mark + 1.55)

    def _draw_expression(self, staff: LaidStaff, bottom: float) -> None:
        """画力度记号（谱表下方文字）与渐变楔形（跨小节连线）。

        楔形按「段内等分」插值：一个 4 小节的 crescendo 每小节张口 1/4，
        因此跨系统换行时分段绘制也仍然连贯。
        """
        track = self._track_of(staff)
        if track is None:
            return
        space = self.space
        base = self._expression_base(staff)
        for measure in staff.measures:
            name = track.dynamics.get(measure.index)
            if name:
                self.canvas.text(
                    measure.x + 0.55 * space, bottom + base * space, str(name),
                    1.30 * space, self.theme.ink, self.opts.font_family,
                    weight="bold", style="italic",
                )
        for start, end, kind in self._wedge_spans(track):
            for measure in staff.measures:
                if start <= measure.index <= end:
                    indent = 0.0
                    if measure.index == start:
                        label = track.dynamics.get(measure.index)
                        if label:  # 与力度记号同起时，楔形从文字右侧起画（记谱惯例）
                            indent = 0.30 + DYNAMIC_CHAR_W * len(str(label))
                    self._draw_hairpin(measure, start, end, kind, bottom, indent, base)

    @staticmethod
    def _wedge_spans(track) -> List[Tuple[int, int, str]]:
        """``[(起小节, 收小节, 类型)]``：把 wedges / wedge_ends 配成区间。"""
        ends = sorted(track.wedge_ends)
        spans: List[Tuple[int, int, str]] = []
        for start, kind in sorted(track.wedges.items()):
            end = next((cand for cand in ends if cand >= start), start)
            spans.append((start, end, str(kind)))
        return spans

    def _draw_hairpin(
        self, measure: LaidMeasure, start: int, end: int, kind: str, bottom: float,
        indent: float = 0.0, base: float = 0.0,
    ) -> None:
        """画一小节内的楔形片段（crescendo 向右张开，diminuendo 向右收拢）。"""
        space = self.space
        total = max(1, end - start + 1)
        offset = measure.index - start
        gap0 = self._wedge_gap(kind, offset / total) * space
        gap1 = self._wedge_gap(kind, (offset + 1) / total) * space
        # 略高于力度文字基线：与文字同起时已用 indent 让开，纵向不必再让
        lane = (DYNAMIC_DY if base is None else base) + WEDGE_DY
        y = bottom + lane * space
        x0 = measure.x + (0.42 + indent) * space
        x1 = measure.right - 0.42 * space
        width = self._w(BARLINE_W)
        self.canvas.line(x0, y - gap0 / 2, x1, y - gap1 / 2, width, self.theme.ink)
        self.canvas.line(x0, y + gap0 / 2, x1, y + gap1 / 2, width, self.theme.ink)

    @staticmethod
    def _wedge_gap(kind: str, ratio: float) -> float:
        """楔形在 ``ratio`` 处的张口（像素）；crescendo 由 0 张开，diminuendo 反之。"""
        clamped = max(0.0, min(1.0, float(ratio)))
        frac = clamped if kind == "crescendo" else 1.0 - clamped
        return WEDGE_GAP * frac

    def _draw_rest(self, cluster: LaidCluster, bottom: float) -> None:
        """休止符：整小节/全音符与二分用横杠，四分用曲线，八分及更短用斜杆 + 旗点。"""
        space = self.space
        x = cluster.x
        if cluster.full_measure or cluster.note_type == "whole":
            y = self._y(bottom, 6)
            self.canvas.rect(
                x - REST_BAR_W / 2 * space, y, REST_BAR_W * space, REST_BAR_H * space, self.theme.ink
            )
            return
        if cluster.note_type == "half":
            y = self._y(bottom, 4)
            self.canvas.rect(
                x - REST_BAR_W / 2 * space, y - REST_BAR_H * space,
                REST_BAR_W * space, REST_BAR_H * space, self.theme.ink,
            )
            return

        mid = self._y(bottom, 4)
        if cluster.note_type == "quarter":
            with self.canvas.group(f"translate({x:.2f},{mid:.2f}) scale({space:.4f})"):
                self.canvas.path(QUARTER_REST, self.theme.ink)
            return

        beams = max(1, cluster.beams)
        self.canvas.line(
            x - 0.30 * space, mid + 0.95 * space, x + 0.30 * space, mid - 0.95 * space,
            self._w(0.235), self.theme.ink,
        )
        for k in range(beams):
            self.canvas.circle(
                x + (0.46 - 0.09 * k) * space,
                mid - (0.52 + 0.46 * k) * space,
                (0.265 - 0.02 * k) * space,
                self.theme.ink,
            )

    # -- 符杠与延音线 ----------------------------------------------------

    def _beams(self, measure: LaidMeasure, bottom: float) -> None:
        """同一连杠组内相邻簇之间画符杠。"""
        groups: Dict[int, List[LaidCluster]] = {}
        for cluster in measure.clusters:
            if cluster.beam_group is not None:
                groups.setdefault(cluster.beam_group, []).append(cluster)
        for members in groups.values():
            for a, b in zip(members, members[1:]):
                self._draw_beam_pair(a, b, bottom)

    def _draw_beam_pair(self, a: LaidCluster, b: LaidCluster, bottom: float) -> None:
        """相邻两簇之间的一至多条符杠（厚度随符杠数叠加）。"""
        space = self.space
        x1, x2 = self._stem_x(a), self._stem_x(b)
        y1, y2 = self._stem_tip_y(a, bottom), self._stem_tip_y(b, bottom)
        thickness = BEAM_THICKNESS * space
        for k in range(min(a.beams, b.beams)):
            if a.stem_up:
                off = k * BEAM_GAP * space
                d = (f"M {x1:.2f},{y1 + off:.2f} L {x2:.2f},{y2 + off:.2f} "
                     f"L {x2:.2f},{y2 + off + thickness:.2f} L {x1:.2f},{y1 + off + thickness:.2f} Z")
            else:
                off = k * BEAM_GAP * space
                d = (f"M {x1:.2f},{y1 - off:.2f} L {x2:.2f},{y2 - off:.2f} "
                     f"L {x2:.2f},{y2 - off - thickness:.2f} L {x1:.2f},{y1 - off - thickness:.2f} Z")
            self.canvas.path(d, self.theme.ink)

    def _draw_ties(self, measure: LaidMeasure, bottom: float) -> None:
        """延音线：由 ``tie_to_next`` 驱动，向后找延续簇。

        - 同一系统内：直接用两端符头 x 连弧（可跨小节线，这是常规记谱）。
        - 跨系统：出侧画到本系统右界，入侧从下一系统左界画到符头，两段各自独立
          （否则一条弧会横穿整页）。
        """
        space = self.space
        for cluster in measure.clusters:
            if cluster.is_rest or not cluster.tie_to_next:
                continue
            for note in cluster.notes:
                found = self._tie_target(cluster, note.note.pitch)
                if found is None:
                    continue
                target_system, target_measure, target = found
                below = not cluster.stem_up
                y = self._y(bottom, note.step_offset)
                x1 = cluster.x + NOTEHEAD_RX * space * 0.95
                if target_system == self._current_system:
                    x2 = target.x - NOTEHEAD_RX * space * 0.95
                    if x2 - x1 >= 0.5 * space:
                        self._tie_arc(x1, y, x2, y, below=below)
                    continue
                x2 = self._system_right - 0.10 * space
                if x2 - x1 >= 0.5 * space:
                    self._tie_arc(x1, y, x2, y, below=below)
                target_bottom = self._staff_bottom.get((target_system, self._current_track))
                if target_bottom is None:  # pragma: no cover - 索引按同轨建立
                    continue
                entry = target_measure.x + 0.10 * space
                exit_x = target.x - NOTEHEAD_RX * space * 0.95
                if exit_x - entry >= 0.5 * space:
                    target_y = self._y(target_bottom, note.step_offset)
                    self._tie_arc(entry, target_y, exit_x, target_y, below=not target.stem_up)

    def _index_ties(self) -> None:
        """建索引：整条轨（跨系统）的簇序列 + 各谱表最下线。

        延音线的延续片段可能落在**下一个小节**甚至**下一个系统**，
        只在小节内查找会漏画；故按轨聚合全曲簇，并记下每个 (系统, 轨) 的谱表 y。
        """
        by_track: Dict[int, List[Tuple[int, LaidMeasure, LaidCluster]]] = {}
        bottoms: Dict[Tuple[int, int], float] = {}
        for s_index, system in enumerate(self.layout.systems):
            for staff in system.staffs:
                bottoms[(s_index, staff.track_index)] = system.y + staff.y
                by_track.setdefault(staff.track_index, []).extend(
                    (s_index, measure, cluster)
                    for measure in staff.measures
                    for cluster in measure.clusters
                )
        self._track_clusters = by_track
        self._staff_bottom = bottoms

    def _tie_target(
        self, cluster: LaidCluster, pitch: int
    ) -> Optional[Tuple[int, LaidMeasure, LaidCluster]]:
        """在本轨中找 ``cluster`` 的延音线延续簇（同声部、同音高、标了 ``tie_from_prev``）。"""
        best: Optional[Tuple[int, LaidMeasure, LaidCluster]] = None
        for item in self._track_clusters.get(self._current_track, ()):
            _, _, candidate = item
            if candidate is cluster or candidate.is_rest:
                continue
            if candidate.voice != cluster.voice or not candidate.tie_from_prev:
                continue
            if candidate.start <= cluster.start + 1e-6:
                continue
            if not any(n.note.pitch == pitch for n in candidate.notes):
                continue
            if best is None or candidate.start < best[2].start:
                best = item
        return best

    def _tie_arc(self, x1: float, y1: float, x2: float, y2: float, *, below: bool) -> None:
        """画一条延音线弧（``below`` 为真时弧向下凸，即符干朝上时置于符头下方）。"""
        bulge = (0.55 if below else -0.55) * self.space
        mid_y = (y1 + y2) / 2.0
        self.canvas.path(
            f"M {x1:.2f},{y1:.2f} Q {(x1 + x2) / 2:.2f},{mid_y + bulge:.2f} {x2:.2f},{y2:.2f}",
            "none", stroke=self.theme.ink, stroke_width=self._w(TIE_W),
        )

    # -- 页眉页脚 --------------------------------------------------------

    def _page_text(self) -> None:
        """标题、署名、速度标记与调号说明。"""
        opts = self.layout.options
        score = self.layout.score
        space = self.space
        title = self.opts.title if self.opts.title is not None else score.title
        composer = self.opts.composer if self.opts.composer is not None else score.composer

        if self.opts.show_title and title:
            self.canvas.text(
                self.layout.page_width / 2.0, opts.margin + 3.0 * space, title,
                2.30 * space, self.theme.ink, self.opts.font_family,
                anchor="middle", weight="bold",
            )
        if composer:
            self.canvas.text(
                self.layout.page_width - opts.margin, opts.margin + 5.5 * space, composer,
                1.25 * space, self.theme.ink, self.opts.font_family,
                anchor="end", style="italic",
            )
        if not self.opts.show_tempo or not self.layout.systems:
            return
        y = self.layout.systems[0].y - 1.0 * space
        mark_right = draw_tempo_mark(
            self.canvas, opts.margin, y, 1.30 * space, self.theme.accent
        )
        self.canvas.text(
            mark_right + 0.45 * space, y, f"= {score.bpm}", 1.30 * space, self.theme.accent,
            self.opts.font_family, weight="bold",
        )
        self.canvas.text(
            self.layout.page_width - opts.margin, y,
            f"{score.key}  ·  {score.time_signature}  ·  {score.bars} 小节",
            1.10 * space, self.theme.faint, self.opts.font_family, anchor="end",
        )


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------


def render_svg(layout: ScoreLayout, options: Optional[SvgOptions] = None) -> str:
    """把排版结果渲染为 SVG 文档字符串。

    Args:
        layout: ``layout_score()`` 的产物。
        options: 渲染选项；None 用默认（白纸黑墨）。

    Returns:
        完整 SVG 文本。
    """
    return ScoreSvgRenderer(layout, options).render()


def write_svg(layout: ScoreLayout, path: str | Path, options: Optional[SvgOptions] = None) -> str:
    """渲染并落盘 .svg。

    Args:
        layout: 排版结果。
        path: 输出路径。
        options: 渲染选项。

    Returns:
        写入的绝对路径。
    """
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_svg(layout, options), encoding="utf-8")
    return str(target)
