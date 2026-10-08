"""通用转码中枢（1.6.0）：音频互转 + 视频分离音频。

门面：
    from sunoauxtool.download.convert import convert_audio, extract_audio
引擎复用 transcoder.find_ffmpeg（SUNO_FFMPEG / SMARTNOTEGEN_FFMPEG 两层环境变量）。
"""

from sunoauxtool.download.convert.audio import SUPPORTED_AUDIO_FMTS, convert_audio
from sunoauxtool.download.convert.profiles import PROFILES, OutputProfile
from sunoauxtool.download.convert.video_extract import SUPPORTED_VIDEO_EXTS, extract_audio

__all__ = [
    "convert_audio",
    "extract_audio",
    "SUPPORTED_AUDIO_FMTS",
    "SUPPORTED_VIDEO_EXTS",
    "PROFILES",
    "OutputProfile",
]
