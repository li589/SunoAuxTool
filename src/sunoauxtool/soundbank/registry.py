"""音源库注册表（2.0 PR-G）：发现 / 查询 / 校验 SF2（及未来 SFZ、SF3）。

设计原则（对应计划书 G5、N4）：

- **只登记，不分发**：本模块不下载、不内置商业音源，只描述用户机器上已有的文件
  与它们的许可信息（`license` 字段强制填写，文档免责）；
- **零重依赖**：只读文件头（``RIFF/sfbk``）做结构校验，深校验才调用 fluidsynth；
- **确定性**：扫描顺序与输出顺序固定（按 id 排序），便于 doctor/CI 比对。

2.0 只有 fluidsynth + SF2 一条路径；2.1 起 `render/backend` 可按 `SoundBank.format`
选后端（SFZ 需外部采样器）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from sunoauxtool.logging_setup import get_logger
from sunoauxtool.soundbank.exceptions import SoundBankError

logger = get_logger("soundbank.registry")

#: 支持的容器格式 → 扩展名
FORMATS = {"sf2": (".sf2",), "sf3": (".sf3",), "sfz": (".sfz",)}

#: RIFF 容器（SF2/SF3）的期望魔数
RIFF_MAGIC = b"RIFF"
SFBK_MAGIC = b"sfbk"

#: 低于该体积的文件几乎肯定是占位/截断（GeneralUser GS 约 30MB）
MIN_BYTES = 64 * 1024


@dataclass(frozen=True)
class SoundBank:
    """一套音源（当前为 SF2 文件）。"""

    id: str
    name: str
    path: Path
    format: str = "sf2"
    license: str = "unknown"
    description: str = ""

    def __post_init__(self) -> None:
        if not str(self.id).strip():
            raise SoundBankError("音源 id 不能为空", code=41)
        if self.format not in FORMATS:
            raise SoundBankError(
                f"未知音源格式 {self.format!r}；可用: {', '.join(sorted(FORMATS))}", code=41
            )
        object.__setattr__(self, "path", Path(self.path))

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    @property
    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 1)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "path": str(self.path),
            "format": self.format,
            "license": self.license,
            "description": self.description,
            "exists": self.exists,
            "size_bytes": self.size_bytes,
        }

    def verify(self) -> "VerifyResult":
        """结构校验：存在性 + 体积 + 魔数（不启动 fluidsynth）。"""
        if not self.exists:
            return VerifyResult(False, 41, f"文件不存在: {self.path}", self.size_bytes)
        size = self.size_bytes
        if self.format in ("sf2", "sf3") and size < MIN_BYTES:
            return VerifyResult(False, 41, f"文件过小（{size} 字节）：疑似占位或截断", size)
        if self.format in ("sf2", "sf3"):
            try:
                with self.path.open("rb") as handle:
                    head = handle.read(12)
            except OSError as exc:
                return VerifyResult(False, 41, f"无法读取: {exc}", size)
            if not head.startswith(RIFF_MAGIC) or head[8:12] != SFBK_MAGIC:
                return VerifyResult(
                    False, 41, f"不是 RIFF/sfbk 容器（头 12 字节: {head!r}）", size
                )
        elif self.format == "sfz":
            try:
                text = self.path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:  # pragma: no cover - 极少见
                return VerifyResult(False, 41, f"无法读取: {exc}", size)
            if "<region" not in text and "<group" not in text:
                return VerifyResult(False, 41, "SFZ 里没有 <region>/<group> 段", size)
        return VerifyResult(True, 0, "结构校验通过", size)


@dataclass(frozen=True)
class VerifyResult:
    """校验结果。"""

    ok: bool
    code: int
    message: str
    size_bytes: int = 0

    def describe(self) -> str:
        mark = "✅" if self.ok else "❌"
        return f"{mark} {self.message}"


def slugify(text: str) -> str:
    """文件名 → 音源 id（小写、非字母数字折成 ``-``，保留中文）。"""
    result: list[str] = []
    for char in str(text).strip():
        if char.isalnum():
            result.append(char.lower())
        elif result and result[-1] != "-":
            result.append("-")
    return "".join(result).strip("-") or "bank"


@dataclass
class SoundBankRegistry:
    """音源注册表：扫描目录 + 显式登记 + 按 id 查询。"""

    roots: Sequence[Path] = field(default_factory=tuple)
    entries: list[SoundBank] = field(default_factory=list)

    def add(self, bank: SoundBank) -> SoundBank:
        """登记一套音源（id 冲突时抛 41，避免静默覆盖）。"""
        if any(existing.id == bank.id for existing in self.entries):
            raise SoundBankError(f"音源 id 已存在: {bank.id!r}", code=41)
        self.entries.append(bank)
        return bank

    def discover(self, roots: Sequence[Path] | None = None) -> list[SoundBank]:
        """递归扫描目录里的 SF2/SF3/SFZ，按 id 去重后登记。"""
        base_roots = list(roots if roots is not None else self.roots)
        found: list[SoundBank] = []
        for root in base_roots:
            root_path = Path(root)
            if not root_path.is_dir():
                continue
            for fmt, suffixes in FORMATS.items():
                if fmt == "sfz":  # SFZ 尚未支持渲染，先不登记
                    continue
                for suffix in suffixes:
                    for path in sorted(root_path.rglob(f"*{suffix}")):
                        found.append(_bank_from_path(path, fmt))
        found.sort(key=lambda bank: (bank.id, str(bank.path)))
        counts: dict[str, int] = {}
        for bank in found:
            counts[bank.id] = counts.get(bank.id, 0) + 1
        for bank in found:
            if counts[bank.id] > 1:
                # 同名不同文件：每个都追加父目录名，避免"谁先谁得 id"的不确定
                bank = SoundBank(
                    id=f"{bank.id}-{slugify(bank.path.parent.name)}",
                    name=bank.name, path=bank.path, format=bank.format,
                    license=bank.license, description=bank.description,
                )
            if any(existing.id == bank.id for existing in self.entries):
                logger.debug("跳过重复音源: %s", bank.path)
                continue
            self.entries.append(bank)
        self.entries.sort(key=lambda bank: bank.id)
        return list(self.entries)

    def list(self) -> list[SoundBank]:
        """全部登记项（按 id 排序）。"""
        return sorted(self.entries, key=lambda bank: bank.id)

    def get(self, bank_id: str) -> SoundBank:
        """按 id 取音源，未登记抛 41。"""
        for bank in self.entries:
            if bank.id == bank_id:
                return bank
        known = ", ".join(bank.id for bank in self.list()) or "（空）"
        raise SoundBankError(
            f"未注册的音源 {bank_id!r}；已注册: {known}\n"
            "  提示：`sunoaux soundbank list` 查看可用 id，或用 --bank 指定绝对路径",
            code=41,
        )

    def available(self) -> list[SoundBank]:
        """结构校验通过的音源。"""
        return [bank for bank in self.list() if bank.verify().ok]

    def default(self) -> SoundBank | None:
        """默认音源：第一个可用的（与配置解耦，供 doctor/自动选择）。"""
        available = self.available()
        return available[0] if available else None

    def verify_all(self) -> dict[str, VerifyResult]:
        """逐个校验（id → 结果）。"""
        return {bank.id: bank.verify() for bank in self.list()}


#: 常见免费音源的许可说明（只做提示，不代替原许可证）
LICENSE_HINTS = {
    "generaluser": "GeneralUser GS（免费使用，禁止转售音源本体）",
    "fluidr3": "FluidR3 GM（MIT/Apache 混合，可再分发）",
    "colombo": "Colombo GMGS2（免费使用，仅限个人/非商业分发限制见原文档）",
    "gmgs": "Colombo GMGS2（免费使用，见原文档）",
    "timidity": "Timidity GM（免费，随发行版分发）",
}


def _license_of(stem: str) -> str:
    lowered = stem.lower()
    for key, text in LICENSE_HINTS.items():
        if key in lowered:
            return text
    return "unknown（请自行确认许可）"


def _bank_from_path(path: Path, fmt: str) -> SoundBank:
    """由文件路径推断 id/名称/许可。"""
    stem = path.stem
    name = stem.replace("_", " ").replace("-", " ")
    license_hint = _license_of(stem)
    return SoundBank(
        id=slugify(stem),
        name=name,
        path=path,
        format=fmt,
        license=license_hint,
        description=f"于 {path.parent.name}/ 发现",
    )


def build_registry(
    roots: Iterable[Path] | None = None, *, extra: Iterable[SoundBank] = ()
) -> SoundBankRegistry:
    """便捷构造：给定扫描根（默认项目 ``module/``）并登记额外音源。"""
    if roots is None:
        from sunoauxtool.env import ProjectRootResolver

        roots = [ProjectRootResolver().resolve() / "module"]
    registry = SoundBankRegistry(roots=tuple(Path(root) for root in roots))
    for bank in extra:
        registry.add(bank)
    registry.discover()
    return registry
