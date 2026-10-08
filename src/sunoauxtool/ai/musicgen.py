"""MusicGen 适配器（已完整实现）。

- melody conditioning：`audiocraft.models.MusicGen.generate_with_chroma`（输入旋律 WAV -> 伴奏 WAV）
- 默认 `facebook/musicgen-medium`（1.5B，fp16，8GB 显存可跑）；`--model-size small` 降档
- 延迟导入：本模块顶部零 torch/audiocraft import（P0 模块零重型 import 约束）
- 显存检查防 OOM：可用显存不足时给出友好提示与降档建议，不崩溃
- `--seed` 可复现（torch.manual_seed + cuda.manual_seed_all）
- 输出 32kHz WAV，可被 `export suno` 消费（内部重采样由导出链完成）
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Optional

from sunoauxtool.ai.base import AIGenerator
from sunoauxtool.exceptions import AiDependencyError, InputFileError, ParameterError

#: 模型规格 -> HuggingFace 模型名
#: 注意（T-P1-1 实测修正）：audiocraft 中仅 facebook/musicgen-melody（1.5B）支持
#: generate_with_chroma 旋律条件；musicgen-medium 同为 1.5B 但不支持 chroma。
#: 因此 "medium" 规格映射到 musicgen-melody（1.5B fp16，8GB 可跑）；"small" 为 300M
#: 降档（不支持 chroma，适配器自动降级为纯文本 generate）。
_MODEL_SIZES = {
    "medium": "facebook/musicgen-melody",
    "small": "facebook/musicgen-small",
}

#: 各规格预估最低可用显存（GB，free 口径）。medium fp16 实测峰值约 4-5GB，留余量设 5.5；
#: 低于阈值给出降档建议并报错（不 OOM 崩溃）。
_VRAM_REQUIREMENTS_GB = {
    "medium": 5.5,
    "small": 3.5,
}

#: 输出采样率（MusicGen 原生）
OUTPUT_SAMPLE_RATE = 32000

#: 默认时长下限/上限（秒）
_MIN_DURATION_S = 1.0
_MAX_DURATION_S = 30.0

_INSTALL_GUIDE = (
    "MusicGen 不可用：未安装 P1 依赖。\n"
    "请先安装: pip install torch --index-url https://download.pytorch.org/whl/cu121\n"
    "然后: pip install -r requirements/ai.txt\n"
    "若下载权重较慢，可设置环境变量 HF_ENDPOINT=https://hf-mirror.com 使用国内镜像。"
)


class MusicGenAdapter(AIGenerator):
    """MusicGen 适配器（melody conditioning 扩编曲）。"""

    def __init__(
        self,
        model_name: Optional[str] = None,
        device: str = "cuda",
        model_size: str = "medium",
    ) -> None:
        """初始化。

        Args:
            model_name: 显式模型名；None 时按 model_size 选择。
            device: 推理设备 cuda|cpu。
            model_size: medium|small（默认 medium）。

        Raises:
            AiDependencyError: model_size 非法（退出码 6）。
        """
        if model_size not in _MODEL_SIZES:
            raise AiDependencyError(
                f"未知模型规格: {model_size!r}（可选 medium|small）", code=6
            )
        self.model_name = model_name or _MODEL_SIZES[model_size]
        self.device = device
        self.model_size = model_size

    # -- 可用性 ------------------------------------------------------------

    def is_available(self) -> bool:
        """检查 audiocraft + torch 是否已安装（find_spec 不触发实际 import）。"""
        return (
            importlib.util.find_spec("audiocraft") is not None
            and importlib.util.find_spec("torch") is not None
        )

    def check_vram(self) -> Optional[float]:
        """返回当前可用显存 GB；无 CUDA 返回 None。"""
        try:
            import torch  # 延迟导入
        except ImportError:  # pragma: no cover - 依赖缺失路径由 is_available 拦截
            return None
        if not torch.cuda.is_available():
            return None
        try:
            free, _total = torch.cuda.mem_get_info(0)
        except (AttributeError, RuntimeError):  # pragma: no cover - 老版本/驱动异常兜底
            props = torch.cuda.get_device_properties(0)
            free = props.total_memory
        return free / (1024**3)

    # -- 推理 --------------------------------------------------------------

    def _read_melody(self, source_wav: str) -> tuple:
        """读取旋律 WAV 为单声道 float32 numpy 数组（延迟导入 soundfile）。"""
        import soundfile as sf

        src = Path(source_wav)
        if not src.is_file():
            raise InputFileError(f"输入旋律 WAV 不存在: {src}", code=3)
        data, sr = sf.read(str(src), dtype="float32", always_2d=True)
        if data.shape[0] == 0:  # pragma: no cover - 空文件防御
            raise InputFileError(f"输入旋律 WAV 为空: {src}", code=3)
        # 混为单声道（多声道取均值）
        mono = data.mean(axis=1)
        return mono, int(sr)

    def _resolve_duration(self, input_seconds: float, duration: Optional[float]) -> float:
        """确定推理时长：--duration 优先；默认对齐输入旋律，落在 [10, 30]s。"""
        if duration is None:
            return max(10.0, min(float(input_seconds), _MAX_DURATION_S))
        d = float(duration)
        if not (_MIN_DURATION_S <= d <= _MAX_DURATION_S):
            raise ParameterError(
                f"duration 超出范围 ({_MIN_DURATION_S:g}-{_MAX_DURATION_S:g}s): {d}", code=1
            )
        return d

    def generate(
        self,
        source_wav: str,
        prompt: str,
        *,
        duration: Optional[float] = None,
        seed: Optional[int] = None,
        output_path: Optional[str] = None,
        **_kw,
    ) -> str:
        """以旋律 WAV 为条件生成伴奏 WAV。

        Args:
            source_wav: 输入旋律 WAV 路径。
            prompt: 风格提示，如 "upbeat pop"。
            duration: 目标时长（秒）；None 时对齐输入旋律（默认 10-30s）。
            seed: 随机种子（可复现）。
            output_path: 输出 WAV 路径；None 时自动命名在输入旁。

        Returns:
            输出 WAV 绝对路径字符串。

        Raises:
            AiDependencyError: P1 依赖未安装或显存不足（退出码 6）。
            InputFileError: 输入文件不存在/无法解析（退出码 3）。
            ParameterError: duration 越界（退出码 1）。
        """
        if not self.is_available():
            raise AiDependencyError(_INSTALL_GUIDE, code=6)

        # 延迟导入（P0 模块零 torch 约束）
        import numpy as np
        import soundfile as sf
        import torch
        try:
            from audiocraft.models import MusicGen
        except ModuleNotFoundError as exc:
            # audiocraft 顶层可导入但其深层依赖缺失（实测：Windows 缺 triton 时
            # audiocraft.models 触发 ModuleNotFoundError）——收口为退出码 6，
            # 全流程验证发现（1.5.6）。
            raise AiDependencyError(
                f"audiocraft 深层依赖缺失: {exc.name}\n"
                f"{_INSTALL_GUIDE}\n"
                "Windows 提示: pip install 'triton-windows<3.2'（需与 torch 版本匹配）",
                code=6,
            ) from exc

        # 输入与显存前置校验（显存不足给出友好提示，不 OOM 崩溃）
        mono, melody_sr = self._read_melody(source_wav)
        if self.device == "cuda":
            free_gb = self.check_vram()
            if free_gb is None:
                raise AiDependencyError(
                    "MusicGen 需要 CUDA GPU（--device cuda 但未检测到 CUDA）。\n"
                    "请使用 --device cpu 显式降级（较慢）或检查驱动。",
                    code=6,
                )
            need_gb = _VRAM_REQUIREMENTS_GB[self.model_size]
            if free_gb < need_gb:
                raise AiDependencyError(
                    f"显存不足：当前可用 {free_gb:.1f}GB < {self.model_size} 所需约 {need_gb:.1f}GB。\n"
                    "建议: 使用 --model-size small 降档，或 --device cpu 显式使用 CPU 推理（较慢）。",
                    code=6,
                )

        # seed 可复现
        if seed is not None:
            torch.manual_seed(seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(seed)

        input_seconds = mono.shape[0] / melody_sr if melody_sr else 0.0
        target_duration = self._resolve_duration(input_seconds, duration)

        # 加载模型（延迟导入；audiocraft 1.4 的 MusicGen 无 .to()，
        # fp16 需对内部 lm/compression_model 转换）
        model = MusicGen.get_pretrained(self.model_name, device=self.device)
        if self.device == "cuda":
            model.lm = model.lm.to(torch.float16)
            model.compression_model = model.compression_model.to(torch.float16)

        model.set_generation_params(duration=target_duration)

        # melody conditioning：输入旋律张量 [1, T]（单声道 float32）
        # musicgen-small 不支持 chroma -> 自动降级为纯文本 generate（记录提示）
        melody_tensor = torch.from_numpy(np.ascontiguousarray(mono)).unsqueeze(0)
        try:
            output = model.generate_with_chroma(
                [prompt], melody_tensor, melody_sr, progress=True
            )
        except RuntimeError as exc:
            if "doesn't support melody conditioning" not in str(exc):
                raise
            print(
                "[MusicGen] 提示: 当前模型不支持旋律条件（musicgen-small 降档），"
                "已降级为纯文本生成（不包含输入旋律对齐）"
            )
            output = model.generate([prompt], progress=True)
        # output: [B, C, T] -> 取第一个样本 -> [C, T]
        wav = output[0].detach().cpu().float()
        if wav.dim() == 3:
            wav = wav[0]
        if wav.dim() == 2:
            wav = wav.transpose(0, 1)  # [C, T] -> [T, C]

        sample_rate = int(getattr(model, "sample_rate", OUTPUT_SAMPLE_RATE))

        out = Path(output_path) if output_path else Path(source_wav).with_name(
            f"{Path(source_wav).stem}_musicgen.wav"
        )
        out = out.expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(out), wav.numpy(), sample_rate, subtype="PCM_16")
        return str(out)
