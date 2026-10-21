"""sunoauxtool.meta 异常（R9，错误码 35-36 段）。"""

from __future__ import annotations

from sunoauxtool.exceptions import SmartNoteGenError


class MetaError(SmartNoteGenError):
    """meta 清理基础异常。"""


class UnsupportedContainerError(MetaError):
    """容器不是受支持的音频格式，或结构损坏不可解析 -> 35。"""

    def __init__(self, path: str, detail: str = "") -> None:
        super().__init__(
            f"不支持的容器或无法解析: {path}"
            + (f"（{detail}）" if detail else "")
            + "（支持: wav / mp3 / flac / m4a / ogg / opus）",
            code=35,
        )


class MetaWriteError(MetaError):
    """标签读写/重写失败 -> 36。"""

    def __init__(self, path: str, detail: str) -> None:
        super().__init__(f"标签处理失败: {path}（{detail}）", code=36)
