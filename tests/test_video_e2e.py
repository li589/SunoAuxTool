"""video 引擎真实 ffmpeg 端到端测试（1.4.0，P2 覆盖率补强）。

此前的引擎测试全部 mock 掉 subprocess；本文件**真实调用 ffmpeg/ffprobe**
（CI 的 ubuntu-24.04 装有 ffmpeg，本地走 SUNO_FFMPEG / PATH / 常见安装位置）。
用小尺寸（320x180）+ 低帧率 + 1s 音频把单条渲染压到 1-2s，避免拖慢套件。

未安装 ffmpeg 时全部 skip（`pytest -m video_e2e` 可单独运行）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from sunoauxtool.video.config import Config
from sunoauxtool.video.engines.ffmpeg_engine import FFmpegEngine
from sunoauxtool.video.engines.frame_engine import FrameEngine
from sunoauxtool.video.visuals import create_visualizer

FFMPEG = shutil.which("ffmpeg") or shutil.which("ffmpeg", path="C:/Windows")
FFPROBE = shutil.which("ffprobe")

pytestmark = pytest.mark.skipif(
    not (FFMPEG and FFPROBE), reason="本机未安装 ffmpeg/ffprobe（真实渲染测试）"
)


@pytest.fixture(scope="module")
def short_wav(tmp_path_factory):
    """1s 440Hz 正弦波（低清渲染足够）。"""
    p = tmp_path_factory.mktemp("e2e_audio") / "short.wav"
    sr = 44100
    t = np.linspace(0, 1.0, sr, endpoint=False)
    sf.write(str(p), (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), sr)
    return str(p)


def _small_config() -> Config:
    """320x180 @ 10fps：渲染秒级完成，仍走完整编码链。"""
    cfg = Config()
    cfg.video.width = 320
    cfg.video.height = 180
    cfg.video.fps = 10
    return cfg


def _probe(path: str) -> dict:
    """ffprobe 取流信息（video+audio、时长、尺寸）。"""
    r = subprocess.run(
        [
            FFPROBE, "-v", "error", "-print_format", "json",
            "-show_streams", "-show_format", path,
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


class TestFFmpegEngineE2E:
    def test_render_waveform(self, tmp_path, short_wav):
        """waveform 风格真实渲染：产物可被 ffprobe 解析，音视频流齐全。"""
        out = str(tmp_path / "wave.mp4")
        engine = FFmpegEngine(_small_config())
        written = engine.render(short_wav, out, visual_style="waveform")
        assert Path(written).is_file() and Path(written).stat().st_size > 0

        info = _probe(out)
        codecs = {s["codec_type"] for s in info["streams"]}
        assert "video" in codecs and "audio" in codecs
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        assert (v["width"], v["height"]) == (320, 180)
        assert float(info["format"]["duration"]) == pytest.approx(1.0, abs=0.3)

    def test_render_spectrum_with_background_and_title(self, tmp_path, short_wav):
        """spectrum + 背景图 + 标题链（drawtext 走真实滤镜图）。"""
        from PIL import Image

        bg = Image.new("RGB", (320, 180), (20, 24, 48))
        out = str(tmp_path / "spec.mp4")
        engine = FFmpegEngine(_small_config())
        written = engine.render(
            short_wav, out,
            visual_style="spectrum", background=bg,
            title="测试标题", subtitle="E2E",
        )
        assert Path(written).is_file()
        info = _probe(out)
        assert "video" in {s["codec_type"] for s in info["streams"]}


class TestFrameEngineE2E:
    def test_render_with_visualizer_bars(self, tmp_path, short_wav):
        """rawvideo 管道逐帧渲染（bars）：真实 ffmpeg 编码，时长对齐。"""
        out = str(tmp_path / "bars.mp4")
        engine = FrameEngine(_small_config())
        viz = create_visualizer("bars", _small_config())
        written = engine.render_with_visualizer(
            viz, short_wav, out, title="管道渲染", render_scale=0.5
        )
        assert Path(written).is_file() and Path(written).stat().st_size > 0

        info = _probe(out)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        assert (v["width"], v["height"]) == (320, 180)
        assert float(info["format"]["duration"]) == pytest.approx(1.0, abs=0.3)

    def test_render_with_background_image(self, tmp_path, short_wav):
        """注入背景图走 FrameEngine（背景缩放 + 逐帧叠加路径）。"""
        from PIL import Image

        out = str(tmp_path / "bg.mp4")
        engine = FrameEngine(_small_config())
        viz = create_visualizer("waveform_scroll", _small_config())
        bg = Image.new("RGB", (640, 360), (10, 60, 30))
        written = engine.render_with_visualizer(
            viz, short_wav, out, background=bg, render_scale=0.5
        )
        assert Path(written).is_file()
        info = _probe(out)
        assert "video" in {s["codec_type"] for s in info["streams"]}
