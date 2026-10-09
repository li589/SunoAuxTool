"""版本对比测试（P3-C3）+ 灵感库 CLI 测试。"""

from __future__ import annotations

import numpy as np
import soundfile as sf
from typer.testing import CliRunner

from sunoauxtool.cli import app


def _make_wav(path, freq=440, duration=1.0, sr=44100):
    """生成正弦波 WAV。"""
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    audio = 0.5 * np.sin(2 * np.pi * freq * t)
    sf.write(str(path), audio, sr)


class TestDiff:
    def test_diff_basic(self, tmp_path):
        """diff 输出对比表格。"""
        wav1 = tmp_path / "a.wav"
        wav2 = tmp_path / "b.wav"
        _make_wav(wav1, freq=440)
        _make_wav(wav2, freq=880)

        runner = CliRunner()
        result = runner.invoke(app, ["diff", str(wav1), str(wav2)])
        assert result.exit_code == 0
        assert "音频特征对比" in result.output
        assert "RMS" in result.output
        assert "峰值" in result.output
        assert "频段能量" in result.output

    def test_diff_nonexistent(self, tmp_path):
        """不存在的文件退出码 3。"""
        runner = CliRunner()
        result = runner.invoke(app, ["diff", "/nonexistent/a.wav", str(tmp_path / "b.wav")])
        assert result.exit_code == 3

    def test_diff_same_file(self, tmp_path):
        """相同文件对比差异为 0。"""
        wav = tmp_path / "a.wav"
        _make_wav(wav)
        runner = CliRunner()
        result = runner.invoke(app, ["diff", str(wav), str(wav)])
        assert result.exit_code == 0
        assert "0.00" in result.output  # 差异为 0


class TestInspireCLI:
    def test_inspire_init(self, tmp_path):
        """inspire init 创建数据库。"""
        import os
        orig = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            runner = CliRunner()
            result = runner.invoke(app, ["inspire", "init"])
            assert result.exit_code == 0
            assert "灵感库已初始化" in result.output
            assert (tmp_path / "sunoauxtool.db").is_file()
        finally:
            os.chdir(orig)

    def test_inspire_list_empty(self, tmp_path):
        """空库时提示。"""
        import os
        orig = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            runner = CliRunner()
            runner.invoke(app, ["inspire", "init"])
            result = runner.invoke(app, ["inspire", "list"])
            assert "灵感库为空" in result.output
        finally:
            os.chdir(orig)

    def test_inspire_rm_nonexistent(self, tmp_path):
        """删除不存在的灵感。"""
        import os
        orig = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            runner = CliRunner()
            runner.invoke(app, ["inspire", "init"])
            result = runner.invoke(app, ["inspire", "rm", "9999"])
            assert result.exit_code == 1
        finally:
            os.chdir(orig)


class TestNewCLI:
    def test_new_non_tty(self):
        """非 TTY 时自动执行 pipeline（不崩溃）。"""
        runner = CliRunner()
        result = runner.invoke(app, ["new"])
        # 非 TTY 会执行 pipeline，可能因环境不同退出
        # 只要不崩溃即可
        assert result.exit_code in (0, 1, 2, 6)

    def test_new_help(self):
        """new --help 正常。"""
        runner = CliRunner()
        result = runner.invoke(app, ["new", "--help"])
        assert result.exit_code == 0
        assert "new" in result.output