"""批量生成完整实现（P1-3）。

- 四维度随机化：和弦进行 / 节奏型 / 风格（乐器）/ 旋律变奏（维度可分别开关）。
- 可复现：全局 --seed S → 第 i 项 seed = S*1000+i（确定性派生）；无 seed 时用系统熵并记录。
- 失败隔离：单次重试；失败项记入 results.failed，不中断整体；退出码 0/8/9。
- 默认串行；--parallel 显式开启（ThreadPoolExecutor，默认 2 并发）。
- 链式 --render/--export：逐项真实 render（PathResolver 前置）+ Suno 导出。
- 产物经 OutputManager 落盘（P2-5 兼容），批次清单写入 metadata.json。
"""

from __future__ import annotations

import csv
import json
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sunoauxtool import __version__
from sunoauxtool.config import Config
from sunoauxtool.dsp import DspOptions, DspProcessor
from sunoauxtool.env import PathResolver
from sunoauxtool.exceptions import ParameterError, SmartNoteGenError
from sunoauxtool.export.suno import ExportOptions, SunoExporter
from sunoauxtool.generators.base import GenerationRequest, SeedContext
from sunoauxtool.generators.procedural import ProceduralGenerator
from sunoauxtool.logging_setup import get_logger
from sunoauxtool.models.midi import MidiDocument
from sunoauxtool.output_manager import ArtifactMeta, OutputManager, RunMeta
from sunoauxtool.render.fluidsynth import FluidSynthRenderer
from sunoauxtool.styles import StyleRegistry

logger = get_logger("batch")

#: 默认和弦池（--chords-choices 未提供时使用）
DEFAULT_CHORD_POOL: List[str] = [
    "C-G-Am-F",
    "Am-F-C-G",
    "C-Am-F-G",
    "G-C-D-Em",
    "F-C-G-Am",
    "Dm-Bb-F-C",
]

#: 生成阶段全局锁（并行模式下保护全局 RNG 的确定性）
_GEN_LOCK = threading.Lock()


@dataclass
class BatchOptions:
    """批量生成选项（P1-3）。"""

    count: int = 3
    seed: Optional[int] = None
    chords_choices: Optional[List[str]] = None  # 和弦池（随机化维度 1）
    style: Optional[str] = None                # 风格基准（P2-4 联动；None=风格池采样）
    variations: bool = False                   # 总开关：风格/乐器维度 + bpm 区间随机
    rhythm_variants: bool = False              # 节奏型变体（维度 2）
    melody_variants: bool = False              # 旋律变奏（维度 4）
    render: bool = False                       # 链式 render（真实引擎）
    export: bool = False                       # 链式 export suno（需 --render）
    parallel: bool = False                     # 默认串行；True 显式并行
    parallel_workers: int = 2
    project: Optional[str] = None              # P2-5 项目名
    output_dir: Optional[str] = None           # P2-5 输出根覆盖
    dry_run: bool = False
    report: Optional[str] = None               # F4：结构化报告路径（.json/.csv 按扩展名）
    # 生成参数
    bpm: Optional[int] = None
    bars: Optional[int] = None
    key: Optional[str] = None
    with_drums: bool = False
    voice_leading: bool = False
    counterpoint: bool = False
    inversion: bool = False
    rhythm_pattern: Optional[str] = None       # 固定节奏型（覆盖采样）


@dataclass
class BatchItemResult:
    """单项结果。"""

    index: int
    seed: int
    status: str  # "ok" | "failed"
    midi_path: Optional[str] = None
    wav_path: Optional[str] = None
    export_path: Optional[str] = None
    error: Optional[str] = None
    duration_s: float = 0.0
    params: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[ArtifactMeta] = field(default_factory=list)


@dataclass
class BatchResult:
    """批量运行结果。"""

    options: BatchOptions
    items: List[BatchItemResult]
    actual_seed: Optional[int]

    @property
    def ok_count(self) -> int:
        return sum(1 for i in self.items if i.status == "ok")

    @property
    def failed_count(self) -> int:
        return len(self.items) - self.ok_count


#: CSV 报告固定列（F4）；params 内的四个采样维度平铺进列
_CSV_COLUMNS = [
    "index", "seed", "status", "duration_s",
    "style", "chords", "rhythm_pattern", "bpm",
    "midi_path", "wav_path", "export_path", "error",
]


def write_report(result: BatchResult, path: str | Path) -> Path:
    """结构化批次报告导出（F4）：按扩展名 ``.json`` / ``.csv`` 写入。

    - JSON：完整结构（版本 / 时间 / 命令 / 汇总 / options / 逐项明细含 params）；
    - CSV：``_CSV_COLUMNS`` 平铺列（Excel 友好，utf-8-sig 带 BOM）。

    部分失败也会完整落盘（失败项含 error 字段）——报告的价值正在于此。
    扩展名非法 → ParameterError(1)；父目录不存在自动创建。
    """
    p = Path(path)
    suffix = p.suffix.lower()
    opts = result.options
    p.parent.mkdir(parents=True, exist_ok=True)

    def _item_row(item: BatchItemResult) -> Dict[str, Any]:
        params = item.params or {}
        return {
            "index": item.index,
            "seed": item.seed,
            "status": item.status,
            "duration_s": round(item.duration_s, 3),
            "style": params.get("style"),
            "chords": params.get("chords"),
            "rhythm_pattern": params.get("rhythm_pattern"),
            "bpm": params.get("bpm"),
            "midi_path": item.midi_path,
            "wav_path": item.wav_path,
            "export_path": item.export_path,
            "error": item.error,
        }

    if suffix == ".json":
        payload = {
            "version": __version__,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "command": BatchRunner._command(opts, result.actual_seed or 0),
            "actual_seed": result.actual_seed,
            "summary": {
                "total": len(result.items),
                "ok": result.ok_count,
                "failed": result.failed_count,
            },
            "options": {
                "count": opts.count,
                "seed": opts.seed,
                "style": opts.style,
                "variations": opts.variations,
                "render": opts.render,
                "export": opts.export,
                "parallel": opts.parallel,
                "dry_run": opts.dry_run,
                "bpm": opts.bpm,
                "bars": opts.bars,
                "key": opts.key,
                "with_drums": opts.with_drums,
            },
            "items": [_item_row(i) for i in result.items],
        }
        p.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    elif suffix == ".csv":
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS)
            writer.writeheader()
            for item in result.items:
                row = _item_row(item)
                writer.writerow({k: ("" if row[k] is None else row[k]) for k in _CSV_COLUMNS})
    else:
        raise ParameterError(
            f"批次报告格式仅支持 .json / .csv: {path}", code=1
        )
    return p


class BatchRunner:
    """批量生成器（P1-3 完整实现）。"""

    def __init__(self, options: BatchOptions, config: Optional[Config] = None) -> None:
        self.options = options
        self.config = config or Config.load()

    # -- 主流程 ------------------------------------------------------------

    def run(self) -> BatchResult:
        """执行批量生成，返回结果（不抛退出码异常；由 CLI 决定 0/8/9）。"""
        opts = self.options
        if opts.count < 1:
            raise ParameterError(f"count 必须 >= 1: {opts.count}", code=1)
        if opts.parallel_workers < 1:
            raise ParameterError(f"parallel_workers 必须 >= 1: {opts.parallel_workers}", code=1)
        if opts.export and not opts.render:
            raise ParameterError("--export 需要 --render（Suno 导出需先渲染 WAV）", code=1)

        global_seed = opts.seed
        if global_seed is None:
            # 限制在 2**22 内，保证派生种子 seed*1000+i 不超出 numpy 种子范围（2**32）
            global_seed = random.SystemRandom().randint(0, 2**22 - 1)
            logger.info("batch 未指定 --seed，使用系统熵生成全局种子: %d", global_seed)

        style_registry = self._style_registry()
        output_manager = OutputManager(self.config, project=opts.project, output_dir=opts.output_dir)

        # 预计算每项参数（串行、确定性）：seed 派生 seed*1000+i
        item_params: List[Dict[str, Any]] = []
        for i in range(opts.count):
            item_seed = global_seed * 1000 + i
            with SeedContext(item_seed):
                params = self._sample_item_params(opts, style_registry, i)
            params["seed"] = item_seed
            params["seq"] = output_manager.next_seq(
                params["style"], params["bpm"], item_seed, "mid"
            )
            item_params.append(params)

        # 执行（默认串行；--parallel 并行；dry-run 走轻量分支）
        if opts.parallel and not opts.dry_run:
            results: List[BatchItemResult] = []
            with ThreadPoolExecutor(max_workers=opts.parallel_workers) as ex:
                futures = [
                    ex.submit(self._run_item, params, i, opts, output_manager)
                    for i, params in enumerate(item_params)
                ]
                for f in futures:
                    results.append(f.result())
        else:
            results = [
                self._run_item(params, i, opts, output_manager)
                for i, params in enumerate(item_params)
            ]

        # 汇总批次清单元数据（P2-5 兼容）
        all_artifacts = [a for r in results for a in r.artifacts]
        if not opts.dry_run and self.config.output.metadata and all_artifacts:
            output_manager.write_metadata(
                RunMeta(
                    command=self._command(opts, global_seed),
                    seed=global_seed,
                    started_at=datetime.now().isoformat(timespec="seconds"),
                    duration_s=0.0,
                    version=__version__,
                    config_path=str(self.config.config_path) if self.config.config_path else None,
                ),
                all_artifacts,
            )
        return BatchResult(options=opts, items=results, actual_seed=global_seed)

    # -- 参数采样 ----------------------------------------------------------

    def _style_registry(self) -> StyleRegistry:
        dirs = []
        styles_dir = Path(self.config.styles.dir).expanduser()
        if not styles_dir.is_absolute():
            styles_dir = Path.cwd() / styles_dir
        dirs.append(styles_dir)
        dirs.extend(Path(x).expanduser() for x in self.config.styles.search_paths)
        return StyleRegistry(extra_dirs=dirs)

    def _sample_item_params(self, opts: BatchOptions, style_registry: StyleRegistry, index: int) -> Dict[str, Any]:
        """采样单项目参数（在 SeedContext(item_seed) 内调用，确定性）。"""
        if opts.style is not None:
            style_name = opts.style
            preset = style_registry.get(style_name)
        else:
            style_name = random.choice(list(style_registry.BUILTIN_NAMES))
            preset = style_registry.get(style_name)

        # 和弦进行（维度 1）
        if opts.chords_choices:
            pool = list(opts.chords_choices)
        elif preset.chord_preference:
            pool = list(preset.chord_preference)
        else:
            pool = list(DEFAULT_CHORD_POOL)
        chords = random.choice(pool)

        # 节奏型（维度 2）
        if opts.rhythm_pattern:
            rhythm_name = opts.rhythm_pattern
        elif opts.rhythm_variants or opts.variations:
            rhythm_name = random.choice(self._rhythm_names())
        else:
            rhythm_name = preset.rhythm_pattern or "pop"

        # BPM：--bpm 优先；否则预设区间（variations 时区间内随机）
        bpm = opts.bpm
        if bpm is None:
            lo, hi = preset.bpm_range
            if opts.variations:
                bpm = random.randint(lo, hi)
            else:
                bpm = (lo + hi) // 2

        return {
            "chords": chords,
            "style": style_name,
            "rhythm_pattern": rhythm_name,
            "bpm": bpm,
            "style_instruments": dict(preset.instruments),
        }

    def _rhythm_names(self) -> List[str]:
        from sunoauxtool.music_theory import RhythmPatternRegistry

        return RhythmPatternRegistry().names()

    # -- 单项执行 ----------------------------------------------------------

    def _run_item(
        self,
        params: Dict[str, Any],
        index: int,
        opts: BatchOptions,
        output_manager: OutputManager,
    ) -> BatchItemResult:
        """单项执行：失败重试一次，仍失败记 failed（不中断整体）。"""
        try:
            return self._run_item_once(params, index, opts, output_manager)
        except SmartNoteGenError as exc:
            logger.warning("batch 第 %d 项失败（将重试一次）: %s", index + 1, exc)
            try:
                return self._run_item_once(params, index, opts, output_manager)
            except SmartNoteGenError as exc2:
                return BatchItemResult(
                    index=index, seed=params["seed"], status="failed",
                    error=str(exc2), params=params,
                )

    def _run_item_once(
        self,
        params: Dict[str, Any],
        index: int,
        opts: BatchOptions,
        output_manager: OutputManager,
    ) -> BatchItemResult:
        item_seed = int(params["seed"])
        style = str(params["style"])
        bpm = int(params["bpm"])
        seq_no = int(params["seq"])

        request = GenerationRequest(
            chords=str(params["chords"]),
            bpm=bpm,
            key=opts.key or "C major",
            time_signature="4/4",
            bars=opts.bars or 8,
            style=style,
            seed=item_seed,
            tracks=["chords", "melody", "bass"],
            with_drums=opts.with_drums,
            rhythm_pattern=str(params["rhythm_pattern"]),
            style_instruments=params.get("style_instruments"),
            enable_voice_leading=opts.voice_leading,
            enable_counterpoint=opts.counterpoint,
            enable_inversion=opts.inversion,
            variations=max(1, 2) if opts.melody_variants else 1,
        )

        # 生成（全局锁保护 RNG 确定性）
        with _GEN_LOCK:
            with SeedContext(item_seed):
                seq = ProceduralGenerator(seed=item_seed).generate(request)

        if opts.dry_run:
            midi_plan = output_manager.plan_path(
                style=style, bpm=bpm, seed=item_seed, ext="mid", seq=seq_no, mkdir=False
            )
            logger.info(
                "[DRY-RUN] batch 第 %d 项（seed=%d, style=%s, chords=%s, rhythm=%s）将生成 MIDI: %s",
                index + 1, item_seed, style, params["chords"], params["rhythm_pattern"], midi_plan,
            )
            return BatchItemResult(
                index=index, seed=item_seed, status="ok",
                midi_path=str(midi_plan), duration_s=seq.duration_seconds(), params=params,
            )

        midi_path = Path(
            MidiDocument.from_sequence(seq).write(
                output_manager.plan_path(style=style, bpm=bpm, seed=item_seed, ext="mid", seq=seq_no)
            )
        )
        artifacts: List[ArtifactMeta] = [
            ArtifactMeta(
                path=str(midi_path), kind="midi",
                params={"chords": params["chords"], "bpm": bpm, "bars": request.bars, "style": style},
                seed=item_seed, seq=seq_no, duration_s=seq.duration_seconds(),
            )
        ]
        wav_path: Optional[str] = None
        export_path: Optional[str] = None

        if opts.render:
            wav_path = self._render_item(midi_path, style, bpm, item_seed, seq_no, output_manager, artifacts)
            if opts.export:
                export_path = self._export_item(wav_path, style, bpm, item_seed, seq_no, output_manager, artifacts)

        return BatchItemResult(
            index=index, seed=item_seed, status="ok",
            midi_path=str(midi_path), wav_path=wav_path, export_path=export_path,
            duration_s=seq.duration_seconds(), params=params, artifacts=artifacts,
        )

    # -- 链式 render / export ---------------------------------------------

    def _render_item(
        self,
        midi_path: Path,
        style: str,
        bpm: int,
        seed: int,
        seq_no: int,
        output_manager: OutputManager,
        artifacts: List[ArtifactMeta],
    ) -> str:
        """逐项真实 render（PathResolver 前置探测；失败项由 _run_item 重试/隔离）。"""
        resolver = PathResolver(self.config)
        resolver.ensure_ready()
        sf = resolver.resolve_soundfont()
        fs = resolver.resolve_fluidsynth()
        renderer = FluidSynthRenderer(fluidsynth_path=str(fs))

        wav_path = output_manager.plan_path(style=style, bpm=bpm, seed=seed, ext="wav", seq=seq_no)
        wav_path = Path(renderer.render(str(midi_path), str(sf), str(wav_path)))

        # DSP 阶段（P2-1）：默认归一化 -1dBFS + 淡入淡出（就地处理）
        dsp = self._dsp_options()
        DspProcessor().process(str(wav_path), dsp, str(wav_path))
        artifacts.append(
            ArtifactMeta(
                path=str(wav_path), kind="wav",
                params={"bpm": bpm, "style": style},
                seed=seed, seq=seq_no, sample_rate=44100,
            )
        )
        return str(wav_path)

    def _export_item(
        self,
        wav_path: str,
        style: str,
        bpm: int,
        seed: int,
        seq_no: int,
        output_manager: OutputManager,
        artifacts: List[ArtifactMeta],
    ) -> str:
        """链式 export suno（合规校验 + P2-5 命名）。"""
        duration = self.config.export.duration
        opts = ExportOptions(
            duration=duration,
            format=self.config.export.format,
            sample_rate=self.config.export.sample_rate,
            bit_depth=self.config.export.bit_depth,
            fade_ms=self.config.export.fade_ms,
        )
        export_path = output_manager.plan_path(
            style=style, bpm=bpm, seed=seed, ext=opts.format, seq=seq_no,
            suffix=f"_suno{duration}s",
        )
        final = Path(SunoExporter().export(wav_path, opts, output_path=export_path))
        meta = SunoExporter.describe(final)
        artifacts.append(
            ArtifactMeta(
                path=str(final), kind="suno",
                params={"duration": duration, "format": opts.format,
                        "sample_rate": opts.sample_rate, "bit_depth": opts.bit_depth},
                seed=seed, seq=seq_no, duration_s=meta["duration_s"],
                sample_rate=meta["sample_rate"],
            )
        )
        return str(final)

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

    # -- 工具 --------------------------------------------------------------

    @staticmethod
    def _command(opts: BatchOptions, seed: int) -> str:
        parts = ["sunoauxtool batch", f"--count {opts.count}", f"--seed {seed}"]
        if opts.style:
            parts.append(f"--style {opts.style}")
        if opts.render:
            parts.append("--render")
        if opts.export:
            parts.append("--export")
        return " ".join(parts)
