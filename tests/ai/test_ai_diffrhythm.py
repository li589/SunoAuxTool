"""DiffRhythm 适配器单测（T-P1-2）。

全部 mock subprocess/torch，不真跑推理、不依赖 GPU/权重（无 GPU 环境可跑）。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np
import pytest

from sunoauxtool.ai.diffrhythm import DiffRhythmAdapter
from sunoauxtool.exceptions import AiDependencyError, ParameterError


# ---------------------------------------------------------------------------
# fake torch（显存检查用）
# ---------------------------------------------------------------------------


class _FakeCuda:
    def __init__(self) -> None:
        self.available = False
        self.free_bytes = 0
        self.total_bytes = 0

    def is_available(self) -> bool:
        return self.available

    def mem_get_info(self, _i: int):
        return (self.free_bytes, self.total_bytes)

    def get_device_properties(self, _i: int):
        return types.SimpleNamespace(total_memory=self.total_bytes)


class _FakeTorch:
    def __init__(self) -> None:
        self.cuda = _FakeCuda()


@pytest.fixture
def fake_torch():
    return _FakeTorch()


# ---------------------------------------------------------------------------
# 仓库 fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repo(tmp_path):
    """构造最小 DiffRhythm 仓库目录。"""
    repo = tmp_path / "diffrhythm"
    (repo / "infer").mkdir(parents=True)
    (repo / "thirdparty").mkdir()
    infer_py = repo / "infer" / "infer.py"
    infer_py.write_text(
        "import torch\n"
        "def inference(cfm_model, chunked=False):\n"
        "    output = decode_audio(latent, vae_model, chunked=chunked)\n"
        "    return output\n",
        encoding="utf-8",
    )
    return repo


@pytest.fixture
def fake_subprocess_run(monkeypatch, tmp_path):
    """mock subprocess.run：解析 --output-dir 并写出 output.wav。"""
    import sunoauxtool.ai.diffrhythm as dr_mod

    def _fake_run(cmd, cwd=None, env=None, capture_output=True, text=True, timeout=3600):
        out_idx = cmd.index("--output-dir") + 1
        out_dir = cmd[out_idx]
        from pathlib import Path

        d = Path(out_dir)
        d.mkdir(parents=True, exist_ok=True)
        sr = 44100
        t = np.linspace(0, 1.0, sr, endpoint=False)
        audio = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        stereo = np.stack([audio, audio], axis=1)
        import soundfile as sf

        sf.write(str(d / "output.wav"), stereo, sr, subtype="PCM_16")
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(dr_mod.subprocess, "run", _fake_run)
    return _fake_run


@pytest.fixture
def mock_go_env(monkeypatch, fake_repo, fake_torch, fake_subprocess_run):
    """GO 分支环境：is_available()=True + fake torch（cuda 8GB 充足）。"""
    import sunoauxtool.ai.diffrhythm as dr_mod

    monkeypatch.setattr(dr_mod.DiffRhythmAdapter, "is_available", lambda self: True)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    fake_torch.cuda.available = True
    fake_torch.cuda.free_bytes = int(8.5 * 1024**3)
    fake_torch.cuda.total_bytes = int(8 * 1024**3)
    return {"repo": fake_repo, "torch": fake_torch}


# ---------------------------------------------------------------------------
# _patch_chunked
# ---------------------------------------------------------------------------


def test_patch_chunked_replaces_both(fake_repo):
    adapter = DiffRhythmAdapter(model_dir=str(fake_repo))
    script = fake_repo / "infer" / "infer.py"
    adapter._patch_chunked(str(script))
    text = script.read_text(encoding="utf-8")
    assert "chunked=True" in text
    assert "chunked=chunked" not in text
    assert "def inference(cfm_model, chunked=True)" in text
    assert "decode_audio(latent, vae_model, chunked=True)" in text


def test_patch_chunked_idempotent(fake_repo):
    adapter = DiffRhythmAdapter(model_dir=str(fake_repo))
    script = fake_repo / "infer" / "infer.py"
    adapter._patch_chunked(str(script))
    adapter._patch_chunked(str(script))  # 第二次调用不应报错
    text = script.read_text(encoding="utf-8")
    assert text.count("chunked=True") >= 2


def test_patch_chunked_raises_when_not_found(tmp_path):
    script = tmp_path / "infer.py"
    script.write_text("def foo():\n    pass\n", encoding="utf-8")
    adapter = DiffRhythmAdapter(model_dir=str(tmp_path))
    with pytest.raises(AiDependencyError) as ei:
        adapter._patch_chunked(str(script))
    assert ei.value.code == 6


# ---------------------------------------------------------------------------
# 仓库发现 / espeak
# ---------------------------------------------------------------------------


def test_repo_dir_explicit(fake_repo):
    assert DiffRhythmAdapter(model_dir=str(fake_repo)).repo_dir() == fake_repo.resolve()


def test_repo_dir_env(monkeypatch, fake_repo):
    monkeypatch.setenv("DIFFRHYTHM_DIR", str(fake_repo))
    assert DiffRhythmAdapter().repo_dir() == fake_repo.resolve()


def test_repo_dir_default_module(tmp_path, monkeypatch):
    repo = tmp_path / "module" / "diffrhythm"
    (repo / "infer").mkdir(parents=True)
    (repo / "infer" / "infer.py").write_text("x=1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert DiffRhythmAdapter().repo_dir() == repo.resolve()


@pytest.mark.parametrize(
    "with_env,with_model_dir,winner",
    [
        (True, False, "env"),
        (False, True, "model"),
        (True, True, "model"),  # 命令行 --diffrhythm-dir 优先于 DIFFRHYTHM_DIR 环境变量
    ],
)
def test_repo_dir_priority_over_env(monkeypatch, tmp_path, with_env, with_model_dir, winner):
    """model_dir（CLI --diffrhythm-dir）优先级高于 DIFFRHYTHM_DIR 环境变量。"""
    env_repo = tmp_path / "env_repo"
    (env_repo / "infer").mkdir(parents=True)
    (env_repo / "infer" / "infer.py").write_text("x=1\n", encoding="utf-8")
    model_repo = tmp_path / "model_repo"
    (model_repo / "infer").mkdir(parents=True)
    (model_repo / "infer" / "infer.py").write_text("x=1\n", encoding="utf-8")
    if with_env:
        monkeypatch.setenv("DIFFRHYTHM_DIR", str(env_repo))
    else:
        monkeypatch.delenv("DIFFRHYTHM_DIR", raising=False)
    adapter = DiffRhythmAdapter(model_dir=str(model_repo) if with_model_dir else None)
    expected = (model_repo if winner == "model" else env_repo).resolve()
    assert adapter.repo_dir() == expected


def test_check_espeak(monkeypatch):
    # 环境无关：mock _espeak_dir 探测结果
    adapter = DiffRhythmAdapter()
    monkeypatch.setattr(DiffRhythmAdapter, "_espeak_dir", lambda self: "C:/Program Files/eSpeak NG")
    assert adapter._check_espeak() is True
    monkeypatch.setattr(DiffRhythmAdapter, "_espeak_dir", lambda self: None)
    assert adapter._check_espeak() is False


def test_is_available_false_without_torch(monkeypatch, fake_repo):
    import sunoauxtool.ai.diffrhythm as dr_mod

    monkeypatch.setattr(dr_mod.importlib.util, "find_spec", lambda _name: None)
    monkeypatch.setattr(dr_mod.shutil, "which", lambda _name: "espeak-ng")
    assert DiffRhythmAdapter(model_dir=str(fake_repo)).is_available() is False


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


def test_generate_go_branch(mock_go_env, tmp_path):
    out = tmp_path / "song.wav"
    adapter = DiffRhythmAdapter(model_dir=str(mock_go_env["repo"]))
    path = adapter.generate(
        "", "slow ballad", lyrics="第一句\n第二句", duration=95, output_path=str(out)
    )
    assert path == str(out.resolve())
    assert out.is_file() and out.stat().st_size > 0
    # chunked 补丁已写入
    text = (mock_go_env["repo"] / "infer" / "infer.py").read_text(encoding="utf-8")
    assert "chunked=True" in text


def test_generate_default_output_path(mock_go_env):
    adapter = DiffRhythmAdapter(model_dir=str(mock_go_env["repo"]))
    path = adapter.generate("", "slow ballad", lyrics="词", duration=95)
    assert path.endswith("output.wav")
    assert Path(path).is_file()


def test_generate_no_deps_exit_6(monkeypatch):
    import sunoauxtool.ai.diffrhythm as dr_mod

    monkeypatch.setattr(dr_mod.DiffRhythmAdapter, "is_available", lambda self: False)
    with pytest.raises(AiDependencyError) as ei:
        DiffRhythmAdapter().generate("", "slow ballad")
    assert ei.value.code == 6
    assert "requirements/ai.txt" in str(ei.value)


def test_generate_vram_insufficient_free(mock_go_env, tmp_path):
    """总显存 8GB 但可用 <6.8GB -> 提示关闭其他 GPU 应用（退出码 6）。"""
    mock_go_env["torch"].cuda.free_bytes = int(6.0 * 1024**3)
    adapter = DiffRhythmAdapter(model_dir=str(mock_go_env["repo"]), device="cuda")
    with pytest.raises(AiDependencyError) as ei:
        adapter.generate("", "slow ballad", output_path=str(tmp_path / "o.wav"))
    assert ei.value.code == 6
    assert "可用显存不足" in str(ei.value)


def test_generate_vram_total_below_8gb(mock_go_env, tmp_path):
    """显卡总显存 <8GB -> 明确不可用（NO-GO 语义）。"""
    mock_go_env["torch"].cuda.total_bytes = int(6 * 1024**3)
    mock_go_env["torch"].cuda.free_bytes = int(7 * 1024**3)  # free 充足，触发 total 检查
    adapter = DiffRhythmAdapter(model_dir=str(mock_go_env["repo"]), device="cuda")
    with pytest.raises(AiDependencyError) as ei:
        adapter.generate("", "slow ballad", output_path=str(tmp_path / "o.wav"))
    assert ei.value.code == 6
    assert "≥8GB" in str(ei.value)


def test_generate_invalid_duration(mock_go_env):
    adapter = DiffRhythmAdapter(model_dir=str(mock_go_env["repo"]))
    with pytest.raises(ParameterError) as ei:
        adapter.generate("", "slow ballad", duration=50)
    assert ei.value.code == 1


def test_generate_cpu_device(mock_go_env, tmp_path, monkeypatch):
    import sunoauxtool.ai.diffrhythm as dr_mod

    captured = {}

    def _fake_run(cmd, cwd=None, env=None, capture_output=True, text=True, timeout=3600):
        captured["env"] = env
        out_idx = cmd.index("--output-dir") + 1
        from pathlib import Path

        d = Path(cmd[out_idx])
        d.mkdir(parents=True, exist_ok=True)
        sr = 44100
        t = np.linspace(0, 0.5, sr // 2, endpoint=False)
        audio = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        import soundfile as sf

        sf.write(str(d / "output.wav"), np.stack([audio, audio], axis=1), sr)
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(dr_mod.subprocess, "run", _fake_run)
    adapter = DiffRhythmAdapter(model_dir=str(mock_go_env["repo"]), device="cpu")
    out = tmp_path / "cpu.wav"
    adapter.generate("", "slow ballad", output_path=str(out))
    assert captured["env"].get("CUDA_VISIBLE_DEVICES") == ""


# ---------------------------------------------------------------------------
# _build_lrc
# ---------------------------------------------------------------------------


def test_build_lrc_with_lyrics(tmp_path):
    adapter = DiffRhythmAdapter()
    lrc = tmp_path / "lyrics.lrc"
    adapter._build_lrc("第一句\n第二句", 90.0, lrc)
    text = lrc.read_text(encoding="utf-8")
    assert "[ti:SunoAuxTool]" in text
    assert "第一句" in text
    assert "第二句" in text


def test_build_lrc_empty(tmp_path):
    adapter = DiffRhythmAdapter()
    lrc = tmp_path / "empty.lrc"
    adapter._build_lrc(None, 95.0, lrc)
    text = lrc.read_text(encoding="utf-8")
    assert "[ti:SunoAuxTool]" in text
