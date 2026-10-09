"""AudioSR（VASR）适配器单元测试（R9）。

不依赖真实上游源码 / torch / 权重：
- ``resolve_audiosr_dir`` / ``is_available`` 走环境变量与默认路径探测；
- ``_load_model`` / ``enhance`` 用 ``sys.modules`` 注入的 audiosr / torch 桩，
  验证「参数透传 chunk/overlap」「缺失依赖退出码 6」「缺失输入退出码 3」。

真实长音频末块 bug 的修复补丁见 ``patches/vasr_super_resolution_long_audio.patch``，
由 ``scripts/setup_vasr.py`` 在部署源码时应用。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np
import pytest

from sunoauxtool.ai import audiosr as mod
from sunoauxtool.exceptions import AiDependencyError, InputFileError


def _make_fake_audiosr(root: Path) -> Path:
    """写一个最小 audiosr 桩包：build_model / super_resolution_long_audio。

    super_resolution_long_audio 把调用参数写入模块级 ``_CAPTURED`` 供测试断言，
    并返回形状 ``(1, N)`` 的伪波（适配器写盘时取 ``.squeeze(0).numpy().T``）。
    """
    pkg = root / "audiosr"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(
        "import numpy as np\n"
        "\n"
        "_CAPTURED = {}\n"
        "\n"
        "class _Model:\n"
        "    pass\n"
        "\n"
        "def build_model(model_name='basic', device='cpu'):\n"
        "    return _Model()\n"
        "\n"
        "class _FakeWave:\n"
        "    def __init__(self, n):\n"
        "        self._arr = np.zeros((1, n), dtype=np.float32)\n"
        "    def squeeze(self, dim):\n"
        "        return self\n"
        "    def numpy(self):\n"
        "        return self._arr\n"
        "\n"
        "def super_resolution_long_audio(model, audio_path, seed=42, ddim_steps=50,\n"
        "                                guidance_scale=3.5, chunk_duration_s=15.0,\n"
        "                                overlap_duration_s=2.0):\n"
        "    _CAPTURED['kwargs'] = dict(seed=seed, ddim_steps=ddim_steps,\n"
        "        guidance_scale=guidance_scale, chunk_duration_s=chunk_duration_s,\n"
        "        overlap_duration_s=overlap_duration_s)\n"
        "    return _FakeWave(4800)\n",
        encoding="utf-8",
    )
    return pkg


@pytest.fixture
def fake_audiosr(tmp_path, monkeypatch):
    """注入一个可用的 audiosr 源码桩 + torch 桩，AUDIOSR_DIR 指向它。"""
    _make_fake_audiosr(tmp_path)
    monkeypatch.setenv("AUDIOSR_DIR", str(tmp_path))
    # 桩 torch，避免依赖真实 torch / CUDA
    torch_stub = types.ModuleType("torch")
    torch_stub.cuda = types.SimpleNamespace(is_available=lambda: False)
    monkeypatch.setitem(sys.modules, "torch", torch_stub)
    # 清掉可能已缓存的真实/旧 audiosr
    monkeypatch.delitem(sys.modules, "audiosr", raising=False)
    yield tmp_path


def _force_missing(monkeypatch, tmp_path):
    """让 resolve_audiosr_dir 在无 AUDIOSR_DIR 时也解析不到任何目录。"""
    monkeypatch.delenv("AUDIOSR_DIR", raising=False)
    monkeypatch.setattr(mod, "_DEFAULT_DIR", tmp_path / "nonexistent_vasr")


def _write_mono_wav(path: Path, n: int = 4800) -> None:
    import soundfile as sf

    sf.write(str(path), np.zeros(n, dtype=np.float32), 48000)


# -- resolve_audiosr_dir / is_available -----------------------------------


def test_resolve_audiosr_dir_from_env(fake_audiosr):
    d = mod.resolve_audiosr_dir()
    assert d is not None
    assert (d / "audiosr").is_dir()


def test_resolve_audiosr_dir_missing_returns_none(monkeypatch, tmp_path):
    _force_missing(monkeypatch, tmp_path)
    assert mod.resolve_audiosr_dir() is None


def test_is_available_true(fake_audiosr):
    assert mod.AudioSRAdapter().is_available() is True


def test_is_available_false_without_dir(monkeypatch, tmp_path):
    _force_missing(monkeypatch, tmp_path)
    assert mod.AudioSRAdapter().is_available() is False


# -- 缺失依赖 -> 退出码 6 （需先通过输入文件检查，否则会先报退出码 3）-----


def test_load_model_raises_when_missing(monkeypatch, tmp_path):
    _force_missing(monkeypatch, tmp_path)
    with pytest.raises(AiDependencyError) as ei:
        mod.AudioSRAdapter()._load_model()
    assert ei.value.code == 6


def test_enhance_missing_deps_raises(monkeypatch, tmp_path):
    _force_missing(monkeypatch, tmp_path)
    src = tmp_path / "x.wav"
    _write_mono_wav(src)
    adapter = mod.AudioSRAdapter()
    with pytest.raises(AiDependencyError) as ei:
        adapter.enhance(str(src), str(tmp_path / "o.wav"))
    assert ei.value.code == 6


# -- enhance 行为 -------------------------------------------------------


def test_enhance_missing_input_raises(fake_audiosr, tmp_path):
    adapter = mod.AudioSRAdapter()
    with pytest.raises(InputFileError) as ei:
        adapter.enhance(str(tmp_path / "nope.wav"), str(tmp_path / "out.wav"))
    assert ei.value.code == 3


def test_enhance_passes_chunk_overlap_and_writes(fake_audiosr, tmp_path):
    src = tmp_path / "in.wav"
    _write_mono_wav(src)
    out = tmp_path / "out.wav"

    adapter = mod.AudioSRAdapter(
        chunk_duration_s=15.0, overlap_duration_s=2.0, ddim_steps=20
    )
    result = adapter.enhance(str(src), str(out))

    assert Path(result).is_file()
    # 参数透传校验：适配器把分块/重叠/步数原样传给上游 super_resolution_long_audio
    import audiosr as audiosr_pkg

    kw = audiosr_pkg._CAPTURED["kwargs"]
    assert kw["chunk_duration_s"] == 15.0
    assert kw["overlap_duration_s"] == 2.0
    assert kw["ddim_steps"] == 20
    assert kw["seed"] == 42


# -- 网络错误镜像提示（2026-10-07 P1 评估） ------------------------------


def test_enhance_hf_timeout_hint(fake_audiosr, tmp_path, monkeypatch):
    """huggingface.co 超时 → AiDependencyError 内嵌 HF_ENDPOINT 镜像指引。"""
    import audiosr as audiosr_pkg

    def _boom(*a, **k):
        raise RuntimeError(
            "HTTPSConnectionPool(host='huggingface.co', port=443): "
            "Read timed out. (read timeout=10)"
        )

    monkeypatch.setattr(audiosr_pkg, "super_resolution_long_audio", _boom)
    src = tmp_path / "in.wav"
    _write_mono_wav(src)
    adapter = mod.AudioSRAdapter()
    with pytest.raises(AiDependencyError) as ei:
        adapter.enhance(str(src), str(tmp_path / "o.wav"))
    assert ei.value.code == 6
    assert "HF_ENDPOINT" in str(ei.value)
    assert "hf-mirror.com" in str(ei.value)


def test_enhance_generic_error_no_hf_hint(fake_audiosr, tmp_path, monkeypatch):
    """与网络无关的推理错误不加镜像提示（避免噪音误导排查方向）。"""
    import audiosr as audiosr_pkg

    def _boom(*a, **k):
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(audiosr_pkg, "super_resolution_long_audio", _boom)
    src = tmp_path / "in.wav"
    _write_mono_wav(src)
    adapter = mod.AudioSRAdapter()
    with pytest.raises(AiDependencyError) as ei:
        adapter.enhance(str(src), str(tmp_path / "o.wav"))
    assert ei.value.code == 6
    assert "HF_ENDPOINT" not in str(ei.value)
