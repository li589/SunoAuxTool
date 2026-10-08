"""CLI 入口（Typer 应用）。

子命令结构：
    sunoauxtool generate midi / generate melody
    sunoauxtool render
    sunoauxtool export suno / suno-pack / suno-manifest
    sunoauxtool pipeline
    sunoauxtool batch            （P1-3 完整实现）
    sunoauxtool config init / config show
    sunoauxtool play / doctor / diff / new
    sunoauxtool tempo / transcribe    （#13 音频测速 + WAV→MIDI 转谱）
    sunoauxtool inspire init / add / list / show / rm / export
    sunoauxtool errors           （P2-3 错误码表）
    sunoauxtool ai musicgen / ai diffrhythm   （AI 适配器，已完整实现）

错误处理：统一捕获 SmartNoteGenError -> 映射退出码 + 友好提示（--debug 才打印堆栈）。

辅助函数见 commands/helpers.py（拆分自 cli.py，保持导入兼容）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

import typer

from sunoauxtool import __version__
from sunoauxtool.commands.helpers import (
    _apply_detected_to_config,
    _config_prompt,
    _diff_metadata,
    _diff_row,
    _doctor_item,
    _export_opts_from_config,
    _get_duration,
    _guard,
    _load_config,
    _prompt,
    _request_from_config,
    _write_single_metadata,
    set_debug,
)
from sunoauxtool.config import Config
from sunoauxtool.env import PathResolver
from sunoauxtool.exceptions import (
    BatchFailedError,
    BatchPartialError,
    ERROR_CODES,
    InputFileError,
    ParameterError,
)
from sunoauxtool.export.suno import SunoExporter
from sunoauxtool.generators.music21_melody import Music21MelodyGenerator
from sunoauxtool.generators.procedural import ProceduralGenerator
from sunoauxtool.logging_setup import get_logger, setup_logging
from sunoauxtool.models.midi import MidiDocument
from sunoauxtool.output_manager import ArtifactMeta, OutputManager
from sunoauxtool.render.fluidsynth import FluidSynthRenderer

logger = get_logger("cli")

app = typer.Typer(
    name="sunoauxtool",
    help="本地 AI 音乐生成 CLI：程序化 MIDI → 渲染 WAV → Suno 合规导出",
    no_args_is_help=True,
    add_completion=False,
)

generate_app = typer.Typer(help="生成 MIDI/旋律", no_args_is_help=True)
export_app = typer.Typer(help="导出音频", no_args_is_help=True)
config_app = typer.Typer(help="配置管理", no_args_is_help=True)
ai_app = typer.Typer(help="AI 模型适配器（P1）", no_args_is_help=True)
inspire_app = typer.Typer(help="灵感库管理（SQLite 存储）")

app.add_typer(generate_app, name="generate")
app.add_typer(export_app, name="export")
app.add_typer(config_app, name="config")
app.add_typer(ai_app, name="ai")
app.add_typer(inspire_app, name="inspire")


# ---------------------------------------------------------------------------
# 顶层回调
# ---------------------------------------------------------------------------

def _version_callback(value: bool) -> None:
    """--version：打印版本号并退出。"""
    if value:
        typer.echo(f"sunoauxtool {__version__}")
        raise typer.Exit(0)


@app.callback()
def main(
    ctx: typer.Context,
    config: Optional[Path] = typer.Option(
        None, "--config", "-c", help="配置文件路径（默认查找项目根 sunoauxtool.toml）"
    ),
    verbose: bool = typer.Option(False, "--verbose", help="输出 DEBUG 级日志"),
    quiet: bool = typer.Option(False, "--quiet", help="仅输出 ERROR 级日志"),
    debug: bool = typer.Option(False, "--debug", help="DEBUG 级日志 + 打印堆栈"),
    version: bool = typer.Option(
        None, "--version", help="显示版本号", callback=_version_callback, is_eager=True
    ),
) -> None:
    """SunoAuxTool 主入口。"""
    set_debug(debug)
    setup_logging(verbose=verbose, quiet=quiet, debug=debug)
    ctx.obj = {"config_path": config}


# ---------------------------------------------------------------------------
# generate midi
# ---------------------------------------------------------------------------

@generate_app.command("midi", help="程序化生成多轨 MIDI（和弦/旋律/贝斯 [+鼓]）")
@_guard
def generate_midi(
    ctx: typer.Context,
    chords: Optional[str] = typer.Option(None, "--chords", help="和弦进行，如 C-G-Am-F"),
    bpm: Optional[int] = typer.Option(None, "--bpm", help="速度"),
    key: Optional[str] = typer.Option(None, "--key", help="调式，如 'C major'"),
    time_signature: Optional[str] = typer.Option(
        None, "--time-signature", help="拍号，如 '4/4'"
    ),
    bars: Optional[int] = typer.Option(None, "--bars", help="小节数"),
    style: Optional[str] = typer.Option(None, "--style", help="风格 pop/rock/electronic/classical 或自定义"),
    seed: Optional[int] = typer.Option(None, "--seed", help="随机种子（可复现）"),
    with_drums: bool = typer.Option(False, "--with-drums", help="追加第 4 轨鼓"),
    track: Optional[List[str]] = typer.Option(
        None, "--track", help="轨道名（可多次，如 --track chords --track melody）"
    ),
    voice_leading: bool = typer.Option(False, "--voice-leading", help="检测/提示平行五度与八度"),
    counterpoint: bool = typer.Option(False, "--counterpoint", help="二声部对位约束"),
    inversion: bool = typer.Option(False, "--inversion", help="和弦转位（低音平滑）"),
    rhythm: Optional[str] = typer.Option(None, "--rhythm", help="节奏型名（pop/rock/... 或自定义）"),
    project: Optional[str] = typer.Option(None, "--project", help="输出项目名（P2-5）"),
    output_dir: Optional[Path] = typer.Option(None, "--output-dir", help="输出根目录覆盖"),
    output: Optional[Path] = typer.Option(None, "--output", help="输出 .mid 路径"),
    score: bool = typer.Option(
        False, "--score", help="同时产出谱面（与 MIDI 同目录同主干，#12）"
    ),
    score_format: str = typer.Option(
        "svg", "--score-format", help="谱面格式（逗号分隔）：svg/png/jianpu/jianpu-txt/musicxml/all"
    ),
    score_theme: str = typer.Option("light", "--score-theme", help="谱面配色 light|dark"),
    score_key: Optional[str] = typer.Option(
        None, "--score-key", help="记谱调式覆盖（默认沿用生成的调式）"
    ),
) -> None:
    """程序化 MIDI 生成（P0-2；P2-2 乐理开关 / P2-4 风格预设 / P2-5 输出管理）。"""
    cfg = _load_config(ctx)
    request = _request_from_config(
        cfg,
        chords=chords,
        bpm=bpm,
        key=key,
        time_signature=time_signature,
        bars=bars,
        style=style,
        seed=seed,
        with_drums=with_drums,
        tracks=track,
        voice_leading=voice_leading,
        counterpoint=counterpoint,
        inversion=inversion,
        rhythm=rhythm,
    )
    gen = ProceduralGenerator(seed=request.seed)
    seq = gen.generate(request)

    if output is None:
        om = OutputManager(cfg, project=project, output_dir=output_dir)
        seq_no = om.next_seq(request.style, request.bpm, request.seed, "mid")
        output = om.plan_path(
            style=request.style, bpm=request.bpm, seed=request.seed, ext="mid", seq=seq_no
        )
    else:
        seq_no = 1  # 显式 --output 时无批次序号语义
    path = MidiDocument.from_sequence(seq).write(output)
    _write_single_metadata(
        cfg,
        command=f"sunoauxtool generate midi --style {request.style} --seed {request.seed}",
        seed=request.seed,
        artifacts=[
            ArtifactMeta(
                path=str(path), kind="midi",
                params={"chords": request.chords, "bpm": request.bpm,
                        "bars": request.bars, "style": request.style},
                seed=request.seed, seq=seq_no, duration_s=seq.duration_seconds(),
            )
        ],
        project=project,
        output_dir=output_dir,
    )
    typer.echo(f"✅ MIDI 已生成: {path}")
    typer.echo(f"   轨道: {', '.join(seq.track_names)}")
    typer.echo(f"   时长: {seq.duration_seconds():.1f}s（{request.bpm}bpm / {request.bars} 小节）")
    if score:
        for name, written in _export_score_for(seq, path, score_format, score_theme, score_key).items():
            typer.echo(f"   谱面[{name}]: {written}")


# ---------------------------------------------------------------------------
# generate melody
# ---------------------------------------------------------------------------

@generate_app.command("melody", help="music21 乐理驱动旋律生成 + 变奏")
@_guard
def generate_melody(
    ctx: typer.Context,
    chords: Optional[str] = typer.Option(None, "--chords", help="和弦进行，如 C-G-Am-F"),
    bpm: Optional[int] = typer.Option(None, "--bpm", help="速度"),
    key: Optional[str] = typer.Option(None, "--key", help="调式，如 'C major'"),
    time_signature: Optional[str] = typer.Option(
        None, "--time-signature", help="拍号，如 '4/4'"
    ),
    bars: Optional[int] = typer.Option(None, "--bars", help="小节数"),
    style: Optional[str] = typer.Option(None, "--style", help="风格标签"),
    seed: Optional[int] = typer.Option(None, "--seed", help="随机种子（可复现）"),
    variations: int = typer.Option(1, "--variations", help="变奏数量（1-3，rhythm/ornament/retrograde）"),
    voice_leading: bool = typer.Option(False, "--voice-leading", help="检测/提示平行五度与八度"),
    counterpoint: bool = typer.Option(False, "--counterpoint", help="二声部对位约束"),
    inversion: bool = typer.Option(False, "--inversion", help="和弦转位（低音平滑）"),
    rhythm: Optional[str] = typer.Option(None, "--rhythm", help="节奏型名"),
    project: Optional[str] = typer.Option(None, "--project", help="输出项目名（P2-5）"),
    output_dir: Optional[Path] = typer.Option(None, "--output-dir", help="输出根目录覆盖"),
    output: Optional[Path] = typer.Option(None, "--output", help="输出 .mid 路径"),
    score: bool = typer.Option(
        False, "--score", help="同时产出谱面（与 MIDI 同目录同主干，#12）"
    ),
    score_format: str = typer.Option(
        "svg", "--score-format", help="谱面格式（逗号分隔）：svg/png/jianpu/jianpu-txt/musicxml/all"
    ),
    score_theme: str = typer.Option("light", "--score-theme", help="谱面配色 light|dark"),
    score_key: Optional[str] = typer.Option(
        None, "--score-key", help="记谱调式覆盖（默认沿用生成的调式）"
    ),
) -> None:
    """乐理旋律生成（P0-3）：主旋律 + N 个变奏（同文件多轨）。"""
    cfg = _load_config(ctx)
    request = _request_from_config(
        cfg,
        chords=chords,
        bpm=bpm,
        key=key,
        time_signature=time_signature,
        bars=bars,
        style=style,
        seed=seed,
        voice_leading=voice_leading,
        counterpoint=counterpoint,
        inversion=inversion,
        rhythm=rhythm,
    )
    request.variations = variations
    gen = Music21MelodyGenerator(seed=request.seed)
    seq = gen.generate(request)

    if output is None:
        om = OutputManager(cfg, project=project, output_dir=output_dir)
        seq_no = om.next_seq(request.style, request.bpm, request.seed, "mid")
        output = om.plan_path(
            style=request.style, bpm=request.bpm, seed=request.seed,
            ext="mid", seq=seq_no, suffix="_melody",
        )
    else:
        seq_no = 1
    path = MidiDocument.from_sequence(seq).write(output)
    _write_single_metadata(
        cfg,
        command=f"sunoauxtool generate melody --seed {request.seed}",
        seed=request.seed,
        artifacts=[
            ArtifactMeta(
                path=str(path), kind="midi",
                params={"chords": request.chords, "bpm": request.bpm,
                        "bars": request.bars, "style": request.style},
                seed=request.seed, seq=seq_no, duration_s=seq.duration_seconds(),
            )
        ],
        project=project,
        output_dir=output_dir,
    )
    typer.echo(f"✅ 旋律 MIDI 已生成: {path}")
    typer.echo(f"   轨道: {', '.join(seq.track_names)}")
    if score:
        for name, written in _export_score_for(seq, path, score_format, score_theme, score_key).items():
            typer.echo(f"   谱面[{name}]: {written}")


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

@app.command("render", help="MIDI -> WAV 渲染（FluidSynth + SoundFont，真实引擎）")
@_guard
def render_cmd(
    ctx: typer.Context,
    input: Path = typer.Option(..., "--input", "-i", help="输入 .mid 路径"),
    soundfont: Optional[str] = typer.Option(None, "--soundfont", help="SoundFont 路径（覆盖配置）"),
    fluidsynth: Optional[str] = typer.Option(None, "--fluidsynth", help="fluidsynth 可执行文件路径"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="输出 .wav 路径"),
    dry_run: bool = typer.Option(False, "--dry-run", help="仅打印将执行的命令，不调用 subprocess、不写产物"),
) -> None:
    """MIDI → WAV 渲染（P0-4）：44.1kHz / 16bit；默认使用 module/ 真实引擎（M-1）。"""
    cfg = _load_config(ctx)
    merged = cfg.merge_cli(soundfont=soundfont, fluidsynth=fluidsynth)

    # 输入存在性前置校验（保证缺失输入 -> 退出码 3）
    midi = Path(input).expanduser().resolve()
    if not midi.is_file():
        from sunoauxtool.exceptions import InputFileError

        raise InputFileError(f"MIDI 文件不存在: {midi}", code=3)

    resolver = PathResolver(merged)
    if dry_run:
        # dry-run 是唯一允许 mock 的通道：不探测、不解析路径
        sf = merged.paths.soundfont
        fs = merged.paths.fluidsynth
    else:
        resolver.ensure_ready()
        sf = resolver.resolve_soundfont()
        fs = resolver.resolve_fluidsynth()

    if output is None:
        output = input.with_name(f"{input.stem}_rendered.wav")
    renderer = FluidSynthRenderer(fluidsynth_path=str(fs))
    path = renderer.render(str(input), str(sf), str(output), dry_run=dry_run)
    prefix = "[DRY-RUN] " if dry_run else "✅ "
    typer.echo(f"{prefix}WAV 已渲染: {path}")


# ---------------------------------------------------------------------------
# export suno
# ---------------------------------------------------------------------------

@export_app.command("suno", help="Suno 合规导出（10–30s 纯器乐 WAV/MP3，恒禁混响）")
@_guard
def export_suno(
    ctx: typer.Context,
    input: Path = typer.Option(..., "--input", "-i", help="输入 WAV 路径"),
    duration: Optional[int] = typer.Option(None, "--duration", help="目标时长（10-30s）"),
    format: Optional[str] = typer.Option(None, "--format", help="导出格式 wav|mp3"),
    sample_rate: Optional[int] = typer.Option(None, "--sample-rate", help="采样率（默认 44100）"),
    bit_depth: Optional[int] = typer.Option(None, "--bit-depth", help="位深（默认 16）"),
    fade_ms: Optional[float] = typer.Option(None, "--fade-ms", help="淡入淡出毫秒数"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="输出路径"),
) -> None:
    """Suno 合规导出（P0-5；无混响合规约束由导出器强制执行）。"""
    cfg = _load_config(ctx)
    opts = _export_opts_from_config(
        cfg, duration=duration, format=format, sample_rate=sample_rate,
        bit_depth=bit_depth, fade_ms=fade_ms,
    )
    exporter = SunoExporter()
    path = exporter.export(str(input), opts, output_path=output)
    meta = exporter.describe(path)
    typer.echo(f"✅ Suno 合规片段已导出: {path}")
    typer.echo(f"   元数据: 时长 {meta['duration_s']}s / {meta['sample_rate']}Hz / {meta['bit_depth']}bit / {meta['channels']}ch")


@export_app.command("suno-pack", help="批量导出 Suno 片段 + 打包（P3-B2）")
@_guard
def export_suno_pack(
    inputs: List[str] = typer.Argument(..., help="WAV 文件路径（可多个）"),
    output_dir: Path = typer.Option(Path("output"), "--output-dir", "-o", help="打包输出目录"),
    pack_name: str = typer.Option("suno_pack", "--name", help="打包目录名"),
    no_zip: bool = typer.Option(False, "--no-zip", help="不生成 zip"),
) -> None:
    """将多个 Suno 合规片段 + metadata 打包成可上传目录（+ zip）。"""
    from sunoauxtool.sunopack import build_pack

    result = build_pack(
        inputs,
        output_dir,
        pack_name=pack_name,
        make_zip=not no_zip,
    )
    if not result["files"]:
        typer.echo("❌ 没有可打包的 WAV 文件")
        raise typer.Exit(1)
    typer.echo(f"✅ 打包完成: {result['pack_dir']}")
    typer.echo(f"   文件数: {len(result['files'])}")
    typer.echo(f"   清单: {result['manifest_path']}")
    if result["zip_path"]:
        typer.echo(f"   zip: {result['zip_path']}")


@export_app.command("suno-manifest", help="生成 Suno 上传清单（CSV/JSON，P3-B3）")
@_guard
def export_suno_manifest(
    inputs: List[str] = typer.Argument(..., help="WAV 文件路径（可多个）"),
    output: Path = typer.Option(Path("suno_upload_manifest.csv"), "--output", "-o", help="清单输出路径"),
    format: str = typer.Option("csv", "--format", help="csv|json"),
) -> None:
    """生成 Suno 上传清单，指引用户逐个上传。"""
    from sunoauxtool.sunopack import write_upload_manifest

    if format not in ("csv", "json"):
        typer.echo("❌ --format 可选 csv|json")
        raise typer.Exit(1)
    path = write_upload_manifest(inputs, output, format=format)
    typer.echo(f"✅ 上传清单已生成: {path}")
    typer.echo("   打开 Suno 后，按清单逐行上传片段即可。")


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------

@app.command("pipeline", help="一键管线：generate → render → DSP → export（零参数跑通 demo）")
@_guard
def pipeline_cmd(
    ctx: typer.Context,
    chords: Optional[str] = typer.Option(None, "--chords", help="和弦进行，如 C-G-Am-F"),
    bpm: Optional[int] = typer.Option(None, "--bpm", help="速度"),
    key: Optional[str] = typer.Option(None, "--key", help="调式，如 'C major'"),
    bars: Optional[int] = typer.Option(None, "--bars", help="小节数"),
    style: Optional[str] = typer.Option(None, "--style", help="风格（P2-4 预设）"),
    seed: Optional[int] = typer.Option(None, "--seed", help="随机种子（可复现）"),
    with_drums: bool = typer.Option(False, "--with-drums", help="追加鼓轨"),
    voice_leading: bool = typer.Option(False, "--voice-leading", help="检测/提示平行五度与八度"),
    counterpoint: bool = typer.Option(False, "--counterpoint", help="二声部对位约束"),
    inversion: bool = typer.Option(False, "--inversion", help="和弦转位（低音平滑）"),
    rhythm: Optional[str] = typer.Option(None, "--rhythm", help="节奏型名"),
    duration: Optional[int] = typer.Option(None, "--duration", help="Suno 目标时长（10-30s）"),
    format: Optional[str] = typer.Option(None, "--format", help="导出格式 wav|mp3"),
    project: Optional[str] = typer.Option(None, "--project", help="输出项目名（P2-5）"),
    output_dir: Optional[Path] = typer.Option(None, "--output-dir", help="输出根目录覆盖"),
    dry_run: bool = typer.Option(False, "--dry-run", help="仅规划并打印命令，不写产物"),
    fade_in: Optional[float] = typer.Option(None, "--fade-in", help="淡入毫秒（0-5000）"),
    fade_out: Optional[float] = typer.Option(None, "--fade-out", help="淡出毫秒（0-5000）"),
    eq: bool = typer.Option(False, "--eq", help="开启低频切 EQ"),
    compressor: bool = typer.Option(False, "--compressor", help="开启轻压缩"),
    reverb: bool = typer.Option(False, "--reverb", help="请求混响（当前未支持，显式报错）"),
    no_preview: bool = typer.Option(False, "--no-preview", help="不生成 HTML 预览页"),
    score: bool = typer.Option(
        False, "--score", help="在 MIDI 旁产出谱面，并把五线谱内嵌进预览页（#12）"
    ),
    score_format: str = typer.Option(
        "svg", "--score-format", help="谱面格式（逗号分隔）：svg/png/jianpu/jianpu-txt/musicxml/all"
    ),
    score_theme: str = typer.Option("light", "--score-theme", help="谱面配色 light|dark"),
    score_key: Optional[str] = typer.Option(
        None, "--score-key", help="记谱调式覆盖（默认沿用生成的调式）"
    ),
) -> None:
    """闭环：生成 → 渲染 → DSP → 合规导出（P0 + P2-1 + P2-5）。"""
    from sunoauxtool.pipeline import Pipeline

    cfg = _load_config(ctx)
    request = _request_from_config(
        cfg, chords=chords, bpm=bpm, key=key, bars=bars, style=style,
        seed=seed, with_drums=with_drums, voice_leading=voice_leading,
        counterpoint=counterpoint, inversion=inversion, rhythm=rhythm,
    )
    opts = _export_opts_from_config(cfg, duration=duration, format=format)
    # 先叠加 DSP/预览覆盖，再交给 Pipeline —— 顺序不能反，否则 --no-preview 会把
    # 前面的 fade/eq/compressor 覆盖一起丢掉（历史缺陷：那行曾是 cfg.merge_cli()）
    merged = cfg.merge_cli(
        fade_in_ms=fade_in, fade_out_ms=fade_out, eq=eq, compressor=compressor, reverb=reverb,
        preview_enabled=False if no_preview else None,
    )
    pipeline = Pipeline(
        merged,
        project=project,
        output_dir=output_dir,
        dry_run=dry_run,
        score=score,
        score_format=score_format,
        score_theme=score_theme,
        score_key=score_key,
    )
    result = pipeline.run(request, opts)
    prefix = "[DRY-RUN] " if dry_run else "✅ "
    typer.echo(f"{prefix}Pipeline 完成")
    typer.echo(f"   最终产物: {result.export_path}")
    typer.echo(
        f"   元数据: 时长 {result.duration_s}s / {result.sample_rate}Hz / "
        f"{result.bit_depth}bit / 和弦 {result.chords} / seed {result.seed} / "
        f"{result.bpm}bpm / {result.bars} 小节"
    )
    for name, path in result.score_paths.items():
        typer.echo(f"   谱面[{name}]: {path}")


# ---------------------------------------------------------------------------
# batch（P1-3 完整实现）
# ---------------------------------------------------------------------------

@app.command("batch", help="批量生成多个变体（随机化 + 可复现 + 失败隔离）")
@_guard
def batch_cmd(
    ctx: typer.Context,
    count: int = typer.Option(3, "--count", help="生成数量（默认 3）"),
    seed: Optional[int] = typer.Option(None, "--seed", help="全局随机种子（可复现）"),
    chords_choices: Optional[List[str]] = typer.Option(
        None, "--chords-choices", help="和弦池（可多次，如 --chords-choices C-G-Am-F Am-F-C-G）"
    ),
    style: Optional[str] = typer.Option(None, "--style", help="风格基准（P2-4 预设）"),
    variations: bool = typer.Option(False, "--variations", help="风格/乐器/bpm 维度变体"),
    rhythm_variants: bool = typer.Option(False, "--rhythm-variants", help="节奏型维度变体"),
    melody_variants: bool = typer.Option(False, "--melody-variants", help="旋律变奏维度"),
    render: bool = typer.Option(False, "--render", help="链式真实渲染 WAV"),
    export: bool = typer.Option(False, "--export", help="链式导出 Suno 合规片段（需 --render）"),
    parallel: bool = typer.Option(False, "--parallel", help="并行执行（默认串行）"),
    parallel_workers: int = typer.Option(2, "--parallel-workers", help="并行并发数"),
    project: Optional[str] = typer.Option(None, "--project", help="输出项目名（P2-5）"),
    output_dir: Optional[Path] = typer.Option(None, "--output-dir", help="输出根目录覆盖"),
    dry_run: bool = typer.Option(False, "--dry-run", help="仅规划并打印，不写产物"),
    report: Optional[Path] = typer.Option(
        None, "--report", help="结构化批次报告路径（F4；.json/.csv 按扩展名识别）"
    ),
    bpm: Optional[int] = typer.Option(None, "--bpm", help="固定 BPM（覆盖预设）"),
    bars: Optional[int] = typer.Option(None, "--bars", help="小节数"),
    with_drums: bool = typer.Option(False, "--with-drums", help="追加鼓轨"),
    voice_leading: bool = typer.Option(False, "--voice-leading", help="检测/提示平行五度与八度"),
    counterpoint: bool = typer.Option(False, "--counterpoint", help="二声部对位约束"),
    inversion: bool = typer.Option(False, "--inversion", help="和弦转位"),
    rhythm: Optional[str] = typer.Option(None, "--rhythm", help="固定节奏型名"),
) -> None:
    """批量生成（P1-3 完整实现）：四维度随机化、失败隔离、退出码 0/8/9。"""
    from sunoauxtool.batch import BatchOptions, BatchRunner

    cfg = _load_config(ctx)
    options = BatchOptions(
        count=count, seed=seed, chords_choices=chords_choices, style=style,
        variations=variations, rhythm_variants=rhythm_variants,
        melody_variants=melody_variants, render=render, export=export,
        parallel=parallel, parallel_workers=parallel_workers,
        project=project, output_dir=output_dir, dry_run=dry_run,
        report=str(report) if report else None,
        bpm=bpm, bars=bars, with_drums=with_drums,
        voice_leading=voice_leading, counterpoint=counterpoint,
        inversion=inversion, rhythm_pattern=rhythm,
    )
    runner = BatchRunner(options, config=cfg)
    result = runner.run()

    for r in result.items:
        icon = "✅" if r.status == "ok" else "❌"
        typer.echo(f"  {icon} 第 {r.index + 1} 项 (seed={r.seed}): {r.status}")
        if r.error:
            typer.echo(f"      错误: {r.error}")
        if r.midi_path:
            typer.echo(f"      MIDI: {r.midi_path}")
        if r.wav_path:
            typer.echo(f"      WAV: {r.wav_path}")
        if r.export_path:
            typer.echo(f"      Suno: {r.export_path}")
    typer.echo(
        f"{'[DRY-RUN] ' if dry_run else '✅ '}批量完成: 成功 {result.ok_count} / "
        f"失败 {result.failed_count}（全局 seed={result.actual_seed}）"
    )

    # F4：结构化报告（部分失败也完整落盘，失败项含 error 字段）
    if options.report:
        from sunoauxtool.batch import write_report

        report_path = write_report(result, options.report)
        typer.echo(f"📄 批次报告: {report_path}")

    if result.failed_count == 0:
        return
    if result.ok_count == 0:
        raise BatchFailedError(
            f"批量全部失败（成功 {result.ok_count} / 失败 {result.failed_count}），"
            "请查看上方错误日志",
            code=9,
        )
    raise BatchPartialError(
        f"批量部分失败（成功 {result.ok_count} / 失败 {result.failed_count}），"
        "失败项已隔离，成功项不受影响",
        code=8,
    )


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

@config_app.command("init", help="生成配置文件模板（交互式引导，--yes 非交互）")
@_guard
def config_init(
    path: Path = typer.Option(Path("sunoauxtool.toml"), "--path", "-p", help="目标路径"),
    yes: bool = typer.Option(False, "--yes", "-y", help="非交互模式，直接使用检测值/默认值"),
) -> None:
    """生成带注释的默认配置模板（P0-6；M-1 交互式引导）。

    - TTY 下：交互式引导用户确认/修改 SoundFont、fluidsynth、项目名、风格等。
    - 非 TTY 或 --yes：直接使用检测到的路径或默认值生成。
    """
    import sys as _sys
    from sunoauxtool.env import PathResolver, ProbeStatus

    target = Path(path).expanduser().resolve()
    template = Path.cwd() / "config" / "default.toml"

    # 检测 module 路径
    cfg = Config.load()
    resolver = PathResolver(cfg)
    probes = {p.component: p for p in resolver.probe_all()}

    is_tty = _sys.stdin.isatty() and not yes
    detected: dict[str, str] = {}

    if is_tty:
        typer.echo("🎛️ SunoAuxTool 配置向导")
        typer.echo("检测到以下环境，按回车使用推荐值：")
        typer.echo("")

        # SoundFont 主库
        sf = probes.get("soundfont")
        if sf and sf.status == ProbeStatus.OK:
            default_sf = str(sf.path)
            typer.echo(f"  ✅ 检测到主音色库: {default_sf}")
        else:
            default_sf = cfg.paths.soundfont
            typer.echo(f"  ⚠️ 未检测到主音色库，将使用默认: {default_sf}")
        detected["soundfont"] = _config_prompt("主音色库路径", default_sf)

        # SoundFont 备选
        backup = probes.get("soundfont_backup")
        if backup and backup.status == ProbeStatus.OK:
            default_bk = str(backup.path)
            typer.echo(f"  ✅ 检测到备选音色库: {default_bk}")
        else:
            default_bk = cfg.paths.soundfont_backup
        detected["soundfont_backup"] = _config_prompt("备选音色库路径", default_bk)

        # FluidSynth
        fs = probes.get("fluidsynth")
        if fs and fs.status == ProbeStatus.OK:
            default_fs = str(fs.path)
            typer.echo(f"  ✅ 检测到 fluidsynth: {default_fs}")
        else:
            default_fs = cfg.paths.fluidsynth
        detected["fluidsynth"] = _config_prompt("fluidsynth 路径", default_fs)

        # 项目名
        detected["project"] = _config_prompt("默认项目名", cfg.output.project)

        # 风格
        detected["style"] = _config_prompt(
            "默认风格", cfg.defaults.style, ["pop", "rock", "electronic", "classical"]
        )

        # BPM
        detected["bpm"] = _config_prompt("默认 BPM", str(cfg.defaults.bpm))

        typer.echo("")
    else:
        # 非交互：使用检测到的路径（若有）
        sf = probes.get("soundfont")
        if sf and sf.status == ProbeStatus.OK:
            detected["soundfont"] = str(sf.path)
        fs = probes.get("fluidsynth")
        if fs and fs.status == ProbeStatus.OK:
            detected["fluidsynth"] = str(fs.path)
        backup = probes.get("soundfont_backup")
        if backup and backup.status == ProbeStatus.OK:
            detected["soundfont_backup"] = str(backup.path)

    # 生成配置：拷贝模板 + 覆盖检测值
    if template.is_file():
        content = template.read_text(encoding="utf-8")
    else:
        content = None

    if content is None:
        # 无模板时用 Config 默认生成
        target.parent.mkdir(parents=True, exist_ok=True)
        target = Path(Config().write_template(target))
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    # 应用检测到的覆盖（追加或就地修改）
    if detected:
        _apply_detected_to_config(target, detected)

    typer.echo(f"✅ 配置文件已生成: {target}")
    typer.echo("   可修改后使用：sunoauxtool --config <path> <子命令>")


@config_app.command("show", help="打印合并后的生效配置")
@_guard
def config_show(ctx: typer.Context) -> None:
    """打印合并后的生效配置（P0-6）。"""
    import tomli_w

    cfg = _load_config(ctx)
    source = f"（来源: {cfg.config_path}）" if cfg.config_path else "（内置默认值）"
    typer.echo(f"# 生效配置 {source}")
    typer.echo(tomli_w.dumps(cfg.to_dict()).rstrip())


# ---------------------------------------------------------------------------
# play（P3-A2 本地播放）
# ---------------------------------------------------------------------------

@app.command("play", help="用系统默认播放器播放 WAV 文件")
@_guard
def play_cmd(
    wav_path: str = typer.Argument(..., help="要播放的 WAV 文件路径"),
) -> None:
    """调用系统默认播放器播放 WAV 文件（P3-A2）。"""
    path = Path(wav_path).expanduser().resolve()
    if not path.is_file():
        from sunoauxtool.exceptions import InputFileError
        raise InputFileError(f"文件不存在: {path}", code=3)
    import os
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        import subprocess
        subprocess.run(["open", str(path)], check=False)
    else:
        import subprocess
        subprocess.run(["xdg-open", str(path)], check=False)
    typer.echo(f"▶ 正在播放: {path}")


# ---------------------------------------------------------------------------
# doctor（P3-E3 环境诊断）
# ---------------------------------------------------------------------------

@app.command("doctor", help="一键环境健康检查")
@_guard
def doctor_cmd(
    ctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="输出详细信息"),
) -> None:
    """环境诊断（P3-E3）：检查 Python/fluidsynth/SF2/AI/显存/espeak 等。"""
    import shutil
    import sys
    from sunoauxtool.env import PathResolver, ProbeStatus

    cfg = _load_config(ctx)
    errors = 0
    warnings = 0

    typer.echo("SunoAuxTool 环境诊断")
    typer.echo("═══════════════════════════════════════")

    # Python
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    _doctor_item("Python", f"✅ {py_ver}" if sys.version_info >= (3, 12) else f"⚠️ {py_ver}（建议 3.12+）")

    # FluidSynth + SoundFont（复用 PathResolver）
    resolver = PathResolver(cfg)
    probes = resolver.probe_all()
    for probe in probes:
        if probe.status == ProbeStatus.OK:
            _doctor_item(probe.component, f"✅ OK ({probe.path})")
        elif probe.status == ProbeStatus.MISSING:
            _doctor_item(probe.component, f"❌ MISSING ({probe.path})")
            typer.echo(f"     → {probe.detail}")
            errors += 1
        else:
            _doctor_item(probe.component, f"⚠️ BROKEN ({probe.path})")
            typer.echo(f"     → {probe.detail}")
            warnings += 1

    # ffmpeg / ffprobe（1.4.7 F1：video / dsp concat / preview 全依赖；
    # 复用 download 包三层定位：SUNO_FFMPEG / SUNO_FFMPEG_DIRS / PATH / 已知目录）
    import importlib.util
    import subprocess

    try:
        from sunoauxtool.download.transcoder import _find_ffprobe, find_ffmpeg

        ffmpeg = find_ffmpeg()
    except Exception:
        _doctor_item(
            "ffmpeg",
            "❌ 未找到（PATH / SUNO_FFMPEG / SUNO_FFMPEG_DIRS 均未命中）",
        )
        typer.echo(
            "     → 修法: 设 SUNO_FFMPEG=<ffmpeg 路径或目录>，或加入 PATH，"
            "或设 SUNO_FFMPEG_DIRS（video / dsp concat / preview 需要）"
        )
        errors += 1
    else:
        version = ""
        try:
            proc = subprocess.run(
                [str(ffmpeg), "-version"], capture_output=True, text=True, timeout=10
            )
            first = (proc.stdout or "").splitlines()
            if first:
                parts = first[0].split()
                version = parts[2] if len(parts) >= 3 else ""
        except Exception:
            version = ""
        _doctor_item("ffmpeg", f"✅ {ffmpeg}" + (f" ({version})" if version else ""))
        try:
            ffprobe = _find_ffprobe(ffmpeg)
            _doctor_item("ffprobe", f"✅ {ffprobe}")
        except Exception:
            _doctor_item("ffprobe", "⚠️ 未找到（视频 e2e 校验/取证需要）")
            warnings += 1

    # AI 依赖
    torch_ok = False
    try:
        torch_ok = importlib.util.find_spec("torch") is not None
    except Exception:
        pass
    if torch_ok:
        _doctor_item("AI 依赖", "✅ torch 已安装")
        # CUDA 检查
        try:
            import torch
            try:
                cuda_ok = torch.cuda.is_available()
            except Exception:
                cuda_ok = False
            if cuda_ok:
                try:
                    props = torch.cuda.get_device_properties(0)
                    total_gb = props.total_mem / 1024**3
                    _doctor_item("CUDA", f"✅ {torch.cuda.get_device_name(0)} ({total_gb:.1f} GB)")
                except Exception:
                    _doctor_item("CUDA", "✅ 可用（获取详情失败）")
            else:
                _doctor_item("CUDA", "⚠️ CUDA 不可用（将使用 CPU）")
                warnings += 1
        except Exception:
            _doctor_item("CUDA", "⚠️ 检查失败")
            warnings += 1
        # audiocraft
        ac_ok = importlib.util.find_spec("audiocraft") is not None
        _doctor_item("audiocraft", "✅ 已安装" if ac_ok else "⚠️ 未安装")
        # diffrhythm
        dr_ok = importlib.util.find_spec("diffrhythm") is not None
        _doctor_item("diffrhythm", "✅ 已安装" if dr_ok else "⚠️ 未安装")
    else:
        _doctor_item("AI 依赖", "❌ torch 未安装")
        typer.echo("     → 安装: pip install torch --index-url https://download.pytorch.org/whl/cu121")
        errors += 1

    # espeak-ng
    espeak = shutil.which("espeak-ng")
    _doctor_item("espeak-ng", f"✅ {espeak}" if espeak else "⚠️ 未安装（DiffRhythm 人声合成需要）")
    if not espeak:
        warnings += 1

    # AudioSR 源码目录（1.4.7 F1：R5 音质提升，可选）
    try:
        from sunoauxtool.ai.audiosr import resolve_audiosr_dir

        asr_dir = resolve_audiosr_dir()
    except Exception:
        asr_dir = None
    if asr_dir:
        _doctor_item("AudioSR", f"✅ 源码目录就位 ({asr_dir})")
    else:
        _doctor_item(
            "AudioSR",
            "⚠️ 源码目录未就位（enhance 需要：设 AUDIOSR_DIR 或克隆到 src/versatile_audio_super_resolution）",
        )
        warnings += 1

    # basic_pitch（1.4.7 F1：复调配调转谱，可选；注意其 __init__ 无 else 分支，
    # 四后端全缺时 import 即 NameError——这里只做 find_spec 轻量探测）
    try:
        bp_ok = importlib.util.find_spec("basic_pitch") is not None
    except Exception:
        bp_ok = False
    if bp_ok:
        backends = [
            name
            for name in ("onnxruntime", "tensorflow", "tflite_runtime", "coremltools")
            if importlib.util.find_spec(name) is not None
        ]
        if backends:
            _doctor_item("basic_pitch", f"✅ 已安装（推理后端: {backends[0]}）")
        else:
            _doctor_item("basic_pitch", "⚠️ 已安装但无可用推理后端")
            typer.echo("     → 修法: pip install onnxruntime（模型已随包打包，不下载权重）")
            warnings += 1
    else:
        _doctor_item(
            "basic_pitch",
            "⚠️ 未安装（transcribe 复调用：pip install basic-pitch --no-deps"
            " + pip install resampy mir-eval onnxruntime）",
        )
        warnings += 1

    # 项目完整性
    project_root = Path.cwd()
    has_pyproject = (project_root / "pyproject.toml").is_file()
    has_module = (project_root / "module").is_dir()
    if has_pyproject and has_module:
        _doctor_item("项目完整性", "✅ pyproject.toml + module/ 存在")
    else:
        missing = []
        if not has_pyproject:
            missing.append("pyproject.toml")
        if not has_module:
            missing.append("module/")
        _doctor_item("项目完整性", f"⚠️ 缺失: {', '.join(missing)}")
        warnings += 1

    # 配置文件（M-4）
    config_default = project_root / "config" / "default.toml"
    user_config = project_root / "sunoauxtool.toml"
    if config_default.is_file() and user_config.is_file():
        _doctor_item("配置", "✅ default.toml + sunoauxtool.toml 存在")
    elif config_default.is_file():
        _doctor_item("配置", "✅ default.toml 存在（无用户配置，用默认）")
    else:
        _doctor_item("配置", "⚠️ 缺少 config/default.toml")
        warnings += 1

    typer.echo("═══════════════════════════════════════")
    if errors == 0 and warnings == 0:
        typer.echo("✅ 状态: 全部正常")
        return
    if errors > 0:
        typer.echo(f"❌ 状态: {errors} 个错误, {warnings} 个警告（需修复）")
        raise typer.Exit(2)
    typer.echo(f"⚠️ 状态: {warnings} 个警告（可继续使用）")
    raise typer.Exit(1)


@inspire_app.command("init", help="初始化灵感库（创建 sunoauxtool.db）")
@_guard
def inspire_init() -> None:
    """创建灵感库数据库。"""
    from sunoauxtool.inspire import InspirationDB
    path = InspirationDB().init_db()
    typer.echo(f"✅ 灵感库已初始化: {path}")


@inspire_app.command("add", help="添加灵感（从 WAV + 元数据提取）")
@_guard
def inspire_add(
    wav_path: str = typer.Argument(..., help="WAV 文件路径"),
    tags: str = typer.Option("", "--tags", help="标签（逗号分隔，如 upbeat,pop）"),
    rating: Optional[int] = typer.Option(None, "--rating", help="评分 1-5"),
) -> None:
    """将 WAV 文件加入灵感库。"""
    from sunoauxtool.inspire import InspirationDB
    insp_id = InspirationDB().add(wav_path, tags=tags, rating=rating)
    typer.echo(f"✅ 灵感已添加 (id={insp_id})")


@inspire_app.command("list", help="列出灵感")
@_guard
def inspire_list(
    style: Optional[str] = typer.Option(None, "--style", help="按风格筛选"),
    tag: Optional[str] = typer.Option(None, "--tag", help="按标签筛选"),
    seed: Optional[int] = typer.Option(None, "--seed", help="按种子筛选"),
    date: Optional[str] = typer.Option(None, "--date", help="按日期筛选（YYYYMMDD）"),
    limit: int = typer.Option(50, "--limit", help="最大返回条数"),
) -> None:
    """列出灵感库中的条目。"""
    from sunoauxtool.inspire import InspirationDB
    items = InspirationDB().list(style=style, tag=tag, seed=seed, date=date, limit=limit)
    if not items:
        typer.echo("（灵感库为空）")
        return
    for item in items:
        tags_str = f" [{item['tags']}]" if item.get("tags") else ""
        rating_str = f" ★{item['rating']}" if item.get("rating") else ""
        style_str = item.get("style") or "?"
        seed_str = f"seed={item['seed']}" if item.get("seed") is not None else ""
        typer.echo(f"  #{item['id']:<4} {style_str:<12} {seed_str:<12} {item['path']}{tags_str}{rating_str}")


@inspire_app.command("show", help="查看灵感详情")
@_guard
def inspire_show(
    id: int = typer.Argument(..., help="灵感 id"),
) -> None:
    """显示灵感详情。"""
    from sunoauxtool.inspire import InspirationDB
    insp = InspirationDB().get(id)
    if not insp:
        typer.echo(f"❌ 灵感不存在: id={id}")
        raise typer.Exit(1)
    typer.echo(f"  id:          {insp['id']}")
    typer.echo(f"  路径:        {insp['path']}")
    typer.echo(f"  风格:        {insp.get('style') or '-'}")
    typer.echo(f"  BPM:         {insp.get('bpm') or '-'}")
    typer.echo(f"  seed:        {insp.get('seed') or '-'}")
    typer.echo(f"  和弦:        {insp.get('chords') or '-'}")
    typer.echo(f"  时长:        {insp.get('duration_s') or '-'}s")
    typer.echo(f"  采样率:      {insp.get('sample_rate') or '-'}Hz")
    typer.echo(f"  RMS:         {insp.get('rms_db') or '-'} dBFS")
    typer.echo(f"  峰值:        {insp.get('peak_db') or '-'} dBFS")
    typer.echo(f"  标签:        {insp.get('tags') or '-'}")
    typer.echo(f"  评分:        {insp.get('rating') or '-'}")
    typer.echo(f"  创建时间:    {insp.get('created_at') or '-'}")


@inspire_app.command("rm", help="删除灵感（仅从库中移除，不删文件）")
@_guard
def inspire_rm(
    id: int = typer.Argument(..., help="灵感 id"),
) -> None:
    """从灵感库中移除。"""
    from sunoauxtool.inspire import InspirationDB
    if InspirationDB().delete(id):
        typer.echo(f"✅ 灵感 #{id} 已删除")
    else:
        typer.echo(f"❌ 灵感不存在: id={id}")
        raise typer.Exit(1)


@inspire_app.command("export", help="导出灵感文件到指定目录")
@_guard
def inspire_export(
    id: int = typer.Argument(..., help="灵感 id"),
    output: Path = typer.Option(..., "--output", "-o", help="目标目录"),
) -> None:
    """复制灵感文件到指定目录。"""
    from sunoauxtool.inspire import InspirationDB
    try:
        paths = InspirationDB().export_files(id, output)
        typer.echo(f"✅ 已导出 {len(paths)} 个文件:")
        for p in paths:
            typer.echo(f"    {p}")
    except (ValueError, FileNotFoundError) as exc:
        typer.echo(f"❌ {exc}")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# diff（P3-C3 版本对比）
# ---------------------------------------------------------------------------

@app.command("diff", help="对比两个 WAV 的音频特征")
@_guard
def diff_cmd(
    wav1: str = typer.Argument(..., help="第一个 WAV 路径"),
    wav2: str = typer.Argument(..., help="第二个 WAV 路径"),
) -> None:
    """对比两个 WAV 的音频特征差异。"""
    from sunoauxtool.preview import compute_audio_features

    p1 = Path(wav1).expanduser().resolve()
    p2 = Path(wav2).expanduser().resolve()

    if not p1.is_file():
        from sunoauxtool.exceptions import InputFileError
        raise InputFileError(f"文件不存在: {p1}", code=3)
    if not p2.is_file():
        from sunoauxtool.exceptions import InputFileError
        raise InputFileError(f"文件不存在: {p2}", code=3)

    f1 = compute_audio_features(p1)
    f2 = compute_audio_features(p2)

    typer.echo("音频特征对比")
    typer.echo("═══════════════════════════════════════")
    _diff_row("时长", _get_duration(p1), _get_duration(p2), "s")
    _diff_row("RMS", f1.rms_db, f2.rms_db, "dBFS")
    _diff_row("峰值", f1.peak_db, f2.peak_db, "dBFS")
    _diff_row("频谱中心", f1.spectral_centroid, f2.spectral_centroid, "Hz")
    typer.echo("")
    typer.echo("频段能量:")
    for band in ("low", "mid", "high"):
        v1 = f1.band_energy.get(band, 0)
        v2 = f2.band_energy.get(band, 0)
        _diff_row(f"  {band}", f"{v1*100:.0f}%", f"{v2*100:.0f}%", "")

    # 尝试从同目录 metadata.json 提取参数对比
    _diff_metadata(p1, p2)


@app.command("tempo", help="估计音频 BPM（onset 自相关，numpy-only）")
@_guard
def tempo_cmd(
    ctx: typer.Context,
    wav: str = typer.Argument(..., help="输入音频路径"),
    min_bpm: float = typer.Option(40.0, "--min-bpm", help="BPM 搜索下限"),
    max_bpm: float = typer.Option(240.0, "--max-bpm", help="BPM 搜索上限"),
    prior_bpm: float = typer.Option(
        120.0,
        "--prior-bpm",
        help="节奏先验中心（化解半频歧义）；0 = 关闭先验（纯自相关）",
    ),
) -> None:
    """估计音频的 BPM 与节拍相位（#13）。

    置信度为归一化自相关峰强（0~1）：≥0.5 高 / ≥0.25 中 / 否则低（节拍感弱）。
    """
    from sunoauxtool.analysis.tempo import estimate_bpm

    est = estimate_bpm(wav, bpm_min=min_bpm, bpm_max=max_bpm, prior_bpm=prior_bpm)
    label = "高" if est.confidence >= 0.5 else ("中" if est.confidence >= 0.25 else "低")
    typer.echo(f"✅ BPM: {est.bpm:.1f}（置信度 {est.confidence:.2f}·{label}）")
    typer.echo(
        f"   时长 {est.duration:.1f}s / 第一拍 {est.beat_offset:.3f}s / "
        f"onset 帧率 {est.onset_rate:.1f}Hz"
    )


@app.command("transcribe", help="WAV → MIDI 转谱（内置单旋律后端；可选 basic-pitch 复调）")
@_guard
def transcribe_cmd(
    ctx: typer.Context,
    wav: str = typer.Argument(..., help="输入音频路径"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="输出 .mid 路径"),
    bpm: str = typer.Option("auto", "--bpm", help="'auto' 自动测速，或数值 (20-400)"),
    grid: str = typer.Option("1/16", "--grid", help="量化网格：1/4 | 1/8 | 1/16 | 1/32 或拍数"),
    backend: str = typer.Option(
        "builtin", "--backend", help="builtin（单旋律，numpy-only）| basic-pitch（复调，可选依赖）"
    ),
    program: int = typer.Option(0, "--program", help="GM 乐器号 (0-127)"),
    min_note_ms: float = typer.Option(60.0, "--min-note-ms", help="音符最短时长（毫秒）"),
    merge_gap_ms: float = typer.Option(40.0, "--merge-gap-ms", help="同音高合并间隙（毫秒）"),
) -> None:
    """把音频转成 MIDI（#13）。

    内置后端是「单旋律 / 主导声部」转谱：谐波 salience 峰值跟踪 + 按拍量化。
    复调请 ``--backend basic-pitch``（需可选安装 basic-pitch，退出码 6 = 未装）。
    """
    source = Path(wav).expanduser().resolve()

    if backend != "builtin":
        # R12：内置 basic-pitch + entry point 插件后端，统一走注册表发现
        from sunoauxtool.analysis import discover_transcribe_backends

        backends = discover_transcribe_backends()
        cls = backends.get(backend)
        if cls is None:
            raise ParameterError(
                f"未知转谱后端: {backend!r}"
                f"（可用 builtin | {', '.join(sorted(backends))}）"
            )
        out_path = cls().transcribe(str(source), str(output) if output else None)
        typer.echo(f"✅ 转谱完成（{backend}）: {out_path}")
        return

    from sunoauxtool.analysis.transcribe import TranscribeOptions, transcribe_wav

    bpm_value: Optional[float] = None
    if bpm.strip().lower() not in ("", "auto"):
        try:
            bpm_value = float(bpm)
        except ValueError as exc:
            raise ParameterError(
                f"--bpm 需 'auto' 或数值 (20-400)，实为 {bpm!r}", code=1
            ) from exc

    options = TranscribeOptions(
        bpm=bpm_value,
        grid=grid,
        program=program,
        min_note_ms=min_note_ms,
        merge_gap_ms=merge_gap_ms,
    )
    result = transcribe_wav(source, options)

    target = output if output else source.with_suffix(".transcribed.mid")
    from sunoauxtool.analysis.transcribe import write_transcribed_midi

    written = write_transcribed_midi(result, target)

    lo, hi = result.pitch_range
    typer.echo(
        f"✅ 转谱完成: {result.note_count} 音 / 音域 {lo}-{hi}（MIDI）/ "
        f"网格 {options.grid_beats:g} 拍"
    )
    if options.bpm is None:
        label = "高" if result.confidence >= 0.5 else (
            "中" if result.confidence >= 0.25 else "低"
        )
        typer.echo(
            f"   自动测速 {result.detected_bpm:.1f} BPM（置信度 {result.confidence:.2f}·{label}）"
        )
    typer.echo(f"   输出: {written}")


@app.command("analyze", help="音频分析（R13）：调性 / 和弦 / 结构分段（numpy-only）")
@_guard
def analyze_cmd(
    wav: str = typer.Argument(..., help="输入音频路径"),
    key: bool = typer.Option(False, "--key", help="调性估计（Krumhansl-Schmuckler 剖面相关）"),
    chords: bool = typer.Option(False, "--chords", help="和弦进行估计（三和弦模板匹配）"),
    structure: bool = typer.Option(
        False, "--structure", help="结构分段（自相似矩阵 + Foote 新奇度）"
    ),
    json_out: Optional[Path] = typer.Option(None, "--json", help="把结果写为 JSON 文件"),
) -> None:
    """对音频做高层分析（R13）。

    三个子分析可单独或组合开启；**一个都不给时默认全跑**。
    与 ``tempo`` / ``transcribe`` 共用 numpy-only 底座（无 librosa）。
    结构段边界可供 ``video --style score`` 的章节切换等下游使用。
    """
    import json

    from sunoauxtool.exceptions import InputFileError

    src = Path(wav).expanduser()
    if not src.is_file():
        raise InputFileError(f"输入音频不存在: {src}", code=3)

    from sunoauxtool.analysis.chords import estimate_chords
    from sunoauxtool.analysis.key import estimate_key
    from sunoauxtool.analysis.structure import estimate_structure
    from sunoauxtool.analysis.tempo import read_wav_mono

    run_all = not (key or chords or structure)
    mono, sr = read_wav_mono(str(src))
    payload: dict = {"file": str(src.resolve()), "sample_rate": sr}

    if key or run_all:
        est = estimate_key(mono, sr)
        payload["key"] = {
            "key": est.key,
            "mode": est.mode,
            "label": est.label,
            "confidence": round(est.confidence, 4),
        }
        typer.echo(f"🎼 调性: {est.label}（置信度 {est.confidence:.2f}）")

    if chords or run_all:
        segs = estimate_chords(mono, sr)
        payload["chords"] = [
            {
                "start": round(s.start, 3),
                "end": round(s.end, 3),
                "root": s.root,
                "quality": s.quality,
                "label": s.label,
            }
            for s in segs
        ]
        typer.echo(f"🎹 和弦: {len(segs)} 段")
        for s in segs[:20]:
            typer.echo(f"   {s.start:6.2f}s - {s.end:6.2f}s  {s.label}")
        if len(segs) > 20:
            typer.echo(f"   ...（其余 {len(segs) - 20} 段见 --json）")

    if structure or run_all:
        secs = estimate_structure(mono, sr)
        payload["structure"] = [
            {
                "index": s.index,
                "start": round(s.start, 3),
                "end": round(s.end, 3),
                "duration": round(s.duration, 3),
            }
            for s in secs
        ]
        typer.echo(f"📐 结构: {len(secs)} 段")
        for s in secs:
            typer.echo(f"   #{s.index} {s.start:6.2f}s - {s.end:6.2f}s（{s.duration:.2f}s）")

    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        typer.echo(f"    JSON 输出: {json_out}")


@app.command("video-preview", help="视频产物预览（R15）：抽缩略帧 + 时间轴 scrub HTML")
@_guard
def video_preview_cmd(
    video: str = typer.Argument(..., help="视频文件路径"),
    n: int = typer.Option(6, "--frames", "-n", help="缩略帧数量"),
    out: Optional[Path] = typer.Option(
        None, "-o", "--out", help="输出根目录（默认 output/，其下新建 preview/<视频名>/）"
    ),
) -> None:
    """为视频产物生成 scrub 预览页（R15）。

    缩略帧写入**新建目录** ``<out>/preview/<视频名>/``（默认 ``output/preview/...``），
    不覆盖既有产物；点击缩略图即 seek 到对应时间点。依赖 ffmpeg/ffprobe（未装会报错）。
    """
    from sunoauxtool.exceptions import InputFileError

    src = Path(video).expanduser()
    if not src.is_file():
        raise InputFileError(f"视频文件不存在: {src}", code=3)

    from sunoauxtool.preview import VideoPreviewGenerator

    gen = VideoPreviewGenerator(n_frames=n)
    html = gen.generate(str(src), output_root=out)
    typer.echo(f"✅ 视频预览已生成: {html}")
    typer.echo(f"   缩略帧 {n} 个，位于 {Path(html).parent}")


@app.command("new", help="交互式引导生成新音乐（新手指南）")
@_guard
def new_cmd() -> None:
    """交互式向导：引导用户逐步生成第一段音乐。"""
    import sys as _sys

    # 非 TTY：直接执行 pipeline 默认参数
    if not _sys.stdin.isatty():
        from sunoauxtool.pipeline import Pipeline
        from sunoauxtool.config import Config
        from sunoauxtool.generators.base import GenerationRequest
        from sunoauxtool.export.suno import ExportOptions

        cfg = Config.load()
        pipeline = Pipeline(cfg)
        result = pipeline.run(
            GenerationRequest(seed=42),
            ExportOptions(duration=25),
        )
        typer.echo(f"✅ Pipeline 完成: {result.export_path}")
        return

    typer.echo("🎵 SunoAuxTool 创作向导")
    typer.echo("按回车使用默认值，或输入自定义值")
    typer.echo("")

    # 步骤 1：风格
    style = _prompt("风格", "pop", ["pop", "rock", "electronic", "classical"])

    # 步骤 2：BPM
    bpm_str = _prompt("BPM", "120")
    bpm = int(bpm_str) if bpm_str.isdigit() else 120

    # 步骤 3：和弦
    chords = _prompt("和弦进行", "C-G-Am-F")

    # 步骤 4：时长
    dur_str = _prompt("Suno 导出时长 (10-30s)", "25")
    duration = max(10, min(30, int(dur_str) if dur_str.isdigit() else 25))

    # 步骤 5：小节数
    bars_str = _prompt("小节数", "8")
    bars = max(1, min(64, int(bars_str) if bars_str.isdigit() else 8))

    # 确认
    typer.echo("")
    typer.echo("📋 确认参数:")
    typer.echo(f"  风格: {style}")
    typer.echo(f"  BPM: {bpm}")
    typer.echo(f"  和弦: {chords}")
    typer.echo(f"  时长: {duration}s")
    typer.echo(f"  小节: {bars}")
    confirm = input("执行？[Y/n] ").strip().lower()
    if confirm in ("n", "no"):
        typer.echo("已取消")
        return

    # 执行
    from sunoauxtool.pipeline import Pipeline
    from sunoauxtool.config import Config
    from sunoauxtool.generators.base import GenerationRequest
    from sunoauxtool.export.suno import ExportOptions

    cfg = Config.load()
    merged = cfg.merge_cli(style=style, bpm=bpm, chords=chords, bars=bars)
    pipeline = Pipeline(merged)
    result = pipeline.run(
        GenerationRequest(style=style, bpm=bpm, chords=chords, bars=bars,
                          seed=merged.random.seed),
        ExportOptions(duration=duration),
    )
    typer.echo(f"✅ 完成: {result.export_path}")
    typer.echo(f"   预览页: {Path(result.export_path).parent / 'preview.html'}")
    typer.echo("   提示: 用 sunoauxtool inspire add <path> 保存为灵感")


@app.command("score", help="从 MIDI 生成谱面（五线谱 SVG/PNG、简谱、MusicXML）")
@_guard
def score_cmd(
    ctx: typer.Context,
    midi: Path = typer.Argument(..., help="输入 .mid 路径"),
    format: str = typer.Option(
        "svg", "--format", "-f",
        help="输出格式（逗号分隔）：svg / png / jianpu / jianpu-txt / musicxml / all",
    ),
    output_dir: Optional[Path] = typer.Option(
        None, "--output-dir", "-o", help="输出目录（默认与输入同目录）"
    ),
    key: Optional[str] = typer.Option(None, "--key", help="调式，如 'C major' / 'a minor'"),
    time_signature: Optional[str] = typer.Option(
        None, "--time-signature", help="拍号，如 '4/4'（默认 4/4）"
    ),
    title: Optional[str] = typer.Option(None, "--title", help="标题（默认取文件名）"),
    composer: Optional[str] = typer.Option(None, "--composer", help="作曲者署名"),
    bars: Optional[int] = typer.Option(None, "--bars", help="小节数（默认按最后一个音推算）"),
    clef: Optional[List[str]] = typer.Option(
        None, "--clef", help="谱号覆盖，格式 '轨道名=treble|bass'（可多次）"
    ),
    theme: str = typer.Option("light", "--theme", help="配色：light（白纸黑墨）/ dark"),
    scale: float = typer.Option(
        2.0, "--scale", help="位图超采样倍数（越大边缘越干净；不改变输出像素尺寸）"
    ),
    page_width: Optional[float] = typer.Option(
        None, "--page-width", help="页面宽度（像素；五线谱与简谱共用）"
    ),
    space: Optional[float] = typer.Option(None, "--space", help="五线谱谱线间距（简谱忽略）"),
    no_title: bool = typer.Option(False, "--no-title", help="不渲染标题区"),
    no_tempo: bool = typer.Option(False, "--no-tempo", help="不渲染速度记号"),
    no_measure_numbers: bool = typer.Option(
        False, "--no-measure-numbers", help="不渲染小节号"
    ),
    no_ties: bool = typer.Option(False, "--no-ties", help="不渲染延音线/连音弧"),
    list_formats: bool = typer.Option(
        False, "--list-formats", help="只打印可用格式与主题，不渲染"
    ),
) -> None:
    """从 .mid 生成谱面产物（Task 8-#12）。

    产物与输入同名不同后缀，落在同一目录（或 --output-dir）：
    ``<stem>.svg`` / ``<stem>.png`` / ``<stem>.jianpu.svg`` /
    ``<stem>.jianpu.txt`` / ``<stem>.musicxml``。

    ``--format all`` 一次产出全部；PNG 需要 Pillow。

    注：``--key`` 只影响调号/拼写，不改变音符本身；MIDI 文件里没有调式信息，
    默认按 C 大调记谱（小调素材请显式传 ``--key 'a minor'``）。
    """
    from sunoauxtool.score_export import (
        SCORE_FORMATS,
        THEMES,
        ScoreExportOptions,
        export_score,
        score_from_midi,
    )

    if list_formats:
        typer.echo("可用谱面格式：")
        for name, desc in SCORE_FORMATS.items():
            typer.echo(f"  {name:11s} {desc}")
        typer.echo("别名：all = 全部；xml = musicxml；txt = jianpu-txt")
        typer.echo("可用主题：" + "、".join(f"{k}（{v}）" for k, v in THEMES.items()))
        return

    source = Path(midi).expanduser()
    if not source.exists():
        raise InputFileError(f"MIDI 文件不存在: {source}")

    clefs = _parse_clef_overrides(clef)
    # 经 score_export 统一入口：非法 --key / --time-signature 归一化为 ParameterError，
    # 与 generate/pipeline 的 --score-key 路径同一错误契约（不再冒成「意外错误」）。
    score = score_from_midi(
        source,
        title=title,
        composer=composer or "",
        key=key,
        time_signature=time_signature,
        bars=bars,
        clefs=clefs or None,
    )
    options = ScoreExportOptions(
        formats=format,
        theme=theme,
        scale=scale,
        page_width=page_width,
        space=space,
        show_title=not no_title,
        show_tempo=not no_tempo,
        show_measure_numbers=not no_measure_numbers,
        show_ties=not no_ties,
    )
    target_dir = Path(output_dir).expanduser() if output_dir else source.parent
    written = export_score(score, target_dir, source.stem, options)

    typer.echo(
        f"✅ 谱面已生成: {score.title}（{score.bars} 小节 / {score.note_count} 音 / "
        f"{len(score.tracks)} 轨 / {score.key}）"
    )
    for name, path in written.items():
        typer.echo(f"   {name:11s} -> {path}")


def _export_score_for(
    seq, midi_path, formats: str, theme: str, key: Optional[str]
) -> dict:
    """``generate midi/melody --score`` 的谱面导出：与 MIDI 同目录同主干。

    与 ``pipeline --score`` 的策略差异（有意为之）：这里**失败即抛出**。``--score``
    是本命令的显式请求，静默降级会让用户以为产物已生成；而 pipeline 的主产物是音频，
    谱面属附加物，故那边只告警。

    Args:
        seq: 生成出的 ``NoteSequence``。
        midi_path: 刚落盘的 .mid 路径。
        formats: 格式串（``ScoreExportOptions`` 会归一化）。
        theme: 配色名。
        key: 记谱调式覆盖。

    Returns:
        ``{格式: 绝对路径}``。
    """
    from sunoauxtool.score_export import (
        ScoreExportOptions,
        export_score,
        score_from_sequence,
    )

    target = Path(midi_path)
    score = score_from_sequence(seq, title=target.stem, key=key)
    return export_score(
        score, target.parent, target.stem, ScoreExportOptions(formats=formats, theme=theme)
    )


def _parse_clef_overrides(items: Optional[List[str]]) -> dict:
    """把 ``--clef '谱表名=treble'`` 解析为 ``{名称: 谱号}``。

    Raises:
        ParameterError: 缺少 ``=``、谱号名非法、或同一谱表重复指定。
    """
    allowed = {"treble", "bass", "alto", "tenor"}
    out: dict = {}
    for item in items or []:
        if "=" not in item:
            raise ParameterError(f"--clef 需要 '谱表名=谱号' 形式，实为 {item!r}")
        name, _, value = item.partition("=")
        name, value = name.strip(), value.strip().lower()
        if not name:
            raise ParameterError(f"--clef 的谱表名不可为空: {item!r}")
        if value not in allowed:
            raise ParameterError(
                f"--clef 的谱号须为 {'/'.join(sorted(allowed))} 之一，实为 {value!r}"
            )
        if name in out:
            raise ParameterError(f"--clef 重复指定谱表 {name!r}")
        out[name] = value
    return out


@app.command("errors", help="打印错误码表")
@_guard
def errors_cmd() -> None:
    """错误码表（P2-3b 文档化入口）。"""
    typer.echo("SunoAuxTool 错误码表")
    for code, name, desc in ERROR_CODES:
        if desc:
            typer.echo(f"  {code}  {name}: {desc}")
        else:
            typer.echo(f"  {code}  {name}")


# ---------------------------------------------------------------------------
# ai（AI 适配器：musicgen / audio-sr，已完整实现）
# ---------------------------------------------------------------------------

@ai_app.command("musicgen", help="MusicGen 适配器（P1）：旋律 WAV -> 伴奏 WAV")
@_guard
def ai_musicgen(
    ctx: typer.Context,
    input: Path = typer.Option(..., "--input", "-i", help="旋律 WAV 路径"),
    prompt: str = typer.Option(..., "--prompt", help="风格提示，如 'upbeat pop'"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="输出 WAV 路径"),
    model_size: Optional[str] = typer.Option(None, "--model-size", help="medium|small（默认 medium，显存不足可 small 降档）"),
    duration: Optional[int] = typer.Option(None, "--duration", help="目标时长（默认对齐输入）"),
    seed: Optional[int] = typer.Option(None, "--seed", help="随机种子"),
    device: Optional[str] = typer.Option(None, "--device", help="cuda|cpu"),
) -> None:
    """MusicGen 扩编曲（P1；P0 环境提示安装依赖，退出码 6）。"""
    from sunoauxtool.ai.musicgen import MusicGenAdapter

    cfg = _load_config(ctx)
    merged = cfg.merge_cli(model_size=model_size, device=device)
    adapter = MusicGenAdapter(model_size=merged.ai.model_size, device=merged.ai.device)
    path = adapter.generate(
        str(input), prompt,
        output_path=str(output) if output else None,
        duration=duration, seed=seed,
    )
    _write_single_metadata(
        cfg,
        command=f"sunoauxtool ai musicgen --input {input} --prompt {prompt!r}",
        seed=seed,
        artifacts=[
            ArtifactMeta(
                path=path, kind="draft",
                params={"prompt": prompt, "model_size": merged.ai.model_size,
                        "duration": duration},
                seed=seed, seq=1, duration_s=0.0, sample_rate=32000,
                contains_vocals=False,
            )
        ],
    )
    typer.echo(f"✅ 伴奏 WAV 已生成: {path}")


@ai_app.command("diffrhythm", help="DiffRhythm 适配器（P1）：风格提示 -> 歌曲草稿 WAV")
@_guard
def ai_diffrhythm(
    ctx: typer.Context,
    prompt: str = typer.Option(..., "--prompt", help="风格提示，如 'slow ballad'"),
    input: Optional[Path] = typer.Option(None, "--input", "-i", help="可选旋律 WAV 路径"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="输出 WAV 路径"),
    lyrics: Optional[str] = typer.Option(None, "--lyrics", help="歌词（纯文本，行分隔；不传为空词哼唱）"),
    duration: Optional[int] = typer.Option(None, "--duration", help="目标时长（默认 95s）"),
    device: Optional[str] = typer.Option(None, "--device", help="cuda|cpu"),
    diffrhythm_dir: Optional[Path] = typer.Option(
        None, "--diffrhythm-dir", help="DiffRhythm 仓库根目录（覆盖 DIFFRHYTHM_DIR 与 module/diffrhythm 默认）"
    ),
) -> None:
    """DiffRhythm 歌曲草稿（P1；P0 环境提示安装依赖，退出码 6）。"""
    from sunoauxtool.ai.diffrhythm import DiffRhythmAdapter

    cfg = _load_config(ctx)
    merged = cfg.merge_cli(device=device)
    adapter = DiffRhythmAdapter(
        device=merged.ai.device,
        model_dir=str(diffrhythm_dir) if diffrhythm_dir else None,
    )
    src = str(input) if input is not None else ""
    path = adapter.generate(
        src, prompt,
        output_path=str(output) if output else None,
        lyrics=lyrics, duration=duration,
    )
    _write_single_metadata(
        cfg,
        command=f"sunoauxtool ai diffrhythm --prompt {prompt!r}",
        seed=None,
        artifacts=[
            ArtifactMeta(
                path=path, kind="draft",
                params={"prompt": prompt, "lyrics": bool(lyrics), "duration": duration,
                        "chunked": merged.ai.diffrhythm_chunked},
                seed=None, seq=1, duration_s=0.0, sample_rate=44100,
                contains_vocals=True,
            )
        ],
    )
    typer.echo(f"✅ 歌曲草稿已生成: {path}")
