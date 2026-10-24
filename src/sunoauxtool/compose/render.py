"""音频渲染编排（2.0 PR-E）：``Song`` → 分轨 WAV + 混音成品。

与 :mod:`sunoauxtool.render.backend` / :mod:`sunoauxtool.render.mixer` 的分工：

- **本模块**：决定"渲染什么、给谁渲染、产物放哪"（领域流程，可注入假后端做测试）；
- backend：怎么把单个 MIDI 变成音频；
- mixer：怎么把多条分轨叠成成品。

流程::

    Song ─按轨道切片→ 单轨 MIDI ─backend.render→ stems/<id>.wav
                                              └─mixer.mixdown→ <song>.wav

每轨单独渲染（而不是一次渲染全曲再分离）是唯一能保证"分轨 = 母带成分"的做法：
混音就是这些 stem 的线性组合，``mix - Σstems`` 恒为 0。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from sunoauxtool.logging_setup import get_logger
from sunoauxtool.midi.document import MidiDocumentV2
from sunoauxtool.midi.io import write_smf
from sunoauxtool.render.backend import RenderBackend, RenderRequest, capabilities_of
from sunoauxtool.render.exceptions import MixdownError
from sunoauxtool.render.mixer import (
    DEFAULT_TARGET_PEAK,
    MixResult,
    align_banks,
    mixdown,
    strips_from_song,
    stem_loudness,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from sunoauxtool.compose.song import Song

logger = get_logger("compose.render")

#: 支持输出的格式（由 soundfile 推断，无需 ffmpeg）
FORMATS = {"wav": ".wav", "flac": ".flac"}


@dataclass
class RenderOptions:
    """渲染参数（默认即可用；CLI 只覆盖少量）。"""

    sample_rate: int = 44100
    fmt: str = "wav"
    stems: bool = True
    normalize: bool = True
    target_peak: float = DEFAULT_TARGET_PEAK
    blocksize: int | None = None
    dry_run: bool = False
    align_loudness: bool = True
    max_align_db: float = 12.0


@dataclass
class RenderResult:
    """渲染产物清单。"""

    out_dir: Path
    mix: Path | None = None
    stems: dict[str, Path] = field(default_factory=dict)
    midi: dict[str, Path] = field(default_factory=dict)
    mix_info: MixResult | None = None
    dry_run: bool = False
    loudness: dict[str, float] = field(default_factory=dict)
    align_db: dict[str, float] = field(default_factory=dict)
    banks: dict[str, str] = field(default_factory=dict)

    @property
    def stem_count(self) -> int:
        return len(self.stems)


def slice_document(document: MidiDocumentV2, index: int) -> MidiDocumentV2:
    """从整曲文档里切出第 ``index`` 条轨的文档（时间表/标题/PPQ 原样保留）。

    按**下标**取轨而不是按名字：轨道名允许重复（两条 ``harmony`` 可同名），
    而下标与 ``song.tracks`` 一一对应。
    """
    if not 0 <= index < len(document.tracks):
        raise MixdownError(f"文档轨道下标越界: {index}（共 {len(document.tracks)} 条）", code=43)
    track = document.tracks[index]
    return MidiDocumentV2(
        tracks=[track],
        tempo_map=list(document.tempo_map),
        meter_map=list(document.meter_map),
        key_map=list(document.key_map),
        markers=list(document.markers),
        raw_events=list(document.raw_events),
        ppq=document.ppq,
        title=document.title,
        end_beat=document.end_beat,
    )


def render_song(
    song: "Song",
    *,
    out_dir: Path,
    backend: RenderBackend,
    soundfont: str,
    options: RenderOptions | None = None,
    bank_paths: dict[str, str] | None = None,
) -> RenderResult:
    """把一首 ``Song`` 渲染成分轨 + 混音。

    Args:
        song: 已编排好的曲子（内部会校验）。
        out_dir: 输出目录（不存在会创建）。
        backend: 渲染后端（真实 fluidsynth 或测试替身）。
        soundfont: 音色库路径（后端解析）。
        options: 渲染参数（``fmt`` 只作用于混音成品；分轨恒为 WAV——fluidsynth 只写 wav）。

    Returns:
        :class:`RenderResult`：``midi/<id>.mid``、``stems/<id>.wav``、
        ``<曲名>.<fmt>``（混音成品）。

    Raises:
        BackendUnavailableError: 后端不可用（42）。
        MixdownError: 分轨渲染/混音失败（43）。
    """
    options = options or RenderOptions()
    if options.fmt not in FORMATS:
        raise MixdownError(
            f"不支持的音频格式 {options.fmt!r}；可用: {', '.join(sorted(FORMATS))}", code=43
        )
    song.validate()
    out = Path(out_dir)
    midi_dir = out / "midi"
    stem_dir = out / "stems"
    document = song.to_midi()
    result = RenderResult(out_dir=out, dry_run=options.dry_run)
    base = _safe_name(song.title or "song")

    if not options.dry_run:
        midi_dir.mkdir(parents=True, exist_ok=True)
        if options.stems:
            stem_dir.mkdir(parents=True, exist_ok=True)

    for index, track in enumerate(song.tracks):
        midi_path = midi_dir / f"{_safe_name(track.id)}.mid"
        result.midi[track.id] = midi_path
        stem_path = stem_dir / f"{_safe_name(track.id)}.wav"
        if options.dry_run:
            if options.stems:
                result.stems[track.id] = stem_path
            continue

        write_smf(slice_document(document, index), midi_path)
        if not options.stems:
            continue
        track_bank = (bank_paths or {}).get(track.id, soundfont)
        result.banks[track.id] = str(track_bank)
        rendered = backend.render(RenderRequest(
            midi_path=midi_path,
            out_path=stem_path,
            soundfont=track_bank,
            sample_rate=options.sample_rate,
            meta={"track": track.id, "role": track.role},
        ))
        result.stems[track.id] = Path(rendered)

    if options.dry_run:
        result.mix = out / f"{base}{FORMATS[options.fmt]}"
        return result

    if not result.stems:
        logger.info("已跳过混音（--no-stems）")
        return result

    strips = strips_from_song(song, result.stems)
    if options.align_loudness and len(set(result.banks.values())) > 1:
        for name, path in result.stems.items():
            lufs = stem_loudness(Path(path), sample_rate=options.sample_rate)
            if lufs is not None:
                result.loudness[name] = lufs
        result.align_db = align_banks(
            result.loudness,
            result.banks,
            reference_bank=str(soundfont),
            max_db=options.max_align_db,
        )
        for strip in strips:
            strip.gain_db += result.align_db.get(strip.name, 0.0)
    mix_path = out / f"{base}{FORMATS[options.fmt]}"
    result.mix_info = mixdown(
        strips,
        mix_path,
        sample_rate=options.sample_rate,
        blocksize=options.blocksize,
        normalize=options.normalize,
        target_peak=options.target_peak,
        subtype="PCM_24" if options.fmt == "wav" else "PCM_16",
    )
    result.mix = result.mix_info.path
    return result


def _safe_name(text: str) -> str:
    """文件名安全化（保留中文，替换路径分隔符与空白）。"""
    keep = []
    for char in str(text).strip():
        keep.append("_" if char in '\\/:*?"<>| \t' else char)
    return "".join(keep).strip("._") or "track"


#: 清单里表示"用配置默认音源"的 bank 取值
DEFAULT_BANK_IDS = ("", "gm", "default", "auto")

def resolve_banks(
    spec,
    registry=None,
    *,
    default_soundfont: str,
    override: str | None = None,
) -> dict[str, str]:
    """按 ``[[track]] bank`` 解析每轨音色库路径（2.1 每轨独立选库）。

    - ``bank`` 为 ``""`` / ``"gm"`` / ``"default"`` / ``"auto"``（或不写）→ 用配置的默认音源；
    - 其他值按音源库 id 到注册表里查（未注册/校验失败 → 错误码 41）；
    - ``override`` 非空时全曲统一用它（CLI 的 ``--bank``）。

    Args:
        spec: ``compose.SongSpec``。
        registry: ``soundbank.SoundBankRegistry``；None 时现场扫描。
        default_soundfont: 默认音色库路径。
        override: 全局覆盖（音源库 id、路径均可，路径直接透传）。

    Returns:
        ``{轨道 id: 音色库路径}``。
    """
    if override:
        return {track.id: str(override) for track in spec.tracks}

    needed = {
        (track.bank or "").strip() for track in spec.tracks
    } - set(DEFAULT_BANK_IDS)
    resolved: dict[str, str] = {}
    if needed:
        if registry is None:
            from sunoauxtool.soundbank import build_registry

            registry = build_registry()
        for bank_id in sorted(needed):
            bank = registry.get(bank_id)          # 未注册 → SoundBankError(41)
            check = bank.verify()
            if not check.ok:
                from sunoauxtool.soundbank import SoundBankError

                raise SoundBankError(
                    f"音源 {bank_id!r} 校验失败: {check.message}", code=41
                )
            resolved[bank_id] = str(bank.path)

    result: dict[str, str] = {}
    for track in spec.tracks:
        bank_id = (track.bank or "").strip()
        result[track.id] = resolved.get(bank_id, str(default_soundfont))
    return result


def describe_result(result: RenderResult) -> list[str]:
    """人类可读摘要（CLI 输出）。"""
    lines = []
    if result.mix_info is not None:
        info = result.mix_info
        lines.append(
            f"混音 {info.path.name}：{info.frames / info.sample_rate:.1f}s｜"
            f"峰值 {info.peak:.3f}｜{len(info.stems)} 轨"
        )
    elif result.mix is not None:
        lines.append(f"混音 {result.mix.name}（dry-run 未生成）")
    if result.stems:
        names = "、".join(sorted(result.stems))
        lines.append(f"分轨 {len(result.stems)} 条：{names}")
    if result.align_db and any(value for value in result.align_db.values()):
        worst = max(result.align_db.items(), key=lambda item: abs(item[1]))
        lines.append(
            f"跨库响度对齐（{len(result.align_db)} 轨）：实测 "
            f"{min(result.loudness.values()):.1f}..{max(result.loudness.values()):.1f} LUFS，"
            f"最大修正 {worst[1]:+.1f} dB（{worst[0]}）"
        )
    if result.banks:
        distinct = sorted(set(result.banks.values()))
        lines.append(f"音源库 {len(distinct)} 套：{'、'.join(Path(p).name for p in distinct)}")
    lines.append(f"输出目录 {result.out_dir}")
    return lines


def backend_summary(backend: object) -> str:
    """后端能力一行摘要（CLI/dry-run 用）。"""
    return capabilities_of(backend).describe()
