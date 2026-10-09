"""视频预览：缩略帧 + 时间轴 scrub（R15）测试。

``runner`` 是命令执行器的**注入点**，故全部用例用 stub 替身，
不依赖真实 ffmpeg/ffprobe、不产生真实视频产物。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from sunoauxtool.cli import app
from sunoauxtool.preview import (
    PREVIEW_DIR_NAME,
    PreviewFrame,
    VideoPreviewGenerator,
    build_video_preview_html,
    extract_preview_frames,
    probe_duration,
)

runner = CliRunner()


def _stub(duration: str = "12.5"):
    """返回 (runner, calls)；ffmpeg 分支会把输出文件写成最小 jpg 占位。"""
    calls: list = []

    def run(cmd):
        calls.append(list(cmd))
        if str(cmd[0]).endswith("ffprobe"):
            return duration
        Path(cmd[-1]).write_bytes(b"\xff\xd8\xff\xd9")  # jpg 魔数占位
        return ""

    return run, calls


def _video(tmp_path: Path, name: str = "demo.mp4") -> Path:
    p = tmp_path / name
    p.write_bytes(b"\x00")
    return p


# ---------------------------------------------------------------------------
# probe_duration
# ---------------------------------------------------------------------------


def test_probe_duration_parses_float(tmp_path):
    run, calls = _stub("12.5")
    assert probe_duration(_video(tmp_path), runner=run) == 12.5
    assert calls[0][0] == "ffprobe"


def test_probe_duration_bad_output_raises(tmp_path):
    run, _ = _stub("not-a-number")
    with pytest.raises(RuntimeError, match="无法解析视频时长"):
        probe_duration(_video(tmp_path), runner=run)


# ---------------------------------------------------------------------------
# extract_preview_frames
# ---------------------------------------------------------------------------


def test_extract_creates_frames_at_midpoints(tmp_path):
    run, _ = _stub()
    out = tmp_path / "thumbs"
    frames = extract_preview_frames(
        _video(tmp_path), out, duration_s=8.0, n=4, runner=run
    )

    assert len(frames) == 4
    assert [f.index for f in frames] == [0, 1, 2, 3]
    # 采样点 = (i + 0.5) * 8 / 4 -> 1, 3, 5, 7（避开 0 与结尾边界）
    assert [round(f.time_s, 3) for f in frames] == [1.0, 3.0, 5.0, 7.0]
    assert all(isinstance(f, PreviewFrame) for f in frames)
    assert all(f.path.is_file() for f in frames)  # stub 已写出占位文件
    assert [f.rel_path for f in frames] == [f"demo_t0{i}.jpg" for i in range(4)]


def test_extract_probes_duration_when_not_given(tmp_path):
    run, calls = _stub("20.0")
    frames = extract_preview_frames(_video(tmp_path), tmp_path / "t", n=2, runner=run)
    assert len(frames) == 2
    assert any(c[0] == "ffprobe" for c in calls)  # 触发了探测


def test_extract_rejects_bad_n(tmp_path):
    run, _ = _stub()
    with pytest.raises(ValueError, match="缩略帧数量"):
        extract_preview_frames(_video(tmp_path), tmp_path / "t", duration_s=5.0, n=0, runner=run)


def test_extract_rejects_bad_duration(tmp_path):
    run, _ = _stub()
    with pytest.raises(ValueError, match="视频时长"):
        extract_preview_frames(_video(tmp_path), tmp_path / "t", duration_s=0.0, n=2, runner=run)


# ---------------------------------------------------------------------------
# build_video_preview_html（纯函数）
# ---------------------------------------------------------------------------


def _frames(n: int = 3) -> list:
    return [
        PreviewFrame(index=i, time_s=float(i), path=Path(f"t{i}.jpg"), rel_path=f"t{i}.jpg")
        for i in range(n)
    ]


def test_html_contains_thumbs_and_seek():
    html = build_video_preview_html("demo.mp4", _frames(3), 9.0, video_href="file:///v.mp4")
    assert "seekTo(" in html
    assert 'id="vp"' in html
    assert 'src="file:///v.mp4"' in html
    for i in range(3):
        assert f't{i}.jpg' in html
    assert "9.00" in html


def test_html_without_href_shows_notice():
    html = build_video_preview_html("demo.mp4", _frames(1), 3.0)
    assert "未提供视频源" in html
    assert 'id="vp"' not in html


def test_html_escapes_name():
    html = build_video_preview_html("<script>x</script>", _frames(1), 3.0)
    assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# VideoPreviewGenerator（落盘布局）
# ---------------------------------------------------------------------------


def test_output_dir_creates_new_preview_subdir(tmp_path):
    gen = VideoPreviewGenerator()
    d = gen.output_dir_for(_video(tmp_path), output_root=tmp_path / "output")
    assert d.is_dir()
    assert d == tmp_path / "output" / PREVIEW_DIR_NAME / "demo"


def test_generate_end_to_end(tmp_path):
    run, _ = _stub("30.0")
    gen = VideoPreviewGenerator(n_frames=3)
    html_path = gen.generate(
        _video(tmp_path), output_root=tmp_path / "output", runner=run
    )

    p = Path(html_path)
    assert p.is_file() and p.name == "preview.html"
    assert p.parent.name == "demo"
    assert p.parent.parent.name == PREVIEW_DIR_NAME  # 新建目录，不在既有产物里
    # 缩略帧与 HTML 同目录
    jpgs = sorted(p.parent.glob("demo_t*.jpg"))
    assert len(jpgs) == 3
    content = p.read_text(encoding="utf-8")
    assert "seekTo(" in content
    assert "demo_t00.jpg" in content


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_video_preview_missing_file_exit_3(tmp_path):
    result = runner.invoke(app, ["video-preview", str(tmp_path / "nope.mp4")])
    assert result.exit_code == 3


def test_cli_video_preview_ok(monkeypatch, tmp_path):
    import sunoauxtool.preview as pv

    seen: dict = {}

    def fake_generate(self, video_path, output_root=None, duration_s=None, runner=None):
        seen["video"] = str(video_path)
        seen["out"] = str(output_root)
        d = Path(output_root) / PREVIEW_DIR_NAME / "demo"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "preview.html"
        p.write_text("<html></html>", encoding="utf-8")
        return str(p)

    monkeypatch.setattr(pv.VideoPreviewGenerator, "generate", fake_generate)

    vid = _video(tmp_path)
    out = tmp_path / "output"
    result = runner.invoke(
        app, ["video-preview", str(vid), "-n", "4", "-o", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert seen["video"] == str(vid)
    assert seen["out"] == str(out)
