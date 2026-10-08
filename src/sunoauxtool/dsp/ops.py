"""DSP 算子框架（R6）：管道式 ops 串解析 + 顺序执行。

设计（继承重构计划 R6 决策）：
- **numpy/scipy 为主**（可控、可单测），ffmpeg 只做兜底不在本层；
- 算子串语法：逗号分隔，``name`` 或 ``name arg`` 或 ``name key=value``，如::

    "norm -1, fade-in 0.5, trim 10-25, loudnorm -16, lowcut 80, resample 32000,
     concat other.wav xf 0.5, compress 3 -14"

- 每个算子是一个纯函数 ``(audio, sr, args) -> (audio, sr)``；注册表驱动，
  新算子加一个函数 + 一行注册即可；
- 错误码分段：解析/参数非法 -> DspParamError(16)；运行失败 -> DspError(15)。
- 既有 ``DspProcessor``（pipeline 内部链）不动，本模块是独立通用层。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from sunoauxtool.dsp import filters
from sunoauxtool.exceptions import DspError, DspParamError
from sunoauxtool.export import audio as audio_ops

#: 算子函数签名：fn(audio, sr, args) -> (audio, sr)
OpFunc = Callable[[np.ndarray, int, Dict[str, str]], Tuple[np.ndarray, int]]


# ---------------------------------------------------------------------------
# ops 串解析
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\s+")


@dataclass
class DspOp:
    """单个算子调用：名称 + 参数字典（位置参数按序落入 _0/_1/...）。"""

    name: str
    args: Dict[str, str] = field(default_factory=dict)

    def arg(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self.args.get(key, default)


def parse_ops(spec: str) -> List[DspOp]:
    """解析 ops 串为算子列表。

    语法：逗号分隔的 ``name [token...]``；token 形如 ``key=value`` 落入字典，
    否则按序落入 ``_0`` ``_1`` ...（第一个 token 通常是主参数，也写入 ``arg``）。

    Raises:
        DspParamError: 空串 / token 语法非法。
    """
    ops: List[DspOp] = []
    for raw in spec.split(","):
        chunk = raw.strip()
        if not chunk:
            continue
        tokens = _TOKEN_RE.split(chunk)
        name = tokens[0]
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
            raise DspParamError(f"非法算子名: {name!r}", code=16)
        args: Dict[str, str] = {}
        positional: List[str] = []
        for tok in tokens[1:]:
            if "=" in tok:
                k, _, v = tok.partition("=")
                if not k or not v:
                    raise DspParamError(f"算子 {name} 的参数 {tok!r} 非法（key=value）", code=16)
                args[k] = v
            else:
                positional.append(tok)
        for i, p in enumerate(positional):
            args[f"_{i}"] = p
        if positional:
            args.setdefault("arg", positional[0])
        ops.append(DspOp(name=name, args=args))
    if not ops:
        raise DspParamError("ops 串为空", code=16)
    return ops


# ---------------------------------------------------------------------------
# 算子实现（纯函数；audio: (N,) 或 (N, ch) float32）
# ---------------------------------------------------------------------------


def op_norm(audio: np.ndarray, sr: int, args: Dict[str, str]) -> Tuple[np.ndarray, int]:
    """峰值归一化到目标 dBFS（默认 -1，可放大可衰减）。``norm [-1]``"""
    db = float(args.get("arg", "-1"))
    if db > 0:
        raise DspParamError("norm 目标 dBFS 必须 <= 0", code=16)
    target = float(10 ** (db / 20.0))
    max_abs = float(np.max(np.abs(audio))) if audio.size else 0.0
    if max_abs <= 1e-9:  # 静音不缩放（避免除零）
        return audio, sr
    return (audio * (target / max_abs)).astype(np.float32), sr


def op_loudnorm(audio: np.ndarray, sr: int, args: Dict[str, str]) -> Tuple[np.ndarray, int]:
    """EBU R128 简化版响度归一：K 加权 + 门控积分响度 -> 目标 LUFS（默认 -16）。

    K 加权两阶段：stage1 高架 +4dB、stage2 RLB 高通（38Hz），均从 ITU 原型
    出发按 sr 重新 bilinear（1.4.6 修复非 48k 频响偏置）。分块 400ms / 步进
    100ms，绝对门 -70 LUFS + 相对门 -10 LU。纯 numpy/scipy 实现。

    峰值保护（1.4.6 B1）：增益后自动串联 ``filters.limiter`` 把峰值硬顶在
    ceiling 之下（默认 -0.3 dBFS）——高动态素材（crest>10）拉响度可能超过
    0dBFS，旧实现靠写盘硬 clip 兜底会产生削波失真。``ceiling=<dB>`` 自定义、
    ``ceiling=off`` 关闭保护。
    """
    target = float(args.get("arg", "-16"))
    if target > 0:
        raise DspParamError("loudnorm 目标 LUFS 必须 <= 0", code=16)
    ceiling_raw = str(args.get("ceiling", "-0.3")).strip().lower()
    ceiling_db: Optional[float]
    if ceiling_raw == "off":
        ceiling_db = None
    else:
        try:
            ceiling_db = float(ceiling_raw)
        except ValueError:
            raise DspParamError(f"loudnorm ceiling 非法: {ceiling_raw!r}", code=16) from None
        if ceiling_db >= 0:
            raise DspParamError(f"loudnorm ceiling 须 < 0 dBFS: {ceiling_db}", code=16)
    from sunoauxtool.dsp.filters import limiter
    from sunoauxtool.dsp.loudness import integrated_lufs

    try:
        lufs = integrated_lufs(audio, sr)
    except ValueError as exc:  # 静音等无法测响度的输入
        raise DspError(f"loudnorm 无法测量响度: {exc}", code=15) from exc
    gain_db = target - lufs
    out = (audio * float(10 ** (gain_db / 20.0))).astype(np.float32)
    if ceiling_db is not None:
        out = limiter(out, sr, ceiling_db=ceiling_db).astype(np.float32)
    return out, sr


def _fit_ramp(ramp: np.ndarray, audio: np.ndarray) -> np.ndarray:
    """把一维 ramp 适配到音频形状：多声道 (N, ch) 广播为 (N, 1)。"""
    if audio.ndim == 2:
        return ramp.astype(np.float32)[:, np.newaxis]
    return ramp.astype(np.float32)


def op_fade_in(audio: np.ndarray, sr: int, args: Dict[str, str]) -> Tuple[np.ndarray, int]:
    """淡入：``fade-in 0.5``（秒，余弦曲线）。"""
    sec = float(args.get("arg", "0"))
    if sec < 0:
        raise DspParamError("fade-in 秒数须 >= 0", code=16)
    n = int(sec * sr)
    if n <= 0:
        return audio, sr
    out = np.array(audio, dtype=np.float32, copy=True)
    if n > out.shape[0]:
        raise DspParamError(f"fade-in {sec}s 超出音频长度", code=16)
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, n, endpoint=False))
    out[:n] *= _fit_ramp(ramp, out)
    return out, sr


def op_fade_out(audio: np.ndarray, sr: int, args: Dict[str, str]) -> Tuple[np.ndarray, int]:
    """淡出：``fade-out 0.5``（秒，余弦曲线）。"""
    sec = float(args.get("arg", "0"))
    if sec < 0:
        raise DspParamError("fade-out 秒数须 >= 0", code=16)
    n = int(sec * sr)
    if n <= 0:
        return audio, sr
    out = np.array(audio, dtype=np.float32, copy=True)
    if n > out.shape[0]:
        raise DspParamError(f"fade-out {sec}s 超出音频长度", code=16)
    ramp = 0.5 + 0.5 * np.cos(np.linspace(0.0, np.pi, n, endpoint=False))
    out[-n:] *= _fit_ramp(ramp, out)
    return out, sr


def op_trim(audio: np.ndarray, sr: int, args: Dict[str, str]) -> Tuple[np.ndarray, int]:
    """裁剪：``trim 10-25``（秒闭开区间 [start, end)）。"""
    rng = args.get("arg", "")
    m = re.fullmatch(r"([0-9.]+)-([0-9.]+)", rng)
    if not m:
        raise DspParamError(f"trim 参数须形如 'START-END'（秒）: {rng!r}", code=16)
    start_s, end_s = float(m.group(1)), float(m.group(2))
    total = audio.shape[0] / sr
    if start_s < 0 or end_s <= start_s or start_s >= total:
        raise DspParamError(
            f"trim 区间非法: [{start_s}, {end_s})（音频 {total:.2f}s）", code=16
        )
    a = int(start_s * sr)
    b = min(int(end_s * sr), audio.shape[0])
    return np.array(audio[a:b], dtype=np.float32, copy=True), sr


def op_resample(audio: np.ndarray, sr: int, args: Dict[str, str]) -> Tuple[np.ndarray, int]:
    """重采样：``resample 32000``（polyphase，抗混叠）。"""
    try:
        target = int(float(args.get("arg", "")))
    except ValueError as exc:
        raise DspParamError(f"resample 目标采样率非法: {args.get('arg')!r}", code=16) from exc
    if target <= 0:
        raise DspParamError(f"resample 目标采样率须 > 0: {target}", code=16)
    if target == sr:
        return audio, sr
    from math import gcd

    from scipy.signal import resample_poly

    g = gcd(target, sr)
    up, down = target // g, sr // g
    if audio.ndim == 2:
        out = resample_poly(audio, up, down, axis=0).astype(np.float32)
    else:
        out = resample_poly(audio, up, down).astype(np.float32)
    return out, target


def op_lowcut(audio: np.ndarray, sr: int, args: Dict[str, str]) -> Tuple[np.ndarray, int]:
    """低频切（EQ）：``lowcut 80``（Hz，复用既有高通滤波器）。"""
    hz = float(args.get("arg", "30"))
    if hz <= 0:
        raise DspParamError(f"lowcut 截止频率须 > 0: {hz}", code=16)
    return filters.highpass(audio, sr, hz), sr


def op_eq(audio: np.ndarray, sr: int, args: Dict[str, str]) -> Tuple[np.ndarray, int]:
    """频段 EQ（1.5.0 F4）：``eq <peak|lowshelf|highshelf> <freq_hz> <gain_db> [q]``。

    - peak：峰值 EQ（中心 freq 处增益 gain，Q 控制带宽，默认 1.0）；
    - lowshelf / highshelf：低/高搁架（freq 以下/以上整体抬降，固定 S=1）。

    频段名非法 / freq 越界（含 ≥ Nyquist）/ |gain| > 24 dB / q <= 0 → 16。
    例：``eq lowshelf 120 -3, eq peak 2500 4 0.8, eq highshelf 9000 2``。
    """
    band = str(args.get("arg", ""))
    if band not in filters.EQ_BANDS:
        raise DspParamError(
            f"eq 频段须为 {', '.join(filters.EQ_BANDS)} 之一: {band!r}", code=16
        )
    freq_raw = args.get("_1", "")
    gain_raw = args.get("_2", "")
    try:
        freq = float(freq_raw)
        gain = float(gain_raw)
    except ValueError as exc:
        raise DspParamError(f"eq 频率/增益非法: {freq_raw!r} {gain_raw!r}", code=16) from exc
    q_raw = args.get("_3", "1.0")
    try:
        q = float(q_raw)
    except ValueError as exc:
        raise DspParamError(f"eq Q 非法: {q_raw!r}", code=16) from exc
    try:
        return filters.eq_band(audio, sr, band, freq, gain, q), sr
    except ValueError as exc:
        raise DspParamError(f"eq 参数非法: {exc}", code=16) from exc


def op_gate(audio: np.ndarray, sr: int, args: Dict[str, str]) -> Tuple[np.ndarray, int]:
    """噪声门（1.5.0 F4）：``gate <threshold_db> [attack_ms] [release_ms]``。

    低于阈值（dBFS，默认 -50）的部分渐闭到静音；attack（默认 5ms）防抖、
    release（默认 100ms）平滑关闭，与 expand（软比例衰减）互补：
    gate 是硬门限 + 时间平滑，expand 是连续软曲线。参数非法 → 16。
    """
    thr_raw = args.get("arg", "-50")
    atk_raw = args.get("_1", "5")
    rel_raw = args.get("_2", "100")
    try:
        thr = float(thr_raw)
        atk = float(atk_raw)
        rel = float(rel_raw)
    except ValueError as exc:
        raise DspParamError(
            f"gate 参数非法: {thr_raw!r} {atk_raw!r} {rel_raw!r}", code=16
        ) from exc
    if thr >= 0:
        raise DspParamError(f"gate 阈值须 < 0 dBFS: {thr}", code=16)
    if atk < 0:
        raise DspParamError(f"gate attack 须 >= 0 ms: {atk}", code=16)
    if rel < 0:
        raise DspParamError(f"gate release 须 >= 0 ms: {rel}", code=16)
    return filters.gate(audio, sr, threshold_db=thr, attack_ms=atk, release_ms=rel), sr


def op_compress(audio: np.ndarray, sr: int, args: Dict[str, str]) -> Tuple[np.ndarray, int]:
    """压缩：``compress 3 [-14]``（ratio须>=1；threshold dBFS 默认 -12）。"""
    ratio = float(args.get("arg", "2"))
    thr = float(args.get("_1", "-12"))
    if ratio < 1:
        raise DspParamError(f"compress ratio 须 >= 1: {ratio}", code=16)
    return filters.compressor(audio, ratio, thr), sr


def op_expand(audio: np.ndarray, sr: int, args: Dict[str, str]) -> Tuple[np.ndarray, int]:
    """向下扩展：``expand 2 [-30]``（ratio须>=1；threshold dBFS 默认 -30）。

    与 compress 相反方向：低于阈值的部分按 ratio 进一步衰减（1.4.0），
    用于压低底噪/弱段，增强动态对比。ratio=1 时恒等。
    """
    ratio = float(args.get("arg", "2"))
    thr = float(args.get("_1", "-30"))
    if ratio < 1:
        raise DspParamError(f"expand ratio 须 >= 1（=1 为恒等）: {ratio}", code=16)
    return filters.expander(audio, ratio, thr), sr


def op_limiter(audio: np.ndarray, sr: int, args: Dict[str, str]) -> Tuple[np.ndarray, int]:
    """限幅：``limiter [-0.3] [5]``（ceiling dBFS 默认 -0.3；lookahead ms 默认 5）。

    前瞻拐点限幅器（1.4.0）：峰值硬顶在 ceiling 之下（brickwall 有数学保证），
    release 50ms 固定。混响/限幅同属"可能明显改变音色"的处理，
    与 reverb 一致只进 standalone ``--ops`` 链，不进 DspProcessor 内部链。
    """
    ceiling = float(args.get("arg", "-0.3"))
    lookahead = float(args.get("_1", "5"))
    if ceiling >= 0:
        raise DspParamError(f"limiter ceiling 须 < 0 dBFS: {ceiling}", code=16)
    if lookahead < 0:
        raise DspParamError(f"limiter lookahead 须 >= 0 ms: {lookahead}", code=16)
    return filters.limiter(audio, sr, ceiling_db=ceiling, lookahead_ms=lookahead), sr


def op_reverb(audio: np.ndarray, sr: int, args: Dict[str, str]) -> Tuple[np.ndarray, int]:
    """混响：``reverb 0.3 [1.2]``（wet ∈ [0,1] 默认 0.3；IR 时长秒 默认 1.2）。

    IR = **确定性合成**的指数衰减噪声（不入库 IR 资源，避免 LFS/版权），
    L1 归一化保证不爆音。

    **合规边界**：仅 standalone 后处理（``post dsp --ops``）可用；
    ``export suno`` 链与 ``pipeline``（走 ``DspProcessor``）恒不带混响。
    """
    from sunoauxtool.dsp.reverb import apply_reverb

    wet = float(args.get("arg", "0.3"))
    seconds = float(args.get("_1", "1.2"))
    if not 0.0 <= wet <= 1.0:
        raise DspParamError(f"reverb wet 须在 [0, 1]: {wet}", code=16)
    if seconds <= 0:
        raise DspParamError(f"reverb IR 时长须 > 0（秒）: {seconds}", code=16)
    return apply_reverb(audio, sr, wet=wet, seconds=seconds), sr


def op_concat(
    audio: np.ndarray, sr: int, args: Dict[str, str], _ctx: Optional[dict] = None
) -> Tuple[np.ndarray, int]:
    """拼接：``concat other.wav [xf 0.5]``（可选交叉淡化秒数）。

    交叉淡化：前段尾部与后段头部按余弦等功率重叠；采样率不一致时后段先重采样。
    相对路径按 ``_base_dir``（由 apply_ops 注入，通常为输入文件目录）解析。
    """
    path = args.get("arg", "")
    if not path:
        raise DspParamError("concat 缺少文件参数", code=16)
    xf = float(args.get("xf", "0"))
    if xf < 0:
        raise DspParamError(f"concat xf 须 >= 0: {xf}", code=16)

    p = Path(path)
    if not p.is_absolute() and args.get("_base_dir"):
        p = Path(args["_base_dir"]) / p
    # read_wav 缺文件抛 InputFileError(3)——concat 源缺失属于输入错误
    other, other_sr = audio_ops.read_wav(p)
    if other_sr != sr:
        other, _ = op_resample(other, other_sr, {"arg": str(sr)})

    # 声道数前置校验（1.4.6 B4）：采样率不一致有自动重采样，声道数不一致旧实现
    # 直接裸 np.concatenate ValueError——这里转成带诊断的 DspParamError(16)
    a_ch = 1 if audio.ndim == 1 else audio.shape[1]
    o_ch = 1 if other.ndim == 1 else other.shape[1]
    if a_ch != o_ch:
        raise DspParamError(
            f"concat 声道数不匹配: 当前音频 {a_ch}ch, 待拼接 {o_ch}ch "
            f"({p.name})——请先各自 upmix/downmix 到相同声道数",
            code=16,
        )

    xf_n = int(xf * sr)
    if xf_n > 0:
        if xf_n > audio.shape[0] or xf_n > other.shape[0]:
            raise DspParamError(
                f"concat 交叉淡化 {xf}s 超出任一侧音频长度", code=16
            )
        fade = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, xf_n, endpoint=False))
        fade = _fit_ramp(fade, audio)
        tail = audio[-xf_n:] * (1.0 - fade)
        head = other[:xf_n] * fade
        merged = tail + head
        return (
            np.concatenate([audio[:-xf_n], merged, other[xf_n:]]).astype(np.float32),
            sr,
        )
    return np.concatenate([audio, other]).astype(np.float32), sr


# ---------------------------------------------------------------------------
# 注册表 + 执行器
# ---------------------------------------------------------------------------

OPS: Dict[str, OpFunc] = {
    "norm": op_norm,
    "loudnorm": op_loudnorm,
    "fade-in": op_fade_in,
    "fade-out": op_fade_out,
    "trim": op_trim,
    "resample": op_resample,
    "lowcut": op_lowcut,
    "eq": op_eq,
    "gate": op_gate,
    "compress": op_compress,
    "expand": op_expand,
    "limiter": op_limiter,
    "reverb": op_reverb,
    "concat": op_concat,
}


def apply_ops(
    audio: np.ndarray,
    sr: int,
    ops: List[DspOp],
    base_dir: Optional[Path] = None,
) -> Tuple[np.ndarray, int]:
    """按序执行算子链；未知算子抛 DspParamError(16)。

    Args:
        base_dir: ``concat`` 相对路径的解析基准（CLI 下为输入文件所在目录）。
    """
    out_audio, out_sr = audio, sr
    for op in ops:
        fn = OPS.get(op.name)
        if fn is None:
            raise DspParamError(
                f"未知算子: {op.name}（可用: {', '.join(sorted(OPS))}）", code=16
            )
        op_args = dict(op.args)
        op_args.setdefault("_base_dir", str(base_dir) if base_dir else "")
        try:
            out_audio, out_sr = fn(out_audio, out_sr, op_args)
        except (DspParamError, DspError):
            raise
        except Exception as exc:
            raise DspError(f"算子 {op.name} 执行失败: {exc}", code=15) from exc
    return out_audio, out_sr
