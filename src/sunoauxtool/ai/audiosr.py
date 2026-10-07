"""AudioSR（VASR）音质提升适配器（R5）。

能力：音频超分 / 高频重建（**不是人声分离**——人声分离另行选型）。
上游：haoheliu/versatile_audio_super_resolution（AudioSR）本地克隆 @ d312fba，
源码位于 ``src/versatile_audio_super_resolution``（自带内嵌 .git，R5 入库时清理）。

设计要点：
- **零顶层重依赖**：本模块顶层只 import 标准库；audiosr/torch 全部在推理时
  延迟导入（CI 零 torch 断言不受影响，未装依赖不影响主包任何功能）。
- **可选装目录模式**（与 diffrhythm 同构）：目录定位顺序
  环境变量 ``AUDIOSR_DIR`` > 默认 ``<repo>/src/versatile_audio_super_resolution``。
- 长音频走上游 ``super_resolution_long_audio``：15s 分块 / 2s 重叠 Hann 交叉淡化
  / 块级峰值还原 / overlap-add 归一化（2026-09-21 实测 32s 冒烟：时长精确对齐）。
- 输出：**单声道 48kHz** WAV（上游管线内部将立体声混为单声道；这是上游行为，
  保留立体声需改上游，暂不做）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from sunoauxtool.exceptions import AiDependencyError, InputFileError

_INSTALL_GUIDE = (
    "audiosr 不可用：未找到 AudioSR 源码目录。\n"
    "方式一：克隆上游仓库到 src/versatile_audio_super_resolution（默认探测路径）\n"
    "  git clone https://github.com/haoheliu/versatile_audio_super_resolution\n"
    "方式二：设环境变量 AUDIOSR_DIR 指向任意 audiosr 源码目录\n"
    "依赖（torch/torchaudio 等）见 requirements/vasr.txt；首次运行自动下载约 2.6GB 权重。\n"
    "网络：直连 huggingface.co 不通时设 HF_ENDPOINT=https://hf-mirror.com（国内镜像）。"
)

_DEFAULT_DIR = Path(__file__).resolve().parents[2] / "versatile_audio_super_resolution"


def resolve_audiosr_dir() -> Optional[Path]:
    """定位 audiosr 源码目录；找不到返回 None。"""
    env = os.environ.get("AUDIOSR_DIR")
    cand = Path(env) if env else _DEFAULT_DIR
    return cand if (cand / "audiosr").is_dir() else None


def _ensure_importable() -> None:
    """把 audiosr 源码目录挂到 sys.path（幂等），失败抛 AiDependencyError。"""
    d = resolve_audiosr_dir()
    if d is None:
        raise AiDependencyError(_INSTALL_GUIDE, code=6)
    d_str = str(d)
    if d_str not in sys.path:
        sys.path.insert(0, d_str)


class AudioSRAdapter:
    """AudioSR 超分适配器（可选 AI 后端，R5）。

    用法::

        adapter = AudioSRAdapter()
        if adapter.is_available():
            adapter.enhance("in.wav", "out.wav")
    """

    def __init__(
        self,
        model_name: str = "basic",
        device: Optional[str] = None,
        seed: int = 42,
        ddim_steps: int = 50,
        guidance_scale: float = 3.5,
        chunk_duration_s: float = 15.0,
        overlap_duration_s: float = 2.0,
    ) -> None:
        """初始化（不触发模型加载）。

        Args:
            model_name: ``basic``（音乐/通用）或 ``speech``。
            device: ``cuda`` / ``cpu``；None 自动选（有 CUDA 用 CUDA）。
            ddim_steps: DDIM 采样步数（默认 50，R0 实测 50 步质量/耗时均衡）。
            chunk_duration_s / overlap_duration_s: 长音频分块参数（须 chunk > overlap）。
        """
        if chunk_duration_s <= overlap_duration_s:
            raise ValueError("chunk_duration_s 必须大于 overlap_duration_s")
        self.model_name = model_name
        self.device = device
        self.seed = seed
        self.ddim_steps = ddim_steps
        self.guidance_scale = guidance_scale
        self.chunk_duration_s = chunk_duration_s
        self.overlap_duration_s = overlap_duration_s
        self._model = None  # 惰性加载

    # -- 可用性 ------------------------------------------------------------

    def is_available(self) -> bool:
        """audiosr 源码目录是否就位（不触发实际 import，轻量检查）。"""
        return resolve_audiosr_dir() is not None

    def _load_model(self):
        """惰性加载 AudioSR 模型（权重缺失/下载失败转 AiDependencyError）。"""
        if self._model is None:
            _ensure_importable()
            try:
                import torch

                import audiosr

                if self.device is not None:
                    device = self.device
                else:
                    device = "cuda" if torch.cuda.is_available() else "cpu"
                self._model = audiosr.build_model(
                    model_name=self.model_name, device=device
                )
            except AiDependencyError:
                raise
            except Exception as exc:
                raise AiDependencyError(
                    f"AudioSR 模型加载失败（权重下载/损坏？依赖缺失？）: {exc}", code=6
                ) from exc
        return self._model

    # -- 推理 --------------------------------------------------------------

    def enhance(self, input_path: str, output_path: str) -> str:
        """对整段音频做超分增强，写 48kHz PCM_24 WAV，返回输出路径。

        长音频自动分块（``super_resolution_long_audio``）；输出为
        **单声道 48kHz**（上游行为）。

        Raises:
            InputFileError: 输入文件不存在（退出码 3）。
            AiDependencyError: 依赖/模型不可用（退出码 6）。
        """
        src = Path(input_path)
        if not src.is_file():
            raise InputFileError(f"输入音频不存在: {src}", code=3)

        import soundfile as sf

        model = self._load_model()
        try:
            import audiosr

            wave = audiosr.super_resolution_long_audio(
                model,
                str(src),
                seed=int(self.seed),
                ddim_steps=int(self.ddim_steps),
                guidance_scale=float(self.guidance_scale),
                chunk_duration_s=float(self.chunk_duration_s),
                overlap_duration_s=float(self.overlap_duration_s),
            )
        except AiDependencyError:
            raise
        except Exception as exc:
            hint = ""
            # huggingface.co 直连不通（本机被墙）时给出镜像指引；
            # 否则用户只会看到一串 ConnectTimeout，不知道怎么修。
            if "huggingface.co" in str(exc) or "timed out" in str(exc):
                hint = (
                    "；网络提示：直连 huggingface.co 超时，"
                    "设 HF_ENDPOINT=https://hf-mirror.com 走国内镜像；"
                    "权重已缓存后可加 HF_HUB_OFFLINE=1 跳过联网校验"
                )
            raise AiDependencyError(f"AudioSR 推理失败: {exc}{hint}", code=6) from exc

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        # wave: torch [1(ch), N] 48kHz -> numpy (N, 1)
        sf.write(str(out), wave.squeeze(0).numpy().T, 48000, subtype="PCM_24")
        return str(out)
