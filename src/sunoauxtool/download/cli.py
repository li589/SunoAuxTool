"""suno CLI：Suno 逆向取证与转码入口。

子命令：
  probe   对文件做取证判定（明文 / 加密密文 / 容器类型）
  decode  解码单个 fMP4（缓存捕获的 *.mp3）为 Opus / MP3
  batch   批量扫描目录：解码所有 fMP4，密文自动跳过并报告
  version 打印版本
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from sunoauxtool.download import __version__
from sunoauxtool.download.exceptions import EncryptedBlobError, SunoError
from sunoauxtool.download.forensics import identify
from sunoauxtool.download.transcoder import decode_fmp4, find_ffmpeg, probe as ffprobe_info

app = typer.Typer(
    help="Suno 逆向与转码：识别猫抓产物、解码 fMP4、判定加密密文",
    add_completion=False,
)


@app.command()
def probe(
    file: Path = typer.Argument(..., help="待取证的文件路径"),
    ffmpeg: Optional[str] = typer.Option(None, "--ffmpeg-path", help="ffmpeg 绝对路径"),
) -> None:
    """对文件做取证判定并打印报告。"""
    verdict = identify(str(file))
    typer.echo(verdict.render())

    if not verdict.is_encrypted:
        try:
            info = ffprobe_info(file, ffmpeg)
        except SunoError as exc:
            typer.echo(f"\n[ffprobe 不可用] {exc.message}")
            return
        typer.echo("\n媒体信息:")
        for key in (
            "codec_name",
            "codec_type",
            "sample_rate",
            "channels",
            "format_name",
            "duration",
            "bit_rate",
        ):
            if key in info:
                typer.echo(f"  {key:12s}: {info[key]}")


@app.command()
def decode(
    file: Path = typer.Argument(..., help="输入文件（fMP4，常被误标为 .mp3）"),
    out: Path = typer.Option(Path("."), "-o", "--out", help="输出目录"),
    fmt: str = typer.Option("both", "--fmt", help="输出格式: opus | mp3 | both"),
    stem: Optional[str] = typer.Option(None, "--stem", help="输出文件名主干"),
    bitrate: str = typer.Option("192k", "--bitrate", help="MP3 码率"),
    ffmpeg: Optional[str] = typer.Option(None, "--ffmpeg-path", help="ffmpeg 绝对路径"),
) -> None:
    """解码 fMP4 为可播放的 Opus / MP3。"""
    if fmt not in ("opus", "mp3", "both"):
        typer.echo("错误: --fmt 只能是 opus / mp3 / both", err=True)
        raise typer.Exit(code=2)

    try:
        outputs = decode_fmp4(file, out, fmt=fmt, stem=stem, bitrate=bitrate, ffmpeg=ffmpeg)
    except EncryptedBlobError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=exc.code or 22) from None
    except SunoError as exc:
        typer.echo(f"错误[{exc.code}]: {exc.message}", err=True)
        raise typer.Exit(code=exc.code or 1) from None

    for path in outputs:
        typer.echo(f"已生成: {path}")


@app.command()
def batch(
    directory: Path = typer.Argument(..., help="待扫描的目录"),
    out: Optional[Path] = typer.Option(None, "-o", "--out", help="输出目录，默认原目录"),
    fmt: str = typer.Option("both", "--fmt", help="输出格式: opus | mp3 | both"),
    bitrate: str = typer.Option("192k", "--bitrate", help="MP3 码率"),
    ffmpeg: Optional[str] = typer.Option(None, "--ffmpeg-path", help="ffmpeg 绝对路径"),
) -> None:
    """批量扫描目录：解码所有 fMP4，加密密文跳过并汇总报告。"""
    target_out = out or directory
    decoded: list[Path] = []
    skipped: list[str] = []
    errors: list[str] = []

    for file in sorted(directory.iterdir()):
        if not file.is_file():
            continue
        verdict = identify(str(file))
        if verdict.is_encrypted:
            skipped.append(f"{file.name}  (加密密文, χ²={verdict.chi_square:.0f})")
            continue
        if verdict.kind != "fmp4":
            continue
        try:
            decoded.extend(decode_fmp4(file, target_out, fmt=fmt, bitrate=bitrate, ffmpeg=ffmpeg))
        except SunoError as exc:
            errors.append(f"{file.name}: {exc.message}")

    typer.echo(f"解码成功 {len(decoded)} 个文件:")
    for path in decoded:
        typer.echo(f"  + {path}")
    if skipped:
        typer.echo(f"\n跳过 {len(skipped)} 个加密密文（无密钥不可解，建议改用缓存捕获产物）:")
        for item in skipped:
            typer.echo(f"  - {item}")
    if errors:
        typer.echo(f"\n失败 {len(errors)} 个:")
        for item in errors:
            typer.echo(f"  ! {item}")


@app.command("convert")
def convert(
    input: Path = typer.Argument(..., help="输入音频（mp3/wav/m4a/flac）"),
    out: Path = typer.Option(Path("."), "-o", "--out", help="输出目录，默认当前目录"),
    fmt: Optional[str] = typer.Option(None, "--fmt", help="目标格式: mp3 | wav | m4a | flac（默认 mp3）"),
    bitrate: Optional[str] = typer.Option(None, "--bitrate", help="有损码率，如 192k（默认 192k）"),
    sample_rate: Optional[int] = typer.Option(None, "--sample-rate", help="目标采样率（默认保持源）"),
    bit_depth: Optional[int] = typer.Option(None, "--bit-depth", help="wav 位深: 16 | 24（默认 16）"),
    profile: Optional[str] = typer.Option(None, "--profile", help="预设: suno | lossless | web（显式参数优先）"),
    stem: Optional[str] = typer.Option(None, "--stem", help="输出文件名主干（默认输入文件名）"),
    overwrite: bool = typer.Option(False, "--overwrite", help="覆盖已存在的输出文件"),
    ffmpeg: Optional[str] = typer.Option(None, "--ffmpeg-path", help="ffmpeg 绝对路径"),
) -> None:
    """音频格式互转（1.6.0）：mp3 / wav / m4a / flac。"""
    from sunoauxtool.download.convert import convert_audio

    try:
        result = convert_audio(
            input, out, fmt=fmt, bitrate=bitrate, sample_rate=sample_rate,
            bit_depth=bit_depth, profile=profile, stem=stem,
            overwrite=overwrite, ffmpeg=ffmpeg,
        )
    except SunoError as exc:
        typer.echo(f"错误[{exc.code}]: {exc.message}", err=True)
        raise typer.Exit(code=exc.code or 1) from None
    typer.echo(f"已转换: {result}")


@app.command("extract-audio")
def extract_audio(
    video: Path = typer.Argument(..., help="输入视频（mp4/mov/flv/webm/mkv/avi/m4v）"),
    out: Path = typer.Option(Path("."), "-o", "--out", help="输出目录，默认当前目录"),
    fmt: Optional[str] = typer.Option(None, "--fmt", help="目标格式: mp3 | wav | m4a | flac（默认 mp3）"),
    bitrate: Optional[str] = typer.Option(None, "--bitrate", help="有损码率（默认 192k）"),
    sample_rate: Optional[int] = typer.Option(None, "--sample-rate", help="目标采样率（默认保持源）"),
    bit_depth: Optional[int] = typer.Option(None, "--bit-depth", help="wav 位深: 16 | 24"),
    profile: Optional[str] = typer.Option(None, "--profile", help="预设: suno | lossless | web"),
    stem: Optional[str] = typer.Option(None, "--stem", help="输出文件名主干"),
    overwrite: bool = typer.Option(False, "--overwrite", help="覆盖已存在的输出文件"),
    copy_first: bool = typer.Option(
        True, "--copy/--no-copy", help="源音轨可无损放入目标容器时直通（不重编码）"
    ),
    ffmpeg: Optional[str] = typer.Option(None, "--ffmpeg-path", help="ffmpeg 绝对路径"),
) -> None:
    """从视频分离音轨（1.6.0）：优先无损直通，回退重编码。"""
    from sunoauxtool.download.convert import extract_audio as _extract

    try:
        result = _extract(
            video, out, fmt=fmt, bitrate=bitrate, sample_rate=sample_rate,
            bit_depth=bit_depth, profile=profile, stem=stem,
            overwrite=overwrite, copy_first=copy_first, ffmpeg=ffmpeg,
        )
    except SunoError as exc:
        typer.echo(f"错误[{exc.code}]: {exc.message}", err=True)
        raise typer.Exit(code=exc.code or 1) from None
    typer.echo(f"已提取: {result}")


@app.command("ncm")
def ncm(
    file: Path = typer.Argument(..., help="输入 .ncm 文件（网易云加密容器）"),
    out: Path = typer.Option(Path("."), "-o", "--out", help="输出目录，默认当前目录"),
    stem: Optional[str] = typer.Option(None, "--stem", help="输出文件名主干（默认按元数据命名）"),
    cover: bool = typer.Option(True, "--cover/--no-cover", help="是否同时导出封面图"),
    overwrite: bool = typer.Option(False, "--overwrite", help="覆盖已存在的输出文件"),
) -> None:
    """解包 NCM 为原始音频（1.6.1）：flac/mp3 原样还原，不做转码。"""
    from sunoauxtool.download.ncm import unpack_file

    if out.exists() and not out.is_dir():
        typer.echo(f"错误: 输出路径不是目录: {out}", err=True)
        raise typer.Exit(code=24)
    try:
        paths = unpack_file(file, out, stem=stem, write_cover=cover, overwrite=overwrite)
    except FileExistsError as exc:
        typer.echo(f"错误: {exc}（加 --overwrite 覆盖）", err=True)
        raise typer.Exit(code=23) from None
    except SunoError as exc:
        typer.echo(f"错误[{exc.code}]: {exc.message}", err=True)
        raise typer.Exit(code=exc.code or 1) from None
    except OSError as exc:
        typer.echo(f"错误[24]: {exc}", err=True)
        raise typer.Exit(code=24) from None
    for path in paths:
        typer.echo(f"已解包: {path}")


@app.command()
def version() -> None:
    """打印版本与 ffmpeg 位置。"""
    typer.echo(f"suno-cat-catch-resolve {__version__}")
    try:
        typer.echo(f"ffmpeg: {find_ffmpeg()}")
    except SunoError:
        typer.echo("ffmpeg: 未找到")


if __name__ == "__main__":
    app()
