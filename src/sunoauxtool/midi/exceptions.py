"""midi 子包异常：通道资源类错误（错误码 45）。"""

from __future__ import annotations

from sunoauxtool.exceptions import SmartNoteGenError


class ChannelAllocationError(SmartNoteGenError):
    """通道分配冲突或资源不足（鼓轨冲突 / 旋律通道耗尽）。

    错误码 45（编曲段）：与参数错误（1）区分，便于调用方在批处理中单独统计。
    """

    code = 45
