"""NCM 专用异常：25=非 NCM 容器，26=容器损坏。"""

from __future__ import annotations

from sunoauxtool.download.exceptions import SunoError


class NotNcmError(SunoError):
    """输入不是 NCM 容器（缺少魔数等）（退出码 25）。"""

    def __init__(self, msg: str) -> None:
        super().__init__(msg, code=25)


class CorruptNcmError(SunoError):
    """NCM 容器损坏 / 密钥段解密失败 / 载荷签名无法识别（退出码 26）。"""

    def __init__(self, msg: str) -> None:
        super().__init__(msg, code=26)
