"""EBU R128 简化版积分响度（ITU-R BS.1770 口径，R6）。

纯 numpy/scipy 实现（无 pyloudnorm 依赖）：
- K 加权 = stage1 高架滤波（+4dB，高频补偿）+ stage2 RLB 高通（38Hz）
  —— 从 ITU 原型（G/Q/fc）出发对每个采样率重新做 bilinear 变换
  （1.4.6 修复：旧实现对 48k 系数做幂次缩放、缺 bilinear 步骤，
  非 48k 频响有 -2~+6dB 系统性偏置；与 pyloudnorm 的
  ``ITU-R BS.1770`` 滤波器同口径）；
- 分块 400ms、步进 100ms（75% 重叠）；
- 门控：绝对门 -70 LUFS + 相对门（块均值 -10 LU）；
- 积分响度 LUFS = -0.691 + 10*log10(门控块能量和 / 块数)。
"""

from __future__ import annotations

import numpy as np
from scipy.signal import lfilter

#: ITU-R BS.1770 stage1（高架 shelving）@48kHz 原始系数（b/a）——仅作 48k 回归参照
_STAGE1_48K = {
    "b": [1.53512485958697, -2.69169618940638, 1.19839281085285],
    "a": [1.0, -1.69065929318241, 0.73248077421585],
}
#: ITU-R BS.1770 stage2（RLB 高通）@48kHz 原始系数——仅作 48k 回归参照
_STAGE2_48K = {
    "b": [1.0, -2.0, 1.0],
    "a": [1.0, -1.99004745483398, 0.99007225036621],
}
#: 48kHz 双线性参考（测试与文档用）
_REF_SR = 48000.0

#: ITU-R BS.1770 高架滤波原型（fc=1681.97Hz, G≈+4dB, Q≈0.7072），
#: pyloudnorm / Adobe BS.1770 实现同源参数
_STAGE1_PROTO = (1681.974450955533, 3.999843853973347, 0.7071752369554196)
#: ITU-R BS.1770 RLB 高通原型（fc≈38.13Hz, Q≈0.5003）
_STAGE2_PROTO = (38.13547087602444, 0.5003270373238773)


def _high_shelf_coeffs(
    sr: float, fc: float, g_db: float, q: float
) -> tuple[np.ndarray, np.ndarray]:
    """按 ITU 口径对高架 shelving 做逐 sr 的 bilinear 变换（a[0]=1 归一化）。"""
    k = float(np.tan(np.pi * fc / sr))
    vh = 10.0 ** (g_db / 20.0)
    vb = vh ** 0.4996667741545416  # pyloudnorm 同款带宽修正常数
    a0 = 1.0 + k / q + k * k
    b = [
        (vh + vb * k / q + k * k) / a0,
        2.0 * (k * k - vh) / a0,
        (vh - vb * k / q + k * k) / a0,
    ]
    a = [1.0, 2.0 * (k * k - 1.0) / a0, (1.0 - k / q + k * k) / a0]
    return np.asarray(b), np.asarray(a)


def _high_pass_coeffs(
    sr: float, fc: float, q: float
) -> tuple[np.ndarray, np.ndarray]:
    """按 ITU 口径对二阶高通做逐 sr 的 bilinear 变换（a[0]=1 归一化）。"""
    k = float(np.tan(np.pi * fc / sr))
    a0 = 1.0 + k / q + k * k
    b = [1.0, -2.0, 1.0]
    a = [1.0, 2.0 * (k * k - 1.0) / a0, (1.0 - k / q + k * k) / a0]
    return np.asarray(b), np.asarray(a)


def _scale_stage(
    b: list[float], a: list[float], sr: float
) -> tuple[np.ndarray, np.ndarray]:
    """按采样率给出 K 加权某一级的 biquad 系数。

    实现：不再对 48kHz 系数做幂次缩放（旧实现，非 48k 有 -2~+6dB 系统性偏置），
    而是从 ITU 原型（G/Q/fc）出发对每个 sr 重新做 bilinear 变换
    （pyloudnorm ``ITU-R BS.1770`` 滤波器同口径）。``b``/``a`` 参数保留仅为
    兼容旧签名，实际系数由原型决定。
    """
    del b, a  # 旧签名兼容：系数由原型参数决定
    fc1, g1, q1 = _STAGE1_PROTO
    return _high_shelf_coeffs(sr, fc1, g1, q1)


def k_weight(audio: np.ndarray, sr: int) -> np.ndarray:
    """K 加权滤波（单声道 (N,) 或多声道 (N, ch)）。"""
    fc1, g1, q1 = _STAGE1_PROTO
    b1, a1 = _high_shelf_coeffs(sr, fc1, g1, q1)
    fc2, q2 = _STAGE2_PROTO
    b2, a2 = _high_pass_coeffs(sr, fc2, q2)
    x = np.atleast_2d(audio.T).T.astype(np.float64)  # (N,) -> (N, 1)
    x = lfilter(b1, a1, x, axis=0)
    x = lfilter(b2, a2, x, axis=0)
    return x


def integrated_lufs(audio: np.ndarray, sr: int) -> float:
    """门控积分响度（LUFS）。

    Raises:
        ValueError: 输入为数字静音（无能量块，无法定义响度）。
    """
    x = k_weight(audio, sr)
    if x.ndim == 1:
        x = x[:, np.newaxis]

    block = int(0.4 * sr)  # 400ms
    step = int(0.1 * sr)  # 100ms
    n_blocks = 1 + (x.shape[0] - block) // step if x.shape[0] >= block else 0
    if n_blocks <= 0:
        raise ValueError("音频短于单个 400ms 测量块")

    # 每块能量 = 各声道均方之和（ITU：z_j = Σ_channels mean-square）
    block_energy = np.empty(n_blocks, dtype=np.float64)
    for i in range(n_blocks):
        seg = x[i * step : i * step + block]
        block_energy[i] = float(np.sum(np.mean(seg**2, axis=0)))

    def _lufs(e: float) -> float:
        return -0.691 + 10.0 * np.log10(max(e, 1e-24))

    # 绝对门 -70 LUFS
    loud = np.array([_lufs(e) for e in block_energy])
    keep = loud > -70.0
    if not np.any(keep):
        raise ValueError("全静音输入（无块高于绝对门 -70 LUFS）")

    # 相对门：过绝对门块的响度均值 -10 LU
    gated_mean = float(np.mean(loud[keep]))
    keep &= loud > gated_mean - 10.0
    if not np.any(keep):
        raise ValueError("无块高于相对门")

    # ITU 公式：门控块能量的**均值**（不是求和！sum 会让结果随块数漂移）
    return float(_lufs(float(np.mean(block_energy[keep]))))


def gain_to_target_lufs(audio: np.ndarray, sr: int, target_lufs: float) -> np.ndarray:
    """返回把 audio 归一到 target_lufs 所需的增益系数（浮点倍数）。"""
    lufs = integrated_lufs(audio, sr)
    return float(10 ** ((target_lufs - lufs) / 20.0))
