"""DSP 滤波器实现（P2-1）：EQ 高通 + 压缩器（纯 numpy）。

1.4.0 起新增：expander（纯 numpy）与 limiter（前瞻拐点限幅；
scipy.ndimage/scipy.signal 延迟导入，函数内 import，不抬高模块加载成本）。
"""

from __future__ import annotations

import numpy as np


def highpass(
    audio: np.ndarray,
    sample_rate: int,
    cutoff_hz: float,
) -> np.ndarray:
    """一阶高通滤波器（单极点 IIR），用于低频切。

    传递函数：y[n] = alpha * (y[n-1] + x[n] - x[n-1])，alpha = 1 / (1 + 2πfc/fs)。
    对直流分量有完全抑制效果。
    """
    alpha = 1.0 / (1.0 + 2.0 * np.pi * float(cutoff_hz) / float(sample_rate))
    out = np.zeros_like(audio, dtype=np.float32)

    def _apply_one(x: np.ndarray, o: np.ndarray) -> None:
        prev_y = 0.0
        prev_x = 0.0
        for i in range(len(x)):
            xi = float(x[i])
            yi = alpha * (prev_y + xi - prev_x)
            prev_y = yi
            prev_x = xi
            o[i] = yi

    if audio.ndim == 2:
        for c in range(audio.shape[1]):
            _apply_one(audio[:, c], out[:, c])
    else:
        _apply_one(audio, out)
    return out


def compressor(
    audio: np.ndarray,
    ratio: float,
    threshold_db: float,
) -> np.ndarray:
    """软拐点压缩器：超过阈值的部分按 ratio 压缩。

    超过阈值部分：output_level = threshold + (level - threshold) / ratio。
    ratio 必须 >= 1（由 DspProcessor.validate 保证）。
    """
    threshold_lin = float(10 ** (float(threshold_db) / 20.0))
    level = np.abs(audio)
    over = level > threshold_lin
    gain = np.ones_like(level, dtype=np.float32)
    if np.any(over):
        gain[over] = (
            (threshold_lin + (level[over] - threshold_lin) / float(ratio)) / level[over]
        ).astype(np.float32)
    return (audio * gain).astype(np.float32)


def expander(
    audio: np.ndarray,
    ratio: float,
    threshold_db: float,
) -> np.ndarray:
    """软拐点向下扩展器（1.4.0）：低于阈值的部分按 ratio 衰减。

    低于阈值部分：output_level = threshold * (level / threshold) ** ratio，
    即增益 = (level / threshold) ** (ratio - 1)；在阈值处增益恰为 1（连续），
    level -> 0 时增益 -> 0（ratio > 1 时把底噪/弱段进一步压低）。
    ratio 必须 >= 1（=1 时恒等，即无操作）。
    """
    threshold_lin = float(10 ** (float(threshold_db) / 20.0))
    level = np.abs(audio)
    under = level < threshold_lin
    gain = np.ones_like(level, dtype=np.float32)
    if np.any(under):
        gain[under] = ((level[under] / threshold_lin) ** (float(ratio) - 1.0)).astype(
            np.float32
        )
    return (audio * gain).astype(np.float32)


def limiter(
    audio: np.ndarray,
    sample_rate: int,
    ceiling_db: float = -0.3,
    lookahead_ms: float = 5.0,
    release_ms: float = 50.0,
) -> np.ndarray:
    """前瞻拐点限幅器（1.4.0）：把峰值硬顶在 ceiling_db 之下。

    实现（numpy + scipy 延迟导入）：
    1. 每样本需求增益 ``need = min(1, target / level)``（多声道按瞬时最大值联动）；
    2. **attack**：对 need 做 lookahead 窗口最小值滤波（scipy.ndimage.minimum_filter1d），
       增益在峰值到达前先降——这是"前瞻"；
    3. **release**：对 need 做一阶低通（scipy.signal.lfilter，时间常数 release_ms），
       峰值过后增益缓慢回升；
    4. 最终增益 = min(attack 增益, release 增益)。由于 attack 增益 <= need[t]，
       ``|out| <= level * need = target`` 处处成立——**brickwall 有数学保证**，
       release 平滑只放慢回升、不放大峰值。
    """
    from scipy.ndimage import minimum_filter1d
    from scipy.signal import lfilter

    target = float(10 ** (float(ceiling_db) / 20.0))
    level = np.abs(audio)
    if level.ndim == 2:  # 多声道瞬时联动：取逐样本最大值
        level = level.max(axis=1, keepdims=True)
    need = np.where(level > 1e-9, target / np.maximum(level, 1e-9), 1.0).astype(np.float32)
    np.clip(need, 0.0, 1.0, out=need)

    win = max(1, int(float(lookahead_ms) * float(sample_rate) / 1000.0))
    gain_attack = (
        minimum_filter1d(need, size=win, axis=0, mode="nearest") if win > 1 else need
    )

    n_rel = max(1.0, float(release_ms) * float(sample_rate) / 1000.0)
    coef = float(np.exp(-1.0 / n_rel))
    # release 在"亏损域"（deficit = 1 - need）上做一阶低通：
    # lfilter 零初始状态 -> 亏损从 0 开始平滑爬升（安静段增益恒 1，无开机瞬态）；
    # 若直接低通 need 本身，增益会从 ~0 慢慢爬到 1（开机瞬态，实测踩过）。
    deficit = (1.0 - need).astype(np.float32)
    smoothed = lfilter([1.0 - coef], [1.0, -coef], deficit, axis=0).astype(np.float32)
    gain_release = 1.0 - smoothed

    gain = np.minimum(gain_attack, gain_release)
    return (audio * gain).astype(np.float32)
