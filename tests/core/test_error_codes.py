"""P2-3 错误码单测：errors 子命令 + 新增异常类（7/8/9）。"""

from __future__ import annotations

from typer.testing import CliRunner

from sunoauxtool.cli import app
from sunoauxtool.exceptions import (
    BatchFailedError,
    BatchPartialError,
    ERROR_CODES,
    ModuleError,
)

runner = CliRunner()


def test_errors_command_lists_codes(tmp_project):
    """sunoauxtool errors 列出全部错误码 0-9。"""
    result = runner.invoke(app, ["errors"])
    assert result.exit_code == 0, result.output
    for code, name, _desc in ERROR_CODES:
        assert str(code) in result.output
    assert "渲染环境不完整" in result.output
    assert "批量部分失败" in result.output
    assert "批量全部失败" in result.output


def test_module_error_code_7():
    err = ModuleError("环境不完整", code=7)
    assert err.code == 7


def test_batch_partial_error_code_8():
    err = BatchPartialError("部分失败", code=8)
    assert err.code == 8


def test_batch_failed_error_code_9():
    err = BatchFailedError("全部失败", code=9)
    assert err.code == 9


def test_error_codes_table_complete():
    """错误码表：0-9 主干连续 + 10-14（video）+ 15/16（DSP）+ 20-24（download）
    + 25/26（API 源）+ 27-29（unlock）+ 30-34（suno-dl，R8）。

    1.4.6 B5：video/download 两段实际在用但曾漏登记（errors 命令与文档不完整）。
    1.8.0：补登记 unlock 段 27-29（1.6.3 起在用但曾漏登记）与 suno-dl 段 30-34。
    """
    codes = [c for c, _n, _d in ERROR_CODES]
    assert codes[:10] == list(range(10))
    assert set(codes) == set(range(10)) | set(range(10, 15)) | {15, 16} | set(
        range(20, 25)
    ) | {25, 26} | set(range(27, 35))


def test_error_codes_video_and_download_segments():
    """B5 回归：10-14 与 20-24 段必须登记且语义与子包异常一致。"""
    table = {c: n for c, n, _d in ERROR_CODES}
    assert table[10] == "视频 ffmpeg 不可用"
    assert table[13] == "视频渲染失败"
    assert table[22] == "输入为加密密文"
    assert table[23] == "下载转码失败"
    # 与子包异常类实际使用的退出码一致
    from sunoauxtool.download.exceptions import FFmpegNotFoundError as DlFFmpegNotFound
    from sunoauxtool.video.exceptions import RenderError as VideoRenderError

    assert DlFFmpegNotFound().code == 20
    assert VideoRenderError("boom").code == 13
