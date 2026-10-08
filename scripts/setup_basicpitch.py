#!/usr/bin/env python3
"""一键安装 basic-pitch 复调配谱后端（1.4.8 F5）。

背景（2026-09-22 实测结论，见 requirements/ai.txt 与 .workbuddy 日志）：
- basic-pitch 0.4.0 的 pip 依赖标记在 py>=3.11 强制 ``tensorflow<2.15.1``，
  而 tensorflow 没有 3.12 轮子 → 直接 ``pip install basic-pitch`` 必炸；
- 推理实际只需要 onnxruntime（模型已随包打包，**不下载权重**）；
- 因此正确装法是 ``--no-deps`` 装主包 + 手动补三个轻量运行依赖。

用法::

    python scripts/setup_basicpitch.py           # 安装到当前解释器（幂等）
    python scripts/setup_basicpitch.py --check   # 只探测不安装（退出码 0/6）

退出码：0 成功；6 AI 模块不可用（与 AiDependencyError 口径一致）；其他 = 安装失败。
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys

#: 主包（--no-deps 安装）
MAIN_PKG = "basic-pitch"
#: 轻量运行依赖（audio 读 + 评估指标 + onnx 推理后端）
DEPS = ("resampy", "mir-eval", "onnxruntime")
#: 可选推理后端（按优先级；装上任意一个即可用）
BACKENDS = ("onnxruntime", "tensorflow", "tflite_runtime", "coremltools")

EXIT_OK = 0
EXIT_AI_UNAVAILABLE = 6
EXIT_INSTALL_FAILED = 7


def log(msg: str) -> None:
    print(f"[setup_basicpitch] {msg}", flush=True)


def _find_spec(name: str):
    try:
        return importlib.util.find_spec(name)
    except Exception:
        return None


def check() -> tuple[bool, str]:
    """探测安装状态。返回 (可用?, 描述)。"""
    if _find_spec("basic_pitch") is None:
        return False, "basic_pitch 未安装"
    backend = next((b for b in BACKENDS if _find_spec(b) is not None), None)
    if backend is None:
        return False, (
            "basic_pitch 已安装但无可用推理后端（其 __init__ 后端选择链无 else，"
            "四后端全缺时 import 即 NameError）"
        )
    return True, f"就绪（推理后端: {backend}）"


def _pip(*args: str) -> None:
    cmd = [sys.executable, "-m", "pip", *args]
    log(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def install() -> None:
    ok, desc = check()
    if ok:
        log(f"已就绪，跳过安装: {desc}")
        return
    log(f"当前状态: {desc}")
    # 主包 --no-deps：绕开 py>=3.11 的 tensorflow<2.15.1 死锁标记
    if _find_spec("basic_pitch") is None:
        try:
            _pip("install", MAIN_PKG, "--no-deps")
        except subprocess.CalledProcessError as exc:
            log(f"{MAIN_PKG} 安装失败: {exc}")
            raise SystemExit(EXIT_INSTALL_FAILED) from exc
    # 轻量运行依赖（已装的 pip 会自动满足/跳过）
    try:
        _pip("install", *DEPS)
    except subprocess.CalledProcessError as exc:
        log(f"运行依赖安装失败: {exc}")
        raise SystemExit(EXIT_INSTALL_FAILED) from exc
    ok, desc = check()
    if not ok:
        log(f"安装后仍不可用: {desc}")
        raise SystemExit(EXIT_INSTALL_FAILED)
    log(f"安装完成: {desc}")


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if "--check" in args:
        ok, desc = check()
        log(desc)
        raise SystemExit(EXIT_OK if ok else EXIT_AI_UNAVAILABLE)
    install()


if __name__ == "__main__":
    main()
