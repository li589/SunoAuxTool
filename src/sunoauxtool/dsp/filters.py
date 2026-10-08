"""DSP 滤波器实现（P2-1）：EQ 高通 + 压缩器（纯 numpy）。

1.4.0 起新增：expander（纯 numpy）与 limiter（前瞻拐点限幅；
scipy.ndimage/scipy.signal 延迟导入，函数内 import，不抬高模块加载成本）。
1.5.0 起新增：eq_band（RBJ cookbook biquad：peak/lowshelf/highshelf 三段 EQ）
与 gate（噪声门，attack/release 平滑；同 limiter 的"窗口滤波 + 亏损域低通"套路）。
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


# ---------------------------------------------------------------------------
# EQ（1.5.0 F4）：RBJ cookbook biquad —— peak / lowshelf / highshelf
# ---------------------------------------------------------------------------

#: 支持的 EQ 频段类型
EQ_BANDS = ("peak", "lowshelf", "highshelf")

#: 峰值/搁架增益上限（dB），防极端参数爆音
EQ_MAX_GAIN_DB = 24.0


def _rbj_coeffs(
    band: str, sr: int, freq_hz: float, gain_db: float, q: float
) -> tuple[np.ndarray, np.ndarray]:
    """RBJ Audio EQ Cookbook 系数（归一化 a0=1）。

    peaking 在 w0 处幅度响应恰为 A^2 = 10^(gain/20)（分子分母在 w0 处
    同时退化为 αA 与 α/A 之比，可解析验证），测试按此断言。
    shelf 按 cookbook 固定 S=1（Q 参数不参与，避免斜率歧义）。
    """
    a = float(10 ** (float(gain_db) / 40.0))  # A = 10^(gain/40)
    w0 = 2.0 * np.pi * float(freq_hz) / float(sr)
    cw = float(np.cos(w0))
    if band == "peak":
        alpha = float(np.sin(w0)) / (2.0 * float(q))
        b = np.array([1.0 + alpha * a, -2.0 * cw, 1.0 - alpha * a])
        aa = np.array([1.0 + alpha / a, -2.0 * cw, 1.0 - alpha / a])
    elif band == "lowshelf":
        alpha = float(np.sin(w0)) / 2.0  # S=1
        sq = 2.0 * float(np.sqrt(a)) * alpha
        b = a * np.array([(a + 1) - (a - 1) * cw + sq,
                          2.0 * ((a - 1) - (a + 1) * cw),
                          (a + 1) - (a - 1) * cw - sq])
        aa = np.array([(a + 1) + (a - 1) * cw + sq,
                       -2.0 * ((a - 1) + (a + 1) * cw),
                       (a + 1) + (a - 1) * cw - sq])
    else:  # highshelf
        alpha = float(np.sin(w0)) / 2.0  # S=1
        sq = 2.0 * float(np.sqrt(a)) * alpha
        b = a * np.array([(a + 1) + (a - 1) * cw + sq,
                          -2.0 * ((a - 1) + (a + 1) * cw),
                          (a + 1) + (a - 1) * cw - sq])
        aa = np.array([(a + 1) - (a - 1) * cw + sq,
                       2.0 * ((a - 1) - (a + 1) * cw),
                       (a + 1) - (a - 1) * cw - sq])
    return (b / aa[0]), (aa / aa[0])


def eq_band(
    audio: np.ndarray,
    sample_rate: int,
    band: str,
    freq_hz: float,
    gain_db: float,
    q: float = 1.0,
) -> np.ndarray:
    """单频段 EQ（1.5.0 F4）：``band ∈ {peak, lowshelf, highshelf}``。

    - ``peak``：峰值 EQ（中心频率 freq_hz 处增益 gain_db，Q 控制带宽）；
    - ``lowshelf``：低搁架（freq_hz 以下整体抬/降，S=1 固定斜率）；
    - ``highshelf``：高搁架（freq_hz 以上整体抬/降）。

    gain_db=0 恒等返回；|gain_db| > 24 / freq 越界 / q <= 0 / 未知 band → ValueError
    （由 ops 层转 DspParamError(16)）。多声道逐声道独立滤波（lfilter axis=0）。
    """
    if band not in EQ_BANDS:
        raise ValueError(f"未知 EQ 频段: {band!r}（可用: {', '.join(EQ_BANDS)}）")
    freq = float(freq_hz)
    if not 0.0 < freq < float(sample_rate) / 2.0:
        raise ValueError(f"EQ 频率须在 (0, Nyquist) 内: {freq}Hz (sr={sample_rate})")
    gain = float(gain_db)
    if abs(gain) > EQ_MAX_GAIN_DB:
        raise ValueError(f"EQ 增益绝对值须 <= {EQ_MAX_GAIN_DB:.0f} dB: {gain}")
    qf = float(q)
    if qf <= 0:
        raise ValueError(f"EQ Q 须 > 0: {qf}")
    if gain == 0.0:
        return audio.astype(np.float32, copy=True)

    from scipy.signal import lfilter

    b, a = _rbj_coeffs(band, sample_rate, freq, gain, qf)
    out = lfilter(b, a, np.asarray(audio, dtype=np.float64), axis=0)
    return out.astype(np.float32)


def gate(
    audio: np.ndarray,
    sample_rate: int,
    threshold_db: float = -50.0,
    attack_ms: float = 5.0,
    release_ms: float = 100.0,
) -> np.ndarray:
    """噪声门（1.5.0 F4）：低于阈值的部分渐闭到静音，高于阈值即时打开。

    实现（与 limiter 同套路，向量化无逐样本循环）：
    1. 门限判定：``need = level > threshold``（多声道按瞬时最大值联动）；
    2. **attack/hold**：对 need 做 attack 窗口最大值滤波（scipy.ndimage
       maximum_filter1d）——打开即时生效，并保持 attack_ms 防抖；
    3. **release**：在"亏损域"（deficit = 1 - need）上做一阶低通（scipy.signal
       lfilter，时间常数 release_ms），gain_release = 1 - smoothed——关闭后
       增益从满值按 release_ms 平滑回落（若直接低通 need，短促开段内增益爬
       不满 1，关门会从低值起衰——实测踩过，与 limiter 的开机瞬态同理）；
       增益 = max(attack 增益, release 增益)，保证打开不被 release 拖慢。
    """
    from scipy.ndimage import maximum_filter1d
    from scipy.signal import lfilter

    threshold_lin = float(10 ** (float(threshold_db) / 20.0))
    level = np.abs(audio)
    if level.ndim == 2:  # 多声道瞬时联动
        level = level.max(axis=1, keepdims=True)
    need = (level > threshold_lin).astype(np.float32)

    win = max(1, int(float(attack_ms) * float(sample_rate) / 1000.0))
    gain_attack = maximum_filter1d(need, size=win, axis=0, mode="nearest") if win > 1 else need

    n_rel = max(1.0, float(release_ms) * float(sample_rate) / 1000.0)
    coef = float(np.exp(-1.0 / n_rel))
    deficit = (1.0 - need).astype(np.float32)
    smoothed = lfilter([1.0 - coef], [1.0, -coef], deficit, axis=0).astype(np.float32)
    gain_release = 1.0 - smoothed

    gain = np.maximum(gain_attack, gain_release)
    if audio.ndim == 1 and gain.ndim == 2:
        gain = gain[:, 0]
    return (audio * gain).astype(np.float32)
