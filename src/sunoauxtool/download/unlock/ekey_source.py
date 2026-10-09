"""QMC EKey 外部供给（1.6.5）：STag / PcV2MusicEx footer 文件无内嵌密钥。

三种供给方式（按优先级组成链，全部命中即返回）：
1. 显式密钥   --ekey <base64>        （单文件手工提供）
2. 本地密钥库 --ekey-db <sqlite>     （QQ 音乐客户端密钥库，纯离线；安卓
   /data/data/com.tencent.qqmusic/databases/player_process_db 的
   audio_file_ekey_table，或 Windows 客户端导出的同名库）
3. 在线查询   --ekey-api <url 模板>  （模板含 {id} 占位符，替换为媒体标识；
   响应支持 JSON（任意层级的 ekey 字段）或纯文本 base64）

设计原则：不内置任何第三方服务地址，零第三方依赖（urllib/sqlite3 均为标准库）；
查询失败抛 KeyMissingError(29)，绝不静默降级。
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterable, Optional

from sunoauxtool.download.unlock.exceptions import KeyMissingError

EKeyProvider = Callable[[Iterable[str]], Optional[str]]

DEFAULT_TIMEOUT = 10.0
UA = "sunoauxtool-unlock/%s" % "1.6.5"

# ekey 候选判定：base64 字母表、足够长、且不是媒体标识本身
_B64 = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
)


def _looks_like_ekey(value: str) -> bool:
    v = value.strip()
    return 8 <= len(v) <= 0x500 and all(c in _B64 for c in v)


def find_ekey_in_db(db_path: str | Path, identifiers: Iterable[str]) -> Optional[str]:
    """在 QQ 音乐客户端密钥库（SQLite）中查找 EKey。

    对表结构不做硬编码假设：扫描所有含 "ekey"（不区分大小写）的表；逐行将
    所有列转为文本，若任一列包含任一媒体标识（resource_id / media_mid /
    media_filename，均按大小写不敏感子串匹配），则从其余列中取第一个
    疑似 ekey（base64 且长度合适）的值。未命中返回 None。
    """
    ids = [i for i in identifiers if i]
    if not ids:
        return None
    path = Path(db_path)
    if not path.is_file():
        raise KeyMissingError("密钥库文件不存在: %s" % path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
        wanted = [t for t in tables if "ekey" in t.lower()] or tables
        for table in wanted:
            try:
                rows = conn.execute('SELECT * FROM "%s"' % table.replace('"', '""'))
            except sqlite3.DatabaseError:
                continue
            for row in rows:
                cells = [
                    v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
                    for v in row
                    if v is not None and str(v).strip()
                ]
                lowered = [c.lower() for c in cells]
                for ident in ids:
                    if any(ident.lower() in c for c in lowered):
                        for cell in cells:
                            if _looks_like_ekey(cell) and cell.strip() != ident:
                                return cell.strip()
                        break
    finally:
        conn.close()
    return None


def _walk_find_ekey(obj) -> Optional[str]:
    """在任意嵌套的 JSON 结构中找第一个 ekey 字段（键名含 ekey，不区分大小写）。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if "ekey" in str(k).lower() and isinstance(v, str) and v.strip():
                return v.strip()
        for v in obj.values():
            hit = _walk_find_ekey(v)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _walk_find_ekey(v)
            if hit:
                return hit
    return None


def fetch_ekey_online(
    api_template: str, identifier: str, timeout: float = DEFAULT_TIMEOUT
) -> Optional[str]:
    """用 URL 模板查询在线 EKey 服务。模板中的 {id} 替换为媒体标识。

    响应解析：优先按 JSON 找任意层级的 ekey 字段；否则整体按纯文本处理
    （去空白后须为合法 base64）。404 / 无结果返回 None；网络错误抛
    KeyMissingError（29）。
    """
    if "{id}" not in api_template:
        raise KeyMissingError("ekey-api 模板缺少 {id} 占位符: %r" % api_template)
    url = api_template.replace("{id}", urllib.parse.quote(identifier, safe=""))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            return None
        raise KeyMissingError(
            "在线 EKey 查询失败（HTTP %d）: %s" % (exc.code, identifier)
        ) from None
    except (urllib.error.URLError, OSError) as exc:
        raise KeyMissingError("在线 EKey 查询失败（%s）: %s" % (exc, identifier)) from None
    if not body.strip():
        return None
    try:
        hit = _walk_find_ekey(json.loads(body))
        if hit:
            return hit
    except (ValueError, TypeError):
        pass
    text = body.strip()
    if _looks_like_ekey(text):
        return text
    return None


def make_ekey_chain(
    ekey: Optional[str] = None,
    ekey_db: Optional[str | Path] = None,
    ekey_api: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> EKeyProvider:
    """按 显式密钥 → 本地密钥库 → 在线查询 组装供给链（命中即返回）。"""
    def provider(identifiers: Iterable[str]) -> Optional[str]:
        ids = list(identifiers)
        if ekey:
            return ekey
        if ekey_db:
            hit = find_ekey_in_db(ekey_db, ids)
            if hit:
                return hit
        if ekey_api:
            for ident in ids:
                hit = fetch_ekey_online(ekey_api, ident, timeout=timeout)
                if hit:
                    return hit
        return None

    return provider


def ekey_chain_from_env() -> tuple[Optional[str], Optional[str], Optional[str], float]:
    """读取环境变量缺省值：SUNO_QMC_EKEY / SUNO_QMC_EKEY_DB / SUNO_QMC_EKEY_API。"""
    ekey = os.environ.get("SUNO_QMC_EKEY") or None
    ekey_db = os.environ.get("SUNO_QMC_EKEY_DB") or None
    ekey_api = os.environ.get("SUNO_QMC_EKEY_API") or None
    timeout = DEFAULT_TIMEOUT
    raw = os.environ.get("SUNO_QMC_EKEY_TIMEOUT")
    if raw:
        try:
            timeout = float(raw)
        except ValueError:
            pass
    return ekey, ekey_db, ekey_api, timeout


def ekey_from_ekey_bytes(ekey_b64: str) -> bytes:
    """辅助：校验显式 ekey 可 base64 解码（供测试与 CLI 预检）。"""
    try:
        return base64.b64decode(ekey_b64, validate=True)
    except Exception:
        return b""
