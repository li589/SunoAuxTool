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
    sunoaux post suno-dl    （R8 已交付：Suno 官方明文下载三段式）
    sunoaux post strip-meta （R9 已交付：移除 Suno 痕迹——标签清除/保留/覆写 + MOTW）
    sunoaux compose init / validate / build  （2.0 PR-C：代码驱动编曲，song.toml → MIDI）

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
from typing import List, Optional

import typer

from sunoauxtool import __version__
from sunoauxtool import cli as core_cli
from sunoauxtool.ai.audiosr import AudioSRAdapter
from sunoauxtool.commands.helpers import _guard
from sunoauxtool.compose import cli as compose_cli
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
pre_app.command("musicgen", help="(= ai musicgen) MusicGen：旋律 WAV -> 伴奏 WAV")(
    core_cli.ai_musicgen
)
pre_app.command("diffrhythm", help="(= ai diffrhythm) DiffRhythm：风格提示 -> 歌曲草稿 WAV")(
    core_cli.ai_diffrhythm
)

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
post_app.command("ncm", help="(= downloadhelper ncm) NCM 解包：网易云容器还原 flac/mp3")(
    download_cli.ncm
)
post_app.command("unlock", help="(= downloadhelper unlock) 通用解密：ncm/kwm/kgm/vpr/qmc 系列")(
    download_cli.unlock
)
@post_app.command(
    "fetch",
    help="取回音频（R7）：catcatch=猫抓缓存扫描转码（默认）；suno-api/haimeng/tianyin=API 源；"
         "usesuno=usesuno.com 转播源（SW-Mango 密文，需自备人验 token）",
)
@_guard
def fetch_cmd(
    query: str = typer.Argument(..., help="catcatch=缓存目录；API 源=歌曲/任务 ID；usesuno=clip_id 或 /song/<uuid> 链接"),
    source: str = typer.Option(
        "catcatch", "--source", "-s", help="源：catcatch | suno-api | haimeng | tianyin | usesuno"
    ),
    out: Path = typer.Option(None, "-o", "--out", help="输出目录（catcatch 默认原目录；API 默认 ./fetched/<source>）"),
    fmt: str = typer.Option("both", "--fmt", help="[catcatch] 输出格式: opus | mp3 | both"),
    bitrate: str = typer.Option("192k", "--bitrate", help="[catcatch] MP3 码率"),
    ffmpeg: Optional[str] = typer.Option(None, "--ffmpeg-path", help="[catcatch] ffmpeg 绝对路径"),
    token: Optional[str] = typer.Option(
        None,
        "--token",
        help="[usesuno] Bearer 会话令牌（默认读 USESUNO_TOKEN 或配置 [sources.usesuno] token）",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="[R11] 仅校验凭证/可用性：不实际取回；缺凭证报 25"
    ),
) -> None:
    """统一取回入口（R7）：错误码 25=凭证缺失、26=请求失败；猫抓沿用 20-24。"""
    if dry_run:
        from sunoauxtool.download.sources.base import list_sources
        from sunoauxtool.download.sources.usesuno import UseSunoSource

        valid = {s.name: s for s in list_sources()}
        if source not in valid:
            known = ", ".join(sorted(valid))
            raise ParameterError(f"未知下载源: {source}（可用: {known}）", code=1)
        adapter = valid[source]
        if isinstance(adapter, UseSunoSource) and token:
            adapter = UseSunoSource(token=token)
        typer.echo(f"🔎 dry-run（不取回）: {adapter.check(query)}")
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
    from sunoauxtool.download.sources.usesuno import UseSunoSource

    valid = {s.name: s for s in list_sources()}
    if source not in valid:
        known = ", ".join(sorted(valid))
        raise ParameterError(f"未知下载源: {source}（可用: {known}）", code=1)

    adapter = valid[source]
    if isinstance(adapter, CatCatchSource):
        adapter.fmt, adapter.bitrate, adapter.ffmpeg = fmt, bitrate, ffmpeg
    elif isinstance(adapter, UseSunoSource) and token:
        adapter = UseSunoSource(token=token)
    target_out = out or Path("fetched") / source
    files = adapter.fetch(query, target_out)
    typer.echo(f"✅ 取回 {len(files)} 个文件（source={source}）:")
    for f in files:
        typer.echo(f"   {f.path}")


@post_app.command(
    "suno-dl",
    help="Suno 官方明文下载（R8 三段式）：CDN 直拼 -> 载荷直链 -> 官方轮询（零解密）",
)
@_guard
def suno_dl_cmd(
    clips: List[str] = typer.Argument(
        ..., help="clip_id / 含 /song/<uuid> 的链接，可多个"
    ),
    out: Path = typer.Option(Path("suno-dl"), "-o", "--out", help="输出目录"),
    fmt: str = typer.Option("mp3", "--fmt", help="格式: mp3 | wav | mp4"),
    cookie: Optional[str] = typer.Option(
        None, "--cookie", help="会话 Cookie 请求头串（默认读 SUNO_DL_COOKIE 或配置 [sources.suno] cookie）"
    ),
    api_base: Optional[str] = typer.Option(
        None, "--api-base", help="官方 API 基址（默认 Suno 官方域，可被 SUNO_DL_API_BASE 覆盖）"
    ),
    authorize: bool = typer.Option(
        False, "--authorize", help="先调 download/authorize 配额门（涉及 credit 扣减）"
    ),
    poll_max: int = typer.Option(90, "--poll-max", help="download/clip 轮询次数上限"),
    poll_interval: float = typer.Option(2.0, "--poll-interval", help="download/clip 轮询间隔秒"),
    overwrite: bool = typer.Option(False, "--overwrite", help="覆盖同名输出（默认 uniquify 追加 -1/-2）"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="仅自检 clip_id 形态与凭证掩码，不发请求"
    ),
) -> None:
    """Suno 官方明文下载（R8）：错误码 30 凭证缺失 / 31 请求失败 / 32 响应解析 /
    33 轮询超时 / 34 全路径失败；批量部分失败沿用 8、全失败 9。"""
    import os

    from sunoauxtool.download.suno_dl import (
        DEFAULT_API_BASE,
        SUPPORTED_FORMATS,
        SunoDownloader,
        extract_clip_id,
        load_cookie_from_config,
    )
    from sunoauxtool.exceptions import BatchFailedError, BatchPartialError, ParameterError

    if fmt not in SUPPORTED_FORMATS:
        raise ParameterError(f"--fmt 只支持 {'|'.join(SUPPORTED_FORMATS)}: {fmt}", code=1)

    resolved_cookie = cookie or os.environ.get("SUNO_DL_COOKIE") or load_cookie_from_config()
    resolved_base = api_base or os.environ.get("SUNO_DL_API_BASE") or DEFAULT_API_BASE
    downloader = SunoDownloader(cookie=resolved_cookie, api_base=resolved_base)

    clip_ids = [extract_clip_id(c) for c in clips]

    if dry_run:
        for cid in clip_ids:
            typer.echo(f"🔎 {downloader.check(cid)}")
        return

    succeeded: List[str] = []
    failed: List[str] = []
    for cid in clip_ids:
        try:
            f = downloader.download(
                cid, out, fmt=fmt, authorize=authorize,
                poll_max=poll_max, poll_interval=poll_interval,
                overwrite=overwrite,
            )
            typer.echo(f"✅ {cid}: {f.path}（via={f.meta.get('via')}）")
            succeeded.append(cid)
        except Exception as exc:  # noqa: BLE001 - 汇总后统一决定部分/全部失败
            typer.echo(f"❌ {cid}: {exc}", err=True)
            failed.append(cid)

    if failed and succeeded:
        raise BatchPartialError(
            f"suno-dl 成功 {len(succeeded)} 个、失败 {len(failed)} 个: {', '.join(failed)}", code=8
        )
    if failed:
        raise BatchFailedError(
            f"suno-dl 全部失败 {len(failed)} 个: {', '.join(failed)}（首个原因见上方 ❌ 行）", code=9
        )


@post_app.command(
    "strip-meta",
    help="移除 Suno 痕迹（R9）：all=移除全部标签/溯源并解除计算机保护 | "
         "keep-basic=只保留曲名/作者/歌词等 | custom=覆写自定义信息（原地）",
)
@_guard
def strip_meta_cmd(
    files: List[str] = typer.Argument(
        ..., help="音频文件（wav/mp3/flac/m4a/ogg/opus），可多个"
    ),
    mode: str = typer.Option("all", "--mode", help="all | keep-basic | custom"),
    title: Optional[str] = typer.Option(None, "--title", help="[custom] 曲名"),
    artist: Optional[str] = typer.Option(None, "--artist", help="[custom] 作者"),
    album: Optional[str] = typer.Option(None, "--album", help="[custom] 专辑"),
    genre: Optional[str] = typer.Option(None, "--genre", help="[custom] 流派"),
    year: Optional[str] = typer.Option(None, "--year", help="[custom] 年份"),
    lyrics: Optional[str] = typer.Option(None, "--lyrics", help="[custom] 歌词"),
    comment: Optional[str] = typer.Option(None, "--comment", help="[custom] 评论"),
    no_motw: bool = typer.Option(
        False, "--no-motw", help="不解除 Windows 计算机保护（zone.Identifier）"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="仅列出将移除/保留/覆写的内容，不写文件"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过原地覆写确认提示"),
) -> None:
    """移除音频元数据/溯源（R9）：错误码 35=容器不支持 / 36=标签读写失败；
    批量部分失败 8、全失败 9。注意：C2PA 音频域水印（soft binding）不在标签层，
    本功能不移除；硬绑定随 manifest 删除而失效。"""
    from sunoauxtool.exceptions import BatchFailedError, BatchPartialError
    from sunoauxtool.meta import MODES, SanitizeSpec, sanitize_file

    if mode not in MODES:
        raise ParameterError(f"--mode 只支持 {'|'.join(MODES)}: {mode}", code=1)

    paths = [Path(f) for f in files]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise InputFileError(f"文件不存在: {', '.join(missing)}", code=3)

    spec = SanitizeSpec(
        mode=mode,
        title=title,
        artist=artist,
        album=album,
        genre=genre,
        year=year,
        lyrics=lyrics,
        comment=comment,
        motw=not no_motw,
    )

    if dry_run:
        for p in paths:
            rep = sanitize_file(p, spec, dry_run=True)
            typer.echo(f"🔎 {p.name} [{rep.container}] {rep.summary()}")
        return

    if not yes:
        proceed = typer.confirm(
            f"⚠️ 将【原地覆写】{len(paths)} 个文件（元数据不可恢复），确认继续？",
            default=False,
        )
        if not proceed:
            typer.echo("已取消，未修改任何文件。")
            raise typer.Exit(0)

    succeeded: List[str] = []
    failed: List[str] = []
    errors: List[Exception] = []
    for p in paths:
        try:
            rep = sanitize_file(p, spec)
            typer.echo(f"✅ {p.name} [{rep.container}] {rep.summary()}")
            succeeded.append(str(p))
        except Exception as exc:  # noqa: BLE001 - 汇总后统一决定部分/全部失败
            typer.echo(f"❌ {p.name}: {exc}", err=True)
            failed.append(str(p))
            errors.append(exc)

    if failed and len(paths) == 1:
        # 单文件：透传原始错误码（35/36 等），比笼统的 9 更可操作
        raise errors[0]
    if failed and succeeded:
        raise BatchPartialError(
            f"strip-meta 成功 {len(succeeded)} 个、失败 {len(failed)} 个: "
            f"{', '.join(Path(f).name for f in failed)}",
            code=8,
        )
    if failed:
        raise BatchFailedError(
            f"strip-meta 全部失败 {len(failed)} 个: "
            f"{', '.join(Path(f).name for f in failed)}（首个原因见上方 ❌ 行）",
            code=9,
        )


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


# ---------------------------------------------------------------------------
# compose：代码驱动编曲（2.0 PR-C 新增，映射 sunoauxtool compose）
# ---------------------------------------------------------------------------

compose_app = typer.Typer(
    help="编曲：song.toml → MIDI + 编排快照（数字轴：代码驱动多轨）",
    no_args_is_help=True,
)
compose_app.command("init", help="(= sunoauxtool compose init) 生成 song.toml 模板")(
    compose_cli.init
)
compose_app.command("validate", help="(= sunoauxtool compose validate) 校验清单")(
    compose_cli.validate_cmd
)
compose_app.command("build", help="(= sunoauxtool compose build) 构建 MIDI + 编排快照")(
    compose_cli.build
)

app.add_typer(compose_app, name="compose")
