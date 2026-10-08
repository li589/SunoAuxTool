"""unlock 通用解密组件（1.6.3）：多平台加密音乐 → 原始音频，零第三方依赖。

覆盖格式：
- NCM   网易云（复用 ncm 包：AES-128 + 定制 RC4）
- KWM   酷我（魔数 yeelion-kuwo，u64 种子 + 32 字节循环密钥异或）
- KGM/KGE/VPR 酷狗（17 字节私钥 + 272 字节修正表 + 73MB 外置公钥，
  公钥经 SUNO_KGM_KEY 环境变量或 ~/.cache/sunoauxtool/kugou_key.xz 提供）
- QMC   QQ 音乐 V1 静态密钥（tkm/bkc*）与 V2 内嵌 EKey（mflac/mgg/qmc*）

算法移植自 unlock-music（MIT）等公开实现，见 docs/unlock.md 致谢。
门面：
    from sunoauxtool.download.unlock import unlock_file, detect_format
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from sunoauxtool.download.unlock._xutil import sniff_audio_ext
from sunoauxtool.download.unlock.exceptions import (
    DecryptFailedError,
    KeyMissingError,
    UnknownFormatError,
)
from sunoauxtool.download.unlock import kgm, kwm, qmc

# 各家扩展名 -> 格式名
_KWM_EXTS = frozenset({"kwm"})
_KGM_EXTS = frozenset({"kgm", "kgm.flac", "kge"})
_VPR_EXTS = frozenset({"vpr"})

ALL_UNLOCK_EXTS = (
    qmc.QMC_EXTENSIONS | _KWM_EXTS | _KGM_EXTS | _VPR_EXTS
)


def detect_format(ext: str, data: bytes) -> Optional[str]:
    """按扩展名 + 魔数综合判定加密格式；返回 'ncm'|'kwm'|'kgm'|'vpr'|'qmc'|None。"""
    ext = ext.lower().lstrip(".")
    if ext == "ncm":
        return "ncm"
    if ext in _KWM_EXTS:
        return "kwm" if kwm.is_kwm(data) else None
    if ext in _VPR_EXTS:
        return "vpr" if kgm.is_vpr(data) else None
    if ext in _KGM_EXTS:
        return "kgm" if kgm.is_kgm(data) else None
    if ext in qmc.QMC_EXTENSIONS:
        return "qmc"
    # 扩展名不可知时按魔数嗅探
    if data[:8] == b"CTENFDAM":
        return "ncm"
    if kwm.is_kwm(data):
        return "kwm"
    if kgm.is_kgm(data) or kgm.is_vpr(data):
        return "kgm"
    if data[-4:] in (b"QTag", b"STag") or data[-8:] == b"musicex\x00":
        return "qmc"
    return None


def unlock_bytes(data: bytes, ext: str = "") -> tuple[bytes, str]:
    """解密字节流。返回 (音频字节, 输出扩展名)。"""
    fmt = detect_format(ext, data)
    if fmt is None:
        raise UnknownFormatError(
            "无法识别的加密音乐格式（扩展名 %r 且魔数不匹配）" % ext
        )
    if fmt == "ncm":
        from sunoauxtool.download.ncm import parse

        content = parse(data)
        out_ext = content.fmt
        return content.payload, out_ext
    if fmt == "kwm":
        out = kwm.decrypt(data)
        return out, _finalize_ext(out, ext)
    if fmt in ("kgm", "vpr"):
        out = kgm.decrypt(data)
        return out, _finalize_ext(out, ext)
    # qmc
    out, out_ext = qmc.decrypt_qmc(data, ext_hint=ext.lower().lstrip("."))
    return out, out_ext


def _finalize_ext(audio: bytes, ext_hint: str) -> str:
    ext = sniff_audio_ext(audio)
    if ext == "bin":
        return "mp3" if ext_hint.lower() in ("kwm", "kgm", "vpr") else "bin"
    return ext


def unlock_file(
    input_path: str | Path,
    output_dir: str | Path,
    overwrite: bool = False,
) -> Path:
    """解密单个文件并写入 output_dir/<stem>.<ext>。返回输出路径。"""
    in_path = Path(input_path)
    out_dir = Path(output_dir)
    data = in_path.read_bytes()
    ext = in_path.suffix.lstrip(".")
    audio, out_ext = unlock_bytes(data, ext)
    if out_ext == "bin":
        raise DecryptFailedError(
            "解密结果无法识别为音频格式（文件可能已损坏或为不支持的子格式）"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / ("%s.%s" % (in_path.stem, out_ext))
    if out_path.exists() and not overwrite:
        n = 1
        while True:
            candidate = out_dir / ("%s (%d).%s" % (in_path.stem, n, out_ext))
            if not candidate.exists():
                out_path = candidate
                break
            n += 1
    out_path.write_bytes(audio)
    return out_path


__all__ = [
    "unlock_file",
    "unlock_bytes",
    "detect_format",
    "UnknownFormatError",
    "DecryptFailedError",
    "KeyMissingError",
]
