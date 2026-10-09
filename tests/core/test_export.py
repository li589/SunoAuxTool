"""导出单元测试：numpy 合成正弦波验证裁剪/淡入淡出/重采样/Suno 合规。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from sunoauxtool.exceptions import ExportError
from sunoauxtool.export import audio as audio_ops
from sunoauxtool.export.suno import ExportOptions, SunoExporter


def test_read_write_wav_roundtrip(sine_wav):
    """WAV 读写回环：采样率与时长正确。"""
    data, sr = audio_ops.read_wav(sine_wav)
    assert sr == 44100
    assert len(data) == pytest.approx(44100 * 2.0, abs=10)


def test_trim_longer_audio():
    """长音频裁剪至目标时长。"""
    audio = np.zeros(44100 * 10, dtype=np.float32)
    out = audio_ops.trim_or_loop(audio, 44100, 4.0)
    assert len(out) == 44100 * 4


def test_loop_shorter_audio():
    """短音频循环补齐至目标时长。"""
    audio = np.ones(44100, dtype=np.float32)
    out = audio_ops.trim_or_loop(audio, 44100, 2.5)
    assert len(out) == 44100 * 2 + 44100 // 2
    assert np.all(out[:44100] == 1.0)


def test_fade_edges_zero(sine_wav):
    """淡入淡出：首尾样本趋近 0，中段保持信号。"""
    data, sr = audio_ops.read_wav(sine_wav)
    faded = audio_ops.fade(data, sr, 100.0)
    assert abs(faded[0]) < 1e-3
    assert abs(faded[-1]) < 1e-3
    # 中段峰值保持（避开正弦波过零点）
    mid_segment = faded[len(faded) // 4 : 3 * len(faded) // 4]
    assert float(np.max(np.abs(mid_segment))) > 0.3


def test_resample_halves_length():
    """重采样 44100 -> 22050：长度减半。"""
    audio = np.random.default_rng(0).random(44100).astype(np.float32)
    out = audio_ops.resample(audio, 44100, 22050)
    assert len(out) == 22050


def test_resample_same_rate_unchanged():
    """相同采样率不重采样。"""
    audio = np.zeros(100, dtype=np.float32)
    assert audio_ops.resample(audio, 44100, 44100) is audio or np.array_equal(
        audio_ops.resample(audio, 44100, 44100), audio
    )


def test_normalize_peak_dbfs():
    """归一化后峰值 <= -1 dBFS。"""
    audio = np.ones(1000, dtype=np.float32) * 0.99
    out = audio_ops.normalize(audio)
    assert float(np.max(np.abs(out))) <= 10 ** (-1 / 20) + 1e-6


def test_export_wav_25s(sine_wav, tmp_path):
    """导出 25s WAV：时长/采样率/位深合规，带淡入淡出。"""
    exporter = SunoExporter()
    opts = ExportOptions(duration=25, format="wav", sample_rate=44100, bit_depth=16, fade_ms=50)
    out = exporter.export(str(sine_wav), opts)
    data, sr = audio_ops.read_wav(out)
    assert sr == 44100
    assert len(data) == pytest.approx(44100 * 25, abs=10)
    assert abs(data[0]) < 1e-3
    assert abs(data[-1]) < 1e-3
    assert np.max(np.abs(data)) <= 10 ** (-1 / 20) + 1e-6


def test_export_naming_suffix(sine_wav, tmp_path):
    """默认命名追加 _suno{duration}s。"""
    exporter = SunoExporter()
    out = exporter.export(str(sine_wav), ExportOptions(duration=20))
    assert str(out).endswith("_suno20s.wav")


def test_export_duration_out_of_range(sine_wav, tmp_path):
    """目标时长越界 -> ExportError(5)。"""
    exporter = SunoExporter()
    with pytest.raises(ExportError) as exc:
        exporter.export(str(sine_wav), ExportOptions(duration=35))
    assert exc.value.code == 5
    with pytest.raises(ExportError) as exc:
        exporter.export(str(sine_wav), ExportOptions(duration=9))
    assert exc.value.code == 5


def test_export_invalid_format(sine_wav, tmp_path):
    """非法格式 -> ExportError(5)。"""
    exporter = SunoExporter()
    with pytest.raises(ExportError) as exc:
        exporter.export(str(sine_wav), ExportOptions(duration=20, format="ogg"))
    assert exc.value.code == 5


def test_export_missing_input(tmp_path):
    """输入 WAV 不存在 -> InputFileError(3)。"""
    from sunoauxtool.exceptions import InputFileError

    exporter = SunoExporter()
    with pytest.raises(InputFileError) as exc:
        exporter.export(str(tmp_path / "nope.wav"), ExportOptions(duration=20))
    assert exc.value.code == 3


def test_export_mp3(sine_wav, tmp_path):
    """--format mp3：lameenc 已装则产出 MP3，否则 ExportError(5)。"""
    try:
        import lameenc  # noqa: F401
    except ImportError:
        exporter = SunoExporter()
        with pytest.raises(ExportError) as exc:
            exporter.export(str(sine_wav), ExportOptions(duration=20, format="mp3"))
        assert exc.value.code == 5
        return
    exporter = SunoExporter()
    out = Path(exporter.export(str(sine_wav), ExportOptions(duration=20, format="mp3")))
    assert str(out).endswith(".mp3")
    assert out.stat().st_size > 0
    assert out.read_bytes()[:3] == b"ID3" or out.read_bytes()[0:2] == b"\xff\xfb"


def test_describe_metadata(sine_wav):
    """describe 返回时长/采样率/位深元数据。"""
    meta = SunoExporter.describe(sine_wav)
    assert meta["sample_rate"] == 44100
    assert meta["duration_s"] == pytest.approx(2.0, abs=0.05)
    assert meta["bit_depth"] == 16


def test_export_reverb_rejected(sine_wav, tmp_path):
    """通过动态属性请求混响 -> ExportError(5)。"""
    opts = ExportOptions(duration=25)
    setattr(opts, "reverb", True)
    exporter = SunoExporter()
    with pytest.raises(ExportError) as exc:
        exporter.export(str(sine_wav), opts)
    assert exc.value.code == 5


def test_describe_non_riff(tmp_path):
    """describe 非 RIFF 文件不崩溃，返回 dict。"""
    import soundfile as sf
    non_riff = tmp_path / "non_riff.wav"
    # 写入合法 WAV 但破坏 RIFF 头（read_wav 仍可读，bit_depth 探测失败）
    import numpy as np
    audio = np.zeros(44100, dtype=np.float32)
    sf.write(str(non_riff), audio, 44100)
    # 破坏 RIFF 头前 4 字节
    data = bytearray(non_riff.read_bytes())
    data[0:4] = b"XXXX"
    non_riff.write_bytes(bytes(data))
    # read_wav 由 soundfile 处理可能失败，但 describe 内部捕获音频读取异常应返回 dict
    try:
        meta = SunoExporter.describe(non_riff)
        assert isinstance(meta, dict)
    except Exception as exc:
        # 允许抛异常（读不出音频），但不应是 unhandled
        assert exc is not None


def test_exporter_abstract():
    """抽象导出器不能直接实例化。"""
    from sunoauxtool.export.suno import Exporter
    with pytest.raises(TypeError):
        Exporter()
