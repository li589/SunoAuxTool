"""渲染后端异常（2.0 PR-E，错误码 42/43）。"""

from __future__ import annotations

from sunoauxtool.exceptions import SmartNoteGenError


class BackendUnavailableError(SmartNoteGenError):
    """渲染后端不可用（42）：二进制缺失、音色库不可加载、后端自检失败。"""

    code = 42


class MixdownError(SmartNoteGenError):
    """分轨渲染或混音失败（43）：stem 写入失败、长度不一致、混音输出不可写。"""

    code = 43
