"""NCM 容器解包：CTENFDAM 魔数 → 密钥段 → 元数据段 → 封面 → 载荷。

容器布局（公开格式规范，各段长度均为小端 u32）：
    magic 'CTENFDAM'(8) + gap(2)
    key_len(4) + key_data（逐字节 XOR 0x64 → AES-128-ECB(core_key) →
                 PKCS#7 去填充 → 去 'neteasecloudmusic' 前缀 = RC4 密钥材料）
    meta_len(4) + meta_data（XOR 0x63 → 去 '163 key(Don't modify):'(22B) →
                 base64 → AES(meta_key) → 去 'music:' 前缀 = JSON）
    crc32(4) + gap(5)
    image_len(4) + image_data（封面 JPEG/PNG）
    payload（RC4 定制流密码 → flac / mp3）
"""

from __future__ import annotations

import base64
import json
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from sunoauxtool.download.ncm.aes_ecb import aes128_ecb_decrypt, pkcs7_unpad
from sunoauxtool.download.ncm.exceptions import CorruptNcmError, NotNcmError
from sunoauxtool.download.ncm.stream import decrypt_payload, parse_rc4_key

PathLike = str | Path

_MAGIC = b"CTENFDAM"
_CORE_KEY = bytes.fromhex("687A4852416D736F356B496E62617857")
_META_KEY = bytes.fromhex("2333466C6A6B5F215C5D2630553C2728")
_KEY_PREFIX = b"neteasecloudmusic"
_META_PREFIX = b"music:"
_META_XOR_PREFIX = b"163 key(Don't modify):"

# 载荷开头与格式对应（flac = 'fLaC'，mp3 = ID3v2 或 0xFFEx 帧头）
_FMT_SIGNATURES = (
    (b"fLaC", "flac"),
    (b"ID3", "mp3"),
    (b"\xff\xfb", "mp3"),
    (b"\xff\xf3", "mp3"),
    (b"\xff\xf2", "mp3"),
)


@dataclass
class NcmContent:
    """解包后的 NCM 内容。"""

    payload: bytes                # 解密后的原始音频字节
    fmt: str                      # 'flac' | 'mp3'（按文件签名判定）
    meta: Dict[str, Any] = field(default_factory=dict)
    image: Optional[bytes] = None  # 封面原始字节（JPEG/PNG），可能为 None

    @property
    def music_name(self) -> str:
        return str(self.meta.get("musicName") or "")

    @property
    def artists(self) -> list[str]:
        raw = self.meta.get("artist") or []
        names = []
        for item in raw:
            if isinstance(item, (list, tuple)) and item:
                names.append(str(item[0]))
            elif isinstance(item, str):
                names.append(item)
        return names

    @property
    def album(self) -> str:
        return str(self.meta.get("album") or "")


def parse(data: bytes) -> NcmContent:
    """解析并解密 NCM 字节流（纯内存，不落盘）。"""
    if not data.startswith(_MAGIC):
        raise NotNcmError("缺少 CTENFDAM 魔数，不是 NCM 文件")
    pos = len(_MAGIC) + 2  # magic + gap

    def _read_u32() -> int:
        nonlocal pos
        if pos + 4 > len(data):
            raise CorruptNcmError("容器在段长度处被截断")
        (value,) = struct.unpack_from("<I", data, pos)
        pos += 4
        return value

    def _read_bytes(n: int) -> bytes:
        nonlocal pos
        if pos + n > len(data):
            raise CorruptNcmError("容器在段数据处被截断")
        chunk = data[pos : pos + n]
        pos += n
        return chunk

    # -- 密钥段 ---------------------------------------------------------
    key_len = _read_u32()
    key_data = _read_bytes(key_len)
    key_data = bytes(b ^ 0x64 for b in key_data)
    try:
        key_plain = pkcs7_unpad(aes128_ecb_decrypt(_CORE_KEY, key_data))
    except ValueError as exc:
        raise CorruptNcmError(f"密钥段解密失败: {exc}") from exc
    if not key_plain.startswith(_KEY_PREFIX):
        raise CorruptNcmError("密钥段前缀不符（core key 错误或容器损坏）")
    rc4_key = parse_rc4_key(key_plain[len(_KEY_PREFIX) :])

    # -- 元数据段（可选：部分文件 meta_len 为 0 或为 1 占位） -------------
    meta: Dict[str, Any] = {}
    meta_len = _read_u32()
    if meta_len > 2:
        meta_data = _read_bytes(meta_len)
        meta_data = bytes(b ^ 0x63 for b in meta_data)
        # 跳过 '163 key(Don't modify):' 前缀（22 字节），其后是 base64
        idx = meta_data.find(_META_XOR_PREFIX)
        b64_body = meta_data[idx + len(_META_XOR_PREFIX) :] if idx >= 0 else meta_data
        try:
            meta_cipher = base64.standard_b64decode(b64_body)
            meta_plain = pkcs7_unpad(aes128_ecb_decrypt(_META_KEY, meta_cipher))
            meta_text = meta_plain[len(_META_PREFIX) :]
            meta = json.loads(meta_text.decode("utf-8", errors="replace"))
        except (ValueError, json.JSONDecodeError) as exc:
            # 元数据损坏不阻塞音频还原
            meta = {"_meta_error": str(exc)}

    # -- crc32(4) + gap(5) ----------------------------------------------
    _read_u32()
    _read_bytes(5)

    # -- 封面 -------------------------------------------------------------
    image: Optional[bytes] = None
    image_len = _read_u32()
    if image_len:
        image = _read_bytes(image_len)

    # -- 载荷 ---------------------------------------------------------------
    payload_enc = data[pos:]
    if not payload_enc:
        raise CorruptNcmError("载荷为空")
    payload = decrypt_payload(payload_enc, rc4_key)

    fmt = _detect_format(payload)
    return NcmContent(payload=payload, fmt=fmt, meta=meta, image=image)


def _detect_format(payload: bytes) -> str:
    for sig, fmt in _FMT_SIGNATURES:
        if payload.startswith(sig):
            return fmt
    raise CorruptNcmError("载荷签名无法识别（不是 flac/mp3）")


_SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|]')


def suggested_filename(content: NcmContent, stem: Optional[str] = None) -> str:
    """按 元数据/覆盖主干 生成安全输出文件名。"""
    if stem:
        base = stem
    else:
        parts = [content.music_name or "untitled"]
        if content.artists:
            parts.append("- " + "、".join(content.artists[:3]))
        base = " ".join(parts) if content.music_name else (content.album or "untitled")
    base = _SAFE_NAME_RE.sub("_", base).strip() or "untitled"
    return f"{base}.{content.fmt}"


def unpack_bytes(data: bytes, out_dir: PathLike, stem: Optional[str] = None,
                 write_cover: bool = True, overwrite: bool = False) -> list[Path]:
    """解包 NCM 字节流并落盘：音频必写，封面可选。返回产物路径列表。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    content = parse(data)
    name = suggested_filename(content, stem)
    audio_path = out / name
    if audio_path.exists() and not overwrite:
        raise FileExistsError(f"目标已存在: {audio_path}")
    audio_path.write_bytes(content.payload)
    paths = [audio_path]
    if write_cover and content.image:
        cover_path = out / f"{Path(name).stem}_cover.jpg"
        if not cover_path.exists() or overwrite:
            cover_path.write_bytes(content.image)
            paths.append(cover_path)
    return paths


def unpack_file(src: PathLike, out_dir: PathLike, stem: Optional[str] = None,
                write_cover: bool = True, overwrite: bool = False) -> list[Path]:
    """解包 .ncm 文件（入口门面）。"""
    return unpack_bytes(Path(src).read_bytes(), out_dir, stem, write_cover, overwrite)
