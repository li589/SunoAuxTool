"""sunoauxtool.video 单元测试（v0.2.0 W5）。

覆盖：analysis 预计算 / visualizer 工厂 / compositor 路由 / presets / config。
真实 ffmpeg 渲染的端到端冒烟见 test_video_e2e.py（1.4.0）。
"""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf

from sunoauxtool.video.analysis import analyze
from sunoauxtool.video.config import Config
from sunoauxtool.video.visuals import create_visualizer
from sunoauxtool.video.visuals.base import VisualContext
from sunoauxtool.video.visuals.reactive import ReactiveVisualizer
from sunoauxtool.video.visuals.spectrum import CircularSpectrumVisualizer
from sunoauxtool.video.presets import PRESETS, get_preset, list_presets, resolve_preset
from sunoauxtool.video.compositor import FFMPEG_STYLES


@pytest.fixture(scope="module")
def sample_wav(tmp_path_factory):
    """生成 2 秒正弦波测试音频。"""
    path = tmp_path_factory.mktemp("audio") / "sample.wav"
    sr = 44100
    t = np.linspace(0, 2, sr * 2)
    audio = 0.5 * np.sin(2 * np.pi * 440 * t)
    sf.write(str(path), audio, sr)
    return str(path)


# ---------------------------------------------------------------------------
# analysis 预计算
# ---------------------------------------------------------------------------

class TestAudioAnalysis:
    def test_analyze_basic(self, sample_wav):
        a = analyze(sample_wav, fps=30)
        assert a.duration_s == pytest.approx(2.0, abs=0.1)
        assert a.sample_rate == 44100
        assert a.fps == 30
        assert a.n_frames == pytest.approx(60, abs=2)

    def test_spectrogram_shape(self, sample_wav):
        a = analyze(sample_wav, fps=30, n_bins=64)
        assert a.spectrogram.shape == (a.n_frames, 64)

    def test_spectrogram_normalized(self, sample_wav):
        a = analyze(sample_wav)
        assert a.spectrogram.max() <= 1.0 + 1e-6
        assert a.spectrogram.min() >= 0.0

    def test_rms_envelope_shape(self, sample_wav):
        a = analyze(sample_wav, fps=30)
        assert a.rms_envelope.shape == (a.n_frames,)
        assert a.rms_envelope.max() <= 1.0 + 1e-6

    def test_sine_wave_has_energy(self, sample_wav):
        """正弦波应产生稳定 RMS（非零）。"""
        a = analyze(sample_wav)
        assert a.rms_envelope.mean() > 0.1

    def test_missing_file_raises(self, tmp_path):
        from sunoauxtool.video.exceptions import AudioReadError
        with pytest.raises(AudioReadError):
            analyze(str(tmp_path / "nonexistent.wav"))


# ---------------------------------------------------------------------------
# visualizer 工厂
# ---------------------------------------------------------------------------

class TestVisualizerFactory:
    def test_create_circular(self):
        viz = create_visualizer("circular_spectrum", Config())
        assert isinstance(viz, CircularSpectrumVisualizer)

    def test_create_reactive(self):
        viz = create_visualizer("reactive", Config())
        assert isinstance(viz, ReactiveVisualizer)

    def test_ffmpeg_styles_rejected(self):
        """waveform/spectrum 走 ffmpeg 引擎，工厂应拒绝。"""
        with pytest.raises(ValueError):
            create_visualizer("waveform", Config())
        with pytest.raises(ValueError):
            create_visualizer("spectrum", Config())

    def test_unknown_style_rejected(self):
        with pytest.raises(ValueError):
            create_visualizer("nonexistent", Config())


# ---------------------------------------------------------------------------
# visualizer 渲染（查表）
# ---------------------------------------------------------------------------

class TestVisualizerRender:
    def _make_ctx(self, sample_wav):
        a = analyze(sample_wav, fps=30)
        return VisualContext(width=320, height=240, fps=30, analysis=a)

    def test_circular_frame_shape(self, sample_wav):
        viz = CircularSpectrumVisualizer(Config())
        frame = viz.render_frame(self._make_ctx(sample_wav), 0)
        assert frame.shape == (240, 320, 3)
        assert frame.dtype == np.uint8

    def test_reactive_frame_shape(self, sample_wav):
        viz = ReactiveVisualizer(Config())
        frame = viz.render_frame(self._make_ctx(sample_wav), 0)
        assert frame.shape == (240, 320, 3)
        assert frame.dtype == np.uint8

    def test_circular_frames_differ(self, tmp_path):
        """幅度调制的音频应产生不同帧（RMS/频谱随时间变化）。"""
        # 生成幅度调制音频（前半弱、后半强）
        sr = 44100
        t = np.linspace(0, 2, sr * 2)
        env = np.concatenate([np.full(sr, 0.1), np.full(sr, 0.9)])
        audio = env * np.sin(2 * np.pi * 440 * t)
        wav = tmp_path / "am.wav"
        sf.write(str(wav), audio, sr)

        viz = CircularSpectrumVisualizer(Config())
        a = analyze(str(wav), fps=30)
        ctx = VisualContext(width=320, height=240, fps=30, analysis=a)
        f_weak = viz.render_frame(ctx, 15)   # 前半（弱）
        f_strong = viz.render_frame(ctx, 45)  # 后半（强）
        assert not np.array_equal(f_weak, f_strong)

    def test_frame_idx_out_of_range_safe(self, sample_wav):
        """越界帧索引应安全钳制。"""
        viz = ReactiveVisualizer(Config())
        ctx = self._make_ctx(sample_wav)
        frame = viz.render_frame(ctx, 99999)
        assert frame.shape == (240, 320, 3)


# ---------------------------------------------------------------------------
# compositor 路由
# ---------------------------------------------------------------------------

class TestCompositorRouting:
    def test_ffmpeg_styles_defined(self):
        assert FFMPEG_STYLES == {"waveform", "spectrum"}

    def test_creative_styles_not_in_ffmpeg(self):
        assert "circular_spectrum" not in FFMPEG_STYLES
        assert "reactive" not in FFMPEG_STYLES


# ---------------------------------------------------------------------------
# presets
# ---------------------------------------------------------------------------

class TestPresets:
    def test_all_four_platforms(self):
        assert set(list_presets()) == {"douyin", "youtube", "instagram", "official"}

    def test_douyin_is_portrait(self):
        p = get_preset("douyin")
        assert p["width"] == 1080
        assert p["height"] == 1920  # 9:16

    def test_youtube_is_landscape(self):
        p = get_preset("youtube")
        assert p["width"] == 1920
        assert p["height"] == 1080  # 16:9

    def test_instagram_is_square(self):
        p = get_preset("instagram")
        assert p["width"] == p["height"] == 1080

    def test_unknown_preset_raises(self):
        with pytest.raises(ValueError):
            get_preset("nonexistent")

    def test_resolve_empty_defaults_douyin(self):
        assert resolve_preset("")["width"] == 1080

    def test_presets_have_safe_area(self):
        for name in list_presets():
            assert "safe_area" in PRESETS[name]


# ---------------------------------------------------------------------------
# logo 预处理（W3）
# ---------------------------------------------------------------------------

class TestLogoPrepare:
    def test_no_logo_returns_none(self):
        from sunoauxtool.video.engines.ffmpeg_engine import FFmpegEngine
        e = FFmpegEngine(Config())  # logo.path 为空
        assert e._prepare_logo(1920, 1080, "out.mp4") is None

    def test_logo_even_size(self, tmp_path):
        """logo 缩放后必须是偶数尺寸（yuv420p 要求）。"""
        from PIL import Image, ImageDraw
        from sunoauxtool.video.engines.ffmpeg_engine import FFmpegEngine

        # 101x99 奇数 logo
        logo = Image.new("RGBA", (101, 99), (0, 0, 0, 0))
        d = ImageDraw.Draw(logo)
        d.ellipse([5, 5, 95, 95], fill=(255, 0, 0, 255))
        logo_path = tmp_path / "logo.png"
        logo.save(str(logo_path))

        cfg = Config()
        cfg.logo.path = str(logo_path)
        e = FFmpegEngine(cfg)
        result = e._prepare_logo(1920, 1080, str(tmp_path / "out.mp4"))
        assert result is not None
        png_path, (x, y) = result
        img = Image.open(png_path)
        assert img.size[0] % 2 == 0
        assert img.size[1] % 2 == 0

    def test_logo_position_bottom_right(self, tmp_path):
        from PIL import Image
        from sunoauxtool.video.engines.ffmpeg_engine import FFmpegEngine

        logo = Image.new("RGBA", (100, 100), (255, 0, 0, 255))
        logo_path = tmp_path / "logo2.png"
        logo.save(str(logo_path))

        cfg = Config()
        cfg.logo.path = str(logo_path)
        e = FFmpegEngine(cfg)
        png_path, (x, y) = e._prepare_logo(1920, 1080, str(tmp_path / "out.mp4"))
        # bottom-right：x + logo_w + margin ≈ 1920
        img = Image.open(png_path)
        assert x + img.size[0] + cfg.logo.margin == 1920


class TestConfigLogo:
    def test_logo_defaults(self):
        cfg = Config()
        assert cfg.logo.path == ""
        assert cfg.logo.position == "bottom-right"

    def test_logo_merge(self, tmp_path):
        import tomli_w
        data = {"logo": {"path": "a.png", "scale": 0.2}}
        p = tmp_path / "cfg.toml"
        p.write_text(tomli_w.dumps(data), encoding="utf-8")
        cfg = Config.load(str(p))
        assert cfg.logo.path == "a.png"
        assert cfg.logo.scale == 0.2
        assert cfg.logo.position == "bottom-right"  # 未指定用默认


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

class TestConfig:
    def test_defaults(self):
        cfg = Config()
        assert cfg.video.width == 1080
        assert cfg.visual.style == "waveform"

    def test_load_nonexistent_returns_default(self, tmp_path):
        cfg = Config.load(str(tmp_path / "nonexistent.toml"))
        assert cfg.video.width == 1080

