"""自定义异常与错误码定义。

错误码约定（分段注册制，1.4.6 B5 补全 video/download 段）：
    0 成功
    1 参数错误/通用错误
    2 配置错误
    3 输入文件错误
    4 渲染失败
    5 导出失败
    6 AI 模块不可用
    7 渲染环境不完整（module 缺失/损坏、fluidsynth 不可执行、SF2 不可加载）
    8 批量部分失败
    9 批量全部失败
    10-14 sunoauxtool.video（视频合成）
    15 DSP 处理失败 / 16 DSP 参数错误
    20-24 sunoauxtool.download（猫抓取回转码）
    25 下载源凭证缺失 / 26 下载源请求失败
    27-29 sunoauxtool.download.unlock（通用解密域）
    30-34 sunoauxtool.download.suno_dl（Suno 官方下载三段式）
"""

from __future__ import annotations

from typing import List, Optional, Tuple

#: 错误码表（供 `sunoauxtool errors` 子命令与文档使用）
ERROR_CODES: List[Tuple[int, str, str]] = [
    (0, "成功", ""),
    (1, "参数错误/通用错误", "未知子命令、非法参数组合、非法 DSP 参数（ratio<1、fade 越界）"),
    (2, "配置错误", "配置文件缺失/非法、用户显式配置的路径无效"),
    (3, "输入文件错误", ".mid/.wav 不存在或无法解析"),
    (4, "渲染失败", "fluidsynth 进程非零退出（加载后执行失败）"),
    (5, "导出失败", "时长越界、MP3 编码器缺失"),
    (6, "AI 模块不可用", "依赖未装、显存不足、DiffRhythm NO-GO"),
    (7, "渲染环境不完整", "module 缺失/损坏、fluidsynth 不可执行、SF2 不可加载"),
    (8, "批量部分失败", "batch 部分项成功部分失败"),
    (9, "批量全部失败", "batch 全部项失败"),
    # ---- sunoauxtool.video 段（10-14，语义见 video/exceptions.py）----
    (10, "视频 ffmpeg 不可用", "ffmpeg 未找到（PATH / SUNO_FFMPEG / --ffmpeg-path 均未命中）"),
    (11, "视频分辨率/比例不支持", "预设或 --width/--height 组合非法"),
    (12, "视频输入音频缺失", "无法读取输入音频文件"),
    (13, "视频渲染失败", "ffmpeg 进程非零退出"),
    (14, "视频输出路径不可写", "视频/封面输出目录不存在或无写权限"),
    # ---- 通用 DSP 段 ----
    (15, "DSP 处理失败", "算子运行期错误（读写音频、重采样、算子链执行失败）"),
    (16, "DSP 参数错误", "ops 串解析失败、算子参数非法（trim 越界、fade<0 等）"),
    # ---- sunoauxtool.download 段（20-24，语义见 download/exceptions.py）----
    (20, "下载 ffmpeg 不可用", "ffmpeg 未找到（SUNO_FFMPEG / SUNO_FFMPEG_DIRS / PATH / --ffmpeg-path）"),
    (21, "非 fragmented MP4", "输入不是可解析的 fMP4（猫抓缓存误标 .mp3 实为 Opus 需先转码）"),
    (22, "输入为加密密文", "取证判定为强加密（卡方 χ²≤360），无密钥不可解码，改用缓存明文副本"),
    (23, "下载转码失败", "ffmpeg 转码进程非零退出"),
    (24, "下载输出路径不可写", "输出目录不存在或无写权限"),
    # ---- 下载源 API 段 ----
    (25, "下载源凭证缺失", "API 下载源（suno-api/haimeng/tianyin）未配置凭证"),
    (26, "下载源请求失败", "API 下载源网络请求/响应解析失败"),
    # ---- download.unlock 段（27-29，语义见 download/unlock/exceptions.py）----
    (27, "无法识别的加密格式", "unlock：魔数/扩展名均不匹配"),
    (28, "解密失败", "unlock：数据损坏、密钥不匹配或结果非可识别音频"),
    (29, "缺少外部密钥", "unlock：需外部密钥文件或在线 EKey"),
    # ---- suno-dl 段（30-34，语义见 download/suno_dl.py）----
    (30, "suno-dl 凭证缺失", "CDN 快路径失败且无 Cookie 进入载荷/轮询路径（--cookie / SUNO_DL_COOKIE / 配置）"),
    (31, "suno-dl 请求失败", "HTTP/网络错误（载荷、authorize、轮询、直链取回）"),
    (32, "suno-dl 响应解析失败", "响应非 JSON 或载荷缺预期字段"),
    (33, "suno-dl 轮询超时", "download/clip ≤90×2s 或 wav 转换 ≤24×5s 耗尽仍未 ready"),
    (34, "suno-dl 全部路径失败", "CDN 直拼 / 载荷直链 / 官方轮询三段式均未取得音频"),
]


class SmartNoteGenError(Exception):
    """所有 SmartNoteGen 自定义异常的基类。

    .. note:: 更名兼容：类名保留（外部 ``except SmartNoteGenError`` 不破坏），
       新代码可用下面的 ``SunoAuxToolError`` 别名，两者是同一对象。
    """

    #: 默认错误码（1 = 参数/通用错误）
    code: int = 1

    def __init__(self, message: str, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code

    def __str__(self) -> str:  # pragma: no cover - 简单透传
        return self.message


#: 更名后的推荐写法（同一类的别名，不是子类）
SunoAuxToolError = SmartNoteGenError


class ParameterError(SmartNoteGenError):
    """参数错误：非法参数组合、未知参数值、越界时长等。"""

    code = 1


class ConfigError(SmartNoteGenError):
    """配置错误：配置文件不存在/格式非法、SoundFont 路径无效等。"""

    code = 2


class InputFileError(SmartNoteGenError):
    """输入文件错误：.mid/.wav 不存在或无法解析。"""

    code = 3


class RenderError(SmartNoteGenError):
    """渲染失败：fluidsynth 未安装/找不到、渲染进程非零退出。"""

    code = 4


class ExportError(SmartNoteGenError):
    """导出失败：时长不在 10–30s、MP3 编码器缺失等。"""

    code = 5


class AiDependencyError(SmartNoteGenError):
    """AI 模块不可用：P1 依赖未安装、显存不足（DiffRhythm）。"""

    code = 6


class ModuleError(SmartNoteGenError):
    """渲染环境不完整：module 缺失/损坏、fluidsynth 不可执行、SF2 不可加载。

    与 ConfigError(2) 的区分：默认 module 环境（非用户显式配置）缺失/损坏用本异常；
    用户显式配置的路径无效仍用 ConfigError(2)。
    """

    code = 7


class BatchPartialError(SmartNoteGenError):
    """批量部分失败：batch 部分项成功部分失败。"""

    code = 8


class BatchFailedError(SmartNoteGenError):
    """批量全部失败：batch 全部项失败。"""

    code = 9


class DspError(SmartNoteGenError):
    """DSP 处理失败（R6）：算子运行期错误（读写/重采样/算子链执行）。"""

    code = 15


class DspParamError(SmartNoteGenError):
    """DSP 参数错误（R6）：ops 串解析失败、算子参数非法。"""

    code = 16


class SourceCredentialError(SmartNoteGenError):
    """下载源凭证缺失（R7）：API 下载源未配置凭证。"""

    code = 25


class SourceRequestError(SmartNoteGenError):
    """下载源请求失败（R7）：API 下载源网络请求/响应解析失败。"""

    code = 26
