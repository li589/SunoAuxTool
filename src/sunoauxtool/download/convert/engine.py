"""通用转码引擎：ffmpeg 统一封装 + ffprobe 流探测。

复用 transcoder.find_ffmpeg 的两层环境变量定位
（SUNO_FFMPEG / SMARTNOTEGEN_FFMPEG + SUNO_FFMPEG_DIRS + 本机兜底），
本模块只负责：命令组装、stderr 捕获、错误映射（TranscodeError, 码 23）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from sunoauxtool.download.exceptions import (
    FFmpegNotFoundError,
    OutputWriteError,
    TranscodeError,
)
from sunoauxtool.download.transcoder import find_ffmpeg

PathLike = Union[str, Path]

# 转码命令普遍比重封装慢（尤其重编码整首歌），放宽到 10 分钟。
_FFMPEG_TIMEOUT_S = 600


def run_ffmpeg(args: List[str], ffmpeg: Optional[PathLike] = None) -> str:
    """执行 ffmpeg 命令，返回 stderr 尾部（用于错误信息）。

    Raises:
        FFmpegNotFoundError: ffmpeg 不可用（码 20）。
        TranscodeError: 退出码非 0（码 23）。
    """
    ff = Path(find_ffmpeg(ffmpeg))
    cmd = [str(ff), "-hide_banner", "-y", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT_S)
    except OSError as exc:
        raise FFmpegNotFoundError(f"无法启动 ffmpeg: {exc}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise TranscodeError(f"ffmpeg 转码失败（退出码 {proc.returncode}）: {tail}")
    return (proc.stderr or "").strip()


def probe_audio_stream(path: PathLike, ffmpeg: Optional[PathLike] = None) -> Dict[str, Any]:
    """用 ffprobe 返回首个音频流的属性（codec_name/sample_rate/channels）。

    无音频流时返回空字典（视频 extract-audio 用它判错）。
    """
    ff = Path(find_ffmpeg(ffmpeg))
    ffprobe = ff.with_name(ff.name.replace("ffmpeg", "ffprobe"))
    if not ffprobe.is_file():
        # ffprobe 与 ffmpeg 必须同目录发行版；找不到时退化用 ffmpeg -i 探测
        return _probe_via_ffmpeg(path, ff)
    cmd = [
        str(ffprobe), "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=codec_name,sample_rate,channels",
        "-of", "json", str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except OSError as exc:
        raise FFmpegNotFoundError(f"无法启动 ffprobe: {exc}") from exc
    if proc.returncode != 0:
        raise TranscodeError(f"ffprobe 探测失败: {(proc.stderr or '').strip()[-300:]}")
    streams = json.loads(proc.stdout or "{}").get("streams", [])
    return streams[0] if streams else {}


def _probe_via_ffmpeg(path: PathLike, ff: Path) -> Dict[str, Any]:
    """无 ffprobe 时的退化探测：解析 `ffmpeg -i` 的 stderr。"""
    cmd = [str(ff), "-hide_banner", "-i", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except OSError as exc:
        raise FFmpegNotFoundError(f"无法启动 ffmpeg: {exc}") from exc
    # ffmpeg -i 无输出文件时退出码非 0 属正常；从 stderr 抓 Audio: 行
    for line in (proc.stderr or "").splitlines():
        if "Audio:" in line:
            seg = line.split("Audio:", 1)[1]
            codec = seg.strip().split(",")[0].strip()
            return {"codec_name": codec}
    return {}


def ensure_output_dir(out_dir: PathLike) -> Path:
    """确保输出目录存在，返回 Path（不可写时码 24）。"""
    out = Path(out_dir)
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputWriteError(str(out)) from exc
    return out
