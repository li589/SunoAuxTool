"""酷狗 KGM/KGE/KGMA 与 VPR 解密（1.6.3 引入，1.6.4 零依赖化）。零第三方依赖。

算法（源自公开的 ghtz08/kugou-kgm-decoder 与 unlock-music 实现，MIT）：
- 头部：偏移 0x10 的 u32 头长度 + 偏移 0x1c-0x2c 的 16 字节文件私钥
  （own key，补 0 成 17 字节）；KGM/VPR 有各自魔数，KGMA 布局相同
  （无固定魔数，按扩展名识别）；
- 音频区每个字节（相对偏移 i）：
      t1 = own[i % 17] ^ enc[i]；t1 = t1 ^ ((t1 & 0x0F) << 4)
      t2 = mend[i % 272] ^ pub[i / 16]；t2 = t2 ^ ((t2 & 0x0F) << 4)
      out[i] = t1 ^ t2
- VPR 额外与 VprMaskDiff[i % 17] 异或；
- pub 流两种来源（已用 ghtz08 官方密钥逐字节验证等价）：
  1. **内嵌 maskV1 递归表**（两张 272 项小表，默认；无需任何外部文件）；
  2. 外置公钥文件（~73MB 预计算流，SUNO_KGM_KEY 提供，作为加速可选项）。
"""

from __future__ import annotations

import lzma
import os
from pathlib import Path
from typing import Optional

from sunoauxtool.download.unlock._xutil import read_u32_le, xor_repeating
from sunoauxtool.download.unlock.exceptions import UnknownFormatError

HEADER_LEN = 1024
OWN_KEY_LEN = 17
PUB_KEY_LEN = 1170494464          # 加密字节总长（文件位置上限）
PUB_KEY_MAGNIFICATION = 16        # pub[i / 16]
PUB_KEY_RAW_LEN = PUB_KEY_LEN // PUB_KEY_MAGNIFICATION  # 73,155,904

KGM_MAGIC = bytes([0x7C, 0xD5, 0x32, 0xEB, 0x86, 0x02, 0x7F, 0x4B,
                   0xA8, 0xAF, 0xA6, 0x8E, 0x0F, 0xFF, 0x99, 0x14])
VPR_MAGIC = bytes([0x05, 0x28, 0xBC, 0x96, 0xE9, 0xE4, 0x5A, 0x43,
                   0x91, 0xAA, 0xBD, 0xD0, 0x7A, 0xF5, 0x36, 0x31])

VPR_MASK_DIFF = bytes([0x25, 0xDF, 0xE8, 0xA6, 0x75, 0x1E, 0x75, 0x0E,
                       0x2F, 0x80, 0xF3, 0x2D, 0xB8, 0xB6, 0xE3, 0x11, 0x00])

# 272 字节固定修正表（unlock-music / ghtz08 官方常量）
PUB_KEY_MEND = bytes([
    0xB8, 0xD5, 0x3D, 0xB2, 0xE9, 0xAF, 0x78, 0x8C, 0x83, 0x33, 0x71, 0x51, 0x76, 0xA0,
    0xCD, 0x37, 0x2F, 0x3E, 0x35, 0x8D, 0xA9, 0xBE, 0x98, 0xB7, 0xE7, 0x8C, 0x22, 0xCE,
    0x5A, 0x61, 0xDF, 0x68, 0x69, 0x89, 0xFE, 0xA5, 0xB6, 0xDE, 0xA9, 0x77, 0xFC, 0xC8,
    0xBD, 0xBD, 0xE5, 0x6D, 0x3E, 0x5A, 0x36, 0xEF, 0x69, 0x4E, 0xBE, 0xE1, 0xE9, 0x66,
    0x1C, 0xF3, 0xD9, 0x02, 0xB6, 0xF2, 0x12, 0x9B, 0x44, 0xD0, 0x6F, 0xB9, 0x35, 0x89,
    0xB6, 0x46, 0x6D, 0x73, 0x82, 0x06, 0x69, 0xC1, 0xED, 0xD7, 0x85, 0xC2, 0x30, 0xDF,
    0xA2, 0x62, 0xBE, 0x79, 0x2D, 0x62, 0x62, 0x3D, 0x0D, 0x7E, 0xBE, 0x48, 0x89, 0x23,
    0x02, 0xA0, 0xE4, 0xD5, 0x75, 0x51, 0x32, 0x02, 0x53, 0xFD, 0x16, 0x3A, 0x21, 0x3B,
    0x16, 0x0F, 0xC3, 0xB2, 0xBB, 0xB3, 0xE2, 0xBA, 0x3A, 0x3D, 0x13, 0xEC, 0xF6, 0x01,
    0x45, 0x84, 0xA5, 0x70, 0x0F, 0x93, 0x49, 0x0C, 0x64, 0xCD, 0x31, 0xD5, 0xCC, 0x4C,
    0x07, 0x01, 0x9E, 0x00, 0x1A, 0x23, 0x90, 0xBF, 0x88, 0x1E, 0x3B, 0xAB, 0xA6, 0x3E,
    0xC4, 0x73, 0x47, 0x10, 0x7E, 0x3B, 0x5E, 0xBC, 0xE3, 0x00, 0x84, 0xFF, 0x09, 0xD4,
    0xE0, 0x89, 0x0F, 0x5B, 0x58, 0x70, 0x4F, 0xFB, 0x65, 0xD8, 0x5C, 0x53, 0x1B, 0xD3,
    0xC8, 0xC6, 0xBF, 0xEF, 0x98, 0xB0, 0x50, 0x4F, 0x0F, 0xEA, 0xE5, 0x83, 0x58, 0x8C,
    0x28, 0x2C, 0x84, 0x67, 0xCD, 0xD0, 0x9E, 0x47, 0xDB, 0x27, 0x50, 0xCA, 0xF4, 0x63,
    0x63, 0xE8, 0x97, 0x7F, 0x1B, 0x4B, 0x0C, 0xC2, 0xC1, 0x21, 0x4C, 0xCC, 0x58, 0xF5,
    0x94, 0x52, 0xA3, 0xF3, 0xD3, 0xE0, 0x68, 0xF4, 0x00, 0x23, 0xF3, 0x5E, 0x0A, 0x7B,
    0x93, 0xDD, 0xAB, 0x12, 0xB2, 0x13, 0xE8, 0x84, 0xD7, 0xA7, 0x9F, 0x0F, 0x32, 0x4C,
    0x55, 0x1D, 0x04, 0x36, 0x52, 0xDC, 0x03, 0xF3, 0xF9, 0x4E, 0x42, 0xE9, 0x3D, 0x61,
    0xEF, 0x7C, 0xB6, 0xB3, 0x93, 0x50,
])

# maskV1 递归掩码的两张 272 项表（unlock-music 社区常量；与 73MB 公钥等价）
_MASK_TABLE1 = [
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 33, 1, 97, 1, 33, 1, 225, 1,
    33, 1, 97, 1, 33, 1, 210, 35, 2, 2, 66, 66, 2, 2, 194, 194, 2, 2, 66, 66, 2, 2, 211,
    211, 2, 3, 99, 67, 99, 3, 227, 195, 227, 3, 99, 67, 99, 3, 148, 180, 148, 101, 4, 4,
    4, 4, 132, 132, 132, 132, 4, 4, 4, 4, 149, 149, 149, 149, 4, 5, 37, 5, 229, 133,
    165, 133, 229, 5, 37, 5, 214, 182, 150, 182, 214, 39, 6, 6, 198, 198, 134, 134, 198,
    198, 6, 6, 215, 215, 151, 151, 215, 215, 6, 7, 231, 199, 231, 135, 231, 199, 231, 7,
    24, 56, 24, 120, 24, 56, 24, 233, 8, 8, 8, 8, 8, 8, 8, 8, 25, 25, 25, 25, 25, 25,
    25, 25, 8, 9, 41, 9, 105, 9, 41, 9, 218, 58, 26, 58, 90, 58, 26, 58, 218, 43, 10,
    10, 74, 74, 10, 10, 219, 219, 27, 27, 91, 91, 27, 27, 219, 219, 10, 11, 107, 75,
    107, 11, 156, 188, 156, 124, 28, 60, 28, 124, 156, 188, 156, 109, 12, 12, 12, 12,
    157, 157, 157, 157, 29, 29, 29, 29, 157, 157, 157, 157, 12, 13, 45, 13, 222, 190,
    158, 190, 222, 62, 30, 62, 222, 190, 158, 190, 222, 47, 14, 14, 223, 223, 159, 159,
    223, 223, 31, 31, 223, 223, 159, 159, 223, 223, 14, 15, 0, 32, 0, 96, 0, 32, 0, 224,
    0, 32, 0, 96, 0, 32, 0, 241,
]
_MASK_TABLE2 = [
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 35, 1, 103, 1, 35, 1, 239, 1,
    35, 1, 103, 1, 35, 1, 223, 33, 2, 2, 70, 70, 2, 2, 206, 206, 2, 2, 70, 70, 2, 2,
    222, 222, 2, 3, 101, 71, 101, 3, 237, 207, 237, 3, 101, 71, 101, 3, 157, 191, 157,
    99, 4, 4, 4, 4, 140, 140, 140, 140, 4, 4, 4, 4, 156, 156, 156, 156, 4, 5, 39, 5,
    235, 141, 175, 141, 235, 5, 39, 5, 219, 189, 159, 189, 219, 37, 6, 6, 202, 202, 142,
    142, 202, 202, 6, 6, 218, 218, 158, 158, 218, 218, 6, 7, 233, 203, 233, 143, 233,
    203, 233, 7, 25, 59, 25, 127, 25, 59, 25, 231, 8, 8, 8, 8, 8, 8, 8, 8, 24, 24, 24,
    24, 24, 24, 24, 24, 8, 9, 43, 9, 111, 9, 43, 9, 215, 57, 27, 57, 95, 57, 27, 57,
    215, 41, 10, 10, 78, 78, 10, 10, 214, 214, 26, 26, 94, 94, 26, 26, 214, 214, 10, 11,
    109, 79, 109, 11, 149, 183, 149, 123, 29, 63, 29, 123, 149, 183, 149, 107, 12, 12,
    12, 12, 148, 148, 148, 148, 28, 28, 28, 28, 148, 148, 148, 148, 12, 13, 47, 13, 211,
    181, 151, 181, 211, 61, 31, 61, 211, 181, 151, 181, 211, 45, 14, 14, 210, 210, 150,
    150, 210, 210, 30, 30, 210, 210, 150, 150, 210, 210, 14, 15, 0, 34, 0, 102, 0, 34,
    0, 238, 0, 34, 0, 102, 0, 34, 0, 254,
]

# 折叠变换 v ^ ((v & 0x0F) << 4) 的 256 项查找表（自逆）
_FOLD_TABLE = bytes(v ^ ((v & 0x0F) << 4) for v in range(256))

DEFAULT_KEY_DIRNAMES = ("sunoauxtool", "smartnotegen")


def is_kgm(data: bytes) -> bool:
    return len(data) >= HEADER_LEN and data[:16] == KGM_MAGIC


def is_vpr(data: bytes) -> bool:
    return len(data) >= HEADER_LEN and data[:16] == VPR_MAGIC


def locate_pub_key() -> Optional[Path]:
    """按 环境变量 SUNO_KGM_KEY -> 默认缓存路径 的顺序查找公钥文件。

    公钥是可选加速项：没有它也能解密（用内嵌 maskV1 表现算 pub 流）。
    """
    env = os.environ.get("SUNO_KGM_KEY", "").strip()
    if env:
        p = Path(env)
        if p.is_file():
            return p
    home = Path.home()
    for base in (home / ".cache", home / ".config"):
        for dirname in DEFAULT_KEY_DIRNAMES:
            for name in ("kugou_key.xz", "kugou_key.bin"):
                p = base / dirname / name
                if p.is_file():
                    return p
    return None


def load_pub_key(path: Optional[Path] = None) -> Optional[bytes]:
    """加载并解压公钥（.xz 用标准库 lzma；.bin 直接读）。

    找不到或长度不符时返回 None（decrypt 会回退到内嵌 maskV1 表现算）。
    """
    if path is None:
        path = locate_pub_key()
    if path is None:
        return None
    raw = path.read_bytes()
    if path.suffix.lower() == ".xz" or raw[:6] == b"\xfd7zXZ\x00":
        raw = lzma.decompress(raw)
    if len(raw) != PUB_KEY_RAW_LEN:
        return None
    return raw


def _audio_start(data: bytes) -> int:
    """音频数据起点：偏移 0x10 的 u32 头长度（TS 实现语义），非法时回退 1024。"""
    if len(data) < 0x14:
        return HEADER_LEN
    header_len = read_u32_le(data, 0x10)
    if 0x40 <= header_len <= len(data):
        return header_len
    return HEADER_LEN


def mask_v1(offset: int) -> int:
    """maskV1 递归掩码（offset 为 16 字节块索引）。

    pub_key[k] == mask_v1(k)（已用 ghtz08 官方 73MB 密钥逐字节抽样验证）。
    """
    value = 0
    o = offset
    while o >= 0x11:
        value ^= _MASK_TABLE1[o % 272]
        o >>= 4
        value ^= _MASK_TABLE2[o % 272]
        o >>= 4
    return value


def _mask_v1_stream(n_blocks: int) -> bytes:
    """前 n_blocks 个块索引的 maskV1 流（等价于公钥前 n_blocks 字节）。"""
    return bytes(mask_v1(k) for k in range(n_blocks))


def plausible_header(data: bytes) -> bool:
    """KGMA 宽松校验（无固定魔数）：头部长度字段可解析且音频区偏移合理。"""
    if len(data) < 0x2C:
        return False
    header_len = read_u32_le(data, 0x10)
    return 0x40 <= header_len <= len(data)


def decrypt(data: bytes, pub_key: Optional[bytes] = None, kgma: bool = False) -> bytes:
    """解密 KGM/KGE/KGMA/VPR 字节流。

    pub_key 为 73,155,904 字节原始公钥（可选加速项）；为 None 时先按
    locate_pub_key 自动查找，找不到则用内嵌 maskV1 表现算 pub 流。
    kgma=True 时跳过 KGM/VPR 魔数校验（KGMA 无固定魔数，由上层按扩展名
    与 plausible_header 识别）。
    """
    vpr = is_vpr(data)
    if not kgma and not vpr and not is_kgm(data):
        raise UnknownFormatError("不是 KGM/KGE/KGMA/VPR 文件（魔数不符或文件过短）")
    if len(data) < 0x2C:
        raise UnknownFormatError("文件过短，缺少 KGM 头部")

    if pub_key is None:
        pub_key = load_pub_key()

    audio = bytearray(data[_audio_start(data):])
    own_key = bytes(data[0x1C:0x2C]) + b"\x00"  # 17 字节（末位恒 0）

    n = len(audio)
    # t1 = own[i % 17] ^ enc[i] -> 折叠
    t1 = xor_repeating(bytes(audio), own_key).translate(_FOLD_TABLE)

    # t2 = mend[i % 272] ^ pub[i / 16] -> 折叠
    # 272 = 17*16，mend 的 16 字节块与 i/16 的块边界天然对齐
    n16 = (n + PUB_KEY_MAGNIFICATION - 1) // PUB_KEY_MAGNIFICATION
    if pub_key is not None and len(pub_key) >= n16:
        pub_slice = pub_key[:n16]
    else:
        pub_slice = _mask_v1_stream(n16)
    mend_stream = (PUB_KEY_MEND * (n // len(PUB_KEY_MEND) + 1))[:n]
    pub_bcast = bytearray(n)
    for k in range(n16):
        lo = k * PUB_KEY_MAGNIFICATION
        hi = min(lo + PUB_KEY_MAGNIFICATION, n)
        if lo >= n:
            break
        pub_bcast[lo:hi] = bytes([pub_slice[k]]) * (hi - lo)
    t2 = xor_repeating(mend_stream, bytes(pub_bcast)).translate(_FOLD_TABLE)

    out = bytearray(
        (int.from_bytes(t1, "little") ^ int.from_bytes(t2, "little")).to_bytes(n, "little")
    )
    if vpr:
        out = bytearray(xor_repeating(bytes(out), VPR_MASK_DIFF))
    return bytes(out)
