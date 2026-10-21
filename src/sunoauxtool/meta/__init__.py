"""sunoauxtool.meta：音频元数据清除 / 保留 / 覆写（R9）。

三级模式（``--mode``）：
    all         移除**所有**标签与溯源信息：WAV 的 LIST/INFO、id3 chunk、C2PA
                （Content Credentials，硬绑定随之失效）；MP3 的 ID3v2/v1、APEv2、
                Lyrics3；FLAC 的 Vorbis comment + 图片块；M4A 的 udta 元数据。
                同时解除 Windows「计算机保护」——删除 zone.Identifier（MOTW，
                文件的「来自 Internet」安全区标记），详见 :func:`strip_motw`。
    keep-basic  只保留 标题/作者/专辑/年份/歌词（+流派），移除其余（C2PA、
                ISFT、COMM、封面、soft-binding 相关自定义字段等）。
    custom      用 ``--title/--artist/--album/--lyrics/--comment/--cover/
                --genre/--year`` 覆写全部字段（未提供的字段被清空）。

诚实边界（不夸大）：
    - C2PA 的 **hard binding**（manifest 内 sha256）随 manifest 移除而失效；
    - C2PA 的 **soft binding**（com.suno.fingerprint.v4 音频域水印）不在标签层，
      本地无法「擦除」，只能靠重编码劣化——本工具不做隐晦承诺，需要时请自行
      重编码并知悉音质损失；
    - MOTW 仅在 Windows NTFS 上可删（其他平台自动跳过并如实报告）。

错误码 35（容器不支持/不可解析）/ 36（标签读写失败）；批量部分失败 8 / 全失败 9。
"""

from __future__ import annotations

from .sanitize import (
    MODES,
    SanitizeReport,
    SanitizeSpec,
    sanitize_file,
    strip_motw,
)

__all__ = [
    "MODES",
    "SanitizeReport",
    "SanitizeSpec",
    "sanitize_file",
    "strip_motw",
]
