"""NCM 解包组件（1.6.1）：网易云加密容器 → 原始音频（flac/mp3），零第三方依赖。

门面：
    from sunoauxtool.download.ncm import unpack_file, parse
密码学：手写 AES-128-ECB（FIPS-197 向量锁定）+ 网易云定制 RC4 流密码
（按绝对偏移取密钥流，可分块并行）。标签/封面嵌入交给 ffmpeg（可选）。
"""

from sunoauxtool.download.ncm.exceptions import CorruptNcmError, NotNcmError
from sunoauxtool.download.ncm.stream import build_key_box, decrypt_payload, segment_key
from sunoauxtool.download.ncm.unpack import (
    NcmContent,
    parse,
    suggested_filename,
    unpack_bytes,
    unpack_file,
)

__all__ = [
    "unpack_file",
    "unpack_bytes",
    "parse",
    "NcmContent",
    "suggested_filename",
    "build_key_box",
    "segment_key",
    "decrypt_payload",
    "NotNcmError",
    "CorruptNcmError",
]
