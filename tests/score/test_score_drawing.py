"""共享绘制层测试：指令流 → SVG / PNG 两个序列化器。

为什么单独测这一层
------------------
五线谱与简谱都只往 ``Canvas`` 里画，几何完全由这一层序列化。PNG 又**无法用文本断言**——
一旦光栅化侧（组变换、路径展平、颜色解析）出偏差，产物会静默变错而没人发现。
所以这里不依赖任何谱面渲染器，直接用最小图元做**像素探针**：指定坐标上有墨 / 没墨，
这是唯一能真正证明「PNG 画对了」的手段。
"""

from __future__ import annotations

import io
import struct

import pytest

from sunoauxtool.score.drawing import Canvas, Op, parse_color, to_png, to_svg
from sunoauxtool.score.drawing import _flatten_path

PAGE = (200.0, 120.0)
PAPER = "#ffffff"
INK = "#000000"


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def render(canvas: Canvas, *, scale: float = 1.0) -> bytes:
    """把画布光栅化为 PNG（默认 1 倍，便于按像素断言）。"""
    return to_png(canvas.ops, PAGE[0], PAGE[1], PAPER, scale=scale)


def pixels(data: bytes):
    """读为 Pillow 图像（PIL 缺失时跳过）。"""
    Image = pytest.importorskip("PIL.Image", reason="PIL 未安装")
    return Image.open(io.BytesIO(data)).convert("RGB")


def is_dark(png, x: int, y: int, threshold: int = 128) -> bool:
    """该像素是否「有墨」。"""
    px = png.getpixel((x, y))
    return sum(px) / 3 < threshold


def png_size(data: bytes) -> tuple[int, int]:
    return struct.unpack(">II", data[16:24])


# ---------------------------------------------------------------------------
# 颜色
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("#ffffff", (255, 255, 255)),
        ("#000000", (0, 0, 0)),
        ("#000", (0, 0, 0)),
        ("#141414", (20, 20, 20)),
        ("none", None),
        ("", None),
    ],
)
def test_parse_color(value, expected):
    assert parse_color(value) == expected


def test_parse_color_rejects_unknown_length():
    with pytest.raises(ValueError, match="颜色"):
        parse_color("#12345")


# ---------------------------------------------------------------------------
# 指令流
# ---------------------------------------------------------------------------


def test_canvas_records_ops_in_order():
    canvas = Canvas()
    canvas.line(0, 0, 1, 1, 1.0, INK)
    canvas.circle(5, 5, 2, INK)
    assert [op.kind for op in canvas.ops] == ["line", "circle"]
    assert len(canvas) == 2


def test_canvas_group_nests_children_into_one_op():
    canvas = Canvas()
    with canvas.group("translate(10,10)"):
        canvas.line(0, 0, 1, 1, 1.0, INK)
        canvas.circle(0, 0, 1, INK)
    assert len(canvas) == 1
    op = canvas.ops[0]
    assert op.kind == "group"
    assert op.data[0] == "translate(10,10)"
    assert [child.kind for child in op.data[1]] == ["line", "circle"]


def test_canvas_group_restores_outer_frame():
    canvas = Canvas()
    with canvas.group("translate(1,1)"):
        canvas.line(0, 0, 1, 1, 1.0, INK)
    canvas.line(2, 2, 3, 3, 1.0, INK)
    assert [op.kind for op in canvas.ops] == ["group", "line"]


# ---------------------------------------------------------------------------
# SVG 序列化
# ---------------------------------------------------------------------------


def test_to_svg_serializes_every_primitive():
    canvas = Canvas()
    canvas.line(1, 2, 3, 4, 0.5, INK)
    canvas.rect(1, 2, 3, 4, INK)
    canvas.circle(5, 6, 7, INK)
    canvas.ellipse(8, 9, 10, 11, 12.0, INK)
    canvas.path("M 0,0 L 1,1", INK)
    canvas.text(1, 2, "x", 10.0, INK, "serif")
    svg = to_svg(canvas.ops, PAGE[0], PAGE[1], PAPER)
    for tag in ("<line", "<rect", "<circle", "<ellipse", "<path", "<text"):
        assert tag in svg
    assert svg.startswith(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200.0 120.0"'
    )


def test_to_svg_escapes_xml_in_text():
    canvas = Canvas()
    canvas.text(0, 0, "a&b<c>", 10.0, INK, "serif")
    svg = to_svg(canvas.ops, PAGE[0], PAGE[1], PAPER)
    assert "a&amp;b&lt;c&gt;" in svg


def test_to_svg_includes_extra_defs_when_given():
    svg = to_svg((), PAGE[0], PAGE[1], PAPER, extra_defs="<style>x</style>")
    assert "<style>x</style>" in svg


def test_to_svg_rejects_unknown_op():
    with pytest.raises(ValueError, match="未知绘制指令"):
        to_svg([Op("bogus", ())], PAGE[0], PAGE[1], PAPER)


# ---------------------------------------------------------------------------
# 路径展平
# ---------------------------------------------------------------------------


def test_flatten_path_reads_polyline():
    subpaths = _flatten_path("M 0,0 L 10,0 L 10,10", None)
    assert len(subpaths) == 1
    points, closed = subpaths[0]
    assert points == [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
    assert closed is False


def test_flatten_path_marks_closed_subpath():
    subpaths = _flatten_path("M 0,0 L 10,0 L 10,10 Z", None)
    assert subpaths[0][1] is True


def test_flatten_path_subdivides_curves():
    cubic = _flatten_path("M 0,0 C 0,10 10,10 10,0", None)[0][0]
    quad = _flatten_path("M 0,0 Q 5,10 10,0", None)[0][0]
    assert len(cubic) > 4
    assert len(quad) > 4
    assert cubic[-1] == pytest.approx((10.0, 0.0))
    assert quad[-1] == pytest.approx((10.0, 0.0))


def test_flatten_path_applies_translate_and_scale():
    subpaths = _flatten_path("M 0,0 L 1,1", "translate(10,20) scale(2,2)")
    assert subpaths[0][0] == [(10.0, 20.0), (12.0, 22.0)]


def test_flatten_path_drops_degenerate_subpaths():
    assert _flatten_path("M 5,5", None) == []


# ---------------------------------------------------------------------------
# PNG 光栅化：尺寸、错误
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scale", [1.0, 2.0, 3.0])
def test_png_output_size_is_independent_of_supersampling(scale):
    canvas = Canvas()
    canvas.line(0, 0, 10, 10, 1.0, INK)
    assert png_size(render(canvas, scale=scale)) == (200, 120)


@pytest.mark.parametrize("scale", [0.0, -2.0])
def test_png_rejects_non_positive_scale(scale):
    with pytest.raises(ValueError, match="scale"):
        render(Canvas(), scale=scale)


def test_png_raises_import_error_without_pillow(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "PIL", None)
    with pytest.raises(ImportError, match="Pillow"):
        render(Canvas())


def test_png_uses_background_color():
    canvas = Canvas()
    data = to_png(canvas.ops, PAGE[0], PAGE[1], "#204060")
    assert pixels(data).getpixel((5, 5)) == (32, 64, 96)


# ---------------------------------------------------------------------------
# PNG 光栅化：像素探针
# ---------------------------------------------------------------------------


def test_png_draws_line_at_requested_position():
    canvas = Canvas()
    canvas.line(20, 40, 180, 40, 4.0, INK)
    png = pixels(render(canvas))
    assert is_dark(png, 100, 40)
    assert not is_dark(png, 100, 90)


def test_png_draws_filled_rectangle():
    canvas = Canvas()
    canvas.rect(20, 20, 60, 40, INK)
    png = pixels(render(canvas))
    assert is_dark(png, 50, 40)
    assert not is_dark(png, 150, 40)


def test_png_draws_filled_circle():
    canvas = Canvas()
    canvas.circle(100, 60, 20, INK)
    png = pixels(render(canvas))
    assert is_dark(png, 100, 60)
    assert not is_dark(png, 100, 20)


def test_png_draws_rotated_ellipse_inside_its_extent():
    canvas = Canvas()
    canvas.ellipse(100, 60, 40, 10, 0.0, INK)
    png = pixels(render(canvas))
    assert is_dark(png, 130, 60)
    assert not is_dark(png, 130, 90)


def test_png_strokes_an_open_path():
    canvas = Canvas()
    canvas.path("M 20,80 L 180,80", "none", stroke=INK, stroke_width=4.0)
    png = pixels(render(canvas))
    assert is_dark(png, 100, 80)


def test_png_fills_a_closed_path():
    canvas = Canvas()
    canvas.path("M 20,20 L 180,20 L 180,100 Z", INK)
    png = pixels(render(canvas))
    assert is_dark(png, 150, 60)


def test_png_draws_text_ink():
    """字体不可控，故用「加了文字与不加文字的位图不同」来判定有墨。"""
    blank = Canvas()
    with_text = Canvas()
    with_text.text(20, 60, "Ag", 24.0, INK, "serif")
    assert render(blank) != render(with_text)


@pytest.mark.parametrize(
    ("transform", "probe", "inside"),
    [
        ("translate(120,60) scale(1,1)", (120, 60), True),
        ("translate(120,60) scale(1,1)", (20, 60), False),
        ("translate(120,60) scale(2,2)", (128, 60), True),
        ("translate(120,60) scale(2,2)", (140, 60), False),
    ],
)
def test_png_applies_group_transform_to_circle(transform, probe, inside):
    canvas = Canvas()
    with canvas.group(transform):
        canvas.circle(0, 0, 6, INK)
    assert is_dark(pixels(render(canvas)), *probe) is inside


def test_png_applies_group_transform_to_rect():
    canvas = Canvas()
    with canvas.group("translate(100,50) scale(2,2)"):
        canvas.rect(0, 0, 10, 5, INK)
    png = pixels(render(canvas))
    assert is_dark(png, 110, 55)          # 缩放后覆盖 (100,50)-(120,60)
    assert not is_dark(png, 20, 55)


def test_png_applies_group_transform_to_line():
    canvas = Canvas()
    with canvas.group("translate(0,80) scale(1,1)"):
        canvas.line(20, 0, 180, 0, 4.0, INK)
    png = pixels(render(canvas))
    assert is_dark(png, 100, 80)
    assert not is_dark(png, 100, 20)


def test_png_applies_group_transform_to_path():
    canvas = Canvas()
    with canvas.group("translate(100,60) scale(1,1)"):
        canvas.path("M -10,0 L 10,0", "none", stroke=INK, stroke_width=4.0)
    png = pixels(render(canvas))
    assert is_dark(png, 100, 60)
    assert not is_dark(png, 20, 60)


def test_png_path_transform_is_applied_exactly_once():
    """回归：``_flatten_path`` 已施加 transform，``_draw_op`` 再乘一遍会把图形甩出页面。

    五线谱的符尾正是这个用法：路径坐标先除以 ``space``，再靠 ``transform=scale(space)``
    还原回页面坐标。若变换施加两次，符尾会被放大 ``space`` 倍并移出页面 ——
    而 PNG 看不见，只能靠像素断言。
    """
    canvas = Canvas()
    canvas.path("M 2,6 L 18,6", "none", stroke=INK, stroke_width=4.0, transform="scale(10)")
    png = pixels(render(canvas))
    assert is_dark(png, 100, 60)     # (10, 6) * 10 -> 线段中段
    assert is_dark(png, 20, 60)      # (2, 6) * 10 -> 起点
    assert not is_dark(png, 10, 60)  # 施加两次会把起点推到 x=200（页外），此处才是空的


def test_png_group_and_path_transform_compose_once():
    canvas = Canvas()
    with canvas.group("translate(50,20) scale(2,2)"):
        canvas.path("M 5,5 L 15,5", "none", stroke=INK, stroke_width=2.0, transform="scale(2)")
    # (10,5) --scale(2)--> (20,10) --translate(50,20) scale(2)--> (90,40)
    png = pixels(render(canvas))
    assert is_dark(png, 90, 40)
    assert not is_dark(png, 10, 10)


def test_png_rejects_unknown_op_inside_group():
    canvas = Canvas()
    with canvas.group("translate(0,0)"):
        pass
    canvas._frames[0][-1] = Op("group", ("translate(0,0)", (Op("bogus", ()),)))
    with pytest.raises(ValueError, match="组变换不支持"):
        render(canvas)


def test_png_rejects_unknown_op():
    canvas = Canvas()
    canvas._emit(Op("bogus", ()))
    with pytest.raises(ValueError, match="未知绘制指令"):
        render(canvas)


def test_png_supersampling_is_antialiased_but_same_size():
    """2 倍超采样后缩回原尺寸：尺寸不变、边缘更柔和（字节不同）。"""
    canvas = Canvas()
    canvas.path("M 20,20 Q 100,100 180,20", "none", stroke=INK, stroke_width=3.0)
    coarse = render(canvas, scale=1.0)
    fine = render(canvas, scale=2.0)
    assert png_size(coarse) == png_size(fine)
    assert coarse != fine
