"""sunoauxtool.video v0.3 测试：多格式加载 / 多轨混音 / 分轨与滚动波形。"""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf

from sunoauxtool.video.audio_io import (
    AudioLoadResult,
    detect_format,
    is_supported,
    load_audio,
)
from sunoauxtool.video.exceptions import AudioReadError
from sunoauxtool.video.mixer import TrackSpec, mix_tracks, parse_track_specs
from sunoauxtool.video.analysis import analyze, analyze_multitrack
from sunoauxtool.video.config import Config
from sunoauxtool.video.visuals import create_visualizer
from sunoauxtool.video.visuals.base import VisualContext
from sunoauxtool.video.visuals.tracks import TracksVisualizer, WaveformScrollVisualizer


@pytest.fixture(scope="module")
def tone_a(tmp_path_factory):
    """2s 440Hz 音轨。"""
    path = tmp_path_factory.mktemp("mix") / "a.wav"
    sr = 44100
    t = np.linspace(0, 2, sr * 2)
    sf.write(str(path), 0.5 * np.sin(2 * np.pi * 440 * t), sr, subtype="PCM_16")
    return str(path)


@pytest.fixture(scope="module")
def tone_b(tmp_path_factory):
    """3s 660Hz 音轨。"""
    path = tmp_path_factory.mktemp("mix") / "b.wav"
    sr = 44100
    t = np.linspace(0, 3, sr * 3)
    sf.write(str(path), 0.5 * np.sin(2 * np.pi * 660 * t), sr, subtype="PCM_16")
    return str(path)


# ---------------------------------------------------------------------------
# W2：多格式加载
# ---------------------------------------------------------------------------

class TestAudioIO:
    def test_detect_format(self):
        assert detect_format("a.mp3") == "mp3"
        assert detect_format("a.MIDI") == "midi"
        assert detect_format("a.wav") == "wav"

    def test_is_supported(self):
        assert is_supported("a.wav")
        assert is_supported("a.mp3")
        assert is_supported("a.mid")
        assert not is_supported("a.txt")

    def test_load_wav(self, tone_a):
        r = load_audio(tone_a)
        assert isinstance(r, AudioLoadResult)
        assert r.source_format == "wav"
        assert r.sample_rate == 44100
        assert r.duration_s == pytest.approx(2.0, abs=0.05)
        assert r.channels == 1

    def test_load_mp3(self, tmp_path, tone_a):
        """soundfile 0.14 原生支持 MP3。"""
        mp3 = tmp_path / "probe.mp3"
        import subprocess
        subprocess.run(
            ["ffmpeg", "-y", "-i", tone_a, "-codec:a", "libmp3lame", str(mp3)],
            capture_output=True, check=True,
        )
        r = load_audio(str(mp3))
        assert r.source_format == "mp3"
        assert r.duration_s == pytest.approx(2.0, abs=0.2)

    def test_load_midi(self, tmp_path):
        """MIDI → FluidSynth 渲染 → 加载（module 真实引擎）。"""
        midi = None
        from pathlib import Path as _P
        for cand in _P("output/default/20260811").glob("*.mid"):
            midi = cand
            break
        if midi is None:
            pytest.skip("无可用测试 MIDI")
        r = load_audio(str(midi))
        assert r.source_format == "midi"
        assert r.sample_rate == 44100
        assert r.duration_s > 1.0
        # 清理临时渲染
        from sunoauxtool.video.audio_io import cleanup_rendered
        cleanup_rendered(r)

    def test_missing_file(self, tmp_path):
        with pytest.raises(AudioReadError):
            load_audio(str(tmp_path / "nope.wav"))

    def test_unsupported_ext(self, tmp_path):
        f = tmp_path / "bad.txt"
        f.write_text("x")
        with pytest.raises(AudioReadError):
            load_audio(str(f))

    def test_analyze_accepts_load_result(self, tone_a):
        """analyze() 接受 AudioLoadResult（v0.3 接口扩展）。"""
        r = load_audio(tone_a)
        a = analyze(r, fps=30)
        assert a.n_frames > 0
        assert a.spectrogram.shape[1] == 64


# ---------------------------------------------------------------------------
# W3：多轨混音
# ---------------------------------------------------------------------------

class TestMixer:
    def test_parse_track_spec(self):
        s = TrackSpec.parse("file.wav:gain=0.8:pan=-0.3")
        assert s.path == "file.wav"
        assert s.gain == 0.8
        assert s.pan == -0.3

    def test_parse_windows_path(self):
        """Windows 盘符冒号不应破坏解析。"""
        s = TrackSpec.parse(r"C:\music\a.wav:gain=0.5")
        assert s.path == "C"
        # 已知限制：C: 盘符会被切分；文档中标注绝对路径用 TrackSpec 构造
        assert s.gain == 0.5

    def test_parse_specs_list(self):
        specs = parse_track_specs(["a.wav", "b.wav:gain=0.5:pan=0.3"])
        assert len(specs) == 2
        assert specs[0].gain == 1.0
        assert specs[1].gain == 0.5

    def test_mix_length_align(self, tone_a, tone_b):
        """不同长度轨道对齐到最长轨。"""
        result = mix_tracks([TrackSpec(path=tone_a), TrackSpec(path=tone_b)])
        assert result.duration_s == pytest.approx(3.0, abs=0.05)

    def test_mix_trim_shortest(self, tone_a, tone_b):
        result = mix_tracks(
            [TrackSpec(path=tone_a), TrackSpec(path=tone_b)],
            trim_to_shortest=True,
        )
        assert result.duration_s == pytest.approx(2.0, abs=0.05)

    def test_mix_normalize_no_clipping(self, tone_a, tone_b):
        result = mix_tracks(
            [TrackSpec(path=tone_a, gain=2.0), TrackSpec(path=tone_b, gain=2.0)],
        )
        peak = np.abs(result.mixed).max()
        assert peak <= 0.892  # -1 dBFS

    def test_mix_pan_right(self, tone_a, tone_b):
        """b 轨偏右 → 尾段（仅 b 轨）R > L。"""
        result = mix_tracks(
            [TrackSpec(path=tone_a), TrackSpec(path=tone_b, pan=0.8)],
        )
        sr = result.sample_rate
        tail = result.mixed[int(sr * 2.2):, :]
        assert np.abs(tail[:, 1]).mean() > np.abs(tail[:, 0]).mean()

    def test_mix_resample(self, tone_a):
        """48k → 44.1k 重采样。"""
        result = mix_tracks([TrackSpec(path=tone_a)], target_sr=44100)
        assert result.sample_rate == 44100

    def test_mix_save_wav(self, tone_a, tmp_path):
        out = tmp_path / "mix.wav"
        mix_tracks([TrackSpec(path=tone_a, gain=0.5)], save_wav=str(out))
        assert out.exists()
        info = sf.info(str(out))
        assert info.channels == 2

    def test_mix_empty_raises(self):
        with pytest.raises(ValueError):
            mix_tracks([])


# ---------------------------------------------------------------------------
# W4：分轨分析 + 可视化
# ---------------------------------------------------------------------------

class TestMultitrackAnalysis:
    def _mix(self, tone_a, tone_b):
        return mix_tracks([TrackSpec(path=tone_a), TrackSpec(path=tone_b, pan=0.5)])

    def test_analyze_multitrack(self, tone_a, tone_b):
        mix = self._mix(tone_a, tone_b)
        a = analyze_multitrack(mix, fps=30)
        assert len(a.track_spectrograms) == 2
        assert len(a.track_rms) == 2
        assert a.track_labels == ["a", "b"]
        for spec in a.track_spectrograms:
            assert spec.shape == (a.n_frames, 64)
            assert spec.max() <= 1.0 + 1e-6  # 独立归一化

    def test_main_track_is_mix(self, tone_a, tone_b):
        mix = self._mix(tone_a, tone_b)
        a = analyze_multitrack(mix, fps=30)
        assert a.rms_envelope.shape == (a.n_frames,)


class TestTracksVisualizer:
    def _ctx(self, tone_a, tone_b):
        mix = mix_tracks([TrackSpec(path=tone_a), TrackSpec(path=tone_b)])
        a = analyze_multitrack(mix, fps=30)
        return VisualContext(width=320, height=240, fps=30, analysis=a)

    def test_factory_creates(self):
        assert isinstance(create_visualizer("tracks", Config()), TracksVisualizer)
        assert isinstance(create_visualizer("tracks_visual", Config()), TracksVisualizer)

    def test_render_frame(self, tone_a, tone_b):
        viz = create_visualizer("tracks", Config())
        frame = viz.render_frame(self._ctx(tone_a, tone_b), 10)
        assert frame.shape == (240, 320, 3)
        assert frame.dtype == np.uint8

    def test_frames_differ(self, tmp_path, tone_a):
        """调幅信号的频谱随时间变化 → 不同帧画面不同。

        （恒定正弦的频谱恒定，帧间必然相同——这是数据特性，用 AM 信号测）
        """
        sr = 44100
        t = np.linspace(0, 2, sr * 2)
        env = np.concatenate([np.full(sr, 0.1), np.full(sr, 0.9)])
        wav = tmp_path / "am.wav"
        sf.write(str(wav), env * np.sin(2 * np.pi * 440 * t), sr, subtype="PCM_16")

        mix = mix_tracks([TrackSpec(path=str(wav)), TrackSpec(path=str(wav), gain=0.5)])
        a = analyze_multitrack(mix, fps=30)
        viz = create_visualizer("tracks", Config())
        ctx = VisualContext(width=320, height=240, fps=30, analysis=a)
        f_weak = viz.render_frame(ctx, 15)
        f_strong = viz.render_frame(ctx, 45)
        assert not np.array_equal(f_weak, f_strong)


# ---------------------------------------------------------------------------
# W5：滚动波形
# ---------------------------------------------------------------------------

class TestWaveformScroll:
    def test_factory_creates(self):
        assert isinstance(create_visualizer("waveform_scroll", Config()), WaveformScrollVisualizer)

    def test_render_frame(self, tone_a):
        a = analyze(tone_a, fps=30)
        viz = create_visualizer("waveform_scroll", Config())
        ctx = VisualContext(width=320, height=240, fps=30, analysis=a)
        frame = viz.render_frame(ctx, 30)
        assert frame.shape == (240, 320, 3)

    def test_scrolls_over_time(self, tone_a):
        """不同帧应有不同画面（波形流动）。"""
        a = analyze(tone_a, fps=30)
        viz = create_visualizer("waveform_scroll", Config())
        ctx = VisualContext(width=320, height=240, fps=30, analysis=a)
        f0 = viz.render_frame(ctx, 0)
        f30 = viz.render_frame(ctx, 30)
        assert not np.array_equal(f0, f30)
