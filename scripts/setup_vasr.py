#!/usr/bin/env python3
"""Setup AudioSR (VASR) source tree for SunoAuxTool (R9).

把 haoheliu/versatile_audio_super_resolution 克隆到
``src/versatile_audio_super_resolution``（或 ``AUDIOSR_DIR`` 指向的目录），
剥离内嵌 ``.git``，并应用 ``patches/vasr_super_resolution_long_audio.patch``
修复长音频末块 bug（[SunoAuxTool patch R5]）。幂等：已存在则跳过。

本脚本只准备**源码**；重型依赖 torch/torchaudio 见 ``requirements/vasr.txt``，
约 2.6GB 模型权重首次运行自动下载到 ``~/.cache/huggingface``。

用法::

    python scripts/setup_vasr.py
    AUDIOSR_DIR=/path/to/audiosr python scripts/setup_vasr.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = REPO_ROOT / "src" / "versatile_audio_super_resolution"
UPSTREAM = "https://github.com/haoheliu/versatile_audio_super_resolution"
PINNED_COMMIT = "d312fba"
PATCH = REPO_ROOT / "patches" / "vasr_super_resolution_long_audio.patch"
PATCH_MARKER = "[SunoAuxTool patch R5]"


def log(msg: str) -> None:
    print(f"[setup_vasr] {msg}", flush=True)


def run(cmd: list[str], cwd: str | None = None) -> None:
    log(f"$ {' '.join(cmd)}" + (f"  (cwd={cwd})" if cwd else ""))
    subprocess.run(cmd, cwd=cwd, check=True)


def resolve_target() -> Path:
    env = os.environ.get("AUDIOSR_DIR")
    return Path(env) if env else DEFAULT_TARGET


def clone(target: Path) -> None:
    if (target / "audiosr").is_dir():
        log(f"audiosr 已存在，跳过克隆: {target}")
        return
    if target.exists():
        log(f"目标目录已存在但无 audiosr 子包，中止以避免覆盖: {target}")
        sys.exit(2)
    tmp = target.with_name(target.name + ".clone_tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    run(["git", "clone", UPSTREAM, str(tmp)])
    run(["git", "-C", str(tmp), "checkout", PINNED_COMMIT])
    # 去掉嵌套 .git，纳入本仓 gitignore 管理（src/versatile_audio_super_resolution/）
    git_dir = tmp / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir)
    tmp.rename(target)
    log(f"克隆完成 @ {PINNED_COMMIT}: {target}")


def apply_patch(target: Path) -> None:
    pipeline = target / "audiosr" / "pipeline.py"
    if not pipeline.is_file():
        log(f"缺失 audiosr/pipeline.py，补丁无法应用: {pipeline}")
        sys.exit(3)
    if PATCH_MARKER in pipeline.read_text(encoding="utf-8"):
        log("补丁 [SunoAuxTool patch R5] 已应用，跳过。")
        return
    if not PATCH.is_file():
        log(f"补丁文件缺失: {PATCH}")
        sys.exit(4)
    # 优先 git apply（可在非 git 仓库目录工作），失败回退 patch -p1
    try:
        subprocess.run(
            ["git", "apply", "-p1", str(PATCH)],
            cwd=str(target),
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        log("git apply 失败，回退 patch -p1 ...")
        try:
            subprocess.run(
                ["patch", "-p1", "-i", str(PATCH)],
                cwd=str(target),
                check=True,
                capture_output=True,
            )
        except FileNotFoundError:
            log("patch 命令不可用；请手动 `git apply -p1 patches/vasr_super_resolution_long_audio.patch`")
            sys.exit(5)
        except subprocess.CalledProcessError as exc:
            log(f"补丁应用失败: {exc}")
            sys.exit(5)
    log("补丁已应用。")


#: B6（1.4.8）：torch>=2.6 起 ``torch.load`` 默认 ``weights_only=True``，
#: 上游 19 处加载点会全部炸掉——克隆后统一补 ``weights_only=False``。
_TORCH_LOAD_RE = re.compile(r"torch\.load\(([^)]*)\)")


def patch_torch_load(target: Path) -> int:
    """给上游所有缺 ``weights_only`` 的 ``torch.load(...)`` 单行调用补参数。

    只处理单行调用（跨行调用保守跳过并计数报告）；幂等：已含 weights_only
    的调用不重复添加。返回本次修改的调用数。
    """
    patched = 0
    skipped_multiline = 0
    for py in sorted((target / "audiosr").rglob("*.py")):
        with open(py, encoding="utf-8", newline="") as fh:  # 保留原换行符
            text = fh.read()
        if "torch.load(" not in text:
            continue
        lines = text.splitlines(keepends=True)
        changed = False
        for i, line in enumerate(lines):
            if "torch.load(" not in line or "weights_only" in line:
                continue
            # 跨行调用（括号不平衡）保守跳过，不盲改
            if line.count("(") != line.count(")"):
                skipped_multiline += 1
                continue
            new = _TORCH_LOAD_RE.sub(lambda m: f"torch.load({m.group(1)}, weights_only=False)", line)
            if new != line:
                lines[i] = new
                changed = True
                patched += 1
        if changed:
            with open(py, "w", encoding="utf-8", newline="") as fh:
                fh.write("".join(lines))
    log(f"torch.load weights_only 补丁: 本次修改 {patched} 处"
        + (f"，跨行调用跳过 {skipped_multiline} 处（需手工确认）" if skipped_multiline else ""))
    return patched


def main() -> None:
    target = resolve_target()
    log(f"目标目录: {target}")
    log(f"上游 @ {PINNED_COMMIT}: {UPSTREAM}")
    clone(target)
    apply_patch(target)
    patch_torch_load(target)  # B6 预防：torch>=2.6 兼容
    log("权重首次运行自动下载到: ~/.cache/huggingface")
    log("运行依赖见 requirements/vasr.txt")
    log("完成。")


if __name__ == "__main__":
    main()
