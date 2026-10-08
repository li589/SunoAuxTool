"""unlock 内部工具：循环密钥异或 + 音频格式嗅探。"""

from __future__ import annotations

import struct

# 魔数 -> 扩展名（覆盖主流无损/有损容器）
_MAGIC_EXTS: tuple[tuple[bytes, str], ...] = (
    (b"fLaC", "flac"),
    (b"OggS", "ogg"),
    (b"RIFF", "wav"),
    (b"MAC ", "ape"),
    (b"\x30\x26\xB2\x75", "wma"),
    (b"FRM8", "dff"),
    (b"\x1A\x45\xDF\xA3", "mka"),
)

_MP3_SYNC = (b"\xff\xfb", b"\xff\xfa", b"\xff\xf3", b"\xff\xf2")


def xor_repeating(data: bytes, key: bytes, key_offset: int = 0) -> bytes:
    """data 与 key 循环异或（key_offset 为 data[0] 对应的 key 相位）。

    用大整数异或实现批量加速；key_offset 只影响 key 的循环相位。
    """
    if not key:
        return bytes(data)
    n = len(key)
    shift = key_offset % n
    aligned = key[shift:] + key[:shift]
    reps = (len(data) + n - 1) // n
    stream = (aligned * reps)[: len(data)]
    a = int.from_bytes(bytes(data), "little")
    b = int.from_bytes(stream, "little")
    return (a ^ b).to_bytes(len(data), "little")


def _syncsafe_int(b4: bytes) -> int:
    if any(c & 0x80 for c in b4):
        return 0
    return (b4[0] << 21) | (b4[1] << 14) | (b4[2] << 7) | b4[3]


def sniff_audio_ext(data: bytes) -> str:
    """嗅探解密结果的音频格式；无法识别返回 'bin'。

    跳过 ID3v2 头后再次探测（QMC/KGM 解密结果常带原始 ID3 标签）。
    """
    if len(data) < 8:
        return "bin"
    for magic, ext in _MAGIC_EXTS:
        if data.startswith(magic):
            return ext
    if data[:2] in _MP3_SYNC:
        return "mp3"
    if data.startswith(b"ID3") and len(data) >= 10:
        size = _syncsafe_int(data[6:10])
        start = 10 + size
        if 0 < start < len(data):
            tail = data[start:]
            for magic, ext in _MAGIC_EXTS:
                if tail.startswith(magic):
                    return ext
            if tail[:2] in _MP3_SYNC:
                return "mp3"
        return "mp3"  # ID3 之后无可见魔数也按 mp3 处理
    return "bin"


def read_u32_le(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]
