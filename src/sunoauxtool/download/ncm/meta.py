"""NCM 元数据嵌入：把解包出的标题/艺术家/专辑/封面写回音频文件。

用 ffmpeg 的 -metadata + attached_pic 路线（mp3 → ID3v2 + APIC，
flac → Vorbis 注释 + METADATA_BLOCK_PICTURE），不自写 ID3 二进制。
ffmpeg 不可用时调用方应优雅跳过（嵌入是可选增值，不是解包前提）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional, Union

from sunoauxtool.download.exceptions import TranscodeError
from sunoauxtool.download.ncm.unpack import NcmContent
from sunoauxtool.download.transcoder import find_ffmpeg

PathLike = Union[str, Path]


def _metadata_args(content: NcmContent) -> list[str]:
    args: list[str] = []
    if content.music_name:
        args += ["-metadata", f"title={content.music_name}"]
    if content.artists:
        args += ["-metadata", f"artist={'; '.join(content.artists)}"]
    if content.album:
        args += ["-metadata", f"album={content.album}"]
    return args


def embed_metadata(
    audio: PathLike,
    content: NcmContent,
    ffmpeg: Optional[PathLike] = None,
    overwrite: bool = True,
) -> Path:
    """把元数据与封面嵌入音频文件（原地覆盖，返回音频路径）。

    Raises:
        TranscodeError: ffmpeg 失败（码 23）。
        FFmpegNotFoundError: ffmpeg 不可用（码 20）。
    """
    src = Path(audio)
    if not src.is_file():
        raise TranscodeError(f"音频文件不存在: {src}")

    ff = Path(find_ffmpeg(ffmpeg))
    # 输出临时文件必须保留原扩展名——ffmpeg 按扩展名推断封装器，
    # ".tagged" 这类无名后缀会报 "Error initializing the muxer"。
    tmp = src.with_suffix(".tagged" + src.suffix)
    args = ["-i", str(src)]
    if content.image:
        args += ["-i", "-"]  # 封面从 stdin 喂给 ffmpeg
    args += ["-map", "0:a"]
    if content.image:
        args += ["-map", "1:v", "-disposition:v", "attached_pic"]
    args += _metadata_args(content)
    args += ["-c:a", "copy", "-c:v", "copy", str(tmp)]

    cmd = [str(ff), "-hide_banner", "-y", *args]
    try:
        proc = subprocess.run(
            cmd,
            input=content.image if content.image else None,
            capture_output=True,
            timeout=300,
        )
    except OSError as exc:
        raise TranscodeError(f"无法启动 ffmpeg: {exc}") from exc
    if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        tail = (proc.stderr or b"")[-2000:].decode("utf-8", errors="replace")
        tmp.unlink(missing_ok=True)
        raise TranscodeError(f"元数据嵌入失败: {tail}")

    if overwrite:
        src.unlink()
    tmp.rename(src if overwrite else src.with_suffix(src.suffix + ".tagged"))
    return src
