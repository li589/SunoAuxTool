"""视频分离音频：mp4 / mov / flv / webm / mkv / avi / m4v → 音频。

策略（--copy/--no-copy，默认 copy 优先）：
    1. ffprobe 读首个音频流 codec；
    2. 若 codec 可无损放入目标容器（mp3→mp3、aac/alac→m4a、flac→flac、
       pcm_*→wav），直接 `-vn -c:a copy` 零损耗直通；
    3. 否则回退重编码（参数与 convert_audio 一致，外加 -vn 丢弃视频）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from sunoauxtool.download.convert.audio import SUPPORTED_AUDIO_FMTS, _codec_args
from sunoauxtool.download.convert.engine import (
    ensure_output_dir,
    probe_audio_stream,
    run_ffmpeg,
)
from sunoauxtool.download.convert.profiles import resolve_profile
from sunoauxtool.download.exceptions import TranscodeError
from sunoauxtool.download.transcoder import find_ffmpeg

PathLike = Union[str, Path]

SUPPORTED_VIDEO_EXTS = (".mp4", ".mov", ".flv", ".webm", ".mkv", ".avi", ".m4v")

# 音频 codec -> 其可无损直通的目标容器
_COPY_CONTAINER = {
    "mp3": "mp3",
    "aac": "m4a",
    "alac": "m4a",
    "flac": "flac",
    "opus": "opus",
}


def _copy_target(codec: Optional[str], requested_fmt: str) -> Optional[str]:
    """返回可直通的实际容器；不可直通返回 None。"""
    if codec is None:
        return None
    natural = _COPY_CONTAINER.get(codec)
    if natural is None:
        return None
    if natural == requested_fmt:
        return natural
    # aac 请求 wav 之类：容器不兼容，必须重编码
    return None


def extract_audio(
    src: PathLike,
    out_dir: PathLike = ".",
    fmt: Optional[str] = None,
    bitrate: Optional[str] = None,
    sample_rate: Optional[int] = None,
    bit_depth: Optional[int] = None,
    profile: Optional[str] = None,
    stem: Optional[str] = None,
    overwrite: bool = False,
    copy_first: bool = True,
    ffmpeg: Optional[PathLike] = None,
) -> Path:
    """从视频文件提取音轨为音频文件。

    Args:
        src: 输入视频（扩展名须在 SUPPORTED_VIDEO_EXTS 内）。
        out_dir: 输出目录。
        fmt / bitrate / sample_rate / bit_depth / profile: 同 convert_audio。
        stem: 输出文件名主干；默认取输入文件名。
        overwrite: 目标已存在时是否覆盖。
        copy_first: True 时若源音轨可无损放入目标容器则直通（不重编码）。
        ffmpeg: ffmpeg 绝对路径。

    Returns:
        生成的音频文件路径。

    Raises:
        TranscodeError: 输入不存在/不是视频扩展名/无音轨/失败（码 23）。
        OutputWriteError: 输出目录不可写（码 24）。
    """
    source = Path(src)
    if not source.is_file():
        raise TranscodeError(f"输入文件不存在: {source}")
    if source.suffix.lower() not in SUPPORTED_VIDEO_EXTS:
        raise TranscodeError(
            f"不支持的视频格式: {source.suffix or '(无扩展名)'}"
            f"（支持: {', '.join(SUPPORTED_VIDEO_EXTS)}）"
        )

    try:
        resolved_fmt, resolved_bitrate, resolved_rate, resolved_depth = resolve_profile(
            profile, fmt, bitrate, sample_rate, bit_depth
        )
    except KeyError as exc:
        raise TranscodeError(
            f"未知预设: {profile!r}（可用: suno, lossless, web）"
        ) from exc
    if resolved_fmt not in SUPPORTED_AUDIO_FMTS and resolved_fmt != "opus":
        raise TranscodeError(
            f"不支持的音频格式: {resolved_fmt!r}（支持: {', '.join(SUPPORTED_AUDIO_FMTS)}）"
        )

    stream = probe_audio_stream(source, ffmpeg)
    codec = stream.get("codec_name")

    copy_target = _copy_target(codec, resolved_fmt) if copy_first else None
    if copy_target is not None:
        out_ext = copy_target
    else:
        out_ext = resolved_fmt

    out = ensure_output_dir(out_dir) / f"{stem or source.stem}.{out_ext}"
    if out.exists() and not overwrite:
        raise TranscodeError(f"目标已存在（加 --overwrite 覆盖）: {out}")

    if copy_target is not None:
        args = ["-i", str(source), "-vn", "-c:a", "copy"]
    else:
        if codec is None:
            raise TranscodeError(f"视频文件中没有音频流: {source}")
        args = [
            "-i", str(source), "-vn",
            *(_codec_args(resolved_fmt, resolved_bitrate, resolved_depth)),
        ]
        if resolved_rate is not None:
            args += ["-ar", str(resolved_rate)]
    args.append(str(out))

    find_ffmpeg(ffmpeg)
    run_ffmpeg(args, ffmpeg)
    if not out.is_file() or out.stat().st_size == 0:
        raise TranscodeError(f"音频提取后输出缺失或为空: {out}")
    return out
