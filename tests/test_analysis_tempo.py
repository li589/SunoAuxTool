"""``analysis.tempo`` 音频测速测试（#13）。

合成点击轨（880Hz 指数衰减短音，间隔 = 拍长）作为真值来源：
BPM 估计应落在真值 ±3% 内（实测 ≤2.5%，ACF 帧分辨率决定下限），
置信度对纯节拍信号应显著高，节拍相位误差 <0.06s。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from sunoauxtool.analysis.tempo import TempoEstimate, beat_grid, estimate_bpm
from sunoauxtool.exceptions import InputFileError, ParameterError

SR = 22050


def write_click_wav(path: Path, bpm: float, beats: int = 24, sr: int = SR) -> None:
    """写一条等间隔点击轨（每拍一个 50ms 衰减音）。"""
    n = int(beats * 60.0 / bpm * sr)
    x = np.zeros(n, dtype=np.float32)
    for k in range(beats):
        start = int(k * 60.0 / bpm * sr)
        dur = int(0.05 * sr)
        t = np.arange(dur) / sr
        x[start : start + dur] += (
            0.6 * np.sin(2 * np.pi * 880.0 * t) * np.exp(-t * 40.0)
        ).astype(np.float32)
    sf.write(path, x, sr, subtype="PCM_16")


@pytest.mark.parametrize("bpm", [60.0, 100.0, 120.0, 145.3])
def test_click_track_bpm_within_3_percent(tmp_path: Path, bpm: float):
    wav = tmp_path / f"click_{bpm}.wav"
    write_click_wav(wav, bpm)
    est = estimate_bpm(wav)
    assert abs(est.bpm - bpm) / bpm < 0.03


def test_fast_material_needs_prior_off(tmp_path: Path):
    """≥160 BPM 的素材会被默认先验折半（拍级歧义的已知边界，见模块文档）；
    关闭先验后纯自相关可正确命中。"""
    wav = tmp_path / "click180.wav"
    write_click_wav(wav, 180.0)
    assert abs(estimate_bpm(wav).bpm - 90.0) / 90.0 < 0.03   # 默认先验：折半
    est = estimate_bpm(wav, prior_bpm=0)                      # 关闭先验：命中
    assert abs(est.bpm - 180.0) / 180.0 < 0.03


def test_prior_resolves_half_tempo_ambiguity(tmp_path: Path):
    """强弱交替（重拍都在 2 拍边界）：纯 ACF 折半到 60，先验拉回真值 120。"""
    wav = tmp_path / "alternating.wav"
    n = int(24 * 60.0 / 120.0 * SR)
    x = np.zeros(n, dtype=np.float32)
    for k in range(24):
        start = int(k * 60.0 / 120.0 * SR)
        dur = int(0.05 * SR)
        t = np.arange(dur) / SR
        amp = 0.6 if k % 2 == 0 else 0.15
        x[start : start + dur] += (
            amp * np.sin(2 * np.pi * 880.0 * t) * np.exp(-t * 40.0)
        ).astype(np.float32)
    sf.write(wav, x, SR, subtype="PCM_16")
    assert abs(estimate_bpm(wav).bpm - 120.0) / 120.0 < 0.03
    assert abs(estimate_bpm(wav, prior_bpm=0).bpm - 60.0) / 60.0 < 0.03


@pytest.mark.parametrize("bpm", [100.0, 120.0, 180.0])
def test_click_track_confidence_and_phase(tmp_path: Path, bpm: float):
    wav = tmp_path / f"conf_{bpm}.wav"
    write_click_wav(wav, bpm)
    est = estimate_bpm(wav)
    assert isinstance(est, TempoEstimate)
    assert est.confidence > 0.5          # 纯节拍信号的自相关峰应显著
    # 相位按真值拍网格取模评估（beat_offset 定义域 [0, est_period)；est=90 折半
    # 时合法锁在奇数拍，如 0.313 @ 180BPM；第一拍在 t=0 时允许 period 附近等价值）
    per = 60.0 / bpm
    phase_err = min(est.beat_offset % per, per - est.beat_offset % per)
    assert phase_err < 0.06
    assert est.onset_rate == pytest.approx(SR / 512, rel=1e-6)
    assert est.duration == pytest.approx(24 * 60.0 / bpm, rel=1e-3)


def test_44100_hz_still_works(tmp_path: Path):
    wav = tmp_path / "click44k.wav"
    write_click_wav(wav, 120.0, sr=44100)
    est = estimate_bpm(wav)
    assert abs(est.bpm - 120.0) / 120.0 < 0.03


def test_silence_raises_parameter_error(tmp_path: Path):
    wav = tmp_path / "silence.wav"
    sf.write(wav, np.zeros(SR * 4, dtype=np.float32), SR, subtype="PCM_16")
    with pytest.raises(ParameterError) as exc:
        estimate_bpm(wav)
    assert exc.value.code == 1


def test_too_short_audio_raises(tmp_path: Path):
    wav = tmp_path / "short.wav"
    sf.write(wav, np.zeros(4000, dtype=np.float32), SR, subtype="PCM_16")
    with pytest.raises(ParameterError) as exc:
        estimate_bpm(wav)
    assert "过短" in str(exc.value)


def test_missing_file_raises_input_error(tmp_path: Path):
    with pytest.raises(InputFileError) as exc:
        estimate_bpm(tmp_path / "nope.wav")
    assert exc.value.code == 3


def test_invalid_bpm_range_raises(tmp_path: Path):
    wav = tmp_path / "click.wav"
    write_click_wav(wav, 120.0)
    with pytest.raises(ParameterError):
        estimate_bpm(wav, bpm_min=100.0, bpm_max=50.0)
    with pytest.raises(ParameterError):
        estimate_bpm(wav, bpm_min=-10.0, bpm_max=240.0)


# ---------------------------------------------------------------------------
# beat_grid
# ---------------------------------------------------------------------------


def test_beat_grid_basic():
    grid = beat_grid(2.0, bpm=120.0, offset=0.0)
    assert np.allclose(grid, [0.0, 0.5, 1.0, 1.5])


def test_beat_grid_with_offset_and_tail():
    grid = beat_grid(1.2, bpm=120.0, offset=0.25)
    assert np.allclose(grid, [0.25, 0.75])


def test_beat_grid_offset_beyond_duration_is_empty():
    assert beat_grid(1.0, bpm=120.0, offset=2.0).size == 0


def test_beat_grid_rejects_nonpositive_bpm():
    with pytest.raises(ParameterError):
        beat_grid(2.0, bpm=0)


# ---------------------------------------------------------------------------
# spectral 共享底座的边界（空信号 / 不足一帧）
# ---------------------------------------------------------------------------


def test_spectral_base_handles_tiny_signals():
    from sunoauxtool.analysis.spectral import (
        frame_view,
        onset_strength,
        stft_magnitude,
    )

    x = np.zeros(100, dtype=np.float32)
    assert frame_view(x, 2048, 512).shape[0] == 0
    assert stft_magnitude(x, 2048, 512).shape == (0, 1025)
    assert onset_strength(x, 2048, 512).size == 0
    assert onset_strength(np.zeros(2048, dtype=np.float32), 2048, 512).size == 1


def test_beat_offset_known_phase_regression(tmp_path: Path):
    """B2 回归（1.4.6）：beat_offset 曾被重复除以 rate（0.25s -> 0.0029s）。

    点击轨第一拍故意错开 0.25s，修复后 beat_offset 必须落在真实相位附近；
    旧实现返回 ~0.003s，任何 ±0.08s 的相位断言都会失败。
    """
    bpm, phase, beats = 120.0, 0.25, 24
    period = 60.0 / bpm
    sr = SR
    n = int(beats * period * sr)
    x = np.zeros(n, dtype=np.float32)
    for k in range(beats - 1):  # 末拍可能越界，留一格
        start = int((k * period + phase) * sr)
        dur = int(0.05 * sr)
        t = np.arange(dur) / sr
        x[start : start + dur] += (
            0.6 * np.sin(2 * np.pi * 880.0 * t) * np.exp(-t * 40.0)
        ).astype(np.float32)
    wav = tmp_path / "click_phase.wav"
    sf.write(wav, x, sr, subtype="PCM_16")

    est = estimate_bpm(wav)
    assert est.bpm == pytest.approx(bpm, rel=0.03)
    assert est.beat_offset == pytest.approx(phase, abs=0.08)
