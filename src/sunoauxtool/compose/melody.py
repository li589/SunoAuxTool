"""旋律与对位（2.0 PR-D）：动机生成 + 发展算子 + 乐句拼装。

思路（与 ``docs/refactor-2.0-multitrack.md`` 5.4 一致）：

1. **动机**：一小节内的"音高细胞 + 节奏细胞"，音高取自和弦音与音阶音的混合，
   节奏落在网格上（默认八分）；
2. **发展**：重复 / 模进 / 倒影 / 逆行 / 节奏扩缩，每小节选 1–2 个算子；
3. **乐句**：末小节做终止式（收在和弦音上），并把整体限制在音域内。

所有随机来自传入的 ``rng``（由 :func:`compose.rng.rng_for` 派生），因此结果确定。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from sunoauxtool.compose.harmony import Chord, Key
from sunoauxtool.midi.document import NoteEvent


@dataclass(frozen=True)
class Motif:
    """动机：等长的音高序列与时值序列（时值之和即动机长度）。"""

    pitches: tuple[int, ...]
    durations: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.pitches) != len(self.durations):
            raise ValueError("动机的音高与时值数量必须一致")
        if not self.pitches:
            raise ValueError("动机不能为空")

    @property
    def length(self) -> float:
        return float(sum(self.durations))

    def starts(self) -> list[float]:
        """每个音的相对起始拍。"""
        offsets: list[float] = []
        cursor = 0.0
        for duration in self.durations:
            offsets.append(cursor)
            cursor += duration
        return offsets

    def to_events(self, *, start: float, velocity: int = 84) -> list[NoteEvent]:
        return [
            NoteEvent(pitch=pitch, start=start + offset, duration=duration, velocity=velocity)
            for pitch, offset, duration in zip(self.pitches, self.starts(), self.durations)
        ]

    # -- 发展算子 ---------------------------------------------------------

    def transposed(self, semitones: int) -> "Motif":
        return Motif(
            pitches=tuple(pitch + int(semitones) for pitch in self.pitches),
            durations=self.durations,
        )

    def inverted(self) -> "Motif":
        """倒影：以首音为轴。"""
        axis = self.pitches[0]
        return Motif(
            pitches=tuple(axis - (pitch - axis) for pitch in self.pitches),
            durations=self.durations,
        )

    def retrograded(self) -> "Motif":
        """逆行：音高与时值同时反序。"""
        return Motif(pitches=self.pitches[::-1], durations=self.durations[::-1])

    def augmented(self, factor: float = 2.0) -> "Motif":
        """节奏扩大（时值 × factor）。"""
        return Motif(
            pitches=self.pitches,
            durations=tuple(duration * float(factor) for duration in self.durations),
        )

    def clamp(self, low: int, high: int, key: Key | None = None) -> "Motif":
        """把越界音折回音域（八度折返，再吸附到音阶）。"""
        folded: list[int] = []
        for pitch in self.pitches:
            candidate = pitch
            while candidate > high:
                candidate -= 12
            while candidate < low:
                candidate += 12
            if key is not None:
                snapped = key.snap(candidate)
                if snapped < low:
                    snapped += 12
                elif snapped > high:
                    snapped -= 12
                candidate = snapped
            folded.append(max(low, min(high, candidate)))
        return Motif(pitches=tuple(folded), durations=self.durations)


def _onsets(beats: float, grid: float, density: float, rng) -> list[float]:
    """在小节内按密度抽起始点（首拍必占）。"""
    slots = max(1, int(round(beats / grid)))
    onsets = [0.0]
    for index in range(1, slots):
        if rng.random() < 0.35 + 0.55 * max(0.0, min(1.0, density)):
            onsets.append(index * grid)
    return onsets


def _durations(onsets: Sequence[float], beats: float, grid: float) -> list[float]:
    """由起始点反推时值（到下一个起始点为止，最短半格、最长两格）。"""
    durations: list[float] = []
    for index, onset in enumerate(onsets):
        following = onsets[index + 1] if index + 1 < len(onsets) else beats
        span = following - onset
        durations.append(max(grid * 0.5, min(span, grid * 2.0)))
    return durations


def make_motif(
    key: Key,
    chord: Chord,
    *,
    low: int,
    high: int,
    rng,
    beats: float = 4.0,
    grid: float = 0.5,
    density: float = 0.5,
    contour: str = "arch",
) -> Motif:
    """生成一小节动机。

    Args:
        key: 调式（音高吸附与音阶运动）。
        chord: 该小节的和弦（首音取和弦音，强调和声）。
        low / high: 音域。
        rng: 随机源。
        beats: 小节长度（动机总长 ≈ beats）。
        grid: 节奏网格（默认八分）。
        density: 0..1，音符密度（越高音越多）。
        contour: ``"arch"``（上行后下行）/ ``"asc"`` / ``"desc"`` / ``"wave"``。

    Returns:
        收在音域内的动机。
    """
    onsets = _onsets(beats, grid, density, rng)
    durations = _durations(onsets, beats, grid)
    scale = key.pitches(low, high) or [low]
    tones = chord.tones(low, high) or scale

    start_pool = [pitch for pitch in tones if low + 5 <= pitch <= high - 5] or tones
    current = rng.choice(start_pool)
    pitches = [current]
    direction = 1
    for index in range(1, len(onsets)):
        ratio = index / max(1, len(onsets) - 1)
        if contour == "asc":
            direction = 1
        elif contour == "desc":
            direction = -1
        elif contour == "wave":
            direction = 1 if rng.random() < 0.5 else -1
        else:  # arch
            direction = 1 if ratio < 0.5 else -1
        step = rng.choice((1, 1, 2, 2, 3)) * direction
        index_now = scale.index(current) if current in scale else 0
        target_index = max(0, min(len(scale) - 1, index_now + step))
        current = scale[target_index]
        pitches.append(current)

    motif = Motif(pitches=tuple(pitches), durations=tuple(durations))
    return motif.clamp(low, high, key)


def phrase(
    key: Key,
    chords: Sequence[Chord],
    *,
    low: int,
    high: int,
    bar_length: float = 4.0,
    rng,
    density: float = 0.5,
    grid: float = 0.5,
    velocity: int = 84,
    cadence: bool = True,
) -> list[NoteEvent]:
    """按小节生成乐句（每小节一个动机变体，末音做终止式）。

    Args:
        key: 调式。
        chords: 每小节一个和弦（长度决定乐句长度；不足时循环）。
        low / high: 音域。
        bar_length: 小节长度（拍）。
        rng: 随机源。
        density: 音符密度。
        grid: 节奏网格。
        velocity: 基准力度。
        cadence: 末小节的末音收在和弦音上。

    Returns:
        相对拍（0 = 乐句起点）的音符事件，升序。
    """
    if not chords:
        return []
    events: list[NoteEvent] = []
    base: Motif | None = None
    operators = ("repeat", "sequence", "invert", "retrograde")
    for bar, chord in enumerate(chords):
        bar_start = bar * bar_length
        if base is None:
            base = make_motif(
                key, chord, low=low, high=high, rng=rng,
                beats=bar_length, grid=grid, density=density,
            )
            motif = base
        else:
            operator = rng.choice(operators)
            if operator == "sequence":
                motif = base.transposed(rng.choice((2, -2, 3, -3)))
            elif operator == "invert":
                motif = base.inverted()
            elif operator == "retrograde":
                motif = base.retrograded()
            else:
                motif = base
            motif = motif.clamp(low, high, key)
        if cadence and bar == len(chords) - 1:
            motif = _cadence(motif, chord, low=low, high=high)
        for event in motif.to_events(start=bar_start, velocity=velocity):
            if event.start + event.duration <= bar_start + bar_length + 1e-9:
                events.append(event)
    events.sort(key=lambda note: (note.start, note.pitch))
    return events


def _cadence(motif: Motif, chord: Chord, *, low: int, high: int) -> Motif:
    """终止式：末音收到最近的和弦音（优先根音/三音）。"""
    tones = chord.tones(low, high)
    if not tones:
        return motif
    last = motif.pitches[-1]
    target = min(tones, key=lambda pitch: (abs(pitch - last), pitch))
    pitches = list(motif.pitches)
    pitches[-1] = target
    return Motif(pitches=tuple(pitches), durations=motif.durations)


def counter_line(
    key: Key,
    melody: Sequence[NoteEvent],
    *,
    low: int,
    high: int,
    rng,
    velocity: int = 70,
) -> list[NoteEvent]:
    """对位声部：对主旋律做倒影并移位，避免与主旋律同度撞音。"""
    if not melody:
        return []
    shift = -12 if rng.random() < 0.5 else -7
    result: list[NoteEvent] = []
    melody_pitch_at = {round(event.start, 3): event.pitch for event in melody}
    for event in melody:
        pitch = event.pitch + shift
        while pitch > high:
            pitch -= 12
        while pitch < low:
            pitch += 12
        pitch = key.snap(max(low, min(high, pitch)))
        if pitch % 12 == melody_pitch_at.get(round(event.start, 3), -1) % 12:
            pitch = key.snap(pitch + 3)
            pitch = max(low, min(high, pitch))
        result.append(NoteEvent(
            pitch=pitch,
            start=event.start,
            duration=event.duration,
            velocity=max(1, min(127, velocity)),
        ))
    return result
