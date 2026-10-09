"""``ai.basicpitch`` 可选转谱后端的门控与接口测试（#13）。

basic-pitch 在本机与 CI 都未安装：真实推理路径无法在此验证（模块文档已声明）。
本文件测的是**不依赖权重**就能钉住的部分：
- 模块顶部零 basic_pitch import（P0 隔离约束）
- 未安装时 ``is_available()`` 为 False、``transcribe()`` 抛退出码 6
- 已安装路径用 fake 模块全流程覆盖（含参数顺序双保险与落盘）
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

from sunoauxtool.ai.basicpitch import BasicPitchAdapter
from sunoauxtool.exceptions import AiDependencyError, InputFileError

HAS_BASICPITCH = importlib.util.find_spec("basic_pitch") is not None


def test_module_import_does_not_pull_basic_pitch():
    """P0 隔离约束：import 适配器模块本身不得触发 basic_pitch 导入。"""
    import sunoauxtool.ai.basicpitch as mod

    assert "basic_pitch" not in sys.modules
    assert mod.BasicPitchAdapter is not None


def test_is_available_false_when_uninstalled():
    if HAS_BASICPITCH:  # pragma: no cover - 本机/CI 均未装
        pytest.skip("本机已安装 basic-pitch")
    assert BasicPitchAdapter().is_available() is False


def test_transcribe_without_dependency_exits_6(tmp_path: Path):
    if HAS_BASICPITCH:  # pragma: no cover
        pytest.skip("本机已安装 basic-pitch")
    adapter = BasicPitchAdapter()
    with pytest.raises(AiDependencyError) as exc:
        adapter.transcribe(str(tmp_path / "any.wav"))
    assert exc.value.code == 6
    assert "pip install basic-pitch" in str(exc.value)


def test_transcribe_missing_input_exits_3(tmp_path: Path, monkeypatch):
    """依赖「已装」（monkeypatch 放行）但输入文件不存在 → 退出码 3。"""
    adapter = BasicPitchAdapter()
    monkeypatch.setattr(BasicPitchAdapter, "is_available", lambda self: True)
    with pytest.raises(InputFileError) as exc:
        adapter.transcribe(str(tmp_path / "nope.wav"))
    assert exc.value.code == 3


# ---------------------------------------------------------------------------
# 已安装路径（fake 模块全流程）
# ---------------------------------------------------------------------------


class FakeMidiData:
    """记录 write 调用的假 midi 对象。"""

    def __init__(self) -> None:
        self.written: list[Path] = []

    def write(self, path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"MThd\x00\x00\x00\x06\x00\x01\x00\x01\x00\x60")
        self.written.append(p)


def _install_fake_basic_pitch(monkeypatch, predict) -> FakeMidiData:
    """往 sys.modules 塞假的 basic_pitch / basic_pitch.inference。"""
    fake_top = types.ModuleType("basic_pitch")
    fake_inf = types.ModuleType("basic_pitch.inference")
    fake_inf.predict = predict
    fake_top.inference = fake_inf
    monkeypatch.setitem(sys.modules, "basic_pitch", fake_top)
    monkeypatch.setitem(sys.modules, "basic_pitch.inference", fake_inf)
    return FakeMidiData()


def test_transcribe_full_flow_with_fake(monkeypatch, tmp_path: Path):
    """真实 0.4.x 签名：predict(audio, model) 返回三元组 (dict, PrettyMIDI, list)。"""
    wav = tmp_path / "in.wav"
    wav.write_bytes(b"RIFF")
    midi = FakeMidiData()
    calls: list[tuple] = []

    def predict(audio, model=None):
        calls.append((audio, model))
        return ({}, midi, [])  # (model_output, midi_data, note_events)

    _install_fake_basic_pitch(monkeypatch, predict)
    adapter = BasicPitchAdapter()
    monkeypatch.setattr(BasicPitchAdapter, "is_available", lambda self: True)

    out = tmp_path / "out.mid"
    written = adapter.transcribe(str(wav), str(out))
    assert Path(written) == out.resolve()
    assert out.is_file()
    assert midi.written == [out.resolve()]
    # 0.4.x 签名：音频在前
    assert calls[0][0] == str(wav.resolve())
    assert calls[0][1] is None  # model_path=None 时不传第二个位置参数


def test_transcribe_keyword_fallback(monkeypatch, tmp_path: Path):
    """兼容旧 keyword-only 签名：位置序抛 TypeError 时自动退到关键字序。"""
    wav = tmp_path / "in.wav"
    wav.write_bytes(b"RIFF")
    midi = FakeMidiData()
    calls: list[tuple] = []

    def predict(*, audio, model=None):  # 只接受关键字的历史签名
        calls.append((audio, model))
        return ({}, midi, [])

    _install_fake_basic_pitch(monkeypatch, predict)
    adapter = BasicPitchAdapter()
    monkeypatch.setattr(BasicPitchAdapter, "is_available", lambda self: True)

    written = adapter.transcribe(str(wav))
    assert Path(written) == (tmp_path / "in_basicpitch.mid").resolve()
    assert calls[0][0] == str(wav.resolve())


def test_model_path_must_exist(monkeypatch, tmp_path: Path):
    """显式模型路径不存在 → AiDependencyError(6)，不裸抛。"""
    missing = tmp_path / "missing.onnx"
    adapter = BasicPitchAdapter(model_path=str(missing))
    monkeypatch.setattr(BasicPitchAdapter, "is_available", lambda self: True)
    with pytest.raises(AiDependencyError) as exc:
        adapter._resolve_model_path()
    assert exc.value.code == 6


def test_import_failure_without_backend_is_code_6(monkeypatch, tmp_path: Path):
    """装了包但没装推理后端 → 退出码 6，不能漏成退出码 1 的 NameError。

    真实场景：basic_pitch/__init__.py 的后端选择链 if/elif 无 else 分支，
    四个后端全缺时模块级抛 `NameError: _default_model_type is not defined`。
    这里用 sys.modules 置 None 触发 ImportError 来模拟「导入即炸」这一类失败。
    """
    wav = tmp_path / "in.wav"
    wav.write_bytes(b"RIFF")

    fake_top = types.ModuleType("basic_pitch")
    monkeypatch.setitem(sys.modules, "basic_pitch", fake_top)
    monkeypatch.setitem(sys.modules, "basic_pitch.inference", None)

    adapter = BasicPitchAdapter()
    monkeypatch.setattr(BasicPitchAdapter, "is_available", lambda self: True)
    with pytest.raises(AiDependencyError) as exc:
        adapter.transcribe(str(wav))
    assert exc.value.code == 6
    assert "onnxruntime" in str(exc.value)  # 指引里必须给可操作的安装命令


def test_generate_is_an_alias_of_transcribe(monkeypatch, tmp_path: Path):
    """AIGenerator 接口兼容：generate() 忽略 prompt、行为同 transcribe()。"""
    wav = tmp_path / "in.wav"
    wav.write_bytes(b"RIFF")
    midi = FakeMidiData()
    _install_fake_basic_pitch(
        monkeypatch, lambda audio, model=None: ({}, midi, [])
    )
    adapter = BasicPitchAdapter()
    monkeypatch.setattr(BasicPitchAdapter, "is_available", lambda self: True)
    written = adapter.generate(str(wav), prompt="ignored")
    assert Path(written).is_file()
