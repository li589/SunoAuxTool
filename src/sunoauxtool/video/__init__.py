"""sunoauxtool.video 包。

SmartNoteGen 音乐视频 / 音频可视化生成器。
消费 SNG 产出的 WAV + metadata.json，合成为发布级 MP4。
v0.3：支持 WAV/MP3/FLAC/OGG/MIDI 多格式输入与多轨混音、分轨可视化。
"""

from __future__ import annotations

__version__ = "1.4.0"
__all__ = ["__version__"]

# 顶层导入，方便使用
from .videomaker import video, video_multitrack, VideoResult
from .config import Config
from .exceptions import VideoMakerError

__all__ += ["video", "video_multitrack", "VideoResult", "Config", "VideoMakerError"]
