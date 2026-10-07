"""R6 DSP 算子框架测试：ops 串解析 + 每算子数值断言 + CLI 端到端。

数值断言口径：
- norm/fade/trim/compress/concat：直接对波形做代数验证；
- loudnorm：997Hz 正弦 @ 已知幅值 -> 理论 LUFS（±0.5 LU 容差）；
- resample：时长比例 + 频谱主频保持。
"""

from __future__ import annotations


import numpy as np
import pytest
import soundfile as sf
from typer.testing import CliRunner

from sunoauxtool.aggregate import app
from sunoauxtool.dsp.loudness import integrated_lufs
from sunoauxtool.dsp.ops import apply_ops, parse_ops
from sunoauxtool.exceptions import DspError, DspParamError

runner = CliRunner()

SR = 48000


def _sine(freq: float = 997.0, sec: float = 2.0, amp: float = 0.5, sr: int = SR) -> np.ndarray:
    t = np.arange(int(sec * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# ---------------------------------------------------------------------------
# ops 串解析
# ---------------------------------------------------------------------------


def test_parse_basic_and_kv():
    ops = parse_ops("norm -1, fade-in 0.5, concat b.wav xf=0.3")
    assert [o.name for o in ops] == ["norm", "fade-in", "concat"]
    assert ops[0].args["arg"] == "-1"
    assert ops[1].args["arg"] == "0.5"
    assert ops[2].args["arg"] == "b.wav"
    assert ops[2].args["xf"] == "0.3"  # key=value 语法


def test_parse_errors_exit_16():
    with pytest.raises(DspParamError):
        parse_ops(",,")  # 空
    with pytest.raises(DspParamError):
        parse_ops("Norm -1")  # 大写非法
    with pytest.raises(DspParamError):
        parse_ops("trim =-5")  # 空 key
    with pytest.raises(DspParamError):
        parse_ops("")  # 全空


def test_unknown_operator_exit_16():
    # 注意别用 reverb——R14 起它是**已实现**算子，不再是「未知算子」样例
    with pytest.raises(DspParamError, match="未知算子"):
        apply_ops(_sine(), SR, parse_ops("definitely-not-an-op 0.5"))


# ---------------------------------------------------------------------------
# 算子数值断言
# ---------------------------------------------------------------------------


def test_norm_peak_target():
    x = _sine(amp=0.25)
    y, _ = apply_ops(x, SR, parse_ops("norm -3"))
    assert np.max(np.abs(y)) == pytest.approx(10 ** (-3 / 20), rel=1e-4)


def test_norm_positive_target_rejected():
    with pytest.raises(DspParamError):
        apply_ops(_sine(), SR, parse_ops("norm 3"))


def test_fade_in_cosine_shape():
    x = np.ones(SR, dtype=np.float32)  # 1s 全 1
    fade_s = 0.25
    y, _ = apply_ops(x, SR, parse_ops(f"fade-in {fade_s}"))
    n = int(fade_s * SR)
    # 起点为 0、终点达到 1（余弦 0->pi 前半），淡入后保持 1
    assert y[0] == pytest.approx(0.0, abs=1e-6)
    assert y[n - 1] == pytest.approx(1.0, abs=1e-5)
    assert np.all(y[n:] == 1.0)
    # 中点值 ≈ 0.5（余弦对称）
    assert y[n // 2] == pytest.approx(0.5, abs=1e-4)


def test_fade_out_and_longer_than_audio_rejected():
    x = np.ones(SR, dtype=np.float32)
    y, _ = apply_ops(x, SR, parse_ops("fade-out 0.5"))
    assert y[-1] == pytest.approx(0.0, abs=1e-6)
    with pytest.raises(DspParamError):
        apply_ops(x, SR, parse_ops("fade-in 2.0"))  # 2s > 1s 音频


def test_trim_seconds():
    x = _sine(sec=4.0)
    y, _ = apply_ops(x, SR, parse_ops("trim 1-2.5"))
    assert y.shape[0] == int(1.5 * SR)


def test_trim_invalid_range_exit_16():
    x = _sine(sec=4.0)
    for spec in ("trim 3-2", "trim 5-6", "trim abc", "trim 1"):
        with pytest.raises(DspParamError):
            apply_ops(x, SR, parse_ops(spec))


def test_resample_length_and_content():
    x = _sine(freq=440.0, sec=1.0)
    y, sr2 = apply_ops(x, SR, parse_ops("resample 24000"))
    assert sr2 == 24000
    assert y.shape[0] == 24000  # 48000*0.5 整除，无取整余数
    # 主频保持：440Hz 峰仍在
    spec = np.abs(np.fft.rfft(y * np.hanning(y.shape[0])))
    freqs = np.fft.rfftfreq(y.shape[0], 1 / sr2)
    assert freqs[np.argmax(spec)] == pytest.approx(440, abs=2)


def test_resample_same_rate_is_noop():
    x = _sine(sec=0.5)
    y, sr2 = apply_ops(x, SR, parse_ops("resample 48000"))
    assert sr2 == SR and y.shape == x.shape


def test_lowcut_attenuates_dc_and_lowfreq():
    # 直流 + 低频 30Hz + 中频 1kHz
    t = np.arange(SR) / SR
    x = (0.3 + 0.3 * np.sin(2 * np.pi * 30 * t) + 0.3 * np.sin(2 * np.pi * 1000 * t)).astype(
        np.float32
    )
    y, _ = apply_ops(x, SR, parse_ops("lowcut 200"))
    # 输出均值（直流残余）应显著小于输入
    assert abs(float(np.mean(y))) < 0.05 * 0.3


def test_compress_reduces_peaks():
    x = _sine(amp=0.9)
    y, _ = apply_ops(x, SR, parse_ops("compress 4 -12"))
    assert np.max(np.abs(y)) < np.max(np.abs(x))
    # 小信号部分不受影响（低于阈值 -12dBFS=0.251）
    assert y[0] == pytest.approx(x[0], rel=1e-3)  # x[0]=0 < 阈值


def test_concat_and_crossfade(tmp_path):
    a = np.ones(SR, dtype=np.float32)  # 1s
    b = 0.5 * np.ones(SR, dtype=np.float32)  # 1s 全 0.5（PCM_16 上限 1.0，不可超）
    pa, pb = tmp_path / "a.wav", tmp_path / "b.wav"
    sf.write(pa, a, SR, subtype="PCM_16")
    sf.write(pb, b, SR, subtype="PCM_16")

    # 无交叉淡化：长度相加，接缝跳变
    y, _ = apply_ops(a, SR, parse_ops(f"concat {pb}"))
    assert y.shape[0] == 2 * SR
    assert y[SR] == pytest.approx(0.5, abs=1e-6)

    # 有交叉淡化 0.5s：总长 1.5s，中点 ≈ (1+2)/2
    y2, _ = apply_ops(a, SR, parse_ops(f"concat {pb} xf=0.5"))
    assert y2.shape[0] == int(1.5 * SR)
    mid = int(0.75 * SR)  # 交叉淡化区 [0.5s,1.0s) 的中点
    assert y2[mid] == pytest.approx(0.75, abs=0.1)  # 等功率混合 (1+0.5)/2

    # 相对路径（base_dir）
    import os

    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        y3, _ = apply_ops(a, SR, parse_ops("concat b.wav xf=0.5"), base_dir=tmp_path)
        assert y3.shape[0] == int(1.5 * SR)
    finally:
        os.chdir(cwd)

    # xf 超长 -> 16
    with pytest.raises(DspParamError):
        apply_ops(a, SR, parse_ops(f"concat {pb} xf=5"))


# ---------------------------------------------------------------------------
# loudnorm（EBU R128 简化版）
# ---------------------------------------------------------------------------


def test_loudnorm_sine_known_lufs():
    # 997Hz 正弦满幅 1.0：raw LUFS = -0.691+10log10(0.5) ≈ -3.70，K 加权 @997Hz ≈ +0.7dB
    x = _sine(amp=1.0, sec=3.0)
    lufs = integrated_lufs(x, SR)
    assert lufs == pytest.approx(-3.0, abs=0.8)


def test_loudnorm_targets_and_uniform():
    x = _sine(amp=0.2, sec=3.0)
    y, _ = apply_ops(x, SR, parse_ops("loudnorm -16"))
    assert integrated_lufs(y, SR) == pytest.approx(-16.0, abs=0.3)
    # 幅值放大但波形形状不变
    scale = np.max(np.abs(y)) / np.max(np.abs(x))
    assert scale == pytest.approx(10 ** ((-16 - integrated_lufs(x, SR)) / 20), rel=1e-3)


def test_loudnorm_silence_raises_15():
    with pytest.raises(DspError):
        apply_ops(np.zeros(SR * 2, dtype=np.float32), SR, parse_ops("loudnorm -16"))


def test_loudnorm_positive_target_rejected():
    with pytest.raises(DspParamError):
        apply_ops(_sine(), SR, parse_ops("loudnorm 5"))


# ---------------------------------------------------------------------------
# CLI 端到端（post dsp）
# ---------------------------------------------------------------------------


def test_cli_dsp_chain(tmp_path):
    src = tmp_path / "in.wav"
    sf.write(src, _sine(sec=4.0), SR, subtype="PCM_16")
    result = runner.invoke(
        app,
        ["post", "dsp", str(src), "--ops", "norm -1, trim 0.5-2, fade-out 0.2, resample 24000"],
    )
    assert result.exit_code == 0, result.output
    out = tmp_path / "in_dsp.wav"
    assert out.is_file()
    info = sf.info(out)
    assert info.samplerate == 24000
    assert abs(info.frames / 24000 - 1.5) < 0.01
    assert "DSP 完成" in result.output


def test_cli_dsp_custom_output_and_bit_depth(tmp_path):
    src = tmp_path / "in.wav"
    sf.write(src, _sine(sec=1.0), SR, subtype="PCM_16")
    out = tmp_path / "deep" / "out.wav"
    result = runner.invoke(
        app, ["post", "dsp", str(src), "--ops", "norm -6", "-o", str(out), "--bit-depth", "24"]
    )
    assert result.exit_code == 0, result.output
    assert sf.info(out).subtype == "PCM_24"


def test_cli_dsp_missing_input_exit_3(tmp_path):
    result = runner.invoke(app, ["post", "dsp", str(tmp_path / "nope.wav"), "--ops", "norm -1"])
    assert result.exit_code == 3


def test_cli_dsp_bad_ops_exit_16(tmp_path):
    src = tmp_path / "in.wav"
    sf.write(src, _sine(sec=1.0), SR, subtype="PCM_16")
    # reverb 1 曾是「未实现算子」样例；R14 起它合法（wet=1 纯湿声），
    # 改用真正不存在的算子名覆盖「未知算子 -> 16」分支
    for spec in ("definitely-not-an-op 1", "trim abc", "norm 3"):
        result = runner.invoke(app, ["post", "dsp", str(src), "--ops", spec])
        assert result.exit_code == 16, spec
    result = runner.invoke(app, ["post", "dsp", str(src), "--ops", "norm -1", "--bit-depth", "8"])
    assert result.exit_code == 16


def test_cli_dsp_runtime_failure_exit_15(tmp_path):
    src = tmp_path / "in.wav"
    sf.write(src, _sine(sec=1.0), SR, subtype="PCM_16")
    # loudnorm 遇静音 -> 运行期 15（解析合法、执行失败）
    silent = tmp_path / "sil.wav"
    sf.write(silent, np.zeros(SR * 2, dtype=np.float32), SR, subtype="PCM_16")
    result = runner.invoke(app, ["post", "dsp", str(silent), "--ops", "loudnorm -16"])
    assert result.exit_code == 15


# ---------------------------------------------------------------------------
# R14：混响 reverb（仅 standalone 算子链；DspProcessor 内部链恒禁）
# ---------------------------------------------------------------------------


def test_synthesize_ir_l1_normalized_and_deterministic():
    from sunoauxtool.dsp.reverb import synthesize_ir

    a = synthesize_ir(SR, seconds=0.5)
    b = synthesize_ir(SR, seconds=0.5)
    assert a.shape[0] == int(round(SR * 0.5))
    assert abs(np.abs(a).sum() - 1.0) < 1e-9  # L1=1 -> 收缩映射，数学上不爆音
    assert np.array_equal(a, b)  # 同 seed 确定性


def test_apply_reverb_wet_zero_is_identity():
    from sunoauxtool.dsp.reverb import apply_reverb

    x = _sine(sec=0.5)
    out = apply_reverb(x, SR, wet=0.0)
    assert out.shape == x.shape
    assert np.allclose(out, x, atol=1e-6)


def test_apply_reverb_tail_in_silence_and_no_clipping():
    """后段静音的输入应长出混响尾巴，且峰值不超过输入峰值。"""
    from sunoauxtool.dsp.reverb import apply_reverb

    half = int(0.5 * SR)
    x = np.concatenate([_sine(sec=0.5, amp=0.8), np.zeros(half, dtype=np.float32)])
    out = apply_reverb(x, SR, wet=1.0, seconds=0.6)

    assert out.shape == x.shape  # insert 效果：时长不变
    assert float(np.abs(out[half + 2000 :]).max()) > 1e-4  # 静音区出现尾巴
    assert float(np.abs(out).max()) <= 0.8 + 1e-6  # 不爆音


def test_apply_reverb_stereo_channels_independent():
    from sunoauxtool.dsp.reverb import apply_reverb

    mono = _sine(sec=0.5)
    stereo = np.stack([mono, np.zeros_like(mono)], axis=1)
    out = apply_reverb(stereo, SR, wet=1.0, seconds=0.3)

    assert out.shape == stereo.shape
    assert float(np.abs(out[:, 1]).max()) < 1e-6  # 全零声道仍全零
    assert float(np.abs(out[:, 0]).max()) > 0.0


def test_apply_reverb_empty_passthrough():
    from sunoauxtool.dsp.reverb import apply_reverb

    assert apply_reverb(np.zeros(0, dtype=np.float32), SR).shape == (0,)


def test_op_reverb_registered_and_runs():
    from sunoauxtool.dsp.ops import OPS

    assert "reverb" in OPS
    x = _sine(sec=0.5)
    out, sr = apply_ops(x, SR, parse_ops("reverb 0.4"))
    assert sr == SR
    assert out.shape == x.shape
    assert not np.allclose(out, x)  # 确实加了混响


def test_op_reverb_rejects_bad_params():
    with pytest.raises(DspParamError):
        apply_ops(_sine(sec=0.2), SR, parse_ops("reverb 1.5"))  # wet > 1
    with pytest.raises(DspParamError):
        apply_ops(_sine(sec=0.2), SR, parse_ops("reverb 0.3 0"))  # 时长 <= 0


def test_cli_dsp_reverb_end_to_end(tmp_path):
    src = tmp_path / "in.wav"
    sf.write(src, _sine(sec=1.0), SR, subtype="PCM_16")
    out = tmp_path / "rev.wav"
    result = runner.invoke(
        app, ["post", "dsp", str(src), "--ops", "reverb 0.3", "-o", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert out.is_file()


def test_dsp_processor_still_rejects_reverb_compliance_boundary(tmp_path):
    """合规边界：pipeline/batch 内部链（DspProcessor）恒不带混响。"""
    from sunoauxtool.dsp import DspOptions, DspProcessor
    from sunoauxtool.exceptions import ParameterError

    src = tmp_path / "in.wav"
    sf.write(src, _sine(sec=0.5), SR, subtype="PCM_16")
    with pytest.raises(ParameterError) as ei:
        DspProcessor().process(str(src), DspOptions(reverb=True), str(tmp_path / "o.wav"))
    assert ei.value.code == 1
    assert "standalone" in str(ei.value)


# -- expand（1.4.0 向下扩展） ---------------------------------------------


def test_expand_attenuates_below_threshold():
    """低于阈值的部分按 ratio 衰减；高于阈值不动。"""
    x = _sine(sec=1.0, amp=0.5)  # 峰值 0.5 = -6dBFS
    y, _ = apply_ops(x, SR, parse_ops("expand 2 -12"))
    # 0.5 > 阈值(0.251) 的样本不动：整体峰值不变
    assert np.isclose(np.max(np.abs(y)), np.max(np.abs(x)), atol=1e-4)
    # 弱段（正弦过零附近 level < 阈值）被压低：RMS 下降
    assert float(np.sqrt(np.mean(y**2))) < float(np.sqrt(np.mean(x**2)))


def test_expand_continuous_at_threshold_and_ratio1_noop():
    """阈值处增益恰为 1（连续）；ratio=1 恒等。"""
    thr_db = -12.0
    x = _sine(sec=0.5, amp=float(10 ** (thr_db / 20)))  # 峰值恰在阈值
    y, _ = apply_ops(x, SR, parse_ops(f"expand 2 {thr_db}"))
    assert np.isclose(np.max(np.abs(y)), np.max(np.abs(x)), atol=1e-4)

    y1, _ = apply_ops(_sine(sec=0.5), SR, parse_ops("expand 1 -30"))
    assert np.allclose(y1, _sine(sec=0.5), atol=1e-6)


def test_expand_invalid_ratio_exit_16():
    with pytest.raises(DspParamError):
        apply_ops(_sine(sec=0.2), SR, parse_ops("expand 0.5"))


# -- limiter（1.4.0 前瞻限幅） ---------------------------------------------


def test_limiter_brickwall():
    """峰值被硬顶在 ceiling 之下（含瞬态过冲样本），brickwall 性质成立。"""
    x = _sine(sec=0.5, amp=1.0)  # 峰值 1.0 = 0dBFS，必被压
    y, _ = apply_ops(x, SR, parse_ops("limiter -0.3"))
    ceiling = float(10 ** (-0.3 / 20))
    assert float(np.max(np.abs(y))) <= ceiling + 1e-4
    # 波形没被摧毁：与限幅后的理论包络强相关（粗查：能量仍在）
    assert float(np.max(np.abs(y))) > ceiling * 0.9  # 峰值贴着天花板


def test_limiter_quiet_signal_untouched():
    """低于 ceiling 的信号增益恒 1（不压不抬）。"""
    x = _sine(sec=0.5, amp=0.1)  # -20dBFS，远低于 -0.3dBFS ceiling
    y, _ = apply_ops(x, SR, parse_ops("limiter"))
    assert np.allclose(y, x, atol=1e-6)


def test_limiter_invalid_params_exit_16():
    with pytest.raises(DspParamError):
        apply_ops(_sine(sec=0.2), SR, parse_ops("limiter 0"))  # ceiling >= 0
    with pytest.raises(DspParamError):
        apply_ops(_sine(sec=0.2), SR, parse_ops("limiter -0.3 -1"))  # lookahead < 0


def test_limiter_cli_end_to_end(tmp_path):
    src = tmp_path / "in.wav"
    sf.write(src, _sine(sec=1.0, amp=1.0), SR, subtype="PCM_16")
    out = tmp_path / "lim.wav"
    result = runner.invoke(
        app, ["post", "dsp", str(src), "--ops", "limiter -1", "-o", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert out.is_file()
    data, _ = sf.read(out)
    assert float(np.max(np.abs(data))) <= float(10 ** (-1 / 20)) + 1e-4
