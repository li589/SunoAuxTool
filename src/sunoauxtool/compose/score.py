"""总谱导出（2.0 PR-F）：``Song`` → 多声部总谱 → SVG/PNG/MusicXML（可分谱）。

与 ``score`` 包的分工：

- ``score.model``：``Score``/``ScoreTrack`` 模型（本 PR 增加 ``transpose``/``part_group``
  与 ``Score.from_song()``）；
- ``score.layout`` / ``svg`` / ``png``：排版与绘制（几何铁律不动）；
- ``score.musicxml``：交付格式（本 PR 增加 ``<transpose>`` 与 ``<part-group>``）；
- **本模块**：决定"从 Song 造谱、导出哪些格式、要不要出分谱"。

注意：**SVG/PNG 按实音（concert pitch）绘制**，移调只体现在 MusicXML 的记谱音高里。
移调乐器在本 PR 的图形谱面上仍按实音显示（PR-F 范围），MusicXML 已是标准记谱。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from sunoauxtool.logging_setup import get_logger
from sunoauxtool.score.layout import LayoutOptions, layout_score, paginate_layout
from sunoauxtool.score.model import Score
from sunoauxtool.score.musicxml import write_musicxml
from sunoauxtool.score.png import write_png
from sunoauxtool.score.svg import write_svg

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from sunoauxtool.compose.song import Song

logger = get_logger("compose.score")

#: 支持的导出格式 → 扩展名
SCORE_FORMATS = {"svg": ".svg", "png": ".png", "musicxml": ".musicxml"}


@dataclass
class ScoreOptions:
    """总谱导出参数。"""

    formats: tuple[str, ...] = ("svg",)
    parts: bool = False
    composer: str = ""
    clefs: dict[str, str] | None = None
    part_groups: bool = True
    scale: float = 2.0
    grand_staff: bool = True
    systems_per_page: int = 0

    def normalized_formats(self) -> tuple[str, ...]:
        """校验并归一化格式列表（去重、保持顺序、小写）。"""
        result: list[str] = []
        for fmt in self.formats:
            key = str(fmt).strip().lower()
            if key == "xml":
                key = "musicxml"
            if key not in SCORE_FORMATS:
                raise ValueError(
                    f"不支持的总谱格式 {fmt!r}；可用: {', '.join(sorted(SCORE_FORMATS))}"
                )
            if key not in result:
                result.append(key)
        return tuple(result)


@dataclass
class ScoreResult:
    """谱面产物清单。"""

    out_dir: Path
    full: dict[str, Path] = field(default_factory=dict)
    parts: dict[str, dict[str, Path]] = field(default_factory=dict)
    score: Score | None = None

    @property
    def part_count(self) -> int:
        return len(self.parts)

    def paths(self) -> list[Path]:
        """全部产物路径（总谱 + 分谱）。"""
        files = list(self.full.values())
        for per_part in self.parts.values():
            files.extend(per_part.values())
        return files


def build_score(
    song: "Song",
    *,
    composer: str = "",
    clefs: dict[str, str] | None = None,
    part_groups: bool = True,
    grand_staff: bool = True,
) -> Score:
    """把编排好的 ``Song`` 转成 ``Score``（多轨、移调、分谱组、2.6 大谱表）。"""
    song.validate()
    return Score.from_song(
        song, composer=composer, clefs=clefs, part_groups=part_groups,
        grand_staff=grand_staff,
    )


def _write(score: Score, out_dir: Path, stem: str, formats: tuple[str, ...],
           *, scale: float, part_groups: bool = True,
           systems_per_page: int = 0) -> dict[str, Path]:
    """按格式渲染一份谱面，返回 ``{格式: 路径}``。

    ``part_groups=False`` 时 MusicXML 不输出任何分组标记（bracket 与 brace 都不写）。
    """
    from sunoauxtool.score.musicxml import MusicXmlOptions

    written: dict[str, Path] = {}
    layout = None
    for fmt in formats:
        target = out_dir / f"{stem}{SCORE_FORMATS[fmt]}"
        if fmt == "musicxml":
            written[fmt] = Path(write_musicxml(
                score, target, MusicXmlOptions(include_part_groups=part_groups)
            ))
            continue
        if layout is None:
            layout = layout_score(
                score, LayoutOptions(systems_per_page=0)  # 先按单页排版再切页
            )
        pages = paginate_layout(layout, systems_per_page)
        if len(pages) == 1:
            if fmt == "svg":
                written[fmt] = Path(write_svg(layout, target))
            else:  # png
                written[fmt] = Path(write_png(layout, target, scale=scale))
            continue
        # 多页：每页一个文件（<stem>-page01.svg …）
        for index, page in enumerate(pages, start=1):
            page_target = out_dir / f"{stem}-page{index:02d}{SCORE_FORMATS[fmt]}"
            if fmt == "svg":
                write_svg(page, page_target)
            else:
                write_png(page, page_target, scale=scale)
            if index == 1:
                written[fmt] = Path(page_target)
    return written


def export_score(
    song: "Song",
    *,
    out_dir: Path,
    options: ScoreOptions | None = None,
) -> ScoreResult:
    """导出总谱（可选分谱）。

    Args:
        song: 已编排的曲子。
        out_dir: 输出目录。
        options: 导出参数（格式、是否出分谱、署名）。

    Returns:
        :class:`ScoreResult`：``full``（总谱各格式）+ ``parts``（分谱各格式）。

    Raises:
        ValueError: 不支持的格式。
    """
    options = options or ScoreOptions()
    formats = options.normalized_formats()
    score = build_score(
        song, composer=options.composer, clefs=options.clefs,
        part_groups=options.part_groups, grand_staff=options.grand_staff,
    )
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(score.title or "score")
    result = ScoreResult(out_dir=out, score=score)
    result.full = _write(score, out, stem, formats, scale=options.scale,
                          part_groups=options.part_groups,
                          systems_per_page=options.systems_per_page)

    if options.parts and len(score.tracks) > 1:
        parts_dir = out / "parts"
        parts_dir.mkdir(parents=True, exist_ok=True)
        singles = score.parts()
        for index, track in enumerate(score.tracks):
            single = singles[index]
            name = _safe_name(track.name or f"part{index + 1}")
            result.parts[track.name or name] = _write(
                single, parts_dir, name, formats, scale=options.scale,
                part_groups=options.part_groups,
                systems_per_page=options.systems_per_page,
            )
    logger.info(
        "总谱导出完成：%s（%d 轨，格式 %s，分谱 %d）",
        out, len(score.tracks), "/".join(formats), len(result.parts),
    )
    return result


def _safe_name(text: str) -> str:
    """文件名安全化（保留中文）。"""
    keep = ["_" if char in '\\/:*?"<>| \t' else char for char in str(text).strip()]
    return "".join(keep).strip("._") or "score"


def describe_score(result: ScoreResult) -> list[str]:
    """人类可读摘要。"""
    score = result.score
    lines = []
    if score is not None:
        transposing = [track.name for track in score.tracks if track.is_transposing]
        # 大谱表的两行算一个声部（brace 相同的连续轨合并计数）
        voices = 0
        last_brace = None
        for track in score.tracks:
            brace = track.brace or ""
            if brace and brace == last_brace:
                continue
            voices += 1
            last_brace = brace
        lines.append(
            f"总谱 {score.title}：{voices} 声部｜{score.bars} 小节｜"
            f"{score.key}｜{score.time_signature}｜{score.note_count} 音符"
        )
        if transposing:
            lines.append(f"移调声部（MusicXML 记谱）：{'、'.join(transposing)}")
    if result.full:
        lines.append("总谱：" + "、".join(path.name for path in result.full.values()))
    if result.parts:
        lines.append(f"分谱 {len(result.parts)} 份（{result.out_dir / 'parts'}）")
    return lines
