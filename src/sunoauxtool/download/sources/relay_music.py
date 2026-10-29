"""中转站音乐生成源（2.5 通道 A）：``POST /v1/media/generate`` 任务式接口。

实测依据（2026-10-29，dolphin.token6688.com，详见 docs/plan-suno-access-channels.md）：

1. **提交**：``POST {base}/media/generate``，体 ``{"model": ..., "prompt": ...,
   "params": {...}}``（model/prompt 必填）→ ``{"code":200,"data":{"status":
   "pending","task_id": "task-..."}}``；
2. **轮询**：``GET {base}/tasks/{task_id}`` → ``is_final`` / ``data[{type:"audio",
   url}]`` / ``billing.actual_cost_credits``；实测 ~60s 出片，**一次返回两条**
   （Suno 惯例双版本），CDN 明文 mp3（48kHz 立体声）；
3. **下载**：``audio_url`` 直接 GET 落盘（明文，零解密）。

凭证查找顺序（与 usesuno 源同构）：构造参数 > 环境变量 > 配置节。
key 缺省回落到 ``SUNOAUX_AI_API_KEY``（与 ``compose ai`` 共用同一个中转站 key）。

错误码：25 凭证缺失（SourceCredentialError）/ 26 请求失败（SourceRequestError）/
47 任务失败或超时（MediaTaskError）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional

from sunoauxtool.download.sources.api import load_source_config
from sunoauxtool.download.sources.base import FetchedFile, SourceAdapter
from sunoauxtool.exceptions import SourceCredentialError, SourceRequestError, SmartNoteGenError

#: 默认端点（OpenAI 兼容中转站；模型/参数见 docs/plan-suno-access-channels.md）
DEFAULT_BASE_URL = "https://dolphin.token6688.com/v1"
#: Suno 生成模型（中转站计费 ≈0.48 元/条，2026-10-29 实测）
DEFAULT_MUSIC_MODEL = "music"

_TIMEOUT_S = 60.0
_POLL_INTERVAL_S = 5.0
_POLL_TIMEOUT_S = 600.0

#: 任务查询响应中表示"已完成"的字段/取值（实测口径：is_final=true + data[].url）
_TERMINAL_FAILURE_STATUES = {"failed", "error", "cancelled"}


class MediaTaskError(SmartNoteGenError):
    """媒体生成任务失败/超时/无产物（错误码 47）。"""

    code = 47


def _mask_key(key: str) -> str:
    if len(key) >= 8:
        return f"{key[:4]}***{key[-2:]}"
    return "***" if key else "(无)"


class RelayMusicSource(SourceAdapter):
    """中转站音乐生成源：prompt → 任务 → 轮询 → 下载明文 mp3。"""

    name = "relay-music"
    description = "中转站音乐生成（media API 任务式；prompt 即生成描述，≈0.5 元/条）"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout_s: float = _TIMEOUT_S,
    ) -> None:
        self._api_key = api_key
        self._base_url = (base_url or "").rstrip("/")
        self._model = model
        self.timeout_s = timeout_s
        self.urlopen = urllib.request.urlopen  # 注入点：契约测试 mock 此处
        self.sleeper: Callable[[float], None] = time.sleep  # 注入点：轮询测试不真等

    # -- 配置 ----------------------------------------------------------------

    @property
    def base_url(self) -> str:
        if self._base_url:
            return self._base_url
        env = os.environ.get("SUNOAUX_RELAY_BASE_URL", "").strip()
        if env:
            return env.rstrip("/")
        cfg = load_source_config("relay-music", require_endpoint=False, missing_ok=True)
        return str(cfg.get("base_url", DEFAULT_BASE_URL)).rstrip("/") or DEFAULT_BASE_URL

    @property
    def model(self) -> str:
        if self._model:
            return self._model
        env = os.environ.get("SUNOAUX_RELAY_MUSIC_MODEL", "").strip()
        if env:
            return env
        cfg = load_source_config("relay-music", require_endpoint=False, missing_ok=True)
        return str(cfg.get("model", DEFAULT_MUSIC_MODEL)) or DEFAULT_MUSIC_MODEL

    def _resolve_key(self) -> str:
        """key 查找顺序：构造参数 > SUNOAUX_RELAY_API_KEY > SUNOAUX_AI_API_KEY > 配置节。"""
        if self._api_key:
            return self._api_key
        env = os.environ.get("SUNOAUX_RELAY_API_KEY", "").strip()
        if env:
            return env
        shared = os.environ.get("SUNOAUX_AI_API_KEY", "").strip()
        if shared:  # 与 compose ai 共用同一个中转站 key
            return shared
        cfg = load_source_config("relay-music", require_endpoint=False, missing_ok=True)
        return str(cfg.get("api_key", ""))

    def _require_key(self) -> str:
        key = self._resolve_key()
        if not key:
            raise SourceCredentialError(
                "relay-music 源缺少 API key。提供方式：--api-key / "
                "SUNOAUX_RELAY_API_KEY（或与 compose ai 共用 SUNOAUX_AI_API_KEY）/ "
                "配置 [sources.relay-music] api_key。",
                code=25,
            )
        return key

    # -- HTTP ----------------------------------------------------------------

    def _request(self, method: str, url: str, *, payload: Optional[dict] = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self._require_key()}",
            "Content-Type": "application/json",
        }
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.urlopen(request, timeout=self.timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # 网络层与解析层统一映射为 26
            raise SourceRequestError(f"relay-music 请求失败 {method} {url}: {exc}") from exc

    # -- 能力 ----------------------------------------------------------------

    def submit(self, prompt: str, *, params: Optional[Dict] = None) -> str:
        """提交生成任务，返回 task_id（错误码 26）。"""
        body: Dict = {"model": self.model, "prompt": prompt}
        if params:
            body["params"] = params
        data = self._request(
            "POST", f"{self.base_url}/media/generate", payload=body
        )
        task_id = str((data.get("data") or {}).get("task_id") or data.get("task_id") or "")
        if not task_id:
            raise SourceRequestError(
                f"relay-music 提交成功但响应缺少 task_id: {json.dumps(data)[:200]}"
            )
        return task_id

    def task_status(self, task_id: str) -> dict:
        """查询任务（原始响应，交由 :meth:`wait_task` 判定终态）。"""
        return self._request("GET", f"{self.base_url}/tasks/{task_id}")

    def wait_task(
        self,
        task_id: str,
        *,
        timeout_s: float = _POLL_TIMEOUT_S,
        interval_s: float = _POLL_INTERVAL_S,
        on_poll: Optional[Callable[[int, dict], None]] = None,
    ) -> List[str]:
        """轮询到终态，返回音频 URL 列表（错误码 47：超时/失败/无产物）。"""
        deadline = time.monotonic() + timeout_s
        polls = 0
        while time.monotonic() < deadline:
            data = self.task_status(task_id)
            polls += 1
            if on_poll is not None:
                on_poll(polls, data)
            if data.get("is_final"):
                urls = [
                    str(item["url"])
                    for item in data.get("data") or []
                    if isinstance(item, dict) and item.get("url")
                ]
                status = str(data.get("status", "")).lower()
                if not urls:
                    raise MediaTaskError(
                        f"任务 {task_id} 已结束但无音频产物（status={status or '未知'}）",
                        code=47,
                    )
                return urls
            if str(data.get("status", "")).lower() in _TERMINAL_FAILURE_STATUES:
                raise MediaTaskError(
                    f"任务 {task_id} 失败: {json.dumps(data)[:200]}", code=47
                )
            self.sleeper(interval_s)
        raise MediaTaskError(
            f"任务 {task_id} 轮询超时（{timeout_s:.0f}s）——稍后可用 "
            f"GET {self.base_url}/tasks/{task_id} 重查",
            code=47,
        )

    def download(self, url: str, dest: Path) -> Path:
        """下载单个音频产物（明文 CDN，零解密）。"""
        request = urllib.request.Request(url, method="GET")
        try:
            with self.urlopen(request, timeout=self.timeout_s) as resp:
                payload = resp.read()
        except Exception as exc:
            raise SourceRequestError(f"relay-music 下载失败 {url}: {exc}") from exc
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        return dest

    # -- SourceAdapter 接口 ---------------------------------------------------

    def check(self, prompt: str) -> str:
        """凭证/参数自检（--dry-run）：不发请求。"""
        key = self._require_key()
        return (
            f"relay-music: key={_mask_key(key)}  base={self.base_url}  "
            f"model={self.model}  prompt={prompt[:40]!r}"
        )

    def fetch(self, query: str, out_dir: Path) -> List[FetchedFile]:
        """prompt → 生成 → 轮询 → 下载（query 语义 = 生成描述）。"""
        if not str(query).strip():
            raise SourceRequestError("relay-music：prompt 不能为空")
        self.fetch_or_raise_dir(out_dir)
        task_id = self.submit(query)
        urls = self.wait_task(task_id)
        files: List[FetchedFile] = []
        for index, url in enumerate(urls, start=1):
            dest = out_dir / f"{task_id}-v{index}.mp3"
            self.download(url, dest)
            files.append(FetchedFile(
                path=dest,
                source=self.name,
                meta={"task_id": task_id, "url": url, "bytes": str(dest.stat().st_size)},
            ))
        return files
