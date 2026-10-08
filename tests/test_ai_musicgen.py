"""MusicGen 适配器单测（T-P1-1）。

全部 mock audiocraft/torch，不真跑模型、不依赖 GPU/权重（无 GPU 环境可跑）。
"""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from sunoauxtool.ai.musicgen import MusicGenAdapter
from sunoauxtool.exceptions import AiDependencyError, InputFileError, ParameterError


# ---------------------------------------------------------------------------
# fake audiocraft / torch
# ---------------------------------------------------------------------------


class _FakeFromNumpy:
    """torch.from_numpy 返回值（仅需 unsqueeze）。"""

    def unsqueeze(self, dim: int) -> "_FakeFromNumpy":
        return self


class _FakeCuda:
    def __init__(self) -> None:
        self.available = False
        self.free_bytes = 0
        self.total_bytes = 0

    def is_available(self) -> bool:
        return self.available

    def manual_seed_all(self, seed: int) -> None:  # pragma: no cover - 简单记录
        return None

    def mem_get_info(self, _i: int):
        return (self.free_bytes, self.total_bytes)


class _FakeTorch:
    float16 = "float16"

    def __init__(self) -> None:
        self.cuda = _FakeCuda()
        self.manual_seed_calls: list[int] = []

    def manual_seed(self, seed: int) -> None:
        self.manual_seed_calls.append(seed)

    def from_numpy(self, _arr: np.ndarray) -> _FakeFromNumpy:
        return _FakeFromNumpy()


class _FakeTensor:
    """模拟模型输出张量（numpy 后端）。"""

    def __init__(self, arr: np.ndarray) -> None:
        self.arr = arr

    def __getitem__(self, idx):
        return _FakeTensor(self.arr[idx])

    def detach(self) -> "_FakeTensor":
        return self

    def cpu(self) -> "_FakeTensor":
        return self

    def float(self) -> "_FakeTensor":
        return self

    def dim(self) -> int:
        return self.arr.ndim

    def transpose(self, a: int, b: int) -> "_FakeTensor":
        # torch 的 transpose(dim0, dim1) 语义是交换两维；numpy 需用 swapaxes 模拟
        return _FakeTensor(self.arr.swapaxes(a, b))

    def numpy(self) -> np.ndarray:
        return self.arr


class _FakeMusicGen:
    sample_rate = 32000

    def __init__(self) -> None:
        class _Sub:
            def to(self, dtype) -> "_Sub":
                return self

        self.lm = _Sub()
        self.compression_model = _Sub()

    @classmethod
    def get_pretrained(cls, name: str, device=None) -> "_FakeMusicGen":
        return cls()

    def to(self, dtype) -> "_FakeMusicGen":
        return self

    def set_generation_params(self, **_kw) -> None:
        return None

    def generate_with_chroma(
        self, descriptions: list[str], melody_wavs, melody_sample_rate: int, progress: bool = False
    ) -> _FakeTensor:
        return _FakeTensor(np.zeros((1, 1, 16000), dtype=np.float32))


@pytest.fixture
def fake_torch():
    return _FakeTorch()


@pytest.fixture
def mock_musicgen_env(monkeypatch, fake_torch, tmp_path):
    """注入 fake torch/audiocraft 到 sys.modules，并 mock find_spec 为已安装。"""
    import sunoauxtool.ai.musicgen as mg_mod

    models_mod = types.ModuleType("audiocraft.models")
    models_mod.MusicGen = _FakeMusicGen
    audiocraft_mod = types.ModuleType("audiocraft")
    audiocraft_mod.models = models_mod

    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "audiocraft", audiocraft_mod)
    monkeypatch.setitem(sys.modules, "audiocraft.models", models_mod)
    monkeypatch.setattr(mg_mod.importlib.util, "find_spec", lambda _name: object())

    # 默认 CUDA 可用且显存充足（8GB 卡，free 7.5GB）
    fake_torch.cuda.available = True
    fake_torch.cuda.total_bytes = int(8 * 1024**3)
    fake_torch.cuda.free_bytes = int(7.5 * 1024**3)

    wav_path = tmp_path / "melody.wav"
    import soundfile as sf

    sr = 44100
    t = np.linspace(0, 2.0, sr * 2, endpoint=False)
    sf.write(str(wav_path), (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), sr)
    return {"wav": str(wav_path), "torch": fake_torch}


def test_is_available_false_when_no_deps(monkeypatch):
    import sunoauxtool.ai.musicgen as mg_mod

    monkeypatch.setattr(mg_mod.importlib.util, "find_spec", lambda _name: None)
    assert MusicGenAdapter().is_available() is False


def test_is_available_true_when_deps_present(mock_musicgen_env):
    assert MusicGenAdapter().is_available() is True


def test_generate_returns_wav(mock_musicgen_env, tmp_path):
    out = tmp_path / "accompaniment.wav"
    path = MusicGenAdapter().generate(
        mock_musicgen_env["wav"], "upbeat pop", output_path=str(out), seed=42
    )
    assert path == str(out.resolve())
    assert out.is_file()
    assert out.stat().st_size > 0
    # seed 已传给 torch.manual_seed
    assert 42 in mock_musicgen_env["torch"].manual_seed_calls


def test_generate_default_output_path(mock_musicgen_env):
    path = MusicGenAdapter().generate(mock_musicgen_env["wav"], "upbeat pop")
    from pathlib import Path

    assert Path(path).is_file()
    assert path.endswith("_musicgen.wav")


def test_generate_medium_uses_chroma(mock_musicgen_env, tmp_path, monkeypatch):
    captured = {}

    def spy_generate_with_chroma(self, descriptions, melody_wavs, melody_sample_rate, progress=False):
        captured["descriptions"] = descriptions
        captured["melody_sample_rate"] = melody_sample_rate
        return _FakeTensor(np.zeros((1, 1, 16000), dtype=np.float32))

    monkeypatch.setattr(_FakeMusicGen, "generate_with_chroma", spy_generate_with_chroma)
    MusicGenAdapter().generate(
        mock_musicgen_env["wav"], "upbeat pop", output_path=str(tmp_path / "o.wav")
    )
    assert captured["descriptions"] == ["upbeat pop"]
    assert captured["melody_sample_rate"] == 44100


def test_generate_no_deps_exit_6(monkeypatch, tmp_path):
    import sunoauxtool.ai.musicgen as mg_mod

    monkeypatch.setattr(mg_mod.importlib.util, "find_spec", lambda _name: None)
    with pytest.raises(AiDependencyError) as ei:
        MusicGenAdapter().generate("x.wav", "upbeat pop")
    assert ei.value.code == 6
    assert "requirements/ai.txt" in str(ei.value)


def test_generate_vram_insufficient_suggests_small(mock_musicgen_env, tmp_path):
    mock_musicgen_env["torch"].cuda.available = True
    mock_musicgen_env["torch"].cuda.free_bytes = int(4.5 * 1024**3)
    mock_musicgen_env["torch"].cuda.total_bytes = int(8 * 1024**3)
    with pytest.raises(AiDependencyError) as ei:
        MusicGenAdapter(model_size="medium", device="cuda").generate(
            mock_musicgen_env["wav"], "upbeat pop"
        )
    assert ei.value.code == 6
    assert "--model-size small" in str(ei.value)


def test_generate_no_cuda_raises(mock_musicgen_env, tmp_path):
    # cuda 设备但未检测到 CUDA -> 明确提示（而非 OOM 崩溃）
    mock_musicgen_env["torch"].cuda.available = False
    with pytest.raises(AiDependencyError) as ei:
        MusicGenAdapter(device="cuda").generate(mock_musicgen_env["wav"], "upbeat pop")
    assert ei.value.code == 6
    assert "--device cpu" in str(ei.value)


def test_generate_cpu_skips_vram_check(mock_musicgen_env, tmp_path):
    # cpu 设备不触发显存检查
    out = tmp_path / "cpu.wav"
    path = MusicGenAdapter(device="cpu").generate(
        mock_musicgen_env["wav"], "upbeat pop", output_path=str(out)
    )
    assert path == str(out.resolve())


def test_generate_duration_out_of_range(mock_musicgen_env):
    with pytest.raises(ParameterError) as ei:
        MusicGenAdapter().generate(mock_musicgen_env["wav"], "upbeat pop", duration=45)
    assert ei.value.code == 1


def test_generate_default_duration_alignment(mock_musicgen_env, tmp_path, monkeypatch):
    captured = {}

    def spy_set_generation_params(self, **_kw):
        captured.update(_kw)
        return None

    monkeypatch.setattr(_FakeMusicGen, "set_generation_params", spy_set_generation_params)
    # 输入 2s 旋律 -> 默认时长下限 10s
    MusicGenAdapter().generate(mock_musicgen_env["wav"], "upbeat pop", output_path=str(tmp_path / "o.wav"))
    assert captured["duration"] == 10.0


def test_generate_missing_input(mock_musicgen_env):
    with pytest.raises(InputFileError) as ei:
        MusicGenAdapter().generate("nope.wav", "upbeat pop")
    assert ei.value.code == 3


def test_check_vram_no_cuda(mock_musicgen_env):
    mock_musicgen_env["torch"].cuda.available = False
    assert MusicGenAdapter().check_vram() is None


def test_check_vram_with_cuda(mock_musicgen_env):
    mock_musicgen_env["torch"].cuda.available = True
    mock_musicgen_env["torch"].cuda.free_bytes = int(6.5 * 1024**3)
    mock_musicgen_env["torch"].cuda.total_bytes = int(8 * 1024**3)
    vram = MusicGenAdapter().check_vram()
    assert vram is not None and abs(vram - 6.5) < 0.1


def test_invalid_model_size():
    with pytest.raises(AiDependencyError):
        MusicGenAdapter(model_size="huge")


def test_generate_deep_dependency_missing_exit_6(mock_musicgen_env, tmp_path, monkeypatch):
    """全流程验证回归（1.5.6）：audiocraft 顶层可导入但深层依赖（triton）缺失时，
    generate 须收口为 AiDependencyError(6)，不得漏成意外错误。"""
    import builtins
    import sys as _sys

    from sunoauxtool.ai import musicgen as mg_mod

    adapter = mg_mod.MusicGenAdapter()
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "audiocraft.models" or name.startswith("audiocraft.models."):
            raise ModuleNotFoundError("No module named 'triton'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    _sys.modules.pop("audiocraft.models", None)
    wav = tmp_path / "m.wav"
    import numpy as np
    import soundfile as sf

    sf.write(str(wav), np.zeros(4410, dtype=np.float32), 44100)
    with pytest.raises(AiDependencyError) as ei:
        adapter.generate(str(wav), prompt="pop", seed=1, output=str(tmp_path / "o.wav"))
    assert ei.value.code == 6
    assert "triton" in str(ei.value)
