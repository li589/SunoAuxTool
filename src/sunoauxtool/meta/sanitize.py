"""sunoauxtool.meta.sanitize：音频元数据清除 / 保留 / 覆写（R9 核心实现）。

容器分工：
    - **WAV**：自写 RIFF chunk 重写器（白名单 ``fmt``/``data``/``fact``），
      不依赖第三方库——因此 C2PA（Content Credentials，官方 WAV 尾部的
      JUMBF/CBOR manifest）、jumb/jumd、ID3 chunk、LIST/INFO 等**任意未知
      chunk** 一律按元数据剔除，不留漏网模式；
    - **MP3 / FLAC / M4A / OGG / Opus**：mutagen 处理标签层，保存后再手工
      剥除文件尾部 legacy 标签（ID3v1 ``TAG``、APEv2 ``APETAGEX``）——
      mutagen 不管尾部，不清会留下「清了个寂寞」的残余；
    - **MOTW**：删除 Windows ``zone.Identifier``（NTFS 备用数据流），
      即「解除计算机保护」。

三级模式（``SanitizeSpec.mode``）：
    all        移除全部标签/溯源（+MOTW）；
    keep-basic 只保留 曲名/作者/专辑/年份/流派/歌词/评论（WAV 例外：INFO 的
               ICMT 是工具倾倒溯源串的惯常位置——Suno 官方 WAV 即把
               "made with suno; created=..." 写在 ICMT——故 WAV 的
               keep-basic 不保留 ICMT）；
    custom     用 ``SanitizeSpec`` 提供的字段覆写（未提供的字段被清空）。

诚实边界（与包 docstring 一致，不夸大）：
    - C2PA hard binding（manifest 内 sha256）随 manifest 移除而失效；
    - C2PA soft binding（音频域水印）不在标签层，本工具无法「擦除」；
    - MOTW 仅 Windows NTFS 可删。

错误码：35 容器不支持/不可解析（UnsupportedContainerError）；
36 标签读写失败（MetaWriteError）。批量 8/9 由 CLI 层汇总。
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from sunoauxtool.exceptions import InputFileError, ParameterError

from .exceptions import MetaWriteError, UnsupportedContainerError

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 清理模式（CLI ``--mode`` 取值）
MODES = ("all", "keep-basic", "custom")

#: 受支持的容器（按扩展名）
SUPPORTED_SUFFIXES = (".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus")

#: WAV 白名单：播放必需 chunk，其余一律按元数据剔除
_WAV_ESSENTIAL = {b"fmt ", b"data", b"fact"}

#: RIFF INFO 字段：keep-basic 保留集（刻意不含 ICMT/ISFT，见模块 docstring）
_WAV_INFO_KEEP = (b"INAM", b"IART", b"IPRD", b"ICRD", b"IGNR")

#: RIFF INFO 字段 <-> 语义名（custom 模式写入用；歌词无 INFO 对应位，只进 id3）
_WAV_INFO_FIELD = (
    (b"INAM", "title"),
    (b"IART", "artist"),
    (b"IPRD", "album"),
    (b"ICRD", "year"),
    (b"IGNR", "genre"),
    (b"ICMT", "comment"),
)

#: MP3 ID3v2：keep-basic 保留的帧类型（USLT=歌词；COMM 仅 desc 为空的普通评论）
_MP3_KEEP_IDS = frozenset({"TIT2", "TPE1", "TALB", "TDRC", "TYER", "TCON", "USLT"})

#: Vorbis comment（FLAC/OGG/Opus）：keep-basic 保留字段（mutagen 落盘即小写键）
_VORBIS_KEEP = ("title", "artist", "album", "date", "genre", "lyrics", "comment")

#: MP4（M4A）：keep-basic 保留的 atom 键
_MP4_KEEP = ("\xa9nam", "\xa9ART", "\xa9alb", "\xa9day", "\xa9gen", "\xa9lyr", "\xa9cmt")

#: custom 模式语义字段 -> Vorbis comment 键
_VORBIS_WRITE = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "year": "date",
    "genre": "genre",
    "lyrics": "lyrics",
    "comment": "comment",
}

#: custom 模式语义字段 -> MP4 atom 键
_MP4_WRITE = {
    "title": "\xa9nam",
    "artist": "\xa9ART",
    "album": "\xa9alb",
    "year": "\xa9day",
    "genre": "\xa9gen",
    "lyrics": "\xa9lyr",
    "comment": "\xa9cmt",
}


# ---------------------------------------------------------------------------
# 规格与报告
# ---------------------------------------------------------------------------


@dataclass
class SanitizeSpec:
    """一次清理任务的规格（mode 校验失败抛 ParameterError=1）。"""

    mode: str = "all"
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    genre: Optional[str] = None
    year: Optional[str] = None
    lyrics: Optional[str] = None
    comment: Optional[str] = None
    #: 是否解除 MOTW（默认 True；--no-motw 关闭）
    motw: bool = True

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ParameterError(
                f"未知 mode: {self.mode}（可用: {'|'.join(MODES)}）", code=1
            )

    def fields(self) -> Dict[str, str]:
        """custom 模式显式提供的字段（空串视为未提供）。"""
        out: Dict[str, str] = {}
        for name in _VORBIS_WRITE:
            val = getattr(self, name)
            if val:
                out[name] = val
        return out


@dataclass
class SanitizeReport:
    """单文件清理结果（人类可读摘要见 :meth:`summary`）。"""

    path: Path
    container: str
    mode: str
    #: 被移除的块/帧/标签描述
    removed: List[str] = field(default_factory=list)
    #: keep-basic 下被保留的字段描述
    kept: List[str] = field(default_factory=list)
    #: custom 模式下被覆写的字段描述
    written: List[str] = field(default_factory=list)
    #: MOTW 处理结果（None=未检查/不适用；True=已删；False=无或删不掉）
    motw_removed: Optional[bool] = None
    bytes_before: int = 0
    bytes_after: int = 0
    dry_run: bool = False

    def summary(self) -> str:
        parts: List[str] = []
        if self.removed:
            parts.append("移除: " + ", ".join(self.removed))
        if self.kept:
            parts.append("保留: " + ", ".join(self.kept))
        if self.written:
            parts.append("覆写: " + ", ".join(self.written))
        if self.motw_removed:
            parts.append("已解除计算机保护(MOTW)")
        if (
            not self.dry_run
            and self.bytes_before
            and self.bytes_after != self.bytes_before
        ):
            parts.append(f"{self.bytes_before} -> {self.bytes_after} B")
        return " | ".join(parts) or "无元数据变更"


# ---------------------------------------------------------------------------
# WAV：RIFF chunk 重写器
# ---------------------------------------------------------------------------


def _ckid(raw: bytes) -> str:
    """chunk ID -> 可读字符串（4 字节定长，含空格也保留）。"""
    return raw.decode("ascii", errors="replace").rstrip("\x00")


def _iter_wav_chunks(path: Path, data: bytes) -> Iterable[Tuple[bytes, int, int]]:
    """遍历 RIFF/WAVE 顶层 chunk：yield ``(chunk_id, body_offset, body_size)``。

    尾块被截断时按剩余字节收尾；解析到不可信区域即停止。
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise UnsupportedContainerError(str(path), "不是 RIFF/WAVE 容器")
    pos, n = 12, len(data)
    while pos + 8 <= n:
        cid = data[pos : pos + 4]
        size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        if pos + 8 + size > n:
            size = n - pos - 8  # 截断的尾块：按剩余字节裁剪
        yield cid, pos + 8, size
        pos += 8 + size + (size & 1)


def _emit_chunk(out: bytearray, cid: bytes, body: bytes) -> None:
    """写入一个 RIFF chunk（含 8 字节头与奇数长度补齐字节）。"""
    out += cid + struct.pack("<I", len(body)) + body
    if len(body) & 1:
        out += b"\x00"


def _parse_info(body: bytes) -> Dict[bytes, bytes]:
    """解析 LIST/INFO 负载（``body`` 以 ``INFO`` 魔数开头）-> ``{ckID: value}``。"""
    out: Dict[bytes, bytes] = {}
    pos = 0
    while pos + 8 <= len(body):
        cid = body[pos : pos + 4]
        size = struct.unpack("<I", body[pos + 4 : pos + 8])[0]
        if pos + 8 + size > len(body):
            break
        out[cid] = body[pos + 8 : pos + 8 + size].rstrip(b"\x00")
        pos += 8 + size + (size & 1)
    return out


def _build_info(fields: Dict[bytes, bytes]) -> bytes:
    """构造 LIST/INFO 负载（含 ``INFO`` 魔数；空 dict -> ``b"INFO"``）。"""
    body = bytearray(b"INFO")
    for cid, val in fields.items():
        val = bytes(val) + b"\x00"  # INFO 值以 NUL 结尾，长度含 NUL
        body += cid + struct.pack("<I", len(val)) + val
        if len(val) & 1:
            body += b"\x00"
    return bytes(body)


def _syncsafe(n: int) -> bytes:
    """ID3v2 syncsafe 长度（4×7bit）。"""
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def _build_id3_chunk(spec: SanitizeSpec) -> Optional[bytes]:
    """custom 模式：用 mutagen 序列化 ID3v2.4 帧，包成 WAV ``id3`` chunk（含头）。

    返回 None 表示无字段可写。chunk ID 用小写 ``id3 ``（4 字节含尾空格，
    mutagen._riff 读时 rstrip、写时 ljust(4)，即其规范形态）。
    """
    from mutagen.id3 import COMM, ID3, TALB, TCON, TDRC, TIT2, TPE1, USLT
    from mutagen.id3._tags import ID3SaveConfig

    f = spec.fields()
    frames = []
    if "title" in f:
        frames.append(TIT2(encoding=3, text=f["title"]))
    if "artist" in f:
        frames.append(TPE1(encoding=3, text=f["artist"]))
    if "album" in f:
        frames.append(TALB(encoding=3, text=f["album"]))
    if "year" in f:
        frames.append(TDRC(encoding=3, text=f["year"]))
    if "genre" in f:
        frames.append(TCON(encoding=3, text=f["genre"]))
    if "comment" in f:
        frames.append(COMM(encoding=3, lang="eng", desc="", text=f["comment"]))
    if "lyrics" in f:
        frames.append(USLT(encoding=3, lang="eng", desc="", text=f["lyrics"]))
    if not frames:
        return None
    tags = ID3()
    for fr in frames:
        tags.add(fr)
    payload = bytes(tags._write(ID3SaveConfig()))
    return b"ID3" + bytes([4, 0, 0]) + _syncsafe(len(payload)) + payload


def _rewrite_wav(
    path: Path, spec: SanitizeSpec, report: SanitizeReport, dry_run: bool
) -> None:
    """按 spec 重写 WAV（原地；``dry_run`` 只填充 report 不落盘）。"""
    data = path.read_bytes()
    out = bytearray(b"RIFF\x00\x00\x00\x00WAVE")
    custom_fields = spec.fields()
    has_data = False
    changed = False

    for cid, body_off, size in _iter_wav_chunks(path, data):
        body = data[body_off : body_off + size]
        if cid in _WAV_ESSENTIAL:
            _emit_chunk(out, cid, body)
            if cid == b"data":
                has_data = True
            continue
        if cid == b"LIST":
            if body[:4] != b"INFO":
                report.removed.append(f"LIST/{_ckid(body[:4])} ({size}B)")
                changed = True
                continue
            info = _parse_info(body[4:])
            if spec.mode == "all":
                report.removed.append(f"LIST/INFO ({size}B)")
            elif spec.mode == "keep-basic":
                kept = {k: v for k, v in info.items() if k in _WAV_INFO_KEEP}
                dropped = [k for k in info if k not in _WAV_INFO_KEEP]
                if kept:
                    _emit_chunk(out, b"LIST", _build_info(kept))
                    report.kept.append("INFO:" + ",".join(_ckid(k) for k in kept))
                if dropped:
                    report.removed.append(
                        "INFO:" + ",".join(_ckid(k) for k in dropped)
                    )
            else:  # custom：原 INFO 整体丢弃，循环后统一重建（原文件无 LIST 也能补写）
                report.removed.append(f"LIST/INFO ({size}B)")
            changed = True
            continue
        if cid in (b"id3 ", b"ID3 ", b"id3", b"ID3"):
            report.removed.append(f"id3 chunk ({size}B)")
            changed = True
            continue
        name = _ckid(cid)
        c2pa = "，Content Credentials" if cid.upper() == b"C2PA" else ""
        report.removed.append(f"{name} ({size}B{c2pa})")
        changed = True

    if not has_data:
        raise UnsupportedContainerError(str(path), "WAV 缺少 data chunk（音频载荷）")

    if spec.mode == "custom":
        # 重建 LIST/INFO（歌词无 INFO 对应位，只进 id3 chunk）
        info_fields = {
            cid_key: custom_fields[fname].encode("utf-8")
            for cid_key, fname in _WAV_INFO_FIELD
            if fname in custom_fields
        }
        if info_fields:
            _emit_chunk(out, b"LIST", _build_info(info_fields))
            report.written.append("INFO:" + ",".join(_ckid(k) for k in info_fields))
            changed = True
        chunk = _build_id3_chunk(spec)
        if chunk:
            # chunk ID 规范形态为 4 字节 "id3 "（mutagen._riff 读时 rstrip、写时 ljust(4)）
            _emit_chunk(out, b"id3 ", chunk)
            report.written.append("id3:" + ",".join(custom_fields))
            changed = True

    if not changed or dry_run:
        return
    out[4:8] = struct.pack("<I", len(out) - 8)
    tmp = path.with_name(path.name + ".sunoaux-tmp")
    tmp.write_bytes(bytes(out))
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# MP3 / FLAC / M4A / OGG / Opus：mutagen + 尾部 legacy 标签
# ---------------------------------------------------------------------------


def _load_tagged(path: Path):
    """按扩展名加载 mutagen 容器对象（``.ogg`` 自动区分 Vorbis/Opus）。"""
    suffix = path.suffix.lower()
    try:
        if suffix == ".mp3":
            from mutagen.mp3 import MP3

            return MP3(path)
        if suffix == ".flac":
            from mutagen.flac import FLAC

            return FLAC(path)
        if suffix == ".m4a":
            from mutagen.mp4 import MP4

            return MP4(path)
        if suffix in (".ogg", ".opus"):
            from mutagen.oggopus import OggOpus
            from mutagen.oggvorbis import OggVorbis

            try:
                return OggVorbis(path)
            except Exception:
                return OggOpus(path)
    except Exception as exc:
        raise UnsupportedContainerError(
            str(path), f"按 {suffix or '?'} 解析失败（{exc}）"
        ) from exc
    raise UnsupportedContainerError(str(path), f"不支持的扩展名 {suffix or '(无)'}")


def _tag_count(tags) -> int:
    try:
        return len(tags)
    except Exception:
        return 0


def _drop_pictures(audio, report: SanitizeReport) -> None:
    """FLAC 图片块清理（OGG 无此属性；MP4 封面在 tags 内随 clear 移除）。"""
    pics = getattr(audio, "pictures", None)
    if pics:
        report.removed.append(f"封面/图片 {len(pics)} 张")
        audio.clear_pictures()


def _clear_all(audio, tags, report: SanitizeReport) -> None:
    if tags is None:
        _drop_pictures(audio, report)
        return
    n = _tag_count(tags)
    tags.clear()
    _drop_pictures(audio, report)
    if n:
        report.removed.append(f"标签 {n} 项")


def _keep_basic(audio, tags, suffix: str, report: SanitizeReport) -> None:
    if tags is None:
        return
    if suffix == ".mp3":
        kept = [
            fr
            for fr in list(tags.values())
            if fr.FrameID in _MP3_KEEP_IDS
            or (fr.FrameID == "COMM" and getattr(fr, "desc", "") == "")
        ]
        n_before = _tag_count(tags)
        tags.clear()
        for fr in kept:
            tags.add(fr)
        if n_before:
            report.removed.append(f"ID3v2 {n_before} 帧 -> {len(kept)} 帧")
        if kept:
            report.kept.append(",".join(sorted({fr.FrameID for fr in kept})))
    elif suffix == ".m4a":
        n_before = _tag_count(tags)
        kept = {k: tags[k] for k in _MP4_KEEP if k in tags}
        tags.clear()
        tags.update(kept)
        if n_before:
            report.removed.append(f"MP4 标签 {n_before} 项 -> {len(kept)} 项")
        if kept:
            report.kept.append(",".join(kept))
    else:  # flac / ogg / opus
        n_before = _tag_count(tags)
        lower = {k.lower(): v for k, v in tags.items()}
        kept = {w: lower[w] for w in _VORBIS_KEEP if w in lower}
        tags.clear()
        tags.update(kept)
        _drop_pictures(audio, report)
        if n_before:
            report.removed.append(f"Vorbis comment {n_before} 项 -> {len(kept)} 项")
        if kept:
            report.kept.append(",".join(kept))


def _mp3_frame(name: str, value: str):
    from mutagen.id3 import COMM, TALB, TCON, TDRC, TIT2, TPE1, USLT

    if name == "title":
        return TIT2(encoding=3, text=value)
    if name == "artist":
        return TPE1(encoding=3, text=value)
    if name == "album":
        return TALB(encoding=3, text=value)
    if name == "year":
        return TDRC(encoding=3, text=value)
    if name == "genre":
        return TCON(encoding=3, text=value)
    if name == "comment":
        return COMM(encoding=3, lang="eng", desc="", text=value)
    return USLT(encoding=3, lang="eng", desc="", text=value)  # lyrics


def _overwrite(audio, tags, suffix: str, spec: SanitizeSpec, report: SanitizeReport) -> None:
    """custom 模式：清空既有标签后写入 spec 提供的字段（未提供的被清空）。"""
    fields = spec.fields()
    if tags is None:
        if suffix != ".mp3":
            raise MetaWriteError(str(audio.filename), "容器无标签对象且无法新建")
        from mutagen.id3 import ID3

        tags = audio.tags = ID3()
    n_before = _tag_count(tags)
    tags.clear()
    _drop_pictures(audio, report)
    if suffix == ".mp3":
        for name, val in fields.items():
            tags.add(_mp3_frame(name, val))
    elif suffix == ".m4a":
        for name, val in fields.items():
            tags[_MP4_WRITE[name]] = [val]
    else:  # flac / ogg / opus
        for name, val in fields.items():
            tags[_VORBIS_WRITE[name]] = [val]
    if n_before:
        report.removed.append(f"既有标签 {n_before} 项")
    if fields:
        report.written.append(
            ",".join(f"{k}={v[:24]}" for k, v in fields.items())
        )


def _sanitize_tagged(
    path: Path, suffix: str, spec: SanitizeSpec, report: SanitizeReport, dry_run: bool
) -> None:
    audio = _load_tagged(path)
    tags = getattr(audio, "tags", None)

    if spec.mode == "keep-basic":
        _keep_basic(audio, tags, suffix, report)
    elif spec.mode == "custom":
        _overwrite(audio, tags, suffix, spec, report)
    else:
        _clear_all(audio, tags, report)

    if dry_run:
        # 内存中的改动被丢弃（不 save），report 已反映「将会」发生的变更
        return
    try:
        audio.save()
    except Exception as exc:
        raise MetaWriteError(str(path), f"mutagen 保存失败（{exc}）") from exc
    report.removed += _strip_trailing_tags(path)


def _ape_tag_len(data: bytes) -> int:
    """APEv2 标签总字节数（items + footer [+ header]）；无 APE 返回 0。

    不解析 footer flags，改用「APETAGEX 头出现在 items 前 32B 处」判定
    header 是否存在，避免各家 flags 位定义记忆偏差导致误截。
    """
    if len(data) < 32 or bytes(data[-32:-24]) != b"APETAGEX":
        return 0
    version, size = struct.unpack("<II", data[-24:-16])
    if not 1000 <= version <= 3000:
        return 0
    if size + 32 > len(data):  # items + footer 必须放得下
        return 0
    items_start = len(data) - 32 - size
    if items_start >= 32 and bytes(data[items_start - 32 : items_start - 24]) == b"APETAGEX":
        return size + 64  # items + header + footer
    return size + 32


def _strip_trailing_tags(path: Path) -> List[str]:
    """剥除文件尾部 legacy 标签：ID3v1（``TAG``+125B）与 APEv2（``APETAGEX``）。"""
    removed: List[str] = []
    try:
        data = bytearray(path.read_bytes())
    except OSError:
        return removed
    changed = True
    while changed:
        changed = False
        if len(data) >= 128 and bytes(data[-128:-125]) == b"TAG":
            del data[-128:]
            removed.append("ID3v1")
            changed = True
            continue
        ape = _ape_tag_len(data)
        if ape:
            del data[-ape:]
            removed.append("APEv2")
            changed = True
    if removed:
        try:
            path.write_bytes(bytes(data))
        except OSError as exc:
            raise MetaWriteError(str(path), f"剥除尾部标签失败（{exc}）") from exc
    return removed


# ---------------------------------------------------------------------------
# MOTW
# ---------------------------------------------------------------------------


def strip_motw(path) -> bool:
    """删除 Windows ``zone.Identifier``（MOTW，「解除计算机保护」）。

    非 Windows 平台、无此 ADS、或删除失败（占用/权限）均返回 False——
    附加产物失败只告警不阻断（项目铁律）。
    """
    if os.name != "nt":
        return False
    try:
        os.unlink(f"{path}:Zone.Identifier")
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def sanitize_file(path, spec: SanitizeSpec, dry_run: bool = False) -> SanitizeReport:
    """清理单个文件（默认原地覆写；``dry_run`` 只分析不写）。

    容器按扩展名判定：wav 走 chunk 重写器，mp3/flac/m4a/ogg/opus 走 mutagen。
    """
    path = Path(path)
    if not path.is_file():
        raise InputFileError(f"文件不存在: {path}", code=3)
    suffix = path.suffix.lower()
    if suffix == ".wav":
        container = "wav"
    elif suffix in (".mp3", ".flac", ".m4a", ".ogg", ".opus"):
        container = suffix.lstrip(".")
    else:
        raise UnsupportedContainerError(str(path), f"扩展名 {suffix or '(无)'}")

    report = SanitizeReport(
        path=path,
        container=container,
        mode=spec.mode,
        bytes_before=path.stat().st_size,
        dry_run=dry_run,
    )
    if container == "wav":
        _rewrite_wav(path, spec, report, dry_run)
    else:
        _sanitize_tagged(path, suffix, spec, report, dry_run)
    if not dry_run and spec.motw:
        report.motw_removed = strip_motw(path)
    report.bytes_after = report.bytes_before if dry_run else path.stat().st_size
    return report
