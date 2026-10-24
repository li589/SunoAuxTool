"""音源库注册表（2.0 PR-G）。

公开 API::

    from sunoauxtool.soundbank import SoundBank, SoundBankRegistry, build_registry

- :class:`SoundBank` —— 一套音源（id/名称/路径/格式/许可）+ 结构校验；
- :class:`SoundBankRegistry` —— 扫描 ``module/``、按 id 查询、批量校验；
- :func:`build_registry` —— 一步构造（自动扫描项目 ``module/``）。

只登记与校验，**不下载、不分发**商业音源（计划书 N4）。
"""

from __future__ import annotations

from sunoauxtool.soundbank.exceptions import SoundBankError
from sunoauxtool.soundbank.registry import (
    FORMATS,
    SoundBank,
    SoundBankRegistry,
    VerifyResult,
    build_registry,
    slugify,
)

__all__ = [
    "FORMATS",
    "SoundBank",
    "SoundBankError",
    "SoundBankRegistry",
    "VerifyResult",
    "build_registry",
    "slugify",
]
