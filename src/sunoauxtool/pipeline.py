"""一键管线（P0 + P1/P2 增量）：generate → render → DSP → export（Suno 合规闭环）。

M-1：管线开头做环境探测（PathResolver.ensure_ready，真实引擎）；构造 Renderer 时
     传入配置路径（renderer 内部解析 module 相对路径并支持双音色库回退）。
P2-1：render 输出后、export 前插入 DspProcessor 阶段（归一化 -1dBFS + 淡入淡出）。
P2-5：产物经 OutputManager 规划（<root>/<project>/<YYYYMMDD>/{style}_{bpm}_{seed}_{seq}）
     并落盘 metadata.json。

零参数 demo：sunoauxtool pipeline
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from sunoauxtool import __version__
from sunoauxtool.config import Config
from sunoauxtool.dsp import DspOptions, DspProcessor
from sunoauxtool.env import PathResolver
from sunoauxtool.export.suno import ExportOptions, SunoExporter
from sunoauxtool.generators.base import GenerationRequest
from sunoauxtool.generators.procedural import ProceduralGenerator
from sunoauxtool.logging_setup import get_logger
from sunoauxtool.models.midi import MidiDocument
from sunoauxtool.output_manager import ArtifactMeta, OutputManager, RunMeta
from sunoauxtool.preview import PreviewGenerator
from sunoauxtool.render.fluidsynth import FluidSynthRenderer
from sunoauxtool.score_export import (
    ScoreExportOptions,
    export_score,
    score_from_sequence,
    score_svg_text,
)

logger = get_logger("pipeline")


@dataclass
class PipelineResult:
    """管线产物元数据。"""

    midi_path: str = ""
    wav_path: str = ""
    export_path: str = ""
    duration_s: float = 0.0
    sample_rate: int = 0
    bit_depth: Optional[int] = None
    chords: str = ""
    seed: Optional[int] = None
    bpm: int = 0
    bars: int = 0
    key: str = ""
    style: str = ""
    format: str = ""
    #: ``{谱面格式: 绝对路径}``；未启用 --score 时为空（#12）
    score_paths: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return field_dict(self)


def field_dict(obj) -> Dict[str, object]:
    """dataclass -> dict（供 CLI 打印）。"""
    return {f.name: getattr(obj, f.name) for f in obj.__dataclass_fields__.values()}


class Pipeline:
    """编排 generate → render → DSP → export，中间产物写入 output/.tmp 并自动清理。"""

    def __init__(
        self,
        config: Config,
        project: Optional[str] = None,
        output_dir: Optional[str | Path] = None,
        dry_run: bool = False,
        score: bool = False,
        score_format: str = "svg",
        score_theme: str = "light",
        score_key: Optional[str] = None,
    ) -> None:
        """初始化。

        Args:
            config: 生效配置。
            project: 项目名（P2-5，--project 覆盖）。
            output_dir: 输出根目录覆盖（--output-dir）。
            dry_run: True 时不写任何产物、不调用 subprocess，仅规划并打印（M-1f）。
            score: True 时在 MIDI 旁产出谱面，并把五线谱 SVG 内嵌进预览页（#12）。
            score_format: 谱面格式（逗号分隔，见 ``commands.score_export``）。
            score_theme: 谱面配色 light / dark。
            score_key: 记谱调式覆盖；None 时用生成请求的调式。
        """
        self.config = config
        self.project = project
        self.output_dir = output_dir
        self.dry_run = dry_run
        # 选项在构造期就校验：参数写错要立刻失败，别等到渲染完 60 秒后才报错
        self.score_options = (
            ScoreExportOptions(formats=score_format, theme=score_theme) if score else None
        )
        self.score_key = score_key

    def run(
        self,
        request: GenerationRequest,
        export_opts: Optional[ExportOptions] = None,
    ) -> PipelineResult:
        """执行完整管线。

        Args:
            request: 生成请求。
            export_opts: 导出选项；None 时使用配置默认值。

        Returns:
            PipelineResult（含最终产物路径与元数据）。

        Raises:
            SmartNoteGenError: 任一步骤失败（渲染/导出等按错误码抛出）。
        """
        opts = export_opts or ExportOptions(
            duration=self.config.export.duration,
            format=self.config.export.format,
            sample_rate=self.config.export.sample_rate,
            bit_depth=self.config.export.bit_depth,
            fade_ms=self.config.export.fade_ms,
        )

        output_manager = OutputManager(self.config, project=self.project, output_dir=self.output_dir)
        output_manager.base_dir()  # 确保输出根目录存在（1.4.7 L3：tmp 中转目录已移除）

        # 1. 环境探测（真实引擎；仅默认 module 环境缺失抛 ModuleError(7)，其余日志提示）
        if not self.dry_run:
            PathResolver(self.config).ensure_ready()

        # 2. 生成 NoteSequence
        generator = ProceduralGenerator(seed=request.seed)
        seq = generator.generate(request)

        # 输出规划（防覆盖序号；产物路径稳定，metadata 可追溯）
        seq_no = output_manager.next_seq(request.style, request.bpm, request.seed, "mid")
        export_ext = opts.format
        midi_plan = output_manager.plan_path(
            style=request.style, bpm=request.bpm, seed=request.seed, ext="mid", seq=seq_no,
            mkdir=False,
        )
        wav_plan = output_manager.plan_path(
            style=request.style, bpm=request.bpm, seed=request.seed, ext="wav", seq=seq_no,
            mkdir=False,
        )
        export_plan = output_manager.plan_path(
            style=request.style, bpm=request.bpm, seed=request.seed,
            ext=export_ext, seq=seq_no, suffix=f"_suno{opts.duration}s", mkdir=False,
        )

        if self.dry_run:
            logger.info(
                "[DRY-RUN] fluidsynth 将执行: fluidsynth -ni -F %s -R %d -O s16 -g 0.60 "
                "<soundfont: %s> <midi: %s>",
                wav_plan, self.config.export.sample_rate, self.config.paths.soundfont, midi_plan,
            )
            logger.info("[DRY-RUN] export suno: %s -> %s", wav_plan, export_plan)
            logger.info("[DRY-RUN] 不写任何产物文件")
            return PipelineResult(
                midi_path=str(midi_plan), wav_path=str(wav_plan),
                export_path=str(export_plan),
                duration_s=float(opts.duration), sample_rate=opts.sample_rate,
                bit_depth=opts.bit_depth, chords=request.chords, seed=request.seed,
                bpm=request.bpm, bars=request.bars, key=request.key,
                style=request.style, format=opts.format,
            )

        # 3. 落盘 .mid（稳定路径；1.4.7 L3：不再创建未使用的 tmp 中转目录）
        midi_path = Path(MidiDocument.from_sequence(seq).write(midi_plan))

        # 3b. 谱面产物（#12，--score）：与 MIDI 同目录同主干；失败只告警不阻断音频
        score_artifacts: list[ArtifactMeta] = []
        score_svg = ""
        score_written: Dict[str, str] = {}
        if self.score_options is not None:
            score_artifacts, score_svg, score_written = self._export_score(
                seq, midi_path, seq_no, request.seed
            )

        # 4. 渲染 WAV（真实引擎；renderer 内部解析 module 路径 + 双音色库回退）
        renderer = FluidSynthRenderer(
            fluidsynth_path=self.config.paths.fluidsynth,
            soundfont_backup=self.config.paths.soundfont_backup,
        )
        wav_path = Path(
            renderer.render(str(midi_path), self.config.paths.soundfont, str(wav_plan))
        )

        # 5. DSP 阶段（P2-1）：render 输出后、export 前（就地处理稳定 wav）
        self._apply_dsp(wav_path)

        # 6. Suno 合规导出（P2-5 命名：{style}_{bpm}_{seed}_{seq}_suno{ds}s）
        exporter = SunoExporter()
        final_path = exporter.export(str(wav_path), opts, output_path=export_plan)

        # 7. 元数据（引用稳定产物路径）
        meta = SunoExporter.describe(final_path)
        result = PipelineResult(
            midi_path=str(midi_path),
            wav_path=str(wav_path),
            export_path=final_path,
            duration_s=meta["duration_s"],
            sample_rate=meta["sample_rate"],
            bit_depth=meta["bit_depth"],
            chords=request.chords,
            seed=request.seed,
            bpm=request.bpm,
            bars=request.bars,
            key=request.key,
            style=request.style,
            format=opts.format,
            score_paths=dict(score_written),
        )
        if self.config.output.metadata:
            output_manager.write_metadata(
                RunMeta(
                    command=f"sunoauxtool pipeline --style {request.style} --seed {request.seed}",
                    seed=request.seed,
                    started_at=datetime.now().isoformat(timespec="seconds"),
                    duration_s=round(meta["duration_s"], 2),
                    version=__version__,
                    config_path=str(self.config.config_path) if self.config.config_path else None,
                ),
                [
                    ArtifactMeta(
                        path=str(midi_path), kind="midi",
                        params={"chords": request.chords, "bpm": request.bpm,
                                "bars": request.bars, "style": request.style},
                        seed=request.seed, seq=seq_no,
                        duration_s=seq.duration_seconds(),
                    ),
                    ArtifactMeta(
                        path=str(wav_path), kind="wav",
                        params={"bpm": request.bpm, "style": request.style},
                        seed=request.seed, seq=seq_no,
                        duration_s=round(meta["duration_s"], 2),
                        sample_rate=meta["sample_rate"],
                    ),
                    ArtifactMeta(
                        path=final_path, kind="suno",
                        params={"duration": opts.duration, "format": opts.format,
                                "sample_rate": opts.sample_rate, "bit_depth": opts.bit_depth},
                        seed=request.seed, seq=seq_no,
                        duration_s=meta["duration_s"],
                        sample_rate=meta["sample_rate"],
                    ),
                    *score_artifacts,
                ],
            )

        # 8. 自动生成 HTML 预览页（P3-A1）
        if self._preview_enabled():
            try:
                preview = PreviewGenerator()
                preview_path = preview.generate_for(
                    final_path,
                    {
                        "时长": f"{meta['duration_s']}s",
                        "采样率": f"{meta['sample_rate']}Hz",
                        "位深": f"{meta['bit_depth']}bit",
                        "风格": request.style,
                        "BPM": request.bpm,
                        "seed": request.seed,
                        "和弦": request.chords,
                    },
                    output_manager.root(),
                    label=Path(final_path).name,
                    score_svg=score_svg,
                )
                logger.info("预览页: %s", preview_path)
            except Exception as exc:  # 预览失败不阻断管线
                logger.warning("预览页生成失败（不影响产物）: %s", exc)

        return result

    # -- 谱面（#12）--------------------------------------------------------

    def _export_score(self, seq, midi_path: Path, seq_no: int, seed: Optional[int]):
        """在 MIDI 旁产出谱面，并返回（元数据条目, 供预览内嵌的 SVG 文本, 路径表）。

        从 ``NoteSequence`` 直接构建 Score（不回读刚写的 .mid）：省一次磁盘往返，
        且与 ``score`` 子命令走同一套 ``Score`` 装配逻辑，结论一致。

        **失败策略：告警不阻断**。管线的主产物是音频；谱面是附加物，任何失败
        （含未装 Pillow）只记 warning，音频与导出照常完成。用户显式要谱面时可在
        结束后从日志看到原因；要「失败即退出」请改用 ``sunoauxtool score``。

        Returns:
            ``([ArtifactMeta, ...], svg_text, {format: path})``；失败时返回 ``([], "", {})``。
        """
        assert self.score_options is not None  # 调用点已判空
        try:
            score = score_from_sequence(seq, title=Path(midi_path).stem, key=self.score_key)
            written = export_score(score, midi_path.parent, midi_path.stem, self.score_options)
            svg = score_svg_text(score, self.score_options)
        except Exception as exc:  # noqa: BLE001 - 谱面失败不应影响音频交付
            logger.warning("谱面生成失败（不影响音频产物）: %s", exc)
            return [], "", {}

        labels = {
            "svg": "score_svg", "png": "score_png", "jianpu": "score_jianpu",
            "jianpu-png": "score_jianpu_png", "jianpu-txt": "score_jianpu_txt",
            "musicxml": "score_musicxml",
        }
        artifacts = [
            ArtifactMeta(
                path=path, kind=labels.get(name, f"score_{name}"),
                params={"format": name, "key": score.key, "bars": score.bars},
                seed=seed, seq=seq_no, duration_s=score.duration_seconds(),
            )
            for name, path in written.items()
        ]
        logger.info("谱面: %s", "、".join(Path(p).name for p in written.values()))
        return artifacts, svg, dict(written)

    # -- DSP（P2-1）--------------------------------------------------------

    def _dsp_options(self) -> DspOptions:
        d = self.config.dsp
        return DspOptions(
            normalize_dbfs=d.normalize_dbfs,
            fade_in_ms=d.fade_in_ms,
            fade_out_ms=d.fade_out_ms,
            eq=d.eq,
            eq_low_cut_hz=d.eq_low_cut_hz,
            compressor=d.compressor,
            compressor_ratio=d.compressor_ratio,
            compressor_threshold_db=d.compressor_threshold_db,
            reverb=d.reverb,
        )

    def _apply_dsp(self, wav_path: Path) -> None:
        """在渲染后应用 DSP（就地处理）。"""
        opts = self._dsp_options()
        processor = DspProcessor()
        processor.validate(opts)
        processor.process(str(wav_path), opts, str(wav_path))

    def _preview_enabled(self) -> bool:
        """预览页是否开启（config 或 dry-run 时关闭）。"""
        if self.dry_run:
            return False
        # 兼容：config 无 preview 节时（旧配置）默认开启
        preview = getattr(self.config, "preview", None)
        if preview is None:
            return True
        return bool(getattr(preview, "enabled", True))

