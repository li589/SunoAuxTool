"""渲染后端协议（2.0 PR-E）。

把"渲染一首曲子"从"调用 fluidsynth"里解耦出来：

- :class:`RenderBackend` —— 后端协议（``available()`` + ``render(request)``）；
- :class:`RenderCapabilities` —— 能力自述（是否支持分轨/多库/采样率），供上层做可行性判断；
- :class:`FluidSynthBackend` —— 现有 fluidsynth 的适配实现（**不修改** 旧
  :class:`sunoauxtool.render.fluidsynth.FluidSynthRenderer`，只组合调用）。

后续（2.1 / 2.2）可加 SFZ、外部采样器、VST 宿主后端，上层代码不用动。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from sunoauxtool.exceptions import SmartNoteGenError
from sunoauxtool.logging_setup import get_logger
from sunoauxtool.render.exceptions import BackendUnavailableError
from sunoauxtool.render.fluidsynth import FluidSynthRenderer

logger = get_logger("render.backend")


@dataclass(frozen=True)
class RenderCapabilities:
    """后端能力自述。"""

    name: str = "fluidsynth"
    supports_stems: bool = True
    supports_multi_bank: bool = False
    sample_rates: tuple[int, ...] = (44100, 48000)
    notes: str = ""

    def describe(self) -> str:
        features = [f"采样率 {', '.join(str(rate) for rate in self.sample_rates)}"]
        features.append("分轨" if self.supports_stems else "仅成品")
        features.append("多音源库" if self.supports_multi_bank else "单音源库")
        return f"{self.name}: " + "｜".join(features)


@dataclass
class RenderRequest:
    """一次渲染请求（单轨/整曲通用：上层把要渲染的内容先写成 MIDI）。"""

    midi_path: Path
    out_path: Path
    soundfont: str
    sample_rate: int = 44100
    gain: float = 0.6
    meta: dict = field(default_factory=dict)


@runtime_checkable
class RenderBackend(Protocol):
    """渲染后端协议（结构类型，鸭子兼容即可用）。"""

    capabilities: RenderCapabilities

    def available(self) -> bool:
        """后端是否可用（不抛异常，只返回布尔）。"""
        ...

    def render(self, request: RenderRequest) -> Path:
        """渲染为音频文件，返回实际产物路径。"""
        ...


class FluidSynthBackend:
    """fluidsynth 后端：组合旧渲染器，保持零行为变化。"""

    capabilities = RenderCapabilities(
        name="fluidsynth",
        supports_stems=True,
        supports_multi_bank=False,
        sample_rates=(44100, 48000),
        notes="内置 GeneralUser GS / FluidR3 等 SF2",
    )

    def __init__(
        self,
        fluidsynth_path: Optional[str] = None,
        *,
        soundfont_backup: Optional[str] = None,
        project_root: Optional[Path] = None,
        gain: float = 0.6,
    ) -> None:
        self._renderer = FluidSynthRenderer(
            fluidsynth_path=fluidsynth_path,
            gain=gain,
            soundfont_backup=soundfont_backup,
            project_root=project_root,
        )
        self.gain = gain

    def available(self) -> bool:
        """探测 fluidsynth 可执行文件是否可解析（不抛异常）。"""
        try:
            return bool(self._renderer._resolve_fluidsynth())  # noqa: SLF001 - 探测专用
        except (SmartNoteGenError, OSError) as exc:
            logger.debug("fluidsynth 不可用: %s", exc)
            return False

    def require(self) -> None:
        """确认后端可用，否则抛 42。"""
        if not self.available():
            raise BackendUnavailableError(
                "渲染后端不可用: 未找到 fluidsynth\n"
                "  修复：--fluidsynth <绝对路径>，或把 fluidsynth 加入 PATH，"
                "或在配置 [paths] fluidsynth 指定（module/fluidsynth/bin 亦可）",
                code=42,
            )

    def render(self, request: RenderRequest) -> Path:
        """渲染单个 MIDI（渲染失败仍是旧语义 RenderError(4)）。"""
        self.require()
        out = self._renderer.render(
            str(request.midi_path),
            request.soundfont,
            str(request.out_path.with_suffix(".wav")),
        )
        return Path(out)


def capabilities_of(backend: object) -> RenderCapabilities:
    """读取后端能力（非协议对象回退默认值，便于测试替身）。"""
    caps = getattr(backend, "capabilities", None)
    return caps if isinstance(caps, RenderCapabilities) else RenderCapabilities()
