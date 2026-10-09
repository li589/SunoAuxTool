"""P2-1 DSP 单测：归一化 / 淡变 / 削波 / EQ / 压缩 / 参数校验。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from sunoauxtool.dsp import DspOptions, DspProcessor
from sunoauxtool.dsp import filters
from sunoauxtool.exceptions import ParameterError
from sunoauxtool.export import audio as audio_ops

PEAK_ABS = float(10 ** (-1.0 / 20.0))  # -1 dBFS


def _sine_wav(path: Path, duration: float = 2.0, amplitude: float = 0.9) -> Path:
    t = np.linspace(0, duration, int(44100 * duration), endpoint=False)
    audio = (amplitude * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    audio_ops.write_wav(path, audio, 44100, 16)
    return path


def _process(opts: DspOptions, src: Path, tmp_path: Path) -> np.ndarray:
    out = tmp_path / "out.wav"
    DspProcessor().process(str(src), opts, str(out))
    data, sr = audio_ops.read_wav(out)
    return data


# ---------------------------------------------------------------------------
# 参数校验（非法参数显式报错，P2-1 验收 3）
# ---------------------------------------------------------------------------

def test_validate_fade_in_out_of_range(tmp_path):
    """fade 越界（>5000ms）-> ParameterError(1)。"""
    proc = DspProcessor()
    with pytest.raises(ParameterError) as exc:
        proc.validate(DspOptions(fade_in_ms=6000))
    assert exc.value.code == 1
    with pytest.raises(ParameterError) as exc:
        proc.validate(DspOptions(fade_out_ms=-1))
    assert exc.value.code == 1


def test_validate_compressor_ratio(tmp_path):
    """ratio<1 -> ParameterError(1)。"""
    proc = DspProcessor()
    with pytest.raises(ParameterError) as exc:
        proc.validate(DspOptions(compressor=True, compressor_ratio=0.5))
    assert exc.value.code == 1


def test_validate_normalize_dbfs_positive(tmp_path):
    """normalize_dbfs>0 -> ParameterError(1)。"""
    proc = DspProcessor()
    with pytest.raises(ParameterError) as exc:
        proc.validate(DspOptions(normalize_dbfs=3.0))
    assert exc.value.code == 1


def test_reverb_not_supported(tmp_path):
    """请求混响 -> ParameterError（显式报未支持，不静默忽略，P2-1d）。"""
    src = _sine_wav(tmp_path / "src.wav")
    with pytest.raises(ParameterError) as exc:
        _process(DspOptions(reverb=True), src, tmp_path)
    assert exc.value.code == 1
    assert "混响" in str(exc.value) or "reverb" in str(exc.value)


# ---------------------------------------------------------------------------
# 归一化（P2-1 验收 1）
# ---------------------------------------------------------------------------

def test_normalize_peak_dbfs(tmp_path):
    """默认峰值 <= -1 dBFS，无削波（峰值采样数=0）。"""
    src = _sine_wav(tmp_path / "loud.wav", amplitude=0.99)
    data = _process(DspOptions(normalize_dbfs=-1.0), src, tmp_path)
    peak = float(np.max(np.abs(data)))
    assert peak <= PEAK_ABS + 1e-4
    # 削波检测：没有任何采样等于 1.0 或 -1.0（16bit 整数溢出前已归一）
    clipped = int(np.sum(np.abs(data) >= 1.0 - 1e-9))
    assert clipped == 0


# ---------------------------------------------------------------------------
# 淡入淡出（P2-1 验收 2）
# ---------------------------------------------------------------------------

def test_fade_edges_zero(tmp_path):
    """淡入/淡出生效：首尾能量趋近 0，中段保持。"""
    src = _sine_wav(tmp_path / "src.wav", duration=3.0)
    data = _process(DspOptions(fade_in_ms=100, fade_out_ms=300), src, tmp_path)
    assert abs(data[0]) < 1e-3
    assert abs(data[-1]) < 1e-3
    mid = data[len(data) // 3 : 2 * len(data) // 3]
    assert float(np.max(np.abs(mid))) > 0.3


def test_fade_zero_keeps_signal(tmp_path):
    """fade=0 时信号保持不变（无淡变）。"""
    src = _sine_wav(tmp_path / "src.wav", duration=1.0)
    data = _process(DspOptions(fade_in_ms=0, fade_out_ms=0), src, tmp_path)
    # 起始样本位于正弦过零点（≈0），检查前 5000 样本峰值保持
    assert float(np.max(np.abs(data[:5000]))) > 0.1


# ---------------------------------------------------------------------------
# EQ / 压缩（P2-1 验收 3）
# ---------------------------------------------------------------------------

def test_highpass_removes_dc():
    """高通滤波抑制直流分量。"""
    audio = np.full(4410, 0.5, dtype=np.float32)
    out = filters.highpass(audio, 44100, 30.0)
    # 滤波后尾部（稳态）直流趋近 0
    assert abs(float(np.mean(out[-1000:]))) < 1e-3


def test_highpass_vectorized_equivalent_to_loop():
    """1.5.2 L5 回归：lfilter 向量化与逐样本循环逐点一致（mono + stereo）。"""
    from scipy.signal import lfilter

    def _loop(x: np.ndarray, sr: int, fc: float) -> np.ndarray:
        alpha = 1.0 / (1.0 + 2.0 * np.pi * fc / sr)
        out = np.zeros_like(x, dtype=np.float64)
        prev_y = prev_x = 0.0
        for i in range(len(x)):
            yi = alpha * (prev_y + x[i] - prev_x)
            prev_y, prev_x = yi, x[i]
            out[i] = yi
        return out

    rng = np.random.RandomState(11)
    sr, fc = 44100, 80.0
    a = 0.5 * np.sin(2 * np.pi * 220 * np.arange(4000) / sr) + 0.05 * rng.randn(4000)
    got = filters.highpass(a.astype(np.float32), sr, fc)
    want = lfilter([alpha := 1.0 / (1 + 2 * np.pi * fc / sr), -alpha],
                   [1.0, -alpha], a)
    assert np.allclose(got, want, atol=1e-6)
    # 与循环参考实现逐点一致
    assert np.allclose(got, _loop(a, sr, fc), atol=1e-6)
    # stereo：逐声道独立
    st = np.stack([a, a[::-1]], axis=1).astype(np.float32)
    got_st = filters.highpass(st, sr, fc)
    assert np.allclose(got_st[:, 0], got, atol=1e-6)
    assert np.allclose(got_st[:, 1], filters.highpass(st[:, 1].copy(), sr, fc), atol=1e-6)


def test_compressor_reduces_peak():
    """压缩器降低超过阈值的峰值。"""
    audio = np.full(4410, 0.9, dtype=np.float32)  # 峰值 0.9（-0.9dBFS）> 阈值 -12dBFS
    out = filters.compressor(audio, ratio=4.0, threshold_db=-12.0)
    assert float(np.max(np.abs(out))) < 0.9 - 1e-6
    # 未超阈值部分不受影响
    quiet = np.full(4410, 0.1, dtype=np.float32)
    out_q = filters.compressor(quiet, ratio=4.0, threshold_db=-12.0)
    assert float(np.max(np.abs(out_q))) == pytest.approx(0.1, abs=1e-6)


def test_eq_compressor_no_clipping(tmp_path):
    """开启 EQ + 压缩后输出无削波/无 NaN。"""
    src = _sine_wav(tmp_path / "src.wav", amplitude=0.85)
    data = _process(
        DspOptions(eq=True, eq_low_cut_hz=30.0, compressor=True,
                   compressor_ratio=2.0, compressor_threshold_db=-12.0),
        src, tmp_path,
    )
    assert np.isfinite(data).all()
    assert float(np.max(np.abs(data))) <= PEAK_ABS + 1e-4


# ---------------------------------------------------------------------------
# 输出完整性（P2-1 验收 5）
# ---------------------------------------------------------------------------

def test_process_writes_file(tmp_path):
    """process 产出 WAV 文件（16bit/44.1kHz）。"""
    src = _sine_wav(tmp_path / "src.wav")
    out = tmp_path / "out.wav"
    ret = DspProcessor().process(str(src), DspOptions(), str(out))
    assert Path(ret).is_file()
    data, sr = audio_ops.read_wav(out)
    assert sr == 44100
    assert len(data) > 0
