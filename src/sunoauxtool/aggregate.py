"""sunoaux：顶层聚合 CLI（前期 pre / 后期 post 的统一入口）。

设计铁律（继承自重构计划 R3）：
  - **薄转发层**：所有子命令直接二次注册既有命令函数（Typer 的 command
    装饰器返回原函数，可安全挂到新 app 上），零参数复制、零业务逻辑；
  - 核心层不得反向依赖 CLI 层——本模块只 import，不被 import；
  - 旧入口 ``sunoauxtool`` / ``videomaker`` / ``downloadhelper`` 保留（兼容期 >= 1 个版本）。

子命令结构：
    sunoaux pre melody / midi / score / render / transcribe     （前期：创作）
    sunoaux post probe / convert / fetch                        （后期：取回与转码）
    sunoaux post video render / multi                           （后期：音乐视频）
    sunoaux post dsp        （R6 已交付：DSP 算子链）
    sunoaux post enhance    （AudioSR 音质提升，R5；依赖可选装）

映射表（新 -> 旧）：
    pre  melody      -> sunoauxtool generate melody
    pre  midi        -> sunoauxtool generate midi
    pre  score       -> sunoauxtool score
    pre  render      -> sunoauxtool render
    pre  transcribe  -> sunoauxtool transcribe
    post probe       -> downloadhelper probe
    post convert     -> downloadhelper decode
    post fetch       -> downloadhelper batch
    post video *     -> videomaker render / multi
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from sunoauxtool import __version__
from sunoauxtool import cli as core_cli
from sunoauxtool.ai.audiosr import AudioSRAdapter
from sunoauxtool.commands.helpers import _guard
from sunoauxtool.download import cli as download_cli
from sunoauxtool.exceptions import InputFileError, ParameterError
from sunoauxtool.video import cli as video_cli

app = typer.Typer(
    name="sunoaux",
    help="SunoAuxTool 聚合入口：pre（前期创作）+ post（后期处理）",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"sunoaux {__version__}")
        raise typer.Exit(0)


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", help="显示版本号", callback=_version_callback, is_eager=True
    ),
) -> None:
    """sunoaux 聚合入口（薄转发层，业务逻辑在既有命令实现中）。"""


# ---------------------------------------------------------------------------
# pre：前期创作（映射 sunoauxtool 既有命令）
# ---------------------------------------------------------------------------

pre_app = typer.Typer(help="前期：旋律 / MIDI / 谱面 / 渲染 / 转谱", no_args_is_help=True)

pre_app.command("melody", help="(= generate melody) 生成旋律 MIDI")(core_cli.generate_melody)
pre_app.command("midi", help="(= generate midi) 程序化生成 MIDI")(core_cli.generate_midi)
pre_app.command("score", help="(= score) 从 MIDI 生成谱面")(core_cli.score_cmd)
pre_app.command("render", help="(= render) MIDI -> WAV 渲染")(core_cli.render_cmd)
pre_app.command("transcribe", help="(= transcribe) WAV -> MIDI 转谱")(core_cli.transcribe_cmd)

app.add_typer(pre_app, name="pre")


# ---------------------------------------------------------------------------
# post：后期处理（映射 downloadhelper / videomaker 既有命令）
# ---------------------------------------------------------------------------

post_app = typer.Typer(help="后期：取回 / 转码 / 视频 / DSP / 音质提升", no_args_is_help=True)

post_app.command("probe", help="(= downloadhelper probe) 取证判定：明文 / 加密密文")(
    download_cli.probe
)
post_app.command("convert", help="(= downloadhelper decode) fMP4 -> Opus/MP3 转码")(
    download_cli.decode
)
post_app.command("convert-audio", help="(= downloadhelper convert) 音频互转 mp3/wav/m4a/flac")(
    download_cli.convert
)
post_app.command("extract-audio", help="(= downloadhelper extract-audio) 视频分离音轨（直通优先）")(
    download_cli.extract_audio
)
@post_app.command(
    "fetch",
    help="取回音频（R7）：catcatch=猫抓缓存扫描转码（默认）；suno-api/haimeng/tianyin=API 源",
)
@_guard
def fetch_cmd(
    query: str = typer.Argument(..., help="catcatch=缓存目录；API 源=歌曲/任务 ID"),
    source: str = typer.Option(
        "catcatch", "--source", "-s", help="源：catcatch | suno-api | haimeng | tianyin"
    ),
    out: Path = typer.Option(None, "-o", "--out", help="输出目录（catcatch 默认原目录；API 默认 ./fetched/<source>）"),
    fmt: str = typer.Option("both", "--fmt", help="[catcatch] 输出格式: opus | mp3 | both"),
    bitrate: str = typer.Option("192k", "--bitrate", help="[catcatch] MP3 码率"),
    ffmpeg: Optional[str] = typer.Option(None, "--ffmpeg-path", help="[catcatch] ffmpeg 绝对路径"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="[R11] 仅校验凭证/可用性：不实际取回；缺凭证报 25"
    ),
) -> None:
    """统一取回入口（R7）：错误码 25=凭证缺失、26=请求失败；猫抓沿用 20-24。"""
    if dry_run:
        from sunoauxtool.download.sources.base import list_sources

        valid = {s.name: s for s in list_sources()}
        if source not in valid:
            known = ", ".join(sorted(valid))
            raise ParameterError(f"未知下载源: {source}（可用: {known}）", code=1)
        typer.echo(f"🔎 dry-run（不取回）: {valid[source].check(query)}")
        return

    if source == "catcatch":
        # 直通既有 batch 实现（能力零复制）
        download_cli.batch(
            directory=Path(query),
            out=out,
            fmt=fmt,
            bitrate=bitrate,
            ffmpeg=ffmpeg,
        )
        return

    from sunoauxtool.download.sources.base import list_sources
    from sunoauxtool.download.sources.catcatch import CatCatchSource

    valid = {s.name: s for s in list_sources()}
    if source not in valid:
        known = ", ".join(sorted(valid))
        raise ParameterError(f"未知下载源: {source}（可用: {known}）", code=1)

    adapter = valid[source]
    if isinstance(adapter, CatCatchSource):
        adapter.fmt, adapter.bitrate, adapter.ffmpeg = fmt, bitrate, ffmpeg
    target_out = out or Path("fetched") / source
    files = adapter.fetch(query, target_out)
    typer.echo(f"✅ 取回 {len(files)} 个文件（source={source}）:")
    for f in files:
        typer.echo(f"   {f.path}")


@post_app.command(
    "dsp",
    help="DSP 算子链：norm/loudnorm/fade-in/fade-out/trim/resample/lowcut/compress/"
         "expand/limiter/reverb/concat",
)
@_guard
def dsp_cmd(
    audio: str = typer.Argument(..., help="输入 WAV 路径"),
    ops_spec: str = typer.Option(
        ...,
        "--ops",
        help='算子串（逗号分隔），如 "norm -1, fade-in 0.5, trim 10-25, resample 32000"',
    ),
    output: Path = typer.Option(None, "-o", "--output", help="输出 WAV（默认 <输入>_dsp.wav）"),
    bit_depth: int = typer.Option(16, "--bit-depth", help="输出位深：16 | 24"),
) -> None:
    """DSP 算子链（R6）：错误码 15=处理失败 / 16=参数错误。"""
    from sunoauxtool.dsp.ops import OPS, apply_ops, parse_ops
    from sunoauxtool.export import audio as audio_ops

    src = Path(audio)
    if not src.is_file():
        raise InputFileError(f"输入音频不存在: {src}", code=3)
    if bit_depth not in (16, 24):
        raise ParameterError(f"bit_depth 只能是 16 或 24: {bit_depth}", code=16)

    ops = parse_ops(ops_spec)
    unknown = [op.name for op in ops if op.name not in OPS]
    if unknown:
        raise ParameterError(
            f"未知算子: {', '.join(unknown)}（可用: {', '.join(sorted(OPS))}）", code=16
        )

    wave, sr = audio_ops.read_wav(src)
    wave, sr = apply_ops(wave, sr, ops, base_dir=src.parent)

    out = output or src.with_name(src.stem + "_dsp.wav")
    written = audio_ops.write_wav(out, wave, sr, bit_depth=bit_depth)
    typer.echo(f"✅ DSP 完成: {written}（{wave.shape[0] / sr:.2f}s @ {sr}Hz, {bit_depth}bit）")


@post_app.command(
    "enhance",
    help="AudioSR 音质提升/超分（R5；长音频自动分块交叉淡化；未装依赖 exit 6）",
)
@_guard
def enhance_cmd(
    audio: str = typer.Argument(..., help="输入音频路径（WAV）"),
    output: Path = typer.Option(None, "-o", "--output", help="输出 WAV（默认 <输入>_enhanced.wav）"),
    model: str = typer.Option("basic", "--model", help="模型：basic（音乐/通用）| speech"),
    seed: int = typer.Option(42, "--seed", help="随机种子"),
    steps: int = typer.Option(50, "--steps", help="DDIM 步数（默认 50）"),
    chunk: float = typer.Option(15.0, "--chunk", help="长音频分块秒数"),
    overlap: float = typer.Option(2.0, "--overlap", help="分块重叠秒数"),
) -> None:
    """AudioSR 超分（R5）：输出单声道 48kHz WAV（上游管线行为）。"""
    src = Path(audio)
    if not src.is_file():
        raise InputFileError(f"输入音频不存在: {src}", code=3)
    out = output or src.with_name(src.stem + "_enhanced.wav")

    adapter = AudioSRAdapter(
        model_name=model,
        seed=seed,
        ddim_steps=steps,
        chunk_duration_s=chunk,
        overlap_duration_s=overlap,
    )
    if not adapter.is_available():
        from sunoauxtool.exceptions import AiDependencyError

        raise AiDependencyError(
            "audiosr 不可用：未找到 AudioSR 源码目录"
            "（设 AUDIOSR_DIR 或克隆到 src/versatile_audio_super_resolution，"
            "依赖见 requirements/vasr.txt）",
            code=6,
        )

    written = adapter.enhance(str(src), str(out))
    typer.echo(f"✅ 音质提升完成: {written}")


video_app = typer.Typer(help="音乐视频（sunoauxtool.video，原 videomaker）", no_args_is_help=True)
video_app.command("render")(video_cli.render)
video_app.command("multi")(video_cli.multi)
video_app.command("presets")(video_cli.presets)
video_app.command("config")(video_cli.config)
video_app.command("version")(video_cli.version)
post_app.add_typer(video_app, name="video")

app.add_typer(post_app, name="post")
