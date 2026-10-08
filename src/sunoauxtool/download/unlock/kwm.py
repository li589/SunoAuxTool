"""酷我 KWM 解密（1.6.3）。零第三方依赖，纯离线。

算法（逆向自 unlock-music web 官方实现，常量经本地 bundle 校验）：
- 文件头 1024 字节：前 16 字节为魔数 ``yeelion-kuwo\0\0\0\0`` 或
  ``yeelion-kuwo-tme``；偏移 24-32 为 8 字节小端 u64 密钥种子；
- 密钥流构造：种子的十进制字符串补齐/截断到 32 字符（JS ``padEnd(32, s)``
  语义：用自身循环填充），与固定字符串逐字符异或得到 32 字节循环密钥；
- 音频数据自偏移 1024 起，与密钥流按 ``i % 32`` 循环异或（自逆运算）。
"""

from __future__ import annotations

from sunoauxtool.download.unlock._xutil import xor_repeating
from sunoauxtool.download.unlock.exceptions import UnknownFormatError

KWM_MAGIC_1 = b"yeelion-kuwo\x00\x00\x00\x00"
KWM_MAGIC_2 = b"yeelion-kuwo-tme"
KWM_MAGICS = (KWM_MAGIC_1, KWM_MAGIC_2)

_HEADER_LEN = 1024
_KEY_SEED_OFFSET = 24
_KEY_SEED_LEN = 8
# 固定异或字符串（unlock-music 官方常量）
_KEY_SEED_STRING = "MoOtOiTvINGwd2E6n0E1i7L5t2IoOoNk"


def is_kwm(data: bytes) -> bool:
    return len(data) >= _HEADER_LEN and data[:16] in KWM_MAGICS


def build_key_stream(seed_u64: int) -> bytes:
    """由 u64 密钥种子构造 32 字节循环密钥（对齐官方 JS 算法语义）。"""
    text = str(seed_u64)
    if len(text) > 32:
        text = text[:32]
    elif len(text) < 32:
        text = (text * (32 // len(text) + 1))[:32]
    return bytes(
        ord(a) ^ ord(b) for a, b in zip(_KEY_SEED_STRING, text)
    )


def decrypt(data: bytes) -> bytes:
    """解密 KWM 字节流，返回音频数据（密钥流异或为自逆运算）。"""
    if not is_kwm(data):
        raise UnknownFormatError("不是 KWM 文件（魔数不符或文件过短）")
    seed = int.from_bytes(
        data[_KEY_SEED_OFFSET : _KEY_SEED_OFFSET + _KEY_SEED_LEN], "little"
    )
    key = build_key_stream(seed)
    return xor_repeating(data[_HEADER_LEN:], key)
