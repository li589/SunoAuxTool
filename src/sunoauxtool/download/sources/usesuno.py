"""usesuno.com 下载源（R9）：Turnstile 门 + SW-Mango AES-CTR 密文取回与解密。

契约（依据 examples/usesuno 保存页 ``suno-media-source.js`` 逆向，2026-10-21；
与 Suno 官方 SW-Mango 播放链同构——usesuno 转播同一内容密钥）：

1. **会话令牌**：浏览器经 Cloudflare Turnstile 人验后，usesuno.com 签发
   Bearer token（前端存 localStorage ``usesuno-access-v2``）。人验**不可
   自动化**——用户需自备 token：``--token`` / 环境变量 ``USESUNO_TOKEN`` /
   配置 ``[sources.usesuno] token``；
2. **密钥协商**：``POST {RIGHTS_URL}``，头 ``Authorization: Bearer <token>``
   + ``X-UseSuno-Protocol: 1``，体
   ``{"content_params": {"content_id": <uuid>, "content_type": "clip"}}``
   -> ``{"key": b64, "iv": b64, "glt": str}``；
3. **密钥解包**：``userKey = SHA-256(glt)``（AES-GCM raw key）；key/iv 各为
   ``base64(iv12B || AES-GCM 密文+tag)``，AAD = content_id（UTF-8）；
4. **取回密文**：``GET {AUDIO_URL_TEMPLATE}``（cloudfront，.m4a 密文）；
5. **解密**：AES-CTR（counter = 16B IV 整 128 位大端递增），**无字节整流**；
   解密结果经 MP4 容器 sanity 校验后落盘。

错误码：25 令牌缺失（SourceCredentialError）/ 26 请求或 rights 响应失败 /
28 密钥解包或解密失败（DecryptFailedError，unlock 域语义）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sunoauxtool.download.sources.api import load_source_config
from sunoauxtool.download.sources.base import FetchedFile, SourceAdapter
from sunoauxtool.download.suno_dl import extract_clip_id
from sunoauxtool.download.unlock.exceptions import DecryptFailedError
from sunoauxtool.exceptions import SourceCredentialError, SourceRequestError

#: 密钥协商端点（usesuno 自有，非 Suno 官方）
RIGHTS_URL = "https://usesuno.com/api/rights"

#: 密文取回模板（usesuno 自有 cloudfront 分发）
AUDIO_URL_TEMPLATE = "https://d2lwuy8qc234o3.cloudfront.net/1/clip/{clip_id}.m4a"

#: 前端协议版本头（与 widget 的 CLIENT_PROTOCOL 一致）
CLIENT_PROTOCOL = "1"

#: clip_id 形态（与 suno_dl 同一口径：UUID）
CLIP_ID_RE = re.compile(
    r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", re.IGNORECASE
)

_TIMEOUT_S = 60.0

#: 缺少 cryptography 依赖时的安装提示
_CRYPTO_HINT = "pip install cryptography（usesuno 源需要 AES-GCM/AES-CTR）"


def _mask_token(token: str) -> str:
    if len(token) >= 8:
        return f"{token[:4]}***{token[-2:]}"
    return "***" if token else "(无)"


class UseSunoSource(SourceAdapter):
    """usesuno.com 转播源：人验令牌 -> rights 协商 -> cloudfront 密文 -> AES-CTR 解密。"""

    name = "usesuno"
    description = (
        "usesuno.com 转播源（SW-Mango AES-CTR 密文；"
        "需浏览器 Turnstile 人验后取得的 Bearer token）"
    )

    def __init__(self, token: Optional[str] = None, rights_url: str = RIGHTS_URL):
        self.name = "usesuno"
        self.description = (
            "usesuno.com 转播源（SW-Mango AES-CTR 密文；"
            "需浏览器 Turnstile 人验后取得的 Bearer token）"
        )
        self._token = token
        self.rights_url = rights_url
        self.urlopen = urllib.request.urlopen  # 注入点：契约测试 mock 此处

    # -- 凭证 ---------------------------------------------------------------

    def _resolve_token(self) -> str:
        """token 查找顺序：构造参数 > 环境变量 USESUNO_TOKEN > 配置节。

        配置节缺失不报错（missing_ok）——token 可完全经 CLI/环境变量提供。
        """
        if self._token:
            return self._token
        env = os.environ.get("USESUNO_TOKEN")
        if env:
            return env
        cfg = load_source_config("usesuno", require_endpoint=False, missing_ok=True)
        return cfg.get("token", "")

    def _require_token(self, clip_id: str) -> str:
        token = self._resolve_token()
        if not token:
            raise SourceCredentialError(
                "usesuno 源缺少会话令牌（Turnstile 人验不可自动化，需用户自备）。"
                "获取方式：浏览器访问 usesuno.com 下载器并完成人验后，从 "
                "localStorage 'usesuno-access-v2' 复制 Bearer token；提供方式："
                "--token / USESUNO_TOKEN / 配置 [sources.usesuno] token。"
                f"（clip={clip_id}）",
                code=25,
            )
        return token

    def check(self, query: str) -> str:
        """凭证/输入自检（--dry-run）：校验 clip_id 形态与 token 掩码，不发请求。"""
        clip_id = extract_clip_id(query)
        token = self._resolve_token()
        if not token:
            raise SourceCredentialError(
                "usesuno 源缺少会话令牌（Turnstile 人验不可自动化，需用户自备）。"
                "获取方式：浏览器访问 usesuno.com 下载器并完成人验后，从 "
                "localStorage 'usesuno-access-v2' 复制 Bearer token；提供方式："
                "--token / USESUNO_TOKEN / 配置 [sources.usesuno] token。",
                code=25,
            )
        return f"usesuno: token={_mask_token(token)}  clip={clip_id}"

    # -- 主流程 ---------------------------------------------------------------

    def fetch(self, query: str, out_dir: Path) -> List[FetchedFile]:
        clip_id = extract_clip_id(query)
        token = self._require_token(clip_id)
        self.fetch_or_raise_dir(out_dir)

        rights = self._fetch_rights(clip_id, token)
        content_key, content_iv = _unwrap_content_key(rights, clip_id)
        cipher = self._download_encrypted(clip_id)
        plain = _decrypt_ctr(cipher, content_key, content_iv)
        _verify_audio_container(plain, clip_id)

        out = out_dir / f"{clip_id}.m4a"
        out.write_bytes(plain)
        return [
            FetchedFile(
                path=out,
                source=self.name,
                meta={"clip_id": clip_id, "bytes": str(len(plain))},
            )
        ]

    # -- HTTP -----------------------------------------------------------------

    def _fetch_rights(self, clip_id: str, token: str) -> Dict[str, str]:
        body = json.dumps(
            {"content_params": {"content_id": clip_id, "content_type": "clip"}}
        ).encode("utf-8")
        req = urllib.request.Request(self.rights_url, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("X-UseSuno-Protocol", CLIENT_PROTOCOL)
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "SunoAuxTool/1.9 (post fetch usesuno)")
        try:
            with self.urlopen(req, timeout=_TIMEOUT_S) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise SourceRequestError(
                f"usesuno: rights 请求失败 {self.rights_url}: {exc}", code=26
            ) from exc
        if not isinstance(payload, dict):
            raise SourceRequestError(
                f"usesuno: rights 响应不是 JSON 对象: {payload!r:.200}", code=26
            )
        missing = [k for k in ("key", "iv", "glt") if not payload.get(k)]
        if missing:
            raise SourceRequestError(
                f"usesuno: rights 响应缺少字段 {missing}"
                "（token 失效或内容不可用时会返回错误体）",
                code=26,
            )
        return payload

    def _download_encrypted(self, clip_id: str) -> bytes:
        url = AUDIO_URL_TEMPLATE.format(clip_id=clip_id)
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "SunoAuxTool/1.9 (post fetch usesuno)")
        try:
            with self.urlopen(req, timeout=_TIMEOUT_S) as resp:
                return resp.read()
        except Exception as exc:
            raise SourceRequestError(f"usesuno: 密文取回失败 {url}: {exc}", code=26) from exc


# ---------------------------------------------------------------------------
# 密码学（与 widget 的 WebCrypto 调用一一对应）
# ---------------------------------------------------------------------------


def _aesgcm(user_key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise DecryptFailedError(f"缺少 cryptography 依赖（{_CRYPTO_HINT}）") from exc
    return AESGCM(user_key)


def _unwrap_field(aesgcm, value: str, clip_id: str, label: str) -> bytes:
    """解包单个 wrapped 字段：base64(iv12B || AES-GCM 密文+tag)，AAD=clip_id。"""
    try:
        wrapped = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise DecryptFailedError(f"usesuno: {label} 不是合法 base64: {exc}") from exc
    if len(wrapped) < 28:
        raise DecryptFailedError(
            f"usesuno: {label} wrapped 长度 {len(wrapped)} < 28（iv12 + 密文 + tag16）"
        )
    try:
        return aesgcm.decrypt(wrapped[:12], wrapped[12:], clip_id.encode("utf-8"))
    except Exception as exc:
        raise DecryptFailedError(
            f"usesuno: {label} 解包失败（token 与内容不匹配或数据损坏）: {exc}"
        ) from exc


def _unwrap_content_key(rights: Dict[str, str], clip_id: str) -> Tuple[bytes, bytes]:
    """userKey=SHA-256(glt) -> 解包 content key 与 16B IV。"""
    glt = rights.get("glt") or ""
    if not isinstance(glt, str) or not glt:
        raise DecryptFailedError("usesuno: rights 响应缺少 glt（guest token）")
    user_key = hashlib.sha256(glt.encode("utf-8")).digest()
    aesgcm = _aesgcm(user_key)
    key = _unwrap_field(aesgcm, rights["key"], clip_id, "key")
    iv = _unwrap_field(aesgcm, rights["iv"], clip_id, "iv")
    if len(iv) != 16:
        raise DecryptFailedError(f"usesuno: 解包得到的 IV 长度 {len(iv)} != 16")
    return key, iv


def _decrypt_ctr(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-CTR：counter = iv 整 128 位大端递增（与 widget addCounter 一致）。"""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise DecryptFailedError(f"缺少 cryptography 依赖（{_CRYPTO_HINT}）") from exc
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()
        return decryptor.update(data) + decryptor.finalize()
    except Exception as exc:
        raise DecryptFailedError(f"usesuno: AES-CTR 解密失败: {exc}") from exc


def _verify_audio_container(data: bytes, clip_id: str) -> None:
    """解密结果 sanity 校验：应为 MP4（ftyp box，size 在头 4 字节大端）。"""
    if len(data) < 12 or data[4:8] != b"ftyp":
        raise DecryptFailedError(
            f"usesuno: 解密结果不是可识别音频容器（clip={clip_id}，"
            f"头部 {data[:8]!r}）——密钥不匹配或数据损坏"
        )
