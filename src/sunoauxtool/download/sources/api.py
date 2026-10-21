"""API 下载源（R7 留位）：suno-api / haimeng / tianyin 统一 HTTP 契约。

**本文件不含任何真实端点/逆向逻辑**。三个源的接入方式 = 在 gitignored 配置里
提供 ``endpoint`` + ``token``，适配器按统一契约调用：

1. ``GET {endpoint}?id={query}``，头 ``Authorization: Bearer {token}``；
2. 响应 JSON：``{"files": [{"url": "...", "name": "..."}, ...]}``；
3. 逐个下载到 out_dir（文件名取 name，缺省从 URL 推断）。

契约测试（tests/test_download_sources.py）以 mock HTTP 锁定上述口径；
真实 API 可用后只需填配置，不改代码。凭证缺失 → 25；请求/解析失败 → 26。
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Dict, List
from urllib.parse import urlencode

from sunoauxtool.download.sources.base import FetchedFile, SourceAdapter
from sunoauxtool.exceptions import SourceCredentialError, SourceRequestError

#: API 源名（--source 取值；配置节 [sources.<name>]）
API_SOURCE_NAMES = ["suno-api", "haimeng", "tianyin"]

#: 配置查找顺序（后者覆盖前者；两者都 gitignored）
CONFIG_CANDIDATES = [
    Path("config/sources.toml"),
    Path.home() / ".sunoauxtool" / "sources.toml",
]

_TIMEOUT_S = 30.0


def load_source_config(
    name: str, require_endpoint: bool = True, missing_ok: bool = False
) -> Dict[str, str]:
    """读取指定源的配置节；缺失抛 SourceCredentialError(25)。

    ``require_endpoint=False`` 用于端点写死在代码里的源（如 usesuno，
    其 rights/CDN 端点是公开契约常量，配置只需 token）。
    ``missing_ok=True`` 用于凭证可经 CLI/环境变量提供的源（如 usesuno 的
    token），无配置节时返回空 dict 而非报错。
    """
    import tomllib

    merged: Dict[str, str] = {}
    found_any = False
    for path in CONFIG_CANDIDATES:
        if not path.is_file():
            continue
        found_any = True
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        section = data.get("sources", {}).get(name, {})
        merged.update({str(k): str(v) for k, v in section.items()})

    if not merged:
        if missing_ok:
            return {}
        hint = ", ".join(str(p) for p in CONFIG_CANDIDATES)
        msg = f"下载源 {name!r} 未配置（{'找到配置文件' if found_any else '配置文件不存在'}: {hint}）"
        raise SourceCredentialError(
            f"{msg}。请在配置中添加:\n[sources.{name}]\nendpoint = \"https://...\"\ntoken = \"...\"",
            code=25,
        )
    if require_endpoint and not merged.get("endpoint"):
        raise SourceCredentialError(
            f"下载源 {name!r} 配置缺少 endpoint（令牌可缺省，端点必填）", code=25
        )
    return merged


class ApiSource(SourceAdapter):
    """通用 API 源：配置驱动的统一 HTTP 契约（见模块 docstring）。"""

    def __init__(self, name: str):
        if name not in API_SOURCE_NAMES:
            raise ValueError(f"未知 API 源: {name}（可用: {', '.join(API_SOURCE_NAMES)}）")
        self.name = name
        self.description = f"API 源 {name}（配置驱动，端点/凭证见 sources.toml）"
        self.urlopen = urllib.request.urlopen  # 注入点：契约测试 mock 此处

    # -- 凭证自检（--dry-run） --------------------------------------------

    def check(self, query: str) -> str:
        """校验配置可用性（**不发网络请求**）；缺配置抛 SourceCredentialError(25)。

        回显 endpoint 与**掩码后**的 token，便于用户侧排错而不泄露凭证。
        """
        cfg = load_source_config(self.name)
        token = cfg.get("token", "")
        if len(token) >= 8:
            masked = f"{token[:4]}***{token[-2:]}"
        elif token:
            masked = "***"
        else:
            masked = "(无)"
        return f"{self.name}: endpoint={cfg['endpoint']}  token={masked}  query={query}"

    # -- 契约实现 ----------------------------------------------------------

    def fetch(self, query: str, out_dir: Path) -> List[FetchedFile]:
        cfg = load_source_config(self.name)
        self.fetch_or_raise_dir(out_dir)

        files = self._list_files(cfg, query)
        results: List[FetchedFile] = []
        for item in files:
            url = item.get("url", "")
            if not url:
                raise SourceRequestError(f"{self.name}: files 项缺少 url: {item}", code=26)
            name = item.get("name") or url.rstrip("/").rsplit("/", 1)[-1] or "download.bin"
            data = self._download(cfg, url)
            target = out_dir / name
            target.write_bytes(data)
            results.append(FetchedFile(path=target, source=self.name, meta=dict(item)))
        return results

    # -- HTTP（可注入测试） -------------------------------------------------

    def _list_files(self, cfg: Dict[str, str], query: str) -> List[Dict[str, str]]:
        endpoint = cfg["endpoint"].rstrip("/")
        sep = "&" if "?" in endpoint else "?"
        url = f"{endpoint}{sep}{urlencode({'id': query})}"
        body = self._http_get(url, token=cfg.get("token"))
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SourceRequestError(f"{self.name}: 响应不是合法 JSON: {exc}", code=26) from exc
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, list):
            raise SourceRequestError(
                f"{self.name}: 响应缺少 files 数组（契约见 docs/downloadhelper.md）", code=26
            )
        return [f for f in files if isinstance(f, dict)]

    def _download(self, cfg: Dict[str, str], url: str) -> bytes:
        return self._http_get(url, token=cfg.get("token"))

    def _http_get(self, url: str, token: str | None) -> bytes:
        req = urllib.request.Request(url)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        req.add_header("User-Agent", "SunoAuxTool/0.7 (post fetch)")
        try:
            with self.urlopen(req, timeout=_TIMEOUT_S) as resp:
                return resp.read()
        except SourceRequestError:
            raise
        except Exception as exc:
            raise SourceRequestError(f"{self.name}: 请求失败 {url}: {exc}", code=26) from exc
