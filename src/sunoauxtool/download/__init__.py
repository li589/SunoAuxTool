"""Suno-Cat-Catch-Resolve — Suno 猫抓产物逆向取证与转码工具包。

命名说明（连字符不是合法 Python 标识符，故发行名与包名分离）：
    目录 / 发行名 : Suno-Cat-Catch-Resolve
    可导入包名     : sunoauxtool.download
`import` 与 `python -m` 一律用后者。

背景（2026-09-20 实测取证）：
    Suno 页面用「猫抓」(CatCatcher) 抓取会得到两类文件，二者是同一首曲子：

    1. 缓存捕获（常被命名成 *.mp3）
       实为 **fragmented MP4**：ftyp + moov + N×(moof/mdat)，音频编码多为
       **Opus 48kHz 立体声**。这是浏览器解密后的**明文**，可直接解码。
       扩展名被错标为 .mp3，播放器按 MP3 解析才打不开。

    2. 直接下载页面媒体（*.m4a，UUID 命名）
       是服务端下发的**加密密文**：无任何容器头、熵≈8.0000、卡方 χ²≈259
       （df=255，均匀随机临界≈310，即字节分布完美均匀）、周期扫描无异于
       随机基线（排除重复密钥 XOR）。判定为 AES / ChaCha20 级密码学强加密，
       **无密钥不可破**（这是数学结论，不是工具能力问题）。

工程结论：
    不要试图破解第 2 类文件——第 1 类 fMP4 已经是同一首曲子的完整明文副本，
    直接从它解码即可。本包的核心价值是**自动识别**这两类文件并把第 1 类转码。

分层（核心层零第三方依赖）：
    fmp4.py       MP4/fMP4 原子解析、mdat 分片提取
    forensics.py  熵 / 卡方 / 周期性检测 → 判定明文 or 密文
    transcoder.py ffmpeg 封装（Opus 重封装 / MP3 转码 / 探测）
    cli.py        Typer CLI（唯一依赖 typer 的模块）

用法：
    from sunoauxtool.download import identify, decode_fmp4
    verdict = identify("Suno _ AI Music.mp3")
    if verdict.kind == "fmp4":
        decode_fmp4("Suno _ AI Music.mp3", out_dir="out", fmt="both")
"""

from __future__ import annotations

from sunoauxtool.download.exceptions import (
    EncryptedBlobError,
    FFmpegNotFoundError,
    NotFragmentedMP4Error,
    OutputWriteError,
    SunoError,
    TranscodeError,
)
from sunoauxtool.download.forensics import Verdict, classify, identify
from sunoauxtool.download.fmp4 import Atom, concatenate_mdat, is_fragmented_mp4, parse_atoms
from sunoauxtool.download.transcoder import decode_fmp4, find_ffmpeg, probe, remux_opus, to_mp3

__version__ = "1.5.6"

__all__ = [
    "Atom",
    "EncryptedBlobError",
    "FFmpegNotFoundError",
    "NotFragmentedMP4Error",
    "OutputWriteError",
    "SunoError",
    "TranscodeError",
    "Verdict",
    "__version__",
    "classify",
    "concatenate_mdat",
    "decode_fmp4",
    "find_ffmpeg",
    "identify",
    "is_fragmented_mp4",
    "parse_atoms",
    "probe",
    "remux_opus",
    "to_mp3",
]
