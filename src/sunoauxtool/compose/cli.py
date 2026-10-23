"""compose 子命令：清单 → 编排 → MIDI/编排快照（2.0 PR-C/D）。

子命令：
    compose init      生成可直接构建的 song.toml 模板（8 轨 6 段，约 90 秒）
    compose validate  校验清单（结构 44 / 语义 40，错误带 JSON Pointer）
    compose build     song.toml → Arranger 编排 → <name>.mid + arrangement.json
                      `--dry-run` 只打印计划；`--no-arrange` 只用手写音符
    compose render    song.toml → 分轨 WAV + 混音成品（每轨独立渲染后混音）
    compose score     song.toml → 多声部总谱 SVG/PNG/MusicXML（--parts 出分谱）
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

import typer

from sunoauxtool.commands.helpers import _guard, _load_config
from sunoauxtool.compose.arranger import ArrangeOptions, arrange
from sunoauxtool.compose.render import (
    FORMATS,
    RenderOptions,
    backend_summary,
    describe_result,
    render_song,
)
from sunoauxtool.compose.score import (
    SCORE_FORMATS,
    ScoreOptions,
    describe_score,
    export_score,
)
from sunoauxtool.compose.song import Song
from sunoauxtool.compose.spec import SpecError, SongSpec
from sunoauxtool.env import PathResolver
from sunoauxtool.logging_setup import get_logger
from sunoauxtool.midi.io import write_smf
from sunoauxtool.render.backend import FluidSynthBackend

logger = get_logger("compose.cli")

app = typer.Typer(
    help="代码驱动编曲：song.toml → 编排 → MIDI + 编排快照（数字轴入口）",
    no_args_is_help=True,
    add_completion=False,
)

#: 文件名安全化白名单（保留中文，只替换路径分隔符等危险字符）
_UNSAFE = re.compile(r'[\\/:*?"<>|\s]+')

_DRUM_CHANNEL = 9

#: ``compose init`` 模板的段落表：(id, 中文名, 小节数, 能量, 和弦进行)
TEMPLATE_SECTIONS: tuple[tuple[str, str, int, float, tuple[str, ...]], ...] = (
    ("intro", "前奏", 4, 0.20, ("Dm", "Bb", "F", "C")),
    ("verse", "主歌", 8, 0.45, ("Dm", "Bb", "F", "C")),
    ("chorus", "副歌", 8, 0.90, ("Bb", "C", "Dm", "Dm")),
    ("bridge", "间奏", 4, 0.35, ("Gm", "Am")),
    ("final", "副歌再现", 8, 0.95, ("Bb", "C", "Dm", "Dm")),
    ("outro", "尾奏", 4, 0.25, ("Bb", "C", "Dm", "Dm")),
)

#: ``compose init`` 模板的轨道表（role 决定生成策略，mix 决定混音；8 轨）
TEMPLATE_TRACKS: tuple[dict, ...] = (
    {"id": "piano", "name": "钢琴", "role": "harmony", "program": 0,
     "sections": ("intro", "verse", "chorus", "bridge", "final", "outro"),
     "mix": {"gain_db": -6.0, "pan": -0.15, "reverb_send": 0.18}},
    {"id": "epiano", "name": "电钢琴", "role": "harmony", "program": 4, "octave": 5,
     "sections": ("chorus", "final"), "density": 0.4,
     "mix": {"gain_db": -14.0, "pan": 0.25}},
    {"id": "bass", "name": "贝斯", "role": "bass", "program": 33,
     "sections": ("verse", "chorus", "bridge", "final"),
     "mix": {"gain_db": -3.0}},
    {"id": "lead", "name": "主奏旋律", "role": "melody", "density": 0.6,
     "sections": ("verse", "chorus", "final")},
    {"id": "oboe", "name": "双簧管对位", "role": "counter",
     "sections": ("final",), "mix": {"gain_db": -8.0, "pan": 0.35}},
    {"id": "harp", "name": "竖琴琶音", "role": "arpeggio",
     "sections": ("chorus", "final"), "mix": {"gain_db": -9.0, "pan": -0.3}},
    {"id": "drums", "name": "鼓组", "role": "drums", "channel": _DRUM_CHANNEL,
     "groove": "pop", "sections": ("verse", "chorus", "final"),
     "mix": {"gain_db": -4.0}},
    {"id": "strings", "name": "弦乐铺底", "role": "pad",
     "sections": ("intro", "outro"),
     "mix": {"gain_db": -10.0, "pan": 0.2, "reverb_send": 0.35}},
)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _slug(text: str, fallback: str = "song") -> str:
    """曲名 → 文件名主干（中文保留，危险字符折叠为 ``_``）。"""
    cleaned = _UNSAFE.sub("_", str(text).strip()).strip("._")
    return cleaned or fallback


def _toml_string(value: str) -> str:
    """最小 TOML 基本字符串转义。"""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return f"{value:g}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{key} = {_toml_value(item)}"
                                for key, item in value.items()) + " }"
    return _toml_string(str(value))


def _template(title: str) -> str:
    """生成可直接构建的模板（8 轨 6 段；音符全部由 Arranger 生成）。"""
    lines = [
        "# SunoAuxTool 2.0 编曲清单（song.toml）",
        "# 只需写段落 + 角色，compose build 时由 Arranger 按 energy 生成音符；",
        "# 想手写音符可加 [[track.note]]（beat 为全曲绝对拍：4/4 下 1 小节 = 4 拍），",
        "# 有显式音符的轨道不再自动生成。",
        "schema = 1",
        "",
        "[song]",
        f"title = {_toml_string(title)}",
        'key = "D minor"',
        "tempo = 96",
        'meter = "4/4"',
        "seed = 20261024",
        "",
        "# ---- 段落：energy 决定配器密度（0.0-0.3 铺底 / 0.3-0.6 节奏组 /"
        " 0.6-0.85 加旋律 / 0.85+ 加对位与琶音）----",
    ]
    for section_id, name, bars, energy, progression in TEMPLATE_SECTIONS:
        lines += [
            "[[section]]",
            f'id = "{section_id}"',
            f"name = {_toml_string(name)}",
            f"bars = {bars}",
            f"energy = {energy:g}",
            f"progression = {_toml_value(list(progression))}",
            "",
        ]

    lines.append("# ---- 轨道：role 决定生成策略，program 决定 GM 音色 ----")
    for track in TEMPLATE_TRACKS:
        lines.append("[[track]]")
        for key, value in track.items():
            if key == "sections":
                lines.append(f"sections = {_toml_value(list(value))}")
            else:
                lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _describe(spec: SongSpec, song: Song | None = None) -> list[str]:
    """人类可读摘要（validate / build 共用）。"""
    notes = sum(len(clip.notes) for track in song.tracks for clip in track.clips) \
        if song is not None else sum(len(track.notes) for track in spec.tracks)
    bars = spec.total_bars
    duration = song.duration_beats if song is not None else spec.total_bars * spec.bar_length()
    seconds = song.duration_seconds() if song is not None else 0.0
    line = (
        f"曲名 {spec.title or '(未命名)'}｜调性 {spec.key}｜"
        f"速度 {spec.tempo:g} BPM｜拍号 {spec.meter}"
    )
    result = [
        line,
        f"段落 {len(spec.sections)} 个 / {bars} 小节｜"
        f"轨道 {len(spec.tracks)} 条｜音符 {notes} 个",
    ]
    if song is not None:
        result.append(f"时长 {duration:g} 拍 = {seconds:.2f} 秒")
    else:
        result.append("（validate 不生成音符；build 时由 Arranger 按 energy 生成）"
                      if notes == 0 else f"时长 {duration:g} 拍")
    return result


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------

@app.command("init")
@_guard
def init(
    name: str = typer.Argument(..., help="工程名（默认生成 <name>/song.toml）"),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="输出目录（默认当前目录下的 <name>/）"
    ),
    title: Optional[str] = typer.Option(None, "--title", help="曲名（默认同工程名）"),
    force: bool = typer.Option(False, "--force", help="覆盖已存在的 song.toml"),
) -> None:
    """生成 song.toml 模板（8 轨 6 段，写完立刻编排一遍自检）。"""
    target_dir = Path(out) if out is not None else Path(name)
    target = target_dir / "song.toml"
    if target.exists() and not force:
        typer.echo(f"已存在 {target}（如需覆盖加 --force）", err=True)
        raise typer.Exit(1)
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(_template(title or name), encoding="utf-8")

    spec = SongSpec.from_toml(target)
    song = arrange(spec)  # 自检：模板必须能编排出来
    typer.echo(f"已生成 {target}")
    for line in _describe(spec, song):
        typer.echo(f"  {line}")
    typer.echo(f"  下一步：sunoaux compose build {target}")


@app.command("validate")
@_guard
def validate_cmd(
    path: Path = typer.Argument(..., help="song.toml 路径"),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出结果（AI/CI 友好）"),
) -> None:
    """校验清单的语法、结构与语义。"""
    if not path.exists():
        typer.echo(f"清单不存在: {path}", err=True)
        raise typer.Exit(1)
    if as_json:
        try:
            spec = SongSpec.from_toml(path)
            spec.validate()
        except SpecError as exc:
            typer.echo(json.dumps({"ok": False, **exc.to_dict()}, ensure_ascii=False))
            raise typer.Exit(exc.code) from exc
        typer.echo(json.dumps({"ok": True, "summary": spec.summary()}, ensure_ascii=False))
        return

    spec = SongSpec.from_toml(path)
    spec.validate()
    typer.echo(f"校验通过: {path}")
    for line in _describe(spec):
        typer.echo(f"  {line}")


@app.command("build")
@_guard
def build(
    path: Path = typer.Argument(..., help="song.toml 路径"),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="输出目录（默认 out/<曲名>/）"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印计划，不写文件"),
    no_arrange: bool = typer.Option(
        False, "--no-arrange", help="不做自动编排（只用清单里的[[track.note]]）"
    ),
    no_humanize: bool = typer.Option(
        False, "--no-humanize", help="关闭力度/时序抖动（严格对齐网格）"
    ),
) -> None:
    """构建成品 MIDI（format 1）+ 编排快照（arrangement.json）。"""
    if not path.exists():
        typer.echo(f"清单不存在: {path}", err=True)
        raise typer.Exit(1)

    spec = SongSpec.from_toml(path)
    options = ArrangeOptions(humanize=not no_humanize)
    song = spec.to_song() if no_arrange else arrange(spec, options)
    document = song.to_midi()

    base = _slug(song.title or path.parent.name)
    out_dir = Path(out) if out is not None else Path("out") / base
    midi_path = out_dir / f"{base}.mid"
    snapshot_path = out_dir / "arrangement.json"

    channels = "、".join(f"{track.name}={track.channel}" for track in document.tracks) \
        or "（无轨道）"
    plan = [
        *_describe(spec, song),
        f"通道分配 {channels}",
        f"输出 {midi_path}｜{snapshot_path}",
    ]
    if dry_run:
        typer.echo("[dry-run] 计划：")
        for line in plan:
            typer.echo(f"  {line}")
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    write_smf(document, midi_path)
    snapshot_path.write_text(
        json.dumps(song.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    typer.echo(f"已构建 {midi_path}")
    typer.echo(f"  编排快照 {snapshot_path}")
    for line in plan[:-1]:
        typer.echo(f"  {line}")


@app.command("render")
@_guard
def render(
    ctx: typer.Context,
    path: Path = typer.Argument(..., help="song.toml 路径"),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="输出目录（默认 out/<曲名>/）"
    ),
    soundfont: Optional[str] = typer.Option(
        None, "--soundfont", help="SoundFont 路径（覆盖配置）"
    ),
    fluidsynth: Optional[str] = typer.Option(
        None, "--fluidsynth", help="fluidsynth 可执行文件路径（覆盖配置）"
    ),
    stems: bool = typer.Option(True, "--stems/--no-stems", help="是否导出分轨"),
    fmt: str = typer.Option("wav", "--format", help="成品格式：wav / flac"),
    sample_rate: int = typer.Option(44100, "--sample-rate", help="采样率"),
    no_normalize: bool = typer.Option(
        False, "--no-normalize", help="不做峰值归一（原样输出，可能削顶）"
    ),
    no_arrange: bool = typer.Option(
        False, "--no-arrange", help="不做自动编排（只用清单里的 [[track.note]]）"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印计划，不写文件"),
) -> None:
    """渲染成品音频：每轨独立渲染成分轨，再按混音通道条叠加。"""
    if not path.exists():
        typer.echo(f"清单不存在: {path}", err=True)
        raise typer.Exit(1)
    if fmt not in FORMATS:
        typer.echo(f"不支持的格式 {fmt!r}；可用: {', '.join(sorted(FORMATS))}", err=True)
        raise typer.Exit(1)

    spec = SongSpec.from_toml(path)
    song = spec.to_song() if no_arrange else arrange(spec)
    base = _slug(song.title or path.parent.name)
    out_dir = Path(out) if out is not None else Path("out") / base

    cfg = _load_config(ctx)
    merged = cfg.merge_cli(soundfont=soundfont, fluidsynth=fluidsynth)
    resolver = PathResolver(merged)
    if dry_run:
        sf_path, fs_path = merged.paths.soundfont, merged.paths.fluidsynth
    else:
        resolver.ensure_ready()
        sf_path, fs_path = resolver.resolve_soundfont(), resolver.resolve_fluidsynth()

    backend = FluidSynthBackend(str(fs_path))
    options = RenderOptions(
        sample_rate=sample_rate,
        fmt=fmt,
        stems=stems,
        normalize=not no_normalize,
        dry_run=dry_run,
    )
    result = render_song(song, out_dir=out_dir, backend=backend, soundfont=str(sf_path),
                         options=options)

    prefix = "[dry-run] " if dry_run else ""
    if dry_run:
        typer.echo(f"{prefix}计划渲染 {len(song.tracks)} 轨 → {out_dir}")
        typer.echo(f"  {backend_summary(backend)}")
    for line in describe_result(result):
        typer.echo(f"  {line}")


@app.command("score")
@_guard
def score(
    path: Path = typer.Argument(..., help="song.toml 路径"),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="输出目录（默认 out/<曲名>/score）"
    ),
    formats: str = typer.Option("svg", "--format", help="svg,png,musicxml（逗号分隔）"),
    parts: bool = typer.Option(False, "--parts", help="额外导出每声部分谱"),
    composer: str = typer.Option("", "--composer", help="作曲者署名"),
    no_part_groups: bool = typer.Option(
        False, "--no-part-groups", help="MusicXML 不输出分谱组括号"
    ),
    scale: float = typer.Option(2.0, "--scale", help="PNG 超采样倍数"),
    no_arrange: bool = typer.Option(
        False, "--no-arrange", help="不做自动编排（只用清单里的 [[track.note]]）"
    ),
) -> None:
    """导出多声部总谱（SVG/PNG/MusicXML，可选分谱）。"""
    if not path.exists():
        typer.echo(f"清单不存在: {path}", err=True)
        raise typer.Exit(1)

    wanted = tuple(part.strip().lower() for part in str(formats).split(",") if part.strip())
    unknown = [fmt for fmt in wanted if fmt not in SCORE_FORMATS and fmt != "xml"]
    if unknown:
        typer.echo(
            f"不支持的格式 {unknown}；可用: {', '.join(sorted(SCORE_FORMATS))}", err=True
        )
        raise typer.Exit(1)

    spec = SongSpec.from_toml(path)
    song = spec.to_song() if no_arrange else arrange(spec)
    base = _slug(song.title or path.parent.name)
    out_dir = Path(out) if out is not None else Path("out") / base / "score"

    result = export_score(
        song,
        out_dir=out_dir,
        options=ScoreOptions(
            formats=wanted,
            parts=parts,
            composer=composer,
            part_groups=not no_part_groups,
            scale=scale,
        ),
    )
    typer.echo(f"已导出总谱 → {out_dir}")
    for line in describe_score(result):
        typer.echo(f"  {line}")
