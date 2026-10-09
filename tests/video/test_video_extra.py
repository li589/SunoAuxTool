"""视频子系统扩展与测试补强（R15）。

覆盖此前薄/零覆盖的部分：
- ``video/visuals``：注册表中每个内置风格都能被 ``create_visualizer`` 实例化；
  新增 ``bars`` 风格做**像素探针**（柱体存在、不越界、峰值帽会衰减）。
- ``video/mixer``：多轨混音与规格解析（真实小 WAV，不依赖 ffmpeg/fluidsynth）。
- ``video/compositor``：ffmpeg 原生风格 vs PIL 创意层的派发常量。
"""

from __future__ import annotations

import numpy as np
import soundfile as sf

from sunoauxtool.video.analysis.audio_analysis import AudioAnalysis
from sunoauxtool.video.config import Config
from sunoauxtool.video.visuals import VisualContext, create_visualizer, discover_visuals

BG = np.array([26, 26, 46], dtype=np.uint8)


def _ctx(n_frames: int = 4, n_bins: int = 64, w: int = 320, h: int = 240, fps: int = 30):
    """构造一个可控的渲染上下文（频谱为递增斜坡，便于探针判定）。"""
    spec = np.zeros((n_frames, n_bins), dtype=np.float64)
    for i in range(n_frames):
        spec[i] = np.linspace(0.2, 1.0, n_bins) * (0.5 + 0.1 * i)
    analysis = AudioAnalysis(
        duration_s=n_frames / fps,
        sample_rate=44100,
        fps=fps,
        n_frames=n_frames,
        spectrogram=spec,
        rms_envelope=np.linspace(0.2, 0.9, n_frames),
    )
    return VisualContext(width=w, height=h, fps=fps, analysis=analysis)


# ---------------------------------------------------------------------------
# visuals
# ---------------------------------------------------------------------------


def test_all_builtin_styles_instantiate():
    cfg = Config()
    registry = discover_visuals()
    assert "bars" in registry  # R15 新增
    for name in sorted(registry):
        viz = create_visualizer(name, cfg)
        assert viz.get_style_name(), name


def test_bars_frame_shape_dtype_and_bars_present():
    viz = create_visualizer("bars", Config())
    frame = viz.render_frame(_ctx(), 1)

    assert frame.shape == (240, 320, 3)
    assert frame.dtype == np.uint8
    # 有柱体（非纯背景）
    assert np.any(frame != BG, axis=-1).mean() > 0.02


def test_bars_stays_within_frame_bottom_margin():
    """像素探针：底部留白区（下 6%）必须是纯背景，柱体不得越界。"""
    viz = create_visualizer("bars", Config())
    frame = viz.render_frame(_ctx(), 0)
    bottom = frame[int(frame.shape[0] * 0.94) :]
    assert np.all(bottom == BG)


def test_bars_peak_cap_decays_on_silence():
    viz = create_visualizer("bars", Config())
    viz.render_frame(_ctx(), 0)
    first = viz._peaks.copy()
    assert first.max() > 0

    silent = _ctx()
    silent.analysis.spectrogram[:] = 0.0
    viz.render_frame(silent, 1)
    assert np.all(viz._peaks < first)  # 峰值帽按系数衰减
    assert np.all(viz._peaks >= 0.0)


def test_bars_accepts_background_frame():
    """引擎注入背景帧时必须被沿用（不是另起纯色）。"""
    viz = create_visualizer("bars", Config())
    bg = np.full((240, 320, 3), 200, dtype=np.uint8)
    ctx = _ctx()
    ctx.background = bg
    frame = viz.render_frame(ctx, 0)
    # 顶部区域应保留背景亮色（说明用的是注入背景）
    assert frame[0, 0].tolist() == [200, 200, 200]


def test_bars_silence_renders_without_error():
    """全静音（频谱全 0）不得抛错且仍产出合法帧。"""
    viz = create_visualizer("bars", Config())
    ctx = _ctx()
    ctx.analysis.spectrogram[:] = 0.0
    frame = viz.render_frame(ctx, 2)
    assert frame.shape == (240, 320, 3)


# ---------------------------------------------------------------------------
# mixer
# ---------------------------------------------------------------------------


def test_parse_track_specs_gain_pan():
    from sunoauxtool.video.mixer import parse_track_specs

    specs = parse_track_specs(["a.wav:gain=0.8:pan=-0.3", "b.wav"])
    assert specs[0].path == "a.wav"
    assert specs[0].gain == 0.8
    assert specs[0].pan == -0.3
    assert specs[1].gain == 1.0 and specs[1].pan == 0.0


def test_mix_tracks_two_wavs(tmp_path):
    from sunoauxtool.video.mixer import TrackSpec, mix_tracks

    sr = 22050
    t = np.arange(int(0.5 * sr)) / sr
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    sf.write(str(a), (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), sr)
    sf.write(str(b), (0.5 * np.sin(2 * np.pi * 880 * t)).astype(np.float32), sr)

    res = mix_tracks([TrackSpec(path=str(a)), TrackSpec(path=str(b))], target_sr=sr)
    assert res.mixed.ndim == 2 and res.mixed.shape[1] == 2  # 立体声
    assert res.sample_rate == sr
    assert res.duration_s > 0
    assert float(np.abs(res.mixed).max()) <= 1.0 + 1e-6  # 归一化上限内不削波
    assert len(res.track_monos) == 2


def test_mix_tracks_trim_to_shortest(tmp_path):
    from sunoauxtool.video.mixer import TrackSpec, mix_tracks

    sr = 22050
    long_t = np.arange(int(1.0 * sr)) / sr
    short_t = np.arange(int(0.25 * sr)) / sr
    a, b = tmp_path / "long.wav", tmp_path / "short.wav"
    sf.write(str(a), (0.4 * np.sin(2 * np.pi * 330 * long_t)).astype(np.float32), sr)
    sf.write(str(b), (0.4 * np.sin(2 * np.pi * 550 * short_t)).astype(np.float32), sr)

    kept = mix_tracks(
        [TrackSpec(path=str(a)), TrackSpec(path=str(b))],
        target_sr=sr,
        trim_to_shortest=False,
    )
    trimmed = mix_tracks(
        [TrackSpec(path=str(a)), TrackSpec(path=str(b))],
        target_sr=sr,
        trim_to_shortest=True,
    )
    assert trimmed.duration_s < kept.duration_s


# ---------------------------------------------------------------------------
# compositor
# ---------------------------------------------------------------------------


def test_ffmpeg_vs_pil_style_split():
    """ffmpeg 原生风格与 PIL 创意层互斥，bars 属后者。"""
    from sunoauxtool.video.compositor import FFMPEG_STYLES
    from sunoauxtool.video.visuals import discover_visuals

    assert {"waveform", "spectrum"} <= set(FFMPEG_STYLES)
    pil_styles = set(discover_visuals())
    assert not (pil_styles & set(FFMPEG_STYLES))  # 无交集
    assert "bars" in pil_styles
