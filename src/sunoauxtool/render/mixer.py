"""分轨混音（2.0 PR-E）：每轨增益/声像/静音/独奏 → 立体声成品。

设计要点：

- **流式**：按 ``blocksize``（默认 5 秒）分块读取与累加，90 分钟长度的工程也不会爆内存；
- **分轨可复现**：``mix - Σstems`` 恒为 0（混音就是 stem 的线性组合），空测 ≤ −60 dBFS；
- **不做母带**：只做通道条（增益/声像）与可选的峰值归一（N6：不承诺发行级母带）。

声像用**立体声 balance 定律**：中置原样通过（1.0/1.0），向一侧推时衰减另一侧
（线性到 0）。不采用等功率定律是有意的——stem 本身已是立体声，
再乘 0.707 会让中置轨整体 −3 dB，混音电平会无故变小。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import soundfile as sf

from sunoauxtool.logging_setup import get_logger
from sunoauxtool.render.exceptions import MixdownError

logger = get_logger("render.mixer")

#: 默认分块长度（秒）
DEFAULT_BLOCKSIZE_SECONDS = 5.0

#: 混音输出默认峰值（留 0.5 dB 余量）
DEFAULT_TARGET_PEAK = 0.944


@dataclass
class StemStrip:
    """一条分轨的通道条参数（由 ``compose.MixStrip`` 映射而来）。"""

    name: str
    path: Path
    gain_db: float = 0.0
    pan: float = 0.0
    mute: bool = False
    solo: bool = False
    reverb_send: float = 0.0
    chorus_send: float = 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": str(self.path),
            "gain_db": self.gain_db,
            "pan": self.pan,
            "mute": self.mute,
            "solo": self.solo,
            "reverb_send": self.reverb_send,
            "chorus_send": self.chorus_send,
        }


@dataclass
class MixResult:
    """混音结果摘要（CLI 输出与测试断言用）。"""

    path: Path
    sample_rate: int
    frames: int
    peak: float
    gain: float
    stems: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.frames / self.sample_rate if self.sample_rate else 0.0


def db_to_gain(db: float) -> float:
    """分贝 → 线性增益。"""
    return float(10.0 ** (float(db) / 20.0))


def pan_gains(pan: float) -> tuple[float, float]:
    """声像 → (左, 右) 增益（立体声 balance：中置 1/1，pan 夹在 -1..1）。"""
    value = max(-1.0, min(1.0, float(pan)))
    if value < 0:
        return 1.0, 1.0 + value
    return 1.0 - value, 1.0


def _active(strips: Sequence[StemStrip]) -> list[StemStrip]:
    """按 solo/mute 规则筛选参与混音的轨道。"""
    soloed = [strip for strip in strips if strip.solo]
    pool = soloed if soloed else strips
    return [strip for strip in pool if not strip.mute]


def _open(path: Path) -> sf.SoundFile:
    try:
        handle = sf.SoundFile(str(path))
    except (RuntimeError, OSError) as exc:
        raise MixdownError(f"无法打开分轨 {path}: {exc}", code=43) from exc
    return handle


def _stack(block: np.ndarray) -> np.ndarray:
    """统一成 (frames, 2)。"""
    if block.ndim == 1:
        return np.column_stack([block, block])
    if block.shape[1] == 1:
        return np.repeat(block, 2, axis=1)
    return block[:, :2]


def _apply_strip(block: np.ndarray, strip: StemStrip) -> np.ndarray:
    """对一块音频施加通道条（增益 + 等功率声像）。"""
    left, right = pan_gains(strip.pan)
    gain = db_to_gain(strip.gain_db)
    return block * np.array([gain * left, gain * right], dtype=np.float32)[None, :]


def peak_of(path: Path, *, blocksize: int) -> float:
    """扫描文件峰值（不载入整段）。"""
    peak = 0.0
    with _open(path) as handle:
        for block in handle.blocks(blocksize=blocksize, dtype="float32", always_2d=True):
            if block.size:
                peak = max(peak, float(np.max(np.abs(block))))
    return peak


def mixdown(
    strips: Sequence[StemStrip],
    out_path: Path,
    *,
    sample_rate: int = 44100,
    blocksize: int | None = None,
    normalize: bool = True,
    target_peak: float = DEFAULT_TARGET_PEAK,
    subtype: str = "PCM_16",
) -> MixResult:
    """把多条分轨混成一个立体声文件。

    Args:
        strips: 分轨通道条列表（顺序无关；``solo`` 存在时只混 solo 轨）。
        out_path: 输出路径（``.wav`` / ``.flac``，由 soundfile 推断）。
        sample_rate: 输出采样率（需与 stems 一致，否则抛 43）。
        blocksize: 分块帧数，``None`` → 5 秒。
        normalize: 是否把峰值归一（False 时原样输出，可能削顶）。
        target_peak: 归一目标峰值（默认 0.944 ≈ −0.5 dBFS）。
        subtype: soundfile 子类型（``PCM_16`` / ``FLOAT`` / ``PCM_24``）。

    Returns:
        :class:`MixResult`（含峰值、帧数、增益）。

    Raises:
        MixdownError: 分轨无法打开、采样率不一致、长度不一致或输出不可写（43）。
    """
    active = _active(strips)
    target = Path(out_path)
    size = int(blocksize) if blocksize else int(DEFAULT_BLOCKSIZE_SECONDS * sample_rate)
    size = max(1, size)

    if not active:
        # 没有任何可用分轨：写等长静音（长度取最长分轨）
        frames = 0
        for strip in strips:
            with _open(strip.path) as handle:
                frames = max(frames, len(handle))
        target.parent.mkdir(parents=True, exist_ok=True)
        with sf.SoundFile(str(target), "w", samplerate=sample_rate, channels=2,
                          subtype=subtype) as out:
            remaining = frames
            while remaining > 0:
                chunk = min(size, remaining)
                out.write(np.zeros((chunk, 2), dtype=np.float32))
                remaining -= chunk
        logger.info("无参与混音的分轨，输出静音: %s", target)
        return MixResult(path=target, sample_rate=sample_rate, frames=frames,
                         peak=0.0, gain=0.0, stems=[strip.name for strip in strips])

    # 校验采样率
    for strip in active:
        with _open(strip.path) as handle:
            if handle.samplerate != sample_rate:
                raise MixdownError(
                    f"分轨 {strip.name} 采样率 {handle.samplerate} 与目标 {sample_rate} 不一致",
                    code=43,
                )

    def _iterate():
        """逐块产生 (Σ 分轨) 的混音块，块长取本块内最长分轨。"""
        handles = [_open(strip.path) for strip in active]
        try:
            while True:
                blocks = []
                for handle in handles:
                    block = handle.read(size, dtype="float32", always_2d=True)
                    blocks.append(_stack(block) if block.size else None)
                if not any(block is not None for block in blocks):
                    return
                length = max(len(block) for block in blocks if block is not None)
                mixed = np.zeros((length, 2), dtype=np.float32)
                for strip, block in zip(active, blocks):
                    if block is None:
                        continue
                    if len(block) < length:  # 尾块补零（各 stem 长度可能差几帧）
                        padded = np.zeros((length, 2), dtype=np.float32)
                        padded[: len(block)] = block
                        block = padded
                    mixed += _apply_strip(block, strip)
                yield mixed
        finally:
            for handle in handles:
                handle.close()

    # pass 1：求峰值与总帧数（不落盘）
    peak = 0.0
    frames = 0
    for mixed in _iterate():
        peak = max(peak, float(np.max(np.abs(mixed))) if mixed.size else 0.0)
        frames += len(mixed)

    gain = 1.0
    if normalize and peak > 0:
        gain = min(float(target_peak) / peak, 64.0)  # 上限防止把噪声放大成爆音

    # pass 2：带增益落盘
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with sf.SoundFile(str(target), "w", samplerate=sample_rate, channels=2,
                          subtype=subtype) as out:
            for mixed in _iterate():
                out.write(mixed * gain)
    except OSError as exc:
        raise MixdownError(f"写入混音文件失败 {target}: {exc}", code=43) from exc

    peak_out = peak * gain if normalize else peak
    logger.info(
        "混音完成: %s（%d 轨，%.1fs，峰值 %.3f，增益 %.2f×）",
        target, len(active), frames / sample_rate if sample_rate else 0.0, peak_out, gain,
    )
    return MixResult(path=target, sample_rate=sample_rate, frames=frames, peak=peak_out,
                     gain=gain, stems=[strip.name for strip in active])


def strips_from_song(song, stems: dict[str, Path]) -> list[StemStrip]:
    """把 ``compose.Song`` 的通道条映射成 :class:`StemStrip`（缺 stem 的轨道跳过）。"""
    result: list[StemStrip] = []
    for track in song.tracks:
        path = stems.get(track.id)
        if path is None:
            continue
        strip = track.mix
        result.append(StemStrip(
            name=track.id,
            path=Path(path),
            gain_db=strip.gain_db,
            pan=strip.pan,
            mute=strip.mute or track.muted,
            solo=strip.solo,
            reverb_send=strip.reverb_send,
            chorus_send=strip.chorus_send,
        ))
    return result


def describe_mix(result: MixResult) -> Iterable[str]:
    """人类可读摘要行。"""
    yield f"混音 {result.path.name}：{result.frames / result.sample_rate:.1f}s｜"\
          f"峰值 {result.peak:.3f}｜{len(result.stems)} 轨"
