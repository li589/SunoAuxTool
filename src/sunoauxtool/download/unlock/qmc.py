"""QQ音乐 QMC 加密格式解密（1.6.3）。零第三方依赖，纯离线。

覆盖：
- V1 静态密钥：.tkm / .bkc* / 十六进制扩展名缓存；
- V2 内嵌 EKey：.mflac / .mgg* / .mmp4 / .qmc0/2/3/4/6/8 / .qmcflac / .qmcogg
  （PcV1Legacy / QTag footer；STag / MusicEx 无内嵌密钥时报 29 提示在线获取）；
- 流密码按主密钥长度自动选择：1-300 字节 Map 密码，>300 字节 RC4 密码；
- tc_tea（腾讯定制 CBC 分组）用于 EKey 解密。

算法移植自 unlock-music 官方 Rust 实现（lib_um_crypto_rust，MIT），
测试向量与官方 Rust 单元测试一致（见 tests/test_download_unlock.py）。
"""

from __future__ import annotations

import base64
import math
import struct
from typing import Optional

from sunoauxtool.download.unlock._xutil import sniff_audio_ext
from sunoauxtool.download.unlock.exceptions import DecryptFailedError, KeyMissingError

# ---------------------------------------------------------------------------
# V1 静态密钥（官方 128 字节常量）
# ---------------------------------------------------------------------------

V1_KEY_SIZE = 128
V1_OFFSET_BOUNDARY = 0x7FFF

V1_STATIC_KEY = bytes([
    0xC3, 0x4A, 0xD6, 0xCA, 0x90, 0x67, 0xF7, 0x52, 0xD8, 0xA1, 0x66, 0x62, 0x9F, 0x5B, 0x09, 0x00,
    0xC3, 0x5E, 0x95, 0x23, 0x9F, 0x13, 0x11, 0x7E, 0xD8, 0x92, 0x3F, 0xBC, 0x90, 0xBB, 0x74, 0x0E,
    0xC3, 0x47, 0x74, 0x3D, 0x90, 0xAA, 0x3F, 0x51, 0xD8, 0xF4, 0x11, 0x84, 0x9F, 0xDE, 0x95, 0x1D,
    0xC3, 0xC6, 0x09, 0xD5, 0x9F, 0xFA, 0x66, 0xF9, 0xD8, 0xF0, 0xF7, 0xA0, 0x90, 0xA1, 0xD6, 0xF3,
    0xC3, 0xF3, 0xD6, 0xA1, 0x90, 0xA0, 0xF7, 0xF0, 0xD8, 0xF9, 0x66, 0xFA, 0x9F, 0xD5, 0x09, 0xC6,
    0xC3, 0x1D, 0x95, 0xDE, 0x9F, 0x84, 0x11, 0xF4, 0xD8, 0x51, 0x3F, 0xAA, 0x90, 0x3D, 0x74, 0x47,
    0xC3, 0x0E, 0x74, 0xBB, 0x90, 0xBC, 0x3F, 0x92, 0xD8, 0x7E, 0x11, 0x13, 0x9F, 0x23, 0x95, 0x5E,
    0xC3, 0x00, 0x09, 0x5B, 0x9F, 0x62, 0x66, 0xA1, 0xD8, 0x52, 0xF7, 0x67, 0x90, 0xCA, 0xD6, 0x4A,
])


def v1_transform(key: bytes, value: int, offset: int) -> int:
    """单字节 V1 变换：offset > 0x7FFF 时先对 0x7FFF 取模（官方边界语义）。"""
    if offset > V1_OFFSET_BOUNDARY:
        offset %= V1_OFFSET_BOUNDARY
    return value ^ key[offset % V1_KEY_SIZE]


def _xor_eq(a: bytes, b: bytes) -> bytes:
    """等长批量异或（大整数加速）。"""
    n = len(a)
    x = int.from_bytes(a, "little") ^ int.from_bytes(b, "little")
    return x.to_bytes(n, "little")


def _v1_transform_block(data: bytes, offset_start: int, key: bytes) -> bytes:
    """按 v1_transform 语义整段变换（offset_start 为绝对偏移）。"""
    out = bytearray(data)
    pos = offset_start
    i = 0
    total = len(out)
    while i < total:
        if pos <= V1_OFFSET_BOUNDARY:
            take = min(0x8000 - pos, total - i)
            phase = pos % V1_KEY_SIZE
        else:
            r = pos % V1_OFFSET_BOUNDARY
            take = min(V1_OFFSET_BOUNDARY - r, total - i)
            phase = r % V1_KEY_SIZE
        base = key if phase == 0 else key[phase:] + key[:phase]
        reps = (take + V1_KEY_SIZE - 1) // V1_KEY_SIZE
        stream = (base * reps)[:take]
        out[i : i + take] = _xor_eq(bytes(out[i : i + take]), stream)
        i += take
        pos += take
    return bytes(out)


def v1_decrypt(data: bytes) -> bytes:
    """整文件 V1 静态密钥解密（自逆，offset 从 0 起）。"""
    return _v1_transform_block(data, 0, V1_STATIC_KEY)


# ---------------------------------------------------------------------------
# tc_tea：腾讯定制 CBC（16 轮 TEA）
# ---------------------------------------------------------------------------

_TEA_DELTA = 0x9E3779B9
_TEA_ROUNDS = 16
_TEA_SALT_LEN = 2
_TEA_ZERO_LEN = 7


def _tea_round(value: int, s: int, k1: int, k2: int) -> int:
    left = ((value << 4) & 0xFFFFFFFF) + k1
    right = (value >> 5) + k2
    mid = (s + value) & 0xFFFFFFFF
    return (left ^ mid ^ right) & 0xFFFFFFFF


def _tea_ecb_decrypt(block: int, k: tuple) -> int:
    y = (block >> 32) & 0xFFFFFFFF
    z = block & 0xFFFFFFFF
    s = (_TEA_DELTA * _TEA_ROUNDS) & 0xFFFFFFFF
    for _ in range(_TEA_ROUNDS):
        z = (z - _tea_round(y, s, k[2], k[3])) & 0xFFFFFFFF
        y = (y - _tea_round(z, s, k[0], k[1])) & 0xFFFFFFFF
        s = (s - _TEA_DELTA) & 0xFFFFFFFF
    return (y << 32) | z


def _tea_ecb_encrypt(block: int, k: tuple) -> int:
    y = (block >> 32) & 0xFFFFFFFF
    z = block & 0xFFFFFFFF
    s = 0
    for _ in range(_TEA_ROUNDS):
        s = (s + _TEA_DELTA) & 0xFFFFFFFF
        y = (y + _tea_round(z, s, k[0], k[1])) & 0xFFFFFFFF
        z = (z + _tea_round(y, s, k[2], k[3])) & 0xFFFFFFFF
    return (y << 32) | z


def tea_cbc_decrypt(ciphertext: bytes, key16: bytes) -> bytes:
    """tc_tea tweaked-CBC 解密，返回去 padding 后的明文。"""
    k = struct.unpack(">IIII", key16)
    if len(ciphertext) % 8 != 0 or len(ciphertext) < 10:
        raise DecryptFailedError("TEA: 密文长度非法 %d" % len(ciphertext))
    iv1 = iv2 = 0
    out = bytearray()
    for i in range(0, len(ciphertext), 8):
        block = int.from_bytes(ciphertext[i : i + 8], "big")
        mixed = (block ^ iv2) & 0xFFFFFFFFFFFFFFFF
        next_iv2 = _tea_ecb_decrypt(mixed, k)
        plain = (next_iv2 ^ iv1) & 0xFFFFFFFFFFFFFFFF
        out += plain.to_bytes(8, "big")
        iv1, iv2 = block, next_iv2
    pad_size = out[0] & 0b111
    start = 1 + pad_size + _TEA_SALT_LEN
    end = len(ciphertext) - _TEA_ZERO_LEN
    if any(out[end:]):
        raise DecryptFailedError("TEA: padding 校验失败")
    return bytes(out[start:end])


def tea_cbc_encrypt(plaintext: bytes, key16: bytes, salt: bytes) -> bytes:
    """tc_tea 加密（salt 10 字节；仅测试构造 EKey 用）。"""
    k = struct.unpack(">IIII", key16)
    out_len = 10 + len(plaintext)
    pad_len = (8 - (out_len & 7)) & 7
    header_len = 1 + pad_len + _TEA_SALT_LEN
    out_len += pad_len

    header = bytearray(16)
    header[:header_len] = salt[:header_len]
    header[0] = (header[0] & ~7) | pad_len
    copy_len = min(16 - header_len, len(plaintext))
    header[header_len : header_len + copy_len] = plaintext[:copy_len]
    rest = plaintext[copy_len:]

    iv1 = iv2 = 0
    out = bytearray(out_len)

    def enc_round(block8: bytes) -> bytes:
        nonlocal iv1, iv2
        b = int.from_bytes(block8, "big")
        iv2_next = (b ^ iv1) & 0xFFFFFFFFFFFFFFFF
        c = (_tea_ecb_encrypt(iv2_next, k) ^ iv2) & 0xFFFFFFFFFFFFFFFF
        iv1, iv2 = c, iv2_next
        return c.to_bytes(8, "big")

    out[0:8] = enc_round(bytes(header[0:8]))
    out[8:16] = enc_round(bytes(header[8:16]))
    pos = 16
    while len(rest) >= 8:
        out[pos : pos + 8] = enc_round(rest[:8])
        rest = rest[8:]
        pos += 8
    if rest:
        out[pos : pos + 8] = enc_round(rest + b"\x00" * (8 - len(rest)))
    return bytes(out[:out_len])


# ---------------------------------------------------------------------------
# EKey 解密（V1 交错密钥 + V2 双层 TEA）
# ---------------------------------------------------------------------------

EKEY_V2_PREFIX = b"UVFNdXNpYyBFbmNWMixLZXk6"  # base64("QQMusic EncV2,Key:")
EKEY_V2_KEY1 = bytes([0x33, 0x38, 0x36, 0x5A, 0x4A, 0x59, 0x21, 0x40,
                      0x23, 0x2A, 0x24, 0x25, 0x5E, 0x26, 0x29, 0x28])
EKEY_V2_KEY2 = bytes([0x2A, 0x2A, 0x23, 0x21, 0x28, 0x23, 0x24, 0x25,
                      0x26, 0x5E, 0x61, 0x31, 0x63, 0x5A, 0x2C, 0x54])


def _f32(x: float) -> float:
    return struct.unpack("f", struct.pack("f", x))[0]


def make_simple_key() -> bytes:
    """官方 make_simple_key::<8>()（f32 语义严格；Rust u8 饱和转换）。"""
    f01 = _f32(0.1)
    result = bytearray()
    for i in range(8):
        v = _f32(106.0 + _f32(i * f01))
        t = abs(math.tan(v))
        v = _f32(_f32(t) * 100.0)
        result.append(max(0, min(int(v), 255)))
    return bytes(result)


_EKEY_SIMPLE_KEY = make_simple_key()


def _ekey_decrypt_v1(ekey: bytes) -> bytes:
    if len(ekey) < 12:
        raise DecryptFailedError("EKey 过短")
    decoded = base64.b64decode(ekey)
    if len(decoded) < 8:
        raise DecryptFailedError("EKey base64 解码后不足 8 字节")
    header, cipher = decoded[:8], decoded[8:]
    tea_key = bytearray()
    for sk, hk in zip(_EKEY_SIMPLE_KEY, header):
        tea_key.append(sk)
        tea_key.append(hk)
    return header + tea_cbc_decrypt(cipher, bytes(tea_key))


def _ekey_decrypt_v2(ekey: bytes) -> bytes:
    payload = tea_cbc_decrypt(base64.b64decode(ekey), EKEY_V2_KEY1)
    payload = tea_cbc_decrypt(payload, EKEY_V2_KEY2)
    zero = payload.find(b"\x00")
    if zero != -1:
        payload = payload[:zero]
    return _ekey_decrypt_v1(payload)


def ekey_decrypt(ekey: bytes) -> bytes:
    """解密 EKey 得到主密钥。V2 前缀走双层 TEA，否则走 V1。"""
    if ekey.startswith(EKEY_V2_PREFIX):
        return _ekey_decrypt_v2(ekey[len(EKEY_V2_PREFIX):])
    return _ekey_decrypt_v1(ekey)


# ---------------------------------------------------------------------------
# V2 流密码：Map（短密钥）/ RC4（长密钥）
# ---------------------------------------------------------------------------


def key_compress(long_key: bytes) -> bytes:
    """官方 key_compress：任意长度密钥压缩为 128 字节。"""
    n = len(long_key)
    if n == 0:
        raise DecryptFailedError("Map 密钥为空")
    result = bytearray()
    for i in range(V1_KEY_SIZE):
        idx = (i * i + 71214) % n
        key = long_key[idx]
        shift = (idx + 4) % 8
        result.append(((key << shift) | (key >> shift)) & 0xFF)
    return bytes(result)


class QMC2Map:
    """短密钥（1-300 字节）Map 密码：压缩后按 V1 变换异或（自逆）。"""

    def __init__(self, key: bytes) -> None:
        self.key = key_compress(bytes(key))

    def decrypt(self, data: bytes, offset: int = 0) -> bytes:
        return _v1_transform_block(data, offset, self.key)


def qmc2_hash(key: bytes) -> float:
    """官方 v2_rc4 hash()。"""
    h = 1
    for v in key:
        if v == 0:
            continue
        nxt = (h * v) & 0xFFFFFFFF
        if nxt == 0 or nxt <= h:
            break
        h = nxt
    return float(h)


def get_segment_key(seg_id: int, seed: int, h: float) -> int:
    """官方 get_segment_key。"""
    if seed == 0:
        return 0
    denom = ((seg_id + 1) * seed) & 0xFFFFFFFFFFFFFFFF
    return int(h / float(denom) * 100.0)


class _ModifiedRC4:
    """改型 RC4：状态长度 = 密钥长度（非 256）。"""

    def __init__(self, key: bytes) -> None:
        n = len(key)
        state = [i & 0xFF for i in range(n)]
        j = 0
        for i in range(n):
            j = (j + state[i] + key[i % n]) % n
            state[i], state[j] = state[j], state[i]
        self.state = state
        self.i = 0
        self.j = 0
        self.n = n

    def generate(self) -> int:
        n = self.n
        self.i = (self.i + 1) % n
        self.j = (self.j + self.state[self.i]) % n
        self.state[self.i], self.state[self.j] = self.state[self.j], self.state[self.i]
        return self.state[(self.state[self.i] + self.state[self.j]) % n]


RC4_FIRST_SEGMENT_SIZE = 0x0080
RC4_OTHER_SEGMENT_SIZE = 0x1400
_RC4_STREAM_CACHE_SIZE = RC4_OTHER_SEGMENT_SIZE + 512


class QMC2RC4:
    """长密钥（>300 字节）RC4 密码：首段逐字节，其余段按块用缓存密钥流。"""

    def __init__(self, key: bytes) -> None:
        key = bytes(key)
        rc4 = _ModifiedRC4(key)
        self.hash = qmc2_hash(key)
        self.key = key
        self.key_stream = bytes(rc4.generate() for _ in range(_RC4_STREAM_CACHE_SIZE))

    def _first_segment(self, buf: bytearray, start: int, length: int, offset: int) -> None:
        n = len(self.key)
        h = self.hash
        key = self.key
        for j in range(length):
            o = offset + j
            idx = get_segment_key(o, key[o % n], h) % n
            buf[start + j] ^= key[idx]

    def _other_segment(self, buf: bytearray, start: int, length: int, offset: int) -> None:
        n = len(self.key)
        seg_id = offset // RC4_OTHER_SEGMENT_SIZE
        block_off = offset % RC4_OTHER_SEGMENT_SIZE
        seed = self.key[seg_id % n]
        skip = get_segment_key(seg_id, seed, self.hash) & 0x1FF
        ks = self.key_stream
        chunk = bytes(buf[start : start + length])
        buf[start : start + length] = _xor_eq(
            chunk, ks[skip + block_off : skip + block_off + length]
        )

    def decrypt(self, data: bytes, offset: int = 0) -> bytes:
        out = bytearray(data)
        n = len(out)
        pos, start = offset, 0
        if pos < RC4_FIRST_SEGMENT_SIZE:
            take = min(RC4_FIRST_SEGMENT_SIZE - pos, n - start)
            self._first_segment(out, start, take, pos)
            start += take
            pos += take
        if pos % RC4_OTHER_SEGMENT_SIZE != 0:
            take = min(RC4_OTHER_SEGMENT_SIZE - (pos % RC4_OTHER_SEGMENT_SIZE), n - start)
            self._other_segment(out, start, take, pos)
            start += take
            pos += take
        while start < n:
            take = min(RC4_OTHER_SEGMENT_SIZE, n - start)
            self._other_segment(out, start, take, pos)
            start += take
            pos += take
        return bytes(out)


def make_qmc2_cipher(master_key: bytes):
    """按主密钥长度选择密码（官方 QMCv2Cipher::new 语义）。"""
    master_key = bytes(master_key)
    if not master_key:
        raise DecryptFailedError("QMCv2 主密钥为空")
    if len(master_key) <= 300:
        return QMC2Map(master_key)
    return QMC2RC4(master_key)


# ---------------------------------------------------------------------------
# Footer 解析（STag / QTag / PcV2MusicEx / PcV1Legacy）
# ---------------------------------------------------------------------------

_B64_ALPHABET = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
)
_MAX_EKEY_LEN = 0x500


def _is_base64_text(s: bytes) -> bool:
    return all(c in _B64_ALPHABET for c in s)


def _read_utf16le(data: bytes) -> str:
    out = []
    for i in range(0, len(data) - 1, 2):
        if data[i] == 0 and data[i + 1] == 0:
            break
        if data[i + 1] == 0 and 0 < data[i] < 128:
            out.append(chr(data[i]))
        else:
            break
    return "".join(out)


class Footer:
    """文件尾部元数据：size=应裁剪字节数，ekey=内嵌密钥（可能为 None）。"""

    __slots__ = ("size", "ekey", "ftype", "extra")

    def __init__(self, size: int, ekey: Optional[str], ftype: str, **extra) -> None:
        self.size = size
        self.ekey = ekey
        self.ftype = ftype
        self.extra = extra


def parse_footer(tail: bytes) -> Optional[Footer]:
    """解析文件末尾片段（建议传最后 1024 字节）。无已知 footer 返回 None。"""
    if len(tail) < 8:
        return None

    if tail.endswith(b"STag"):
        footer = tail[:-4]
        payload, size_bytes = footer[:-4], footer[-4:]
        payload_len = int.from_bytes(size_bytes, "big")
        if len(payload) < payload_len:
            raise DecryptFailedError("STag footer 长度不一致")
        csv = payload[len(payload) - payload_len :].decode("utf-8", "replace")
        parts = csv.split(",")
        if len(parts) != 3:
            raise DecryptFailedError("STag footer 格式错误")
        rid, ver, media_mid = parts
        return Footer(payload_len + 8, None, "STag",
                      resource_id=rid, media_mid=media_mid)

    if tail.endswith(b"QTag"):
        footer = tail[:-4]
        payload, size_bytes = footer[:-4], footer[-4:]
        payload_len = int.from_bytes(size_bytes, "big")
        if len(payload) < payload_len:
            raise DecryptFailedError("QTag footer 长度不一致")
        csv = payload[len(payload) - payload_len :].decode("utf-8", "replace")
        parts = csv.split(",")
        if len(parts) != 3:
            raise DecryptFailedError("QTag footer 格式错误")
        ekey, rid, ver = parts
        if not _is_base64_text(ekey.encode("latin-1")):
            raise DecryptFailedError("QTag EKey 非法")
        return Footer(payload_len + 8, ekey, "QTag", resource_id=rid)

    if tail.endswith(b"musicex\x00"):
        payload = tail[:-8]
        if len(payload) < 8:
            return None
        data, version_bytes = payload[:-4], payload[-4:]
        version = int.from_bytes(version_bytes, "little")
        if version != 1:
            return None
        if len(data) < 8:
            return None
        payload2, payload_len_bytes = data[:-4], data[-4:]
        payload_len = int.from_bytes(payload_len_bytes, "little")
        if payload_len != 0xC0:
            return None
        inner = payload2[len(payload2) - (payload_len - 0x10):]
        mid = _read_utf16le(inner[12:12 + 60])
        media_filename = _read_utf16le(inner[12 + 60:12 + 60 + 100])
        return Footer(payload_len, None, "PcV2MusicEx",
                      mid=mid, media_filename=media_filename)

    # PcV1Legacy（经典 .mflac/.mgg/.qmcflac）
    payload, size_bytes = tail[:-4], tail[-4:]
    payload_len = int.from_bytes(size_bytes, "little")
    if payload_len == 0 or payload_len > _MAX_EKEY_LEN:
        return None
    if len(payload) < payload_len:
        return None
    ekey_bytes = payload[len(payload) - payload_len :]
    zero = ekey_bytes.find(b"\x00")
    if zero != -1:
        ekey_bytes = ekey_bytes[:zero]
    if not ekey_bytes or not _is_base64_text(ekey_bytes):
        return None
    return Footer(payload_len + 4, ekey_bytes.decode("latin-1"), "PcV1Legacy")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

# V2 扩展名（用于提示与回退扩展名）
V2_EXTENSIONS = frozenset({
    "mgg", "mgg0", "mggl", "mgg1", "mflac", "mflac0", "mmp4",
    "qmcflac", "qmcogg", "qmc0", "qmc2", "qmc3", "qmc4", "qmc6", "qmc8",
})
V1_EXTENSIONS = frozenset({
    "bkcmp3", "bkcm4a", "bkcflac", "bkcwav", "bkcape", "bkcogg", "bkcwma",
    "tkm", "666c6163", "6d7033", "6f6767", "6d3461", "776176",
})
QMC_EXTENSIONS = V2_EXTENSIONS | V1_EXTENSIONS

_FALLBACK_EXT = {
    "mgg": "ogg", "mgg0": "ogg", "mggl": "ogg", "mgg1": "ogg",
    "mflac": "flac", "mflac0": "flac", "mmp4": "mp4",
    "qmcflac": "flac", "qmcogg": "ogg", "qmc0": "mp3", "qmc2": "ogg",
    "qmc3": "mp3", "qmc4": "ogg", "qmc6": "ogg", "qmc8": "ogg",
    "bkcmp3": "mp3", "bkcm4a": "m4a", "bkcflac": "flac", "bkcwav": "wav",
    "bkcape": "ape", "bkcogg": "ogg", "bkcwma": "wma", "tkm": "m4a",
    "666c6163": "flac", "6d7033": "mp3", "6f6767": "ogg",
    "6d3461": "m4a", "776176": "wav",
}


def decrypt_qmc(data: bytes, ext_hint: str = "", ekey_override: Optional[str] = None) -> tuple[bytes, str]:
    """解密 QMC 字节流。返回 (音频字节, 输出扩展名)。

    流程：先解析 footer，有内嵌 EKey（或 ekey_override）走 V2；
    否则尝试 V1 静态密钥；无法识别为音频时按扩展名回退。
    """
    if len(data) < 16:
        raise DecryptFailedError("文件过短，不是有效的 QMC 文件")
    tail = data[-1024:]
    footer = None
    try:
        footer = parse_footer(tail)
    except DecryptFailedError:
        footer = None

    audio_data = data
    if footer is not None:
        audio_data = data[: len(data) - footer.size]
        ekey = footer.ekey or ekey_override
        if ekey is None:
            # 无内嵌密钥（STag/MusicEx）：无法离线解密
            raise KeyMissingError(
                "该 %s 文件未内嵌解密密钥（%s），需在线获取或用 QQ 音乐"
                "密钥库提供 EKey 后重试" % (footer.ftype, ext_hint or "qmc")
            )
        master_key = ekey_decrypt(ekey.encode("latin-1"))
        cipher = make_qmc2_cipher(master_key)
        out = cipher.decrypt(audio_data, 0)
        ext = sniff_audio_ext(out)
        if ext == "bin":
            # 官方语义：V2 路径嗅探失败按扩展名回退（可能是旧 key 或文件损坏）
            ext = _FALLBACK_EXT.get(ext_hint, "bin")
        return out, ext
    # 无 footer：走 V1 静态密钥（ekey_override 仅在有 footer 时生效）
    out = v1_decrypt(data)
    ext = sniff_audio_ext(out)
    if ext == "bin":
        raise DecryptFailedError(
            "静态密钥解密后不是可识别的音频数据，不是受支持的 QMC 文件"
        )
    return out, ext
