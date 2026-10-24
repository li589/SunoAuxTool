"""音源库异常（2.0 PR-G，错误码 41）。"""

from __future__ import annotations

from sunoauxtool.exceptions import SmartNoteGenError


class SoundBankError(SmartNoteGenError):
    """音源缺失或校验失败（41）：未注册、文件不存在、格式不识别、加载失败。"""

    code = 41
