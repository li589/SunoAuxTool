"""1.4.8 F5/F6：安装脚手架测试（setup_basicpitch + setup_vasr 的 torch.load 补丁）。

scripts/ 不是包，用 importlib 按路径加载（同 test_version_alignment 的
check_changelog 先例）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name, _ROOT / "scripts" / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bp():
    return _load("setup_basicpitch")


@pytest.fixture(scope="module")
def vasr():
    return _load("setup_vasr")


# ---------------------------------------------------------------------------
# F5：setup_basicpitch
# ---------------------------------------------------------------------------


def test_check_not_installed(bp, monkeypatch):
    monkeypatch.setattr(bp, "_find_spec", lambda name: None)
    ok, desc = bp.check()
    assert not ok
    assert "未安装" in desc


def test_check_installed_no_backend(bp, monkeypatch):
    """已装主包但四后端全缺 -> 不可用（规避 __init__ 无 else 的 NameError）。"""
    monkeypatch.setattr(bp, "_find_spec", lambda name: None if name != "basic_pitch" else object())
    ok, desc = bp.check()
    assert not ok
    assert "无可用推理后端" in desc


def test_check_ready(bp, monkeypatch):
    specs = {"basic_pitch", "onnxruntime"}
    monkeypatch.setattr(bp, "_find_spec", lambda name: object() if name in specs else None)
    ok, desc = bp.check()
    assert ok
    assert "onnxruntime" in desc


def test_check_priority_onnxruntime_first(bp, monkeypatch):
    """多后端并存时报优先级最高的（onnxruntime 优先于 tensorflow）。"""
    specs = {"basic_pitch", "tensorflow", "onnxruntime"}
    monkeypatch.setattr(bp, "_find_spec", lambda name: object() if name in specs else None)
    _, desc = bp.check()
    assert "onnxruntime" in desc


def test_main_check_exit_codes(bp, monkeypatch):
    monkeypatch.setattr(bp, "_find_spec", lambda name: None)
    with pytest.raises(SystemExit) as ei:
        bp.main(["--check"])
    assert ei.value.code == bp.EXIT_AI_UNAVAILABLE  # 6，与 AiDependencyError 口径一致


def test_main_check_ready_exits_zero(bp, monkeypatch):
    specs = {"basic_pitch", "onnxruntime"}
    monkeypatch.setattr(bp, "_find_spec", lambda name: object() if name in specs else None)
    with pytest.raises(SystemExit) as ei:
        bp.main(["--check"])
    assert ei.value.code == bp.EXIT_OK


# ---------------------------------------------------------------------------
# F6：setup_vasr.patch_torch_load（B6 预防，torch>=2.6 前置）
# ---------------------------------------------------------------------------


def _write(py: Path, text: str, newline: str = "\n") -> None:
    with open(py, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(text)


def _read(py: Path) -> str:
    with open(py, encoding="utf-8", newline="") as fh:
        return fh.read()


def test_patch_torch_load_adds_weights_only(vasr, tmp_path):
    """单行 torch.load 补 weights_only=False；已带参数/无调用的文件不动。"""
    pkg = tmp_path / "audiosr"
    pkg.mkdir()
    _write(pkg / "a.py", "x = torch.load(p)\ny = torch.load(q, map_location='cpu')\n")
    _write(pkg / "b.py", "z = torch.load(w, weights_only=True)\n")
    _write(pkg / "c.py", "plain = 1\n")

    n = vasr.patch_torch_load(tmp_path)
    assert n == 2
    out = _read(pkg / "a.py")
    assert "torch.load(p, weights_only=False)" in out
    assert "torch.load(q, map_location='cpu', weights_only=False)" in out
    assert "weights_only=True" in _read(pkg / "b.py")
    assert "torch.load" not in _read(pkg / "c.py")


def test_patch_torch_load_idempotent(vasr, tmp_path):
    """幂等：二次运行 0 处修改。"""
    pkg = tmp_path / "audiosr"
    pkg.mkdir()
    _write(pkg / "a.py", "x = torch.load(p)\n")
    assert vasr.patch_torch_load(tmp_path) == 1
    assert vasr.patch_torch_load(tmp_path) == 0


def test_patch_torch_load_skips_multiline(vasr, tmp_path):
    """跨行调用（括号不平衡）保守跳过，不盲改。"""
    pkg = tmp_path / "audiosr"
    pkg.mkdir()
    _write(
        pkg / "a.py",
        "x = torch.load(\n    path,\n    map_location='cpu',\n)\n",
    )
    assert vasr.patch_torch_load(tmp_path) == 0
    assert "weights_only" not in _read(pkg / "a.py")


def test_patch_torch_load_preserves_crlf(vasr, tmp_path):
    """上游文件是 CRLF 时补丁不改写换行符。"""
    pkg = tmp_path / "audiosr"
    pkg.mkdir()
    _write(pkg / "a.py", "x = 1\r\ny = torch.load(p)\r\n", newline="")
    assert vasr.patch_torch_load(tmp_path) == 1
    assert "\r\n" in _read(pkg / "a.py")


def test_patch_torch_load_missing_pkg_dir(vasr, tmp_path):
    """audiosr 目录不存在（未克隆）：安全返回 0。"""
    assert vasr.patch_torch_load(tmp_path) == 0


def test_vasr_script_constants(vasr):
    """脚本常量与文档口径一致（防漂移）。"""
    assert vasr.PINNED_COMMIT == "d312fba"
    assert vasr.PATCH_MARKER == "[SunoAuxTool patch R5]"
