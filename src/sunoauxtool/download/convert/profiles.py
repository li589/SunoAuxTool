"""输出预设（profiles）：把常见目标场景收敛为单一开关。

- suno     : WAV / 44.1kHz / 16bit —— Suno 上传合规（恒禁混响与此无关，仅格式）
- lossless : FLAC —— 归档无损
- web      : MP3 / 192k / 44.1kHz —— 分享兼容
显式给出的 --fmt/--bitrate/--sample-rate/--bit-depth 优先级高于预设。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class OutputProfile:
    """一个目标场景的默认编码参数。"""

    name: str
    fmt: str
    bitrate: Optional[str] = None        # 仅有损格式使用（如 '192k'）
    sample_rate: Optional[int] = None    # None = 保持源采样率
    bit_depth: Optional[int] = None      # 仅 wav 使用（16/24）


PROFILES: dict[str, OutputProfile] = {
    "suno": OutputProfile("suno", fmt="wav", sample_rate=44100, bit_depth=16),
    "lossless": OutputProfile("lossless", fmt="flac"),
    "web": OutputProfile("web", fmt="mp3", bitrate="192k", sample_rate=44100),
}


def resolve_profile(
    profile: Optional[str],
    fmt: Optional[str],
    bitrate: Optional[str],
    sample_rate: Optional[int],
    bit_depth: Optional[int],
) -> tuple[str, str, Optional[int], int]:
    """合并预设与显式参数，返回 (fmt, bitrate, sample_rate, bit_depth)。

    显式参数 > 预设 > 引擎默认（mp3 192k / 44100 / 16）。
    """
    p = PROFILES.get(profile) if profile else None
    if profile and p is None:
        raise KeyError(profile)
    resolved_fmt = fmt or (p.fmt if p else "mp3")
    resolved_bitrate = bitrate or (p.bitrate if p else "192k")
    resolved_rate = sample_rate if sample_rate is not None else (p.sample_rate if p else None)
    resolved_depth = bit_depth if bit_depth is not None else (p.bit_depth if p else 16)
    return resolved_fmt, resolved_bitrate, resolved_rate, resolved_depth
