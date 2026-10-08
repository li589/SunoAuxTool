"""1.4.7 F3：sunoauxtool.video CLI 转发面测试。

覆盖 1.4.5 新转发的 presets / config / version 三个子命令（此前 0 测试，
video/cli.py 覆盖率仅 29%），以及聚合入口 `sunoaux post video ...` 的二次注册。
"""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from sunoauxtool.aggregate import app as aggregate_app
from sunoauxtool.video import __version__ as video_version
from sunoauxtool.video import cli as video_cli

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _out(result) -> str:
    """剥 ANSI（Linux rich 彩色输出）后的输出文本。"""
    return _ANSI.sub("", result.output)


# ---------------------------------------------------------------------------
# 直接入口（sunoauxtool.video.cli.app）
# ---------------------------------------------------------------------------


def test_presets_lists_all_platforms():
    result = runner.invoke(video_cli.app, ["presets"])
    assert result.exit_code == 0, result.output
    out = _out(result)
    for platform in ("douyin", "youtube", "instagram", "official"):
        assert platform in out


def test_presets_detail_by_name():
    result = runner.invoke(video_cli.app, ["presets", "--name", "douyin"])
    assert result.exit_code == 0, result.output
    out = _out(result)
    assert "预设: douyin" in out
    # 详情应含分辨率字段
    assert "width" in out or "1080" in out


def test_config_show_prints_effective_config():
    result = runner.invoke(video_cli.app, ["config", "show"])
    assert result.exit_code == 0, result.output
    assert "当前配置" in _out(result)


def test_config_init_writes_template(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(video_cli.app, ["config", "init"])
    assert result.exit_code == 0, result.output
    assert "配置文件已生成" in _out(result)
    assert (tmp_path / "sunoauxtool.video.toml").is_file()


def test_config_init_custom_path(tmp_path):
    target = tmp_path / "custom.toml"
    result = runner.invoke(video_cli.app, ["config", "init", "--path", str(target)])
    assert result.exit_code == 0, result.output
    assert target.is_file()


def test_config_unknown_action_exits_1():
    result = runner.invoke(video_cli.app, ["config", "bogus"])
    assert result.exit_code == 1
    assert "未知操作" in _out(result)


def test_version_matches_package():
    result = runner.invoke(video_cli.app, ["version"])
    assert result.exit_code == 0, result.output
    assert video_version in _out(result)
    assert _out(result).startswith("sunoauxtool.video")


def test_callback_version_flag():
    result = runner.invoke(video_cli.app, ["--version"])
    assert result.exit_code == 0, result.output
    assert video_version in _out(result)


def test_render_missing_audio_fails(tmp_path):
    """render 对不存在音频走失败路径（AudioReadError -> 非零退出），不真渲染。"""
    result = runner.invoke(
        video_cli.app, ["render", str(tmp_path / "nope.wav"), "-o", str(tmp_path / "o.mp4")]
    )
    assert result.exit_code != 0


def test_multi_unknown_preset_rejected(tmp_path):
    """multi 的未知预设前置校验（不进入渲染）。"""
    result = runner.invoke(
        video_cli.app,
        [
            "multi", str(tmp_path / "nope.wav"),
            "--presets", "douyin,not_a_platform",
        ],
    )
    assert result.exit_code == 1
    assert "未知预设" in _out(result)


# ---------------------------------------------------------------------------
# 聚合入口（sunoaux post video ...）——1.4.5 二次注册转发面
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args", [["presets"], ["config", "show"], ["version"]]
)
def test_aggregate_post_video_forwards(args):
    result = runner.invoke(aggregate_app, ["post", "video", *args])
    assert result.exit_code == 0, result.output


def test_aggregate_post_video_version_matches():
    result = runner.invoke(aggregate_app, ["post", "video", "version"])
    assert video_version in _out(result)
