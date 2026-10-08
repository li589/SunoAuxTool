"""sunoauxtool.video 配置：VideoConfig dataclass。

合并优先级（低 → 高）：
    内置默认值 < 配置文件（sunoauxtool.video.toml）< CLI 参数

与 SmartNoteGen Config 范式一致，可复用同一 TOML 文件的 [video] 节。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import tomli_w


# ---------------------------------------------------------------------------
# 内置默认值
# ---------------------------------------------------------------------------

DEFAULT_PATHS = {
    "ffmpeg": "ffmpeg",
    "output_dir": "output",
}

DEFAULT_VIDEO = {
    "width": 1080,
    "height": 1920,  # 9:16 默认竖屏（短视频）
    "fps": 30,
    "crf": 18,
    "preset": "medium",
}

DEFAULT_TEXT = {
    "title": "",
    "subtitle": "",
    "font": "",
    "font_size": 48,
    "font_color": "white",
    "stroke_color": "black",
    "stroke_width": 2,
}

DEFAULT_BACKGROUND = {
    "type": "gradient",  # solid | gradient | image | video
    "solid_color": "#1a1a2e",
    "gradient_start": "#1a1a2e",
    "gradient_end": "#16213e",
    "image": "",
    "video": "",
}

DEFAULT_VISUAL = {
    "style": "waveform",  # waveform | spectrum | circular_spectrum | reactive
    "color": "#4a9eff",
    "position": "center",  # top | center | bottom | fill
    "mirror": True,
}

DEFAULT_LOGO = {
    "path": "",            # PNG 路径（空=不叠加）
    "position": "bottom-right",  # top-left | top-right | bottom-left | bottom-right
    "margin": 40,          # 边距（像素，按 1080 宽基准缩放）
    "scale": 0.12,         # logo 宽度占视频宽度的比例
    "opacity": 1.0,        # 不透明度 0-1
}

DEFAULT_PRESET = "douyin"  # douyin | youtube | instagram | official

DEFAULT_OUTPUT = {
    "layout": "project-date",
    "project": "default",
    "naming": "{style}_{bpm}_{seed}_{seq}",
    "metadata": True,
}


@dataclass
class PathsConfig:
    ffmpeg: str = "ffmpeg"
    output_dir: str = "output"


@dataclass
class VideoConfig:
    width: int = 1080
    height: int = 1920
    fps: int = 30
    crf: int = 18
    preset: str = "medium"


@dataclass
class TextConfig:
    title: str = ""
    subtitle: str = ""
    font: str = ""
    font_size: int = 48
    font_color: str = "white"
    stroke_color: str = "black"
    stroke_width: int = 2


@dataclass
class BackgroundConfig:
    type: str = "gradient"
    solid_color: str = "#1a1a2e"
    gradient_start: str = "#1a1a2e"
    gradient_end: str = "#16213e"
    image: str = ""
    video: str = ""


@dataclass
class VisualConfig:
    style: str = "waveform"
    color: str = "#4a9eff"
    position: str = "center"
    mirror: bool = True


@dataclass
class LogoConfig:
    path: str = ""
    position: str = "bottom-right"
    margin: int = 40
    scale: float = 0.12
    opacity: float = 1.0


@dataclass
class OutputConfig:
    layout: str = "project-date"
    project: str = "default"
    naming: str = "{style}_{bpm}_{seed}_{seq}"
    metadata: bool = True


@dataclass
class Config:
    paths: PathsConfig = field(default_factory=PathsConfig)
    video: VideoConfig = field(default_factory=VideoConfig)
    text: TextConfig = field(default_factory=TextConfig)
    background: BackgroundConfig = field(default_factory=BackgroundConfig)
    visual: VisualConfig = field(default_factory=VisualConfig)
    logo: LogoConfig = field(default_factory=LogoConfig)
    preset: str = DEFAULT_PRESET
    output: OutputConfig = field(default_factory=OutputConfig)

    # 可选：从 SmartNoteGen 元数据继承
    sng_metadata_path: Optional[str] = None
    chords: str = ""
    bpm: int = 0
    seed: Optional[int] = None
    style: str = ""

    @classmethod
    def load(cls, path: Optional[str | Path] = None) -> "Config":
        """从 TOML 文件加载（如存在）。None 时使用内置默认值。"""
        if path is None:
            return cls()
        p = Path(path).expanduser()
        if not p.exists():
            return cls()
        try:
            import tomllib
            data = tomllib.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return cls()
        return cls._merge(data)

    @classmethod
    def _merge(cls, data: Dict[str, Any]) -> "Config":
        cfg = cls()
        if "paths" in data:
            d = data["paths"]
            cfg.paths = PathsConfig(ffmpeg=d.get("ffmpeg", cfg.paths.ffmpeg),
                                    output_dir=d.get("output_dir", cfg.paths.output_dir))
        if "video" in data:
            d = data["video"]
            cfg.video = VideoConfig(width=d.get("width", cfg.video.width),
                                    height=d.get("height", cfg.video.height),
                                    fps=d.get("fps", cfg.video.fps),
                                    crf=d.get("crf", cfg.video.crf),
                                    preset=d.get("preset", cfg.video.preset))
        if "text" in data:
            d = data["text"]
            cfg.text = TextConfig(title=d.get("title", cfg.text.title),
                                  subtitle=d.get("subtitle", cfg.text.subtitle),
                                  font=d.get("font", cfg.text.font),
                                  font_size=d.get("font_size", cfg.text.font_size),
                                  font_color=d.get("font_color", cfg.text.font_color),
                                  stroke_color=d.get("stroke_color", cfg.text.stroke_color),
                                  stroke_width=d.get("stroke_width", cfg.text.stroke_width))
        if "background" in data:
            d = data["background"]
            cfg.background = BackgroundConfig(type=d.get("type", cfg.background.type),
                                              solid_color=d.get("solid_color", cfg.background.solid_color),
                                              gradient_start=d.get("gradient_start", cfg.background.gradient_start),
                                              gradient_end=d.get("gradient_end", cfg.background.gradient_end),
                                              image=d.get("image", cfg.background.image),
                                              video=d.get("video", cfg.background.video))
        if "visual" in data:
            d = data["visual"]
            cfg.visual = VisualConfig(style=d.get("style", cfg.visual.style),
                                      color=d.get("color", cfg.visual.color),
                                      position=d.get("position", cfg.visual.position),
                                      mirror=d.get("mirror", cfg.visual.mirror))
        if "logo" in data:
            d = data["logo"]
            cfg.logo = LogoConfig(path=d.get("path", cfg.logo.path),
                                  position=d.get("position", cfg.logo.position),
                                  margin=d.get("margin", cfg.logo.margin),
                                  scale=d.get("scale", cfg.logo.scale),
                                  opacity=d.get("opacity", cfg.logo.opacity))
        if "preset" in data:
            cfg.preset = data["preset"]
        if "output" in data:
            d = data["output"]
            cfg.output = OutputConfig(layout=d.get("layout", cfg.output.layout),
                                      project=d.get("project", cfg.output.project),
                                      naming=d.get("naming", cfg.output.naming),
                                      metadata=d.get("metadata", cfg.output.metadata))
        if "sng" in data:
            sng = data["sng"]
            cfg.sng_metadata_path = sng.get("metadata_path")
            cfg.chords = sng.get("chords", cfg.chords)
            cfg.bpm = sng.get("bpm", cfg.bpm)
            cfg.seed = sng.get("seed")
            cfg.style = sng.get("style", cfg.style)
        return cfg

    def write_template(self, path: str | Path) -> str:
        """生成带注释的默认配置模板。

        1.4.7 修复（F3 测试发现）：默认 Config 含 None 字段（如 logo.path），
        asdict 后直接 tomli_w.dumps 会抛 "NoneType is not TOML serializable"；
        序列化前递归剔除 None（省略 = 用内置默认值，模板语义不变）。
        """
        default = self._to_dict()

        def _strip_nones(obj: Any) -> Any:
            if isinstance(obj, dict):
                return {k: _strip_nones(v) for k, v in obj.items() if v is not None}
            return obj

        template = tomli_w.dumps(_strip_nones(default))
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(template, encoding="utf-8")
        return str(p)

    def _to_dict(self) -> Dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)
