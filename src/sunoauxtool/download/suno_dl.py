"""suno-dl（R8）：Suno 官方明文下载三段式（零解密）。

依据逆向报告（output/ref/suno_analysis_report.md 第三/四节，2026-10-09）：

下载路径优先级（前者成功即止，全部失败 -> 34）：
    1. CDN 直拼：``https://cdn1.suno.ai/{clip_id}.{ext}``（匿名快路径，
       参考扩展 examples/suno_downloader content.js:207）；
    2. clip 载荷：``GET {api_base}/api/clip/{clip_id}``（需 Cookie）
       -> ``audio_url``/``video_url`` 明文 CDN，顺带取 title/handle 供命名；
    3. 官方轮询：``GET {api_base}/api/download/clip/{clip_id}?format={fmt}``
       （需 Cookie，status=="ready" -> download_url，≤poll_max 次 × poll_interval s）；
       wav 另有 ``GET /api/gen/{clip_id}/wav_file/`` 直取与
       ``POST /api/gen/{clip_id}/convert_wav/`` 转换轮询（≤24×5s）。

可选配额门：``POST {api_base}/api/download/authorize``（--authorize，
涉及 credit 扣减；显式请求失败即抛 31）。

合规：不内置任何第三方中转 API；api_base 仅指向 Suno 官方域且可配置。
错误码 30-34（分段注册制，见 sunoauxtool.exceptions.ERROR_CODES）。
凭证（Cookie）查找顺序：CLI --cookie > 环境变量 SUNO_DL_COOKIE > 配置
``[sources.suno] cookie``（config/sources.toml 或 ~/.sunoauxtool/sources.toml）。
**任何路径不得回显 Cookie 明文**。
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from sunoauxtool.download.exceptions import SunoError
from sunoauxtool.download.sources.base import FetchedFile

#: Suno 官方 API 基址（可经 CLI --api-base / 配置 / 环境变量 SUNO_DL_API_BASE 覆盖）
DEFAULT_API_BASE = "https://studio-api.prod.suno.com"

#: 匿名 CDN 直拼模板（clip_id 由官方载荷/分享链接同源产生）
CDN_URL_TEMPLATE = "https://cdn1.suno.ai/{clip_id}.{ext}"

#: 支持的输出格式
SUPPORTED_FORMATS = ("mp3", "wav", "mp4")

#: clip_id 形态（同时用于从 /song/{id} 链接中提取）
CLIP_ID_RE = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}")

#: 凭证配置查找（与 sources/api.py 同一套文件、不同节名）
COOKIE_CONFIG_CANDIDATES = [
    Path("config/sources.toml"),
    Path.home() / ".sunoauxtool" / "sources.toml",
]

_TIMEOUT_S = 30.0
#: CDN 直拼响应有效性下限（字节）——更小视为软 404/错误页
_MIN_AUDIO_BYTES = 1024


# ---------------------------------------------------------------------------
# 异常（30-34 段）
# ---------------------------------------------------------------------------


class SunoDlError(SunoError):
    """suno-dl 基础异常（SunoError 子类，code=30-34）。"""


class SunoDlCredentialError(SunoDlError):
    """Cookie 缺失（CDN 快路径也失败、且无凭证进入载荷/轮询段）-> 30。"""

    def __init__(self, detail: str = "") -> None:
        super().__init__(
            "suno-dl 缺少会话 Cookie，无法进入 clip 载荷/官方轮询路径。"
            "提供方式（任选其一）：--cookie '<Cookie 请求头串>' | 环境变量 "
            "SUNO_DL_COOKIE | 配置文件 [sources.suno] cookie = \"...\"。"
            f"{(' ' + detail) if detail else ''}",
            code=30,
        )


class SunoDlRequestError(SunoDlError):
    """HTTP/网络请求失败 -> 31。"""


class SunoDlResponseError(SunoDlError):
    """响应不符合预期结构（非 JSON、载荷缺字段）-> 32。"""


class SunoDlPollTimeoutError(SunoDlError):
    """轮询耗尽仍未 ready -> 33。"""


class SunoDlAllPathsFailedError(SunoDlError):
    """三段式全部失败 -> 34。"""

    def __init__(self, clip_id: str, reasons: List[str]) -> None:
        detail = "\n".join(f"  - {r}" for r in reasons)
        super().__init__(
            f"suno-dl 全部路径失败（clip={clip_id}）：\n{detail}\n"
            "建议：确认 clip_id 是否为公开/本人可访问曲目，以及 Cookie 是否有效。",
            code=34,
        )


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def sanitize_filename(name: str, max_len: int = 200) -> str:
    """文件名清洗（复刻扩展 content.js:284 口径）：非法字符 -> '-'、空白 -> '_'。"""
    cleaned = re.sub(r"[/\\?%*:|\"<>]", "-", name)
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._")
    return cleaned[:max_len] or "untitled"


def unique_path(target: Path) -> Path:
    """uniquify 语义（复刻 chrome.downloads conflictAction）：已存在则追加 -1/-2/…。"""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for i in range(1, 1000):
        candidate = target.with_name(f"{stem}-{i}{suffix}")
        if not candidate.exists():
            return candidate
    raise SunoDlError(f"重名冲突过多，无法生成唯一文件名: {target}", code=24)


def extract_clip_id(raw: str) -> str:
    """从裸 ID 或含 /song/{id} 的 URL 提取 clip_id；提取失败 -> 参数错误 1。"""
    from sunoauxtool.exceptions import ParameterError

    match = CLIP_ID_RE.search(raw)
    if not match:
        raise ParameterError(
            f"无法从输入解析 clip_id: {raw!r}（期望 UUID 或含 /song/<uuid> 的链接）", code=1
        )
    return match.group(0)


def load_cookie_from_config() -> Optional[str]:
    """读配置 [sources.suno] cookie；无则 None（**不抛**，由调用方决定 30 时机）。"""
    import tomllib

    for path in COOKIE_CONFIG_CANDIDATES:
        if not path.is_file():
            continue
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        cookie = data.get("sources", {}).get("suno", {}).get("cookie")
        if cookie:
            return str(cookie)
    return None


# ---------------------------------------------------------------------------
# 下载器
# ---------------------------------------------------------------------------


class SunoDownloader:
    """Suno 官方明文下载（三段式）。urlopen/sleep 可注入供测试。"""

    def __init__(
        self,
        cookie: Optional[str] = None,
        api_base: str = DEFAULT_API_BASE,
        *,
        urlopen: Callable = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cookie = cookie
        self.api_base = api_base.rstrip("/")
        self.urlopen = urlopen
        self.sleep = sleep

    # -- HTTP ---------------------------------------------------------------

    def _http(
        self,
        url: str,
        *,
        method: str = "GET",
        data: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = _TIMEOUT_S,
    ) -> Tuple[int, bytes, Dict[str, str]]:
        """原始请求；返回 (status, body, headers)。网络异常抛 31，HTTP >=400 不抛由调用方分诊。"""
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", "SunoAuxTool/suno-dl")
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with self.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers.items())
        except urllib.error.HTTPError as exc:  # 有状态码的错误响应，交由调用方分诊
            try:
                body = exc.read()
            except Exception:  # pragma: no cover - read 失败按空体
                body = b""
            return exc.code, body, dict(exc.headers.items()) if exc.headers else {}
        except SunoDlError:
            raise
        except Exception as exc:
            raise SunoDlRequestError(f"请求失败 {method} {url}: {exc}", code=31) from exc

    def _json(self, url: str, **kw) -> Tuple[int, dict]:
        status, body, _ = self._http(url, **kw)
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SunoDlResponseError(
                f"响应不是合法 JSON（HTTP {status}）: {url}: {exc}", code=32
            ) from exc
        if not isinstance(payload, dict):
            raise SunoDlResponseError(
                f"响应 JSON 不是对象（HTTP {status}）: {url}", code=32
            )
        return status, payload

    @staticmethod
    def _looks_like_audio(body: bytes, headers: Dict[str, str]) -> bool:
        """CDN 直拼响应有效性判据：够长且不是 JSON/HTML 错误页。"""
        ctype = headers.get("Content-Type", headers.get("content-type", "")).lower()
        if any(t in ctype for t in ("json", "html", "text")):
            return False
        if len(body) < _MIN_AUDIO_BYTES:
            return False
        head = body[:16].lstrip()
        return not head.startswith((b"{", b"<!DOCTYPE", b"<html"))

    # -- 三段式路径 ----------------------------------------------------------

    def _try_cdn(self, clip_id: str, ext: str) -> Optional[Tuple[bytes, Dict[str, str]]]:
        """段 1：CDN 匿名直拼。失败返回 None（不抛，快路径允许静默降级）。"""
        url = CDN_URL_TEMPLATE.format(clip_id=clip_id, ext=ext)
        try:
            status, body, headers = self._http(url)
        except SunoDlError:
            return None
        if status == 200 and self._looks_like_audio(body, headers):
            return body, {"via": "cdn", "url": url}
        return None

    def _clip_payload(self, clip_id: str) -> Dict:
        """段 2 前置：clip 载荷（需 Cookie）。供 audio_url/title 提取。"""
        status, payload = self._json(f"{self.api_base}/api/clip/{clip_id}")
        if status != 200:
            raise SunoDlRequestError(
                f"clip 载荷请求失败（HTTP {status}）: /api/clip/{clip_id}", code=31
            )
        return payload

    def _try_payload_url(self, clip_id: str, ext: str) -> Optional[Tuple[bytes, Dict[str, str]]]:
        """段 2：载荷直链（mp3->audio_url、mp4->video_url）。无 Cookie 返回 None。"""
        if not self.cookie:
            return None
        payload = self._clip_payload(clip_id)
        field = "video_url" if ext == "mp4" else "audio_url"
        url = payload.get(field)
        if not url:
            return None
        status, body, headers = self._http(url)
        if status == 200 and self._looks_like_audio(body, headers):
            meta = {"via": "payload", "url": url, "clip_payload": "fetched"}
            title = payload.get("title") or ""
            if title:
                meta["title"] = str(title)
            return body, meta
        return None

    def _authorize(self, clip_id: str) -> None:
        """配额门（--authorize 时显式调用）：失败即抛（显式请求失败即抛原则）。"""
        url = f"{self.api_base}/api/download/authorize"
        body = json.dumps({"item_id": clip_id, "item_type": "clip"}).encode()
        status, payload = self._json(
            url, method="POST", data=body, headers={"Content-Type": "application/json"}
        )
        if status not in (200, 201):
            raise SunoDlRequestError(
                f"download/authorize 失败（HTTP {status}）: {payload}", code=31
            )

    def _download_url(self, url: str) -> bytes:
        status, body, headers = self._http(url)
        if status != 200 or not self._looks_like_audio(body, headers):
            raise SunoDlRequestError(
                f"下载直链取回失败（HTTP {status}）: {url}", code=31
            )
        return body

    def _poll_download_clip(self, clip_id: str, fmt: str, *, poll_max: int, poll_interval: float) -> bytes:
        """段 3（mp3/mp4）：官方轮询 <=poll_max 次 x poll_interval s -> download_url。"""
        url = f"{self.api_base}/api/download/clip/{clip_id}?format={fmt}"
        last_payload: Dict = {}
        for _ in range(poll_max):
            status, payload = self._json(url)
            last_payload = payload
            if status == 200 and payload.get("status") == "ready" and payload.get("download_url"):
                return self._download_url(str(payload["download_url"]))
            self.sleep(poll_interval)
        raise SunoDlPollTimeoutError(
            f"download/clip 轮询 {poll_max} 次未就绪（fmt={fmt}，最后状态: {last_payload}）",
            code=33,
        )

    def _fetch_wav(self, clip_id: str, *, wav_poll_max: int, wav_poll_interval: float) -> Tuple[bytes, str]:
        """段 3（wav）：wav_file 直取 -> convert_wav 转换轮询（报告 3.3 口径）。"""
        # 直取
        status, body, headers = self._http(f"{self.api_base}/api/gen/{clip_id}/wav_file/")
        if status == 200 and self._looks_like_audio(body, headers):
            return body, "gen-wav-file"
        # 转换轮询
        url = f"{self.api_base}/api/gen/{clip_id}/convert_wav/"
        self._http(url, method="POST", data=b"")
        for _ in range(wav_poll_max):
            self.sleep(wav_poll_interval)
            status, body, headers = self._http(url)
            if status == 200 and self._looks_like_audio(body, headers):
                return body, "convert-wav"
        raise SunoDlPollTimeoutError(
            f"wav 转换轮询 {wav_poll_max} 次未就绪（clip={clip_id}）", code=33
        )

    # -- 入口 -----------------------------------------------------------------

    def download(
        self,
        clip_id: str,
        out_dir: Path,
        *,
        fmt: str = "mp3",
        authorize: bool = False,
        poll_max: int = 90,
        poll_interval: float = 2.0,
        wav_poll_max: int = 24,
        wav_poll_interval: float = 5.0,
        overwrite: bool = False,
    ) -> FetchedFile:
        """三段式下载单个 clip；全部失败抛 34。"""
        if fmt not in SUPPORTED_FORMATS:
            from sunoauxtool.exceptions import ParameterError

            raise ParameterError(
                f"不支持的格式: {fmt}（可用: {'|'.join(SUPPORTED_FORMATS)}）", code=1
            )
        out_dir.mkdir(parents=True, exist_ok=True)
        reasons: List[str] = []
        title = ""

        if authorize:
            self._require_cookie("authorize 需要会话 Cookie")
            self._authorize(clip_id)

        # 段 1：CDN 匿名直拼
        got = self._try_cdn(clip_id, fmt)
        if got:
            body, meta = got
        else:
            reasons.append("cdn: 直拼无有效音频响应（匿名路径不可用或曲目非公开）")
            # 段 2：载荷直链（同时取 title 供命名）；wav 无载荷直链字段，直接跳段 3
            got = None
            if fmt in ("mp3", "mp4"):
                try:
                    got = self._try_payload_url(clip_id, fmt)
                except SunoDlError as exc:
                    got = None
                    reasons.append(f"payload: {exc.message}")
            if got:
                body, meta = got
                title = meta.get("title", "")
            else:
                if fmt in ("mp3", "mp4") and not any(r.startswith("payload") for r in reasons):
                    reasons.append("payload: 无直链可用（未配置 Cookie 或载荷缺直链字段）")
                # 段 3：官方轮询
                self._require_cookie("官方下载轮询需要会话 Cookie")
                try:
                    if fmt == "wav":
                        body, via = self._fetch_wav(
                            clip_id,
                            wav_poll_max=wav_poll_max,
                            wav_poll_interval=wav_poll_interval,
                        )
                        meta = {"via": via}
                    else:
                        body = self._poll_download_clip(
                            clip_id, fmt, poll_max=poll_max, poll_interval=poll_interval
                        )
                        meta = {"via": "download-clip"}
                    # title 属附加信息：失败只告警不回滚（附加产物失败只告警原则）
                    try:
                        payload = self._clip_payload(clip_id)
                        title = str(payload.get("title") or "")
                    except SunoDlError:
                        pass
                except SunoDlError as exc:
                    reasons.append(f"poll: {exc.message}")
                    raise SunoDlAllPathsFailedError(clip_id, reasons) from exc

        name = sanitize_filename(title) if title else clip_id
        target = unique_path(out_dir / f"{name}.{fmt}")
        target.write_bytes(body)
        meta.update({"clip_id": clip_id})
        if title:
            meta["title"] = title
        return FetchedFile(path=target, source="suno-dl", meta=meta)

    def _require_cookie(self, why: str) -> None:
        if not self.cookie:
            raise SunoDlCredentialError(why)

    # -- dry-run 自检 -----------------------------------------------------------

    def check(self, clip_id: str) -> str:
        """凭证/参数自检（不发请求）；Cookie 掩码显示。"""
        if not clip_id or not CLIP_ID_RE.fullmatch(clip_id):
            return f"suno-dl: clip_id 形态非法: {clip_id!r}（期望 UUID）"
        masked = "(无)"
        if self.cookie and len(self.cookie) >= 8:
            masked = f"{self.cookie[:4]}***{self.cookie[-2:]}"
        elif self.cookie:
            masked = "***"
        return (
            f"suno-dl: clip={clip_id}  api_base={self.api_base}  cookie={masked}"
            "（路径顺序: cdn 直拼 -> 载荷直链 -> 官方轮询）"
        )
