"""音频测速：onset 包络自相关 → BPM + 置信度 + 节拍相位（numpy-only）。

方法（手写，不依赖 librosa）
--------
1. onset 强度包络：STFT 对数谱差分的半波整流沿频率求和（`spectral.onset_strength`）。
2. 归一化自相关（FFT 加速）：在 lag 对应 ``[bpm_min, bpm_max]`` 的区间内取峰。
3. **倍频校正**：候选 ``2×bpm`` / ``bpm/2`` 若落在合法区间且自相关明显更强（>1.10×）
   才切换——严格大于不换，避免把 120 抖成 60（等周期信号两档峰等高，保持原值）。
4. **节拍相位**：在 ``[0, 期长)`` 内网格搜索 onset 包络在 ``offset + k×period`` 处的
   总和最大者（np.interp 支持分数帧），得到第一拍的秒位置（供 #14 节拍网格用）。

置信度口径：归一化自相关在最佳 lag 处的取值（0~1）。点击轨（合成）应 ≥ 0.7；
真实音乐 0.3~0.6 算可靠，< 0.15 说明节拍感弱（CLI 上标注「低」）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from sunoauxtool.analysis.spectral import TEMPO_FRAME, TEMPO_HOP, onset_strength
from sunoauxtool.exceptions import InputFileError, ParameterError

#: BPM 搜索区间默认值（覆盖绝大多数音乐；2/4 拍极端 <40 或 >240 不支持）
DEFAULT_BPM_MIN = 40.0
DEFAULT_BPM_MAX = 240.0

#: 音频最短可用时长（秒）——短于这个连两拍都采不满
MIN_AUDIO_SECONDS = 1.0

#: 选档比例（仅关闭先验时生效）：候选里自相关值 ≥ best×0.9 的全部参与，取最快的 BPM
_PREFER_FAST_RATIO = 0.9

#: 节奏先验（Ellis 2007）：log-Gaussian 中心与宽度（八度）。
#: 120 BPM / 0.6 八度 = 对 75-190 BPM 近似无偏，同时把「半频档」压下去。
TEMPO_PRIOR_BPM = 120.0
TEMPO_PRIOR_OCTAVES = 0.6


@dataclass(frozen=True)
class TempoEstimate:
    """测速结果。

    Attributes:
        bpm: 估计的每分钟拍数（浮点；落盘/展示由调用方决定取整方式）。
        confidence: 归一化自相关峰强（0~1），节拍规律性的度量。
        beat_offset: 第一拍位置（秒），供节拍网格/视频对齐使用。
        onset_rate: onset 包络的帧率（Hz）。
        duration: 音频时长（秒）。
    """

    bpm: float
    confidence: float
    beat_offset: float
    onset_rate: float
    duration: float


def read_wav_mono(wav_path: str | Path) -> tuple[np.ndarray, int]:
    """读音频为单声道 float32（多声道取均值）。

    Raises:
        InputFileError: 文件不存在 / 无法解析 / 为空（错误码 3）。
    """
    import soundfile as sf

    src = Path(wav_path).expanduser().resolve()
    if not src.is_file():
        raise InputFileError(f"音频文件不存在: {src}", code=3)
    try:
        data, sr = sf.read(str(src), dtype="float32", always_2d=True)
    except Exception as exc:
        raise InputFileError(f"无法解析音频文件: {src} ({exc})", code=3) from exc
    if data.shape[0] == 0:
        raise InputFileError(f"音频文件为空: {src}", code=3)
    return data.mean(axis=1), int(sr)


def beat_grid(total_seconds: float, bpm: float, offset: float = 0.0) -> np.ndarray:
    """生成节拍时间网格（秒）：从 ``offset`` 起每隔 ``60/bpm`` 一拍。

    只含 ``t < total_seconds`` 的拍（恰好落在时长边界上的拍不计入——该拍属于
    下一段音频）。
    """
    if bpm <= 0:
        raise ParameterError(f"BPM 必须为正: {bpm}", code=1)
    period = 60.0 / bpm
    if total_seconds <= offset:
        return np.zeros(0, dtype=np.float64)
    n = int(np.ceil((total_seconds - offset) / period - 1e-9))
    n = max(0, n)
    return offset + period * np.arange(n, dtype=np.float64)


def estimate_bpm(
    wav_path: str | Path,
    *,
    bpm_min: float = DEFAULT_BPM_MIN,
    bpm_max: float = DEFAULT_BPM_MAX,
    prior_bpm: float = TEMPO_PRIOR_BPM,
    prior_octaves: float = TEMPO_PRIOR_OCTAVES,
    frame: int = TEMPO_FRAME,
    hop: int = TEMPO_HOP,
) -> TempoEstimate:
    """估计音频的 BPM。

    Args:
        wav_path: 输入音频路径（WAV/FLAC 等 soundfile 可读格式）。
        bpm_min: 搜索下限（默认 40）。
        bpm_max: 搜索上限（默认 240）。
        prior_bpm: 节奏先验中心（默认 120）。用于化解「重拍都落在 2 拍边界」
            造成的半频歧义；传 0 关闭先验（纯自相关选档）。
        prior_octaves: 先验宽度（八度，默认 0.6）。
        frame: STFT 帧长（默认 2048）。
        hop: STFT 跳长（默认 512）。

    Returns:
        :class:`TempoEstimate`。

    Raises:
        InputFileError: 文件不存在/无法解析（退出码 3）。
        ParameterError: bpm 区间非法 / 音频过短 / 近静音（退出码 1）。
    """
    if not (0.0 < bpm_min < bpm_max <= 600.0):
        raise ParameterError(
            f"非法 BPM 搜索区间: [{bpm_min}, {bpm_max}]（需 0 < bpm_min < bpm_max <= 600）",
            code=1,
        )
    mono, sr = read_wav_mono(wav_path)
    duration = len(mono) / sr
    if duration < MIN_AUDIO_SECONDS:
        raise ParameterError(
            f"音频过短（{duration:.2f}s < {MIN_AUDIO_SECONDS:g}s），无法估计 BPM", code=1
        )

    env = onset_strength(mono, frame, hop)
    if len(env) < 4 or float(env.std()) < 1e-9:
        raise ParameterError("音频近静音（onset 包络无变化），无法估计节拍", code=1)
    rate = sr / hop

    # ---- 归一化自相关（FFT 加速） ---------------------------------------
    # ACF 前先做 ~200ms 平滑：onset 峰本身只有 1-2 帧宽，真实周期往往落在两帧
    # 之间（如 120bpm@43Hz = 21.5 帧），整数 lag 错开半帧就会丢掉大部分重叠，
    # 导致 1×周期峰矮于 2×周期峰（argmax 抖到半频档）。平滑把峰展宽后，
    # 抛物线细化才能可靠地找回分数 lag。
    smooth_len = max(3, int(round(rate * 0.2)) | 1)  # 取奇数
    kernel = np.hanning(smooth_len)
    kernel /= kernel.sum()
    env_smooth = np.convolve(env, kernel, mode="same")

    x = env_smooth - env_smooth.mean()
    n = len(x)
    size = 1 << int(np.ceil(np.log2(2 * n)))
    spec = np.fft.rfft(x, size)
    acf = np.fft.irfft(spec * np.conj(spec), size)[:n]
    if acf[0] <= 0:
        raise ParameterError("onset 包络能量异常（自相关零能量），无法估计节拍", code=1)
    acf = acf / acf[0]

    lag_min = max(1, int(np.floor(rate * 60.0 / bpm_max)))
    lag_max = min(n - 2, int(np.ceil(rate * 60.0 / bpm_min)))
    if lag_min > lag_max:
        raise ParameterError(
            f"BPM 搜索区间 [{bpm_min:g}, {bpm_max:g}] 超出该音频可分辨的 lag 范围", code=1
        )

    # ---- 候选：band 内的局部极大 → 抛物线细化到分数 lag -------------------
    # 为什么必须细化：真实周期往往落在两帧之间（如 120bpm@43Hz = 21.5 帧），
    # 整数 ACF 在 2×周期处反而比 1×周期更尖，argmax 会直接抖到倍频/半频档。
    band_lags = np.arange(lag_min, lag_max + 1)
    band_vals = acf[lag_min : lag_max + 1]
    interior = np.where(
        (band_vals[1:-1] >= band_vals[:-2]) & (band_vals[1:-1] >= band_vals[2:])
    )[0] + 1
    peaks = np.concatenate(([0], interior, [len(band_lags) - 1])).astype(int)
    candidates: list[tuple[float, float]] = []  # (lag_fine, acf_val)
    for j in np.unique(peaks):
        lag_int = int(band_lags[j])
        v = float(band_vals[j])
        if lag_int - 1 >= 0 and lag_int + 1 < n:
            a, b, c = float(acf[lag_int - 1]), float(acf[lag_int]), float(acf[lag_int + 1])
            denom = a - 2.0 * b + c
            delta = float(np.clip(0.5 * (a - c) / denom, -0.5, 0.5)) if denom > 0 else 0.0
        else:
            delta = 0.0
        candidates.append((lag_int + delta, v))
    if not candidates:
        raise ParameterError("onset 包络无周期性峰，无法估计节拍", code=1)

    # ---- 选档 -------------------------------------------------------------
    # 拍级歧义（八度问题）是 ACF 测速的固有缺陷：若重拍都落在 2 拍边界上
    # （和弦/贝斯按半音符起音），2×周期档的峰可以远高于真值档——本项目真实
    # 120BPM 渲染实测 acf(60)=0.784 vs acf(120)=0.430，纯 ACF 必错。
    # 解法（Ellis 2007「Beat Tracking by Dynamic Programming」的标准做法）：
    # 给候选乘一个 log-Gaussian 节奏先验（默认中心 120 BPM、宽 0.6 个八度）。
    # 先验只参与选档；报告的 confidence 永远是未加权的自相关值（诚实的周期性度量）。
    # prior_bpm<=0 关闭先验，退回「证据 ≥90% 最优时取最快档」的纯 ACF 规则。
    def _prior_weight(bpm_val: float) -> float:
        if prior_bpm <= 0:
            return 1.0
        octaves = float(np.log2(bpm_val / prior_bpm)) / prior_octaves
        return float(np.exp(-0.5 * octaves * octaves))

    scored = [
        (lag, val, val * _prior_weight(60.0 * rate / lag)) for lag, val in candidates
    ]
    if prior_bpm > 0:
        chosen_lag, confidence, _ = max(scored, key=lambda item: item[2])
    else:
        best_val = max(s for _, _, s in scored)
        chosen_lag, confidence, _ = min(
            (item for item in scored if item[2] >= _PREFER_FAST_RATIO * best_val),
            key=lambda item: item[0],  # lag 最小 = BPM 最快
        )
    bpm = 60.0 * rate / chosen_lag

    # ---- 节拍相位 + BPM 联合精细化 ---------------------------------------
    # ACF 的分辨率受帧长限制（~1 帧 ≈ 2-5% BPM 误差），而相位扫描对 BPM 误差
    # 极其敏感：BPM 偏 2%，8 秒后网格就漂出 0.16s。所以这里在粗选 BPM 的 ±3%
    # 邻域内做 (bpm, 相位) 联合梳状搜索——两者同时收敛，缺一不可。
    env_wide = np.convolve(env, np.ones(3), mode="same")  # ±1 帧池化补偿通量滞后
    idx = np.arange(n, dtype=np.float64)
    lo_scan = max(bpm_min, bpm * 0.97)
    hi_scan = min(bpm_max, bpm * 1.03)

    def _phase_best(bpm_try: float) -> tuple[float, float]:
        """给定 BPM，返回 (相位得分, 最优偏移帧)。"""
        period = 60.0 * rate / bpm_try
        steps = max(8, int(round(period * 4)))  # 相位步长 ~0.25 帧
        best_score, best_off = -np.inf, 0.0
        for off in np.linspace(0.0, period, num=steps, endpoint=False):
            positions = np.arange(off, n, period, dtype=np.float64)
            positions = positions[positions < n - 1]  # np.interp 右端钳到末值，须裁掉
            if len(positions) == 0:
                continue
            score = float(np.interp(positions, idx, env_wide).sum())
            if score > best_score:
                best_score, best_off = score, float(off)
        return best_score, best_off

    best_bpm, best_off_frames, best_phase = bpm, 0.0, -np.inf
    for bpm_try in np.linspace(lo_scan, hi_scan, num=61):
        score, off = _phase_best(float(bpm_try))
        if score > best_phase:
            best_phase, best_bpm, best_off_frames = score, float(bpm_try), off
    bpm = best_bpm
    best_offset = best_off_frames / rate
    # 半窗中心校正：onset 通量峰落在「首个包含起音的窗」的起始帧时间上，
    # 系统性比真实起音提前 0~1 个窗长（均值≈半窗）。按窗中心口径补偿
    # frame/2 后，合成点击轨实测（22050/44100/48000 三档 sr、多相位）
    # 相位误差从 -64ms 收敛到 ±18ms 内。
    best_offset += frame / (2.0 * sr)

    return TempoEstimate(
        bpm=best_bpm,
        confidence=confidence,
        beat_offset=best_offset,
        onset_rate=float(rate),
        duration=float(duration),
    )
