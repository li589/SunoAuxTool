"""音频格式互转：mp3 / wav / m4a / flac。

编码器映射（全部为 ffmpeg 内建/标配）：
    mp3 -> libmp3lame (-b:a)      wav  -> pcm_s16le / pcm_s24le
    m4a -> aac (-b:a)             flac -> flac（无损，bit_depth 无意义）
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from sunoauxtool.download.convert.engine import ensure_output_dir, run_ffmpeg
from sunoauxtool.download.convert.profiles import resolve_profile
from sunoauxtool.download.exceptions import TranscodeError
from sunoauxtool.download.transcoder import find_ffmpeg

PathLike = Union[str, Path]

SUPPORTED_AUDIO_FMTS = ("mp3", "wav", "m4a", "flac")

# 各目标格式的 ffmpeg 编码参数组装
_AUDIO_CODEC_ARGS = {
    "mp3": lambda bitrate, _depth: ["-c:a", "libmp3lame", "-b:a", bitrate],
    "m4a": lambda bitrate, _depth: ["-c:a", "aac", "-b:a", bitrate],
    "flac": lambda _bitrate, _depth: ["-c:a", "flac"],
    "wav": lambda _bitrate, depth: [
        "-c:a", "pcm_s24le" if depth >= 24 else "pcm_s16le"
    ],
}


def _codec_args(fmt: str, bitrate: str, bit_depth: int) -> list[str]:
    builder = _AUDIO_CODEC_ARGS.get(fmt)
    if builder is None:
        raise TranscodeError(
            f"不支持的音频格式: {fmt!r}（支持: {', '.join(SUPPORTED_AUDIO_FMTS)}）", None
        )
    return builder(bitrate, bit_depth)


def convert_audio(
    src: PathLike,
    out_dir: PathLike = ".",
    fmt: Optional[str] = None,
    bitrate: Optional[str] = None,
    sample_rate: Optional[int] = None,
    bit_depth: Optional[int] = None,
    profile: Optional[str] = None,
    stem: Optional[str] = None,
    overwrite: bool = False,
    ffmpeg: Optional[PathLike] = None,
) -> Path:
    """把音频文件转为目标格式。

    Args:
        src: 输入音频（扩展名须为 mp3/wav/m4a/flac 之一，大小写不敏感）。
        out_dir: 输出目录（默认当前目录）。
        fmt: 目标格式；None 时按 profile 或默认 mp3。
        bitrate: 有损码率（mp3/m4a），如 '192k'。
        sample_rate: 目标采样率；None 保持源。
        bit_depth: wav 位深 16/24（其他格式忽略）。
        profile: 预设名（suno/lossless/web）；显式参数优先。
        stem: 输出文件名主干；默认取输入文件名。
        overwrite: 目标已存在时是否覆盖（False 则报 TranscodeError）。
        ffmpeg: ffmpeg 绝对路径（默认自动定位）。

    Returns:
        生成的音频文件路径。

    Raises:
        TranscodeError: 输入不存在/格式不支持/目标已存在/ffmpeg 失败（码 23）。
        OutputWriteError: 输出目录不可写（码 24）。
    """
    source = Path(src)
    if not source.is_file():
        raise TranscodeError(f"输入文件不存在: {source}")
    source_fmt = source.suffix.lower().lstrip(".")
    if source_fmt not in SUPPORTED_AUDIO_FMTS:
        raise TranscodeError(
            f"不支持的输入格式: .{source_fmt}（支持: {', '.join(SUPPORTED_AUDIO_FMTS)}）"
        )

    try:
        resolved_fmt, resolved_bitrate, resolved_rate, resolved_depth = resolve_profile(
            profile, fmt, bitrate, sample_rate, bit_depth
        )
    except KeyError as exc:
        raise TranscodeError(
            f"未知预设: {profile!r}（可用: suno, lossless, web）"
        ) from exc
    if resolved_fmt not in SUPPORTED_AUDIO_FMTS:
        raise TranscodeError(
            f"不支持的音频格式: {resolved_fmt!r}（支持: {', '.join(SUPPORTED_AUDIO_FMTS)}）"
        )

    out = ensure_output_dir(out_dir) / f"{stem or source.stem}.{resolved_fmt}"
    if out.exists() and not overwrite:
        raise TranscodeError(f"目标已存在（加 --overwrite 覆盖）: {out}")

    args = ["-i", str(source), *(_codec_args(resolved_fmt, resolved_bitrate, resolved_depth))]
    if resolved_rate is not None:
        args += ["-ar", str(resolved_rate)]
    args.append(str(out))

    find_ffmpeg(ffmpeg)  # 提前给出友好的码 20，而不是让 run_ffmpeg 在组装后报
    run_ffmpeg(args, ffmpeg)
    if not out.is_file() or out.stat().st_size == 0:
        raise TranscodeError(f"转码后输出缺失或为空: {out}")
    return out
