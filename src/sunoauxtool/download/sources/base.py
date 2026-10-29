"""下载源适配器基类（R7）。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List


@dataclass
class FetchedFile:
    """一次取回产出的单个文件。"""

    path: Path
    source: str
    meta: Dict[str, str] = field(default_factory=dict)


class SourceAdapter(ABC):
    """下载源适配器接口：``fetch(query, out_dir) -> [FetchedFile]``。

    query 语义由各源自定义：catcatch = 目录路径；API 源 = 歌曲/任务 ID。
    """

    #: 源标识（--source 取值）
    name: str = ""
    #: 一句话能力说明（CLI help 用）
    description: str = ""

    @abstractmethod
    def fetch(self, query: str, out_dir: Path) -> List[FetchedFile]:
        """执行取回；失败抛 SmartNoteGenError 子类（退出码见 exceptions.py）。"""

    def fetch_or_raise_dir(self, out_dir: Path) -> Path:
        """确保输出目录存在（幂等）。"""
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir

    def check(self, query: str) -> str:
        """凭证/可用性自检（``--dry-run``）：返回一条人类可读结论。

        默认实现：本地源，无需凭证。需要凭证的源应覆盖此方法，
        凭证缺失/不可用抛 ``SourceCredentialError(25)``；**不得回显明文凭证**。
        """
        return f"{self.name or 'source'}: 本地源，无需凭证"


def discover_sources() -> Dict[str, SourceAdapter]:
    """全部已注册源 ``{name: adapter}`` = 内置硬编码 + entry point 插件。

    扩展点：``sunoauxtool.download_sources``（见 :mod:`sunoauxtool.plugins`）。
    """
    from sunoauxtool.download.sources.api import API_SOURCE_NAMES, ApiSource
    from sunoauxtool.download.sources.catcatch import CatCatchSource
    from sunoauxtool.download.sources.relay_music import RelayMusicSource
    from sunoauxtool.download.sources.suno_web import SunoWebSource
    from sunoauxtool.download.sources.usesuno import UseSunoSource

    from sunoauxtool.plugins import discover

    builtins: Dict[str, SourceAdapter] = {"catcatch": CatCatchSource()}
    for name in API_SOURCE_NAMES:
        builtins[name] = ApiSource(name)
    builtins["usesuno"] = UseSunoSource()
    builtins["relay-music"] = RelayMusicSource()
    builtins["suno-web"] = SunoWebSource()
    return discover("download_sources", builtins, base=SourceAdapter)


def list_sources() -> List[SourceAdapter]:
    """全部已注册源（兼容旧签名）= ``discover_sources()`` 的值列表。"""
    return list(discover_sources().values())
