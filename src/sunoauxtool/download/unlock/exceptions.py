"""unlock 组件专用异常：27=无法识别的加密格式，28=解密失败，29=缺少外部密钥。"""

from __future__ import annotations

from sunoauxtool.download.exceptions import SunoError


class UnknownFormatError(SunoError):
    """输入不是可识别的加密音乐格式（魔数/扩展名均不匹配）（退出码 27）。"""

    def __init__(self, msg: str) -> None:
        super().__init__(msg, code=27)


class DecryptFailedError(SunoError):
    """解密失败：数据损坏、密钥不匹配或结果不是可识别音频（退出码 28）。"""

    def __init__(self, msg: str) -> None:
        super().__init__(msg, code=28)


class KeyMissingError(SunoError):
    """需要外部密钥（如酷狗公钥文件）或在线获取 EKey（退出码 29）。"""

    def __init__(self, msg: str) -> None:
        super().__init__(msg, code=29)
