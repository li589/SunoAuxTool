"""编排主流程（2.0 PR-D）：``SongSpec`` + 种子 → 音符齐全的 ``Song``。

设计要点：

- **显式优先**：清单里写了 ``[[track.note]]`` 的轨道原样保留，Arranger 只补全
  "一个音符都没有"的轨道（可混合手写与自动生成）；
- **能量驱动**：段落 ``energy`` 决定参与角色（:func:`roles_for_energy`）与力度；
- **确定性**：每条 ``(段落, 轨道, 角色)`` 用独立 RNG（:func:`compose.rng.rng_for`），
  增删轨道/改某轨 density 都不影响其它轨道的音符；
- **纯领域**：不碰 CLI / 渲染 / 文件，输入 ``SongSpec``，输出 ``Song``。

流程::

    SongSpec ─validate→ Song 骨架 ─按角色生成 Clip 音符→ humanize → Song.validate
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from sunoauxtool.compose.harmony import Chord, Key, parse_chord, parse_key, voice_chord
from sunoauxtool.compose.expression import (
    apply_articulations,
    apply_dynamics,
    apply_swing,
    dynamic_for_energy,
)
from sunoauxtool.compose.humanize import humanize as humanize_notes
from sunoauxtool.compose.melody import counter_line, phrase
from sunoauxtool.compose.orchestration import (
    base_velocity,
    default_instrument,
    effective_range,
    roles_for_energy,
)
from sunoauxtool.compose.rhythm import default_groove, section_hits
from sunoauxtool.compose.rng import rng_for
from sunoauxtool.compose.song import Clip, Section, Song, Track
from sunoauxtool.compose.spec import SongSpec, TrackSpec
from sunoauxtool.midi.document import NoteEvent

#: 生成顺序（melody 先算，counter 才能对位）
ROLE_ORDER = (
    "melody", "counter", "harmony", "arpeggio", "pad", "bass", "drums", "percussion",
)


@dataclass
class ArrangeOptions:
    """编排开关（默认全开；``--no-arrange`` 可整体关闭生成）。"""

    humanize: bool = True
    timing_ms: float = 10.0
    velocity_jitter: int = 8
    ppq: int = 960
    fill: bool = True
    crash: bool = True
    energy_gate: bool = True
    swing: float | None = None
    """全曲摇摆覆盖（None = 用 Song/轨道的设置）。"""


def _fallback_chord(key: Key) -> Chord:
    """无和弦进行时的主和弦（按调式）。"""
    quality = "m" if key.mode in ("minor", "aeolian") else ""
    return Chord(root=key.tonic, quality=quality)


def _chord_sequence(section: Section, bars: int, key: Key) -> list[Chord]:
    progression = [parse_chord(symbol) for symbol in section.progression]
    if not progression:
        return [_fallback_chord(key)] * bars
    return [progression[index % len(progression)] for index in range(bars)]


# ---------------------------------------------------------------------------
# 各角色的音符生成
# ---------------------------------------------------------------------------

def _harmony_notes(
    chords: Sequence[Chord], *, start: float, bar_length: float, low: int, high: int,
    velocity: int, energy: float,
) -> list[NoteEvent]:
    """和声：每小节一个密集排列和弦（高能量拆成两个打击式和弦）。"""
    events: list[NoteEvent] = []
    previous: list[int] | None = None
    stabs = 2 if energy >= 0.7 else 1
    step = bar_length / stabs
    for index, chord in enumerate(chords):
        voicing = voice_chord(chord, low=low, high=high, previous=previous)
        previous = voicing
        for stab in range(stabs):
            offset = index * bar_length + stab * step
            for pitch in voicing:
                events.append(NoteEvent(
                    pitch=pitch,
                    start=start + offset,
                    duration=max(0.25, step * 0.9),
                    velocity=velocity,
                ))
    return events


def _pad_notes(
    chords: Sequence[Chord], *, start: float, bar_length: float, low: int, high: int,
    velocity: int,
) -> list[NoteEvent]:
    """铺底：每小节一个长音和弦（三声部）。"""
    events: list[NoteEvent] = []
    previous: list[int] | None = None
    for index, chord in enumerate(chords):
        voicing = voice_chord(chord, low=low, high=high, voices=3, previous=previous)
        previous = voicing
        for pitch in voicing:
            events.append(NoteEvent(
                pitch=pitch,
                start=start + index * bar_length,
                duration=bar_length * 0.98,
                velocity=velocity,
            ))
    return events


def _arpeggio_notes(
    chords: Sequence[Chord], *, start: float, bar_length: float, low: int, high: int,
    velocity: int, grid: float = 0.5,
) -> list[NoteEvent]:
    """琶音：每小节按八分音符上行循环和弦音。"""
    events: list[NoteEvent] = []
    steps = max(1, int(round(bar_length / grid)))
    for index, chord in enumerate(chords):
        tones = chord.tones(low, high)
        if not tones:
            continue
        for step in range(steps):
            pitch = tones[step % len(tones)]
            events.append(NoteEvent(
                pitch=pitch,
                start=start + index * bar_length + step * grid,
                duration=grid * 0.9,
                velocity=velocity,
            ))
    return events


def _bass_notes(
    chords: Sequence[Chord], *, start: float, bar_length: float, low: int, high: int,
    velocity: int, energy: float,
) -> list[NoteEvent]:
    """贝斯：每小节根音；能量高时补五度与反拍。"""
    events: list[NoteEvent] = []
    for index, chord in enumerate(chords):
        root = chord.root_pitch(low, high)
        bar_start = start + index * bar_length
        events.append(NoteEvent(pitch=root, start=bar_start,
                                duration=bar_length * 0.85, velocity=velocity))
        if energy >= 0.55 and bar_length >= 3.0:
            events.append(NoteEvent(pitch=root, start=bar_start + bar_length * 0.5,
                                    duration=bar_length * 0.35,
                                    velocity=max(1, velocity - 8)))
        if energy >= 0.8:
            fifth = root + 7
            if fifth <= high:
                events.append(NoteEvent(pitch=fifth, start=bar_start + bar_length * 0.75,
                                        duration=bar_length * 0.2,
                                        velocity=max(1, velocity - 12)))
    return events


def _melody_notes(
    key: Key, chords: Sequence[Chord], *, start: float, bar_length: float, low: int,
    high: int, velocity: int, density: float, rng,
) -> list[NoteEvent]:
    """主旋律：按小节做动机发展，末小节终止式。"""
    events = phrase(
        key, chords, low=low, high=high, bar_length=bar_length,
        rng=rng, density=density, velocity=velocity,
    )
    return [NoteEvent(pitch=n.pitch, start=start + n.start, duration=n.duration,
                      velocity=n.velocity) for n in events]


def _counter_notes(
    key: Key, chords: Sequence[Chord], melody: Sequence[NoteEvent], *, start: float,
    bar_length: float, low: int, high: int, velocity: int, density: float, rng,
) -> list[NoteEvent]:
    """对位：优先对主旋律做倒影/移位；没有旋律时独立生成。"""
    if melody:
        aligned = [
            NoteEvent(pitch=n.pitch, start=n.start - start, duration=n.duration,
                      velocity=n.velocity)
            for n in melody
        ]
        return [
            NoteEvent(pitch=n.pitch, start=start + n.start, duration=n.duration,
                      velocity=n.velocity)
            for n in counter_line(key, aligned, low=low, high=high, rng=rng,
                                  velocity=velocity)
        ]
    events = phrase(key, chords, low=low, high=high, bar_length=bar_length, rng=rng,
                    density=max(0.2, density * 0.7), velocity=velocity, cadence=False)
    return [NoteEvent(pitch=n.pitch, start=start + n.start, duration=n.duration,
                      velocity=n.velocity) for n in events]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _generate_clip_notes(
    song: Song,
    track: Track,
    tspec: TrackSpec,
    section: Section,
    key: Key,
    options: ArrangeOptions,
    melody_notes: Sequence[NoteEvent],
) -> list[NoteEvent]:
    role = track.role
    if options.energy_gate and role not in roles_for_energy(section.energy):
        return []

    start = section.start
    bar_length = song.bar_length_at(start)
    low, high = effective_range(role, octave=tspec.octave, explicit=tspec.range)
    velocity = base_velocity(role, section.energy)
    density = 0.5 if tspec.density is None else max(0.05, min(1.0, tspec.density))
    rng = rng_for(song.seed, section.id, track.id, role)

    if role == "drums":
        groove = tspec.groove or default_groove(section.energy, bar_length=bar_length)
        return section_hits(
            groove,
            start=start,
            beats=section.bars * bar_length,
            bar_length=bar_length,
            seed=song.seed,
            section_id=section.id,
            track_id=track.id,
            fill_last=options.fill,
            crash_first=options.crash,
            energy=section.energy,
        )

    chords = _chord_sequence(section, section.bars, key)

    if role == "melody":
        return _melody_notes(key, chords, start=start, bar_length=bar_length, low=low,
                             high=high, velocity=velocity, density=density, rng=rng)
    if role == "counter":
        return _counter_notes(key, chords, melody_notes, start=start, bar_length=bar_length,
                              low=low, high=high, velocity=velocity, density=density, rng=rng)
    if role == "harmony":
        return _harmony_notes(chords, start=start, bar_length=bar_length, low=low,
                              high=high, velocity=velocity, energy=section.energy)
    if role == "pad":
        return _pad_notes(chords, start=start, bar_length=bar_length, low=low, high=high,
                          velocity=velocity)
    if role == "arpeggio":
        return _arpeggio_notes(chords, start=start, bar_length=bar_length, low=low,
                               high=high, velocity=velocity)
    if role == "bass":
        return _bass_notes(chords, start=start, bar_length=bar_length, low=low, high=high,
                           velocity=velocity, energy=section.energy)
    return []  # percussion 等尚无生成器：留空（不报错）


def _clips_for(
    song: Song, track: Track, tspec: TrackSpec, key: Key, options: ArrangeOptions
) -> list[Clip]:
    """为一条待补全轨道重建片段（按声明段落；未声明则全曲）。"""
    clips: list[Clip] = []
    melody_by_section: dict[str, list[NoteEvent]] = {}
    melody_track = next((item for item in song.tracks if item.role == "melody"), None)
    if melody_track is not None:
        melody_by_section = {clip.section_id: clip.notes for clip in melody_track.clips}

    for section in song.sections:
        if tspec.sections and section.id not in tspec.sections:
            continue
        start = section.start
        length = song.section_end(section) - start
        notes = _generate_clip_notes(
            song, track, tspec, section, key, options,
            melody_by_section.get(section.id, ()),
        )
        if notes:
            notes = _apply_expression(song, track, section, notes, options)
        clips.append(Clip(start=start, length=length, notes=list(notes),
                          section_id=section.id))
    return clips


def _tempo_at(song: Song, beat: float) -> float:
    """取 ``beat`` 处的速度（速度表按 beat 升序）。"""
    bpm = song.tempo_map[0].bpm
    for point in song.tempo_map:
        if point.beat <= beat + 1e-9:
            bpm = point.bpm
        else:
            break
    return float(bpm)


def _apply_expression(
    song: Song,
    track: Track,
    section: Section,
    notes: list[NoteEvent],
    options: ArrangeOptions,
) -> list[NoteEvent]:
    """表现力处理链：摇摆 → 演奏法 → 力度 → 人性化。

    顺序有讲究：摇摆只动 ``offset_ticks``，演奏法改时值/力度，力度缩放力度，
    人性化最后抖动（这样"抖"的是最终力度，而不是被后续修正吃掉）。
    """
    ratio = (
        float(options.swing) if options.swing is not None
        else (track.swing or song.swing or 0.0)
    )
    if ratio:
        # 摇摆永远以八分为单位（3/4 也一样：每对八分的前半/后半）
        notes = apply_swing(notes, ratio, ppq=options.ppq, grid=0.5)
    notes = apply_articulations(notes, track.articulation or None)
    dynamic = section.dynamic or dynamic_for_energy(section.energy)
    notes = apply_dynamics(
        notes, dynamic=dynamic, ramp=section.ramp,
    )
    if options.humanize:
        bpm = _tempo_at(song, section.start)
        notes = humanize_notes(
            notes,
            rng_for(song.seed, section.id, track.id, "humanize"),
            ppq=options.ppq,
            velocity_jitter=options.velocity_jitter,
            timing_ms=options.timing_ms,
            bpm=bpm,
        )
    return notes


def arrange(spec: SongSpec, options: ArrangeOptions | None = None) -> Song:
    """把清单编排成完整 ``Song``（显式音符保留，空轨道自动补全）。

    Args:
        spec: 已解析的清单（内部会再校验一次）。
        options: 编排开关，``None`` 用默认值。

    Returns:
        音符齐全、已通过 ``Song.validate()`` 的曲子。

    Raises:
        SpecError: 清单非法（40/44）。
        ChannelAllocationError: 轨道过多（45）。
    """
    options = options or ArrangeOptions()
    song = spec.to_song()
    key = parse_key(spec.key)
    specs = {track.id: track for track in spec.tracks}

    ordered = sorted(
        song.tracks,
        key=lambda item: ROLE_ORDER.index(item.role) if item.role in ROLE_ORDER else len(ROLE_ORDER),
    )
    sections_by_id = {section.id: section for section in song.sections}
    for track in ordered:
        tspec = specs[track.id]
        if any(clip.notes for clip in track.clips):
            # 显式音符优先：整轨跳过**生成**，但表现力照常施加
            # （摇摆/演奏法/力度/人性化是"怎么演奏"，与音符从哪来无关）
            for clip in track.clips:
                section = sections_by_id.get(clip.section_id)
                if section is None or not clip.notes:
                    continue
                clip.notes = _apply_expression(
                    song, track, section, list(clip.notes), options
                )
            continue
        if not track.instrument.has_explicit_program:
            track.instrument = default_instrument(track.role, bank=track.instrument.bank)
        track.clips = _clips_for(song, track, tspec, key, options)

    song.validate()
    return song
