"""Suno Web 凭证源（2.5 通道 B，P1 只读）：浏览器登录态 Cookie → 只读接口 → 下载。

设计（docs/plan-suno-access-channels.md §4）：

1. **凭证 = 用户自己导出的浏览器 Cookie**（等同账号密码）：构造参数 >
   环境变量 ``SUNOAUX_SUNO_COOKIE`` > 配置 ``[sources.suno-web] cookie``。
   **不做密码登录/验证码**——封号与合规双重红线；401/403 明确报"凭证过期"，
   不自动续期；
2. **P1 只读**：我的曲目 feed / 单 clip 元数据 / 音频下载（audio_url 为
   CDN 明文，直接 GET）。生成能力留 P2 且默认关闭；
3. **端点可注入**：Web 前端接口随改版漂移，基址与路径做成模块常量 +
   构造参数，实测时可一键校准（P1 交付时以真实 Cookie 校准一次）。

错误码：25 凭证缺失/过期（SourceCredentialError）/ 26 请求失败
（SourceRequestError）。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

from sunoauxtool.download.sources.api import load_source_config
from sunoauxtool.download.sources.base import FetchedFile, SourceAdapter
from sunoauxtool.exceptions import SourceCredentialError, SourceRequestError

#: Web 前端基址（studio 前端域；P1 实测时校准）
DEFAULT_WEB_BASE = "https://suno.com"
#: 我的曲目（分页 feed；P1 实测时确认精确路径与分页参数）
FEED_PATH = "/api/feed/"
#: 单 clip 元数据
CLIP_PATH = "/api/feed/{clip_id}"

_TIMEOUT_S = 60.0

_CLIP_ID_RE = re.compile(
    r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", re.IGNORECASE
)


def _mask_cookie(cookie: str) -> str:
    if len(cookie) >= 8:
        return f"{cookie[:4]}***{cookie[-2:]}"
    return "***" if cookie else "(无)"


def extract_clip_id(text: str) -> str:
    """从任意文本（链接/ID）中取 clip UUID。"""
    match = _CLIP_ID_RE.search(str(text))
    if not match:
        raise SourceRequestError(f"无法从输入中解析 clip_id: {text[:80]!r}")
    return match.group(0)


class SunoWebSource(SourceAdapter):
    """Suno Web 凭证源（P1 只读）：Cookie → feed/clip → 下载明文 mp3。"""

    name = "suno-web"
    description = (
        "Suno Web 凭证源（P1 只读）：浏览器登录态 Cookie 访问 feed/clip 并下载；"
        "不做密码登录，生成能力留 P2"
    )

    def __init__(
        self,
        *,
        cookie: Optional[str] = None,
        web_base: Optional[str] = None,
        timeout_s: float = _TIMEOUT_S,
    ) -> None:
        self._cookie = cookie
        self._web_base = (web_base or "").rstrip("/")
        self.timeout_s = timeout_s
        self.urlopen = urllib.request.urlopen  # 注入点：契约测试 mock 此处

    # -- 凭证 ----------------------------------------------------------------

    @property
    def web_base(self) -> str:
        if self._web_base:
            return self._web_base
        env = os.environ.get("SUNOAUX_SUNO_WEB_BASE", "").strip()
        if env:
            return env.rstrip("/")
        cfg = load_source_config("suno-web", require_endpoint=False, missing_ok=True)
        return str(cfg.get("web_base", DEFAULT_WEB_BASE)).rstrip("/") or DEFAULT_WEB_BASE

    def _resolve_cookie(self) -> str:
        if self._cookie:
            return self._cookie
        env = os.environ.get("SUNOAUX_SUNO_COOKIE", "").strip()
        if env:
            return env
        cfg = load_source_config("suno-web", require_endpoint=False, missing_ok=True)
        return str(cfg.get("cookie", ""))

    def _require_cookie(self) -> str:
        cookie = self._resolve_cookie()
        if not cookie:
            raise SourceCredentialError(
                "suno-web 源缺少 Cookie（P1 只读：登录 suno.com 后从开发者工具"
                "复制请求头里的整串 Cookie）。提供方式：--cookie / "
                "SUNOAUX_SUNO_COOKIE / 配置 [sources.suno-web] cookie。"
                "注意 Cookie 等同账号密码，勿提交到仓库。",
                code=25,
            )
        return cookie

    # -- HTTP ----------------------------------------------------------------

    def _get_json(self, path: str) -> object:
        """带 Cookie 的只读 GET；401/403 → 明确报凭证过期（25）。"""
        url = f"{self.web_base}{path}"
        request = urllib.request.Request(url, headers={
            "Cookie": self._require_cookie(),
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (sunoauxtool P1 read-only)",
        }, method="GET")
        try:
            with self.urlopen(request, timeout=self.timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise SourceCredentialError(
                    f"suno-web 凭证已被拒绝（HTTP {exc.code}）——Cookie 过期或失效，"
                    "请重新导出后重试（不自动续期）。",
                    code=25,
                ) from exc
            raise SourceRequestError(
                f"suno-web 请求失败 GET {path}: HTTP {exc.code}"
            ) from exc
        except Exception as exc:
            raise SourceRequestError(f"suno-web 请求失败 GET {path}: {exc}") from exc

    def _download(self, url: str, dest: Path) -> Path:
        request = urllib.request.Request(url, method="GET")
        try:
            with self.urlopen(request, timeout=self.timeout_s) as resp:
                payload = resp.read()
        except Exception as exc:
            raise SourceRequestError(f"suno-web 下载失败 {url}: {exc}") from exc
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        return dest

    # -- P1 只读能力 -----------------------------------------------------------

    @staticmethod
    def _extract_audio_urls(clip: object) -> List[str]:
        """从 clip 载荷提取音频 URL（结构随前端漂移，做防御式多路径）。"""
        urls: List[str] = []
        if not isinstance(clip, dict):
            return urls
        for key in ("audio_url", "audio_source_url"):
            value = clip.get(key)
            if isinstance(value, str) and value.startswith("http"):
                urls.append(value)
        for item in clip.get("versions") or []:
            if isinstance(item, dict):
                for key in ("audio_url", "audio_source_url"):
                    value = item.get(key)
                    if isinstance(value, str) and value.startswith("http"):
                        urls.append(value)
        return urls

    def list_clips(self, *, limit: int = 20) -> List[Dict]:
        """我的曲目 feed（最新在前）。返回归一化的 ``[{id, title, urls}]``。"""
        payload = self._get_json(f"{FEED_PATH}?page=0")  # 分页参数实测时校准
        items: List[object] = []
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = payload.get("clips") or payload.get("items") or []
        out: List[Dict] = []
        for item in items[:max(1, limit)]:
            if not isinstance(item, dict):
                continue
            clip = item.get("clip") if isinstance(item.get("clip"), dict) else item
            out.append({
                "id": str(clip.get("id", "")),
                "title": str(clip.get("title", "")),
                "urls": self._extract_audio_urls(clip),
            })
        return out

    def get_clip(self, clip_id: str) -> Dict:
        """单 clip 元数据（归一化同 :meth:`list_clips`）。"""
        clip_id = extract_clip_id(clip_id)
        payload = self._get_json(CLIP_PATH.format(clip_id=clip_id))
        clip = payload.get("clip") if isinstance(payload, dict) and isinstance(
            payload.get("clip"), dict
        ) else payload
        return {
            "id": str(clip.get("id", clip_id)) if isinstance(clip, dict) else clip_id,
            "title": str(clip.get("title", "")) if isinstance(clip, dict) else "",
            "urls": self._extract_audio_urls(clip),
        }

    # -- SourceAdapter 接口 ---------------------------------------------------

    def check(self, query: str) -> str:
        """凭证/输入自检（--dry-run）：不发请求。"""
        cookie = self._require_cookie()
        clip_id = extract_clip_id(query)
        return f"suno-web: cookie={_mask_cookie(cookie)}  clip={clip_id}  base={self.web_base}"

    def fetch(self, query: str, out_dir: Path) -> List[FetchedFile]:
        """clip_id（或含 UUID 的链接）→ 取元数据 → 下载全部音频版本。"""
        clip = self.get_clip(query)
        self.fetch_or_raise_dir(out_dir)
        clip_id = clip["id"] or extract_clip_id(query)
        if not clip["urls"]:
            raise SourceRequestError(
                f"clip {clip_id} 载荷中没有可下载的音频 URL（接口结构可能已改版，"
                "请更新 FEED_PATH/CLIP_PATH 常量）"
            )
        files: List[FetchedFile] = []
        for index, url in enumerate(clip["urls"], start=1):
            suffix = ".mp3" if "/mp3/" in url or url.endswith(".mp3") else ".mp4"
            dest = out_dir / f"{clip_id}-v{index}{suffix}"
            self._download(url, dest)
            files.append(FetchedFile(
                path=dest, source=self.name,
                meta={"clip_id": clip_id, "title": clip["title"], "url": url},
            ))
        return files
