"""和声：调式与和弦解析 + 声部配置（2.0 PR-D）。

自足实现（不引入 music21）：compose 域要保持"零重依赖"（G9），且需要**确定性**
——同样的输入必然得到同样的声部排列。

和弦写法（与清单 ``progression`` 一致）::

    "Dm"      -> D 小三和弦
    "Bb"      -> Bb 大三和弦
    "F/A"     -> F 大三和弦，A 作低音（转位）
    "Cmaj7"   -> C 大七和弦
    "G7"      -> G 属七和弦
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Iterable, Sequence

from sunoauxtool.exceptions import ParameterError

#: 音名 → 音级（C = 0）
NOTE_NAMES = {
    "C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11,
}

#: 调式音阶间隔（半音）
MODE_INTERVALS = {
    "major": (0, 2, 4, 5, 7, 9, 11),
    "ionian": (0, 2, 4, 5, 7, 9, 11),
    "minor": (0, 2, 3, 5, 7, 8, 10),
    "aeolian": (0, 2, 3, 5, 7, 8, 10),
    "dorian": (0, 2, 3, 5, 7, 9, 10),
    "phrygian": (0, 1, 3, 5, 7, 8, 10),
    "lydian": (0, 2, 4, 6, 7, 9, 11),
    "mixolydian": (0, 2, 4, 5, 7, 9, 10),
    "locrian": (0, 1, 3, 5, 6, 8, 10),
}

#: 和弦性质 → 相对根音的半音间隔（按从简到繁匹配后缀，长后缀优先）
CHORD_QUALITIES: dict[str, tuple[int, ...]] = {
    "": (0, 4, 7),
    "maj": (0, 4, 7),
    "M": (0, 4, 7),
    "m": (0, 3, 7),
    "min": (0, 3, 7),
    "dim": (0, 3, 6),
    "dim7": (0, 3, 6, 9),
    "m7b5": (0, 3, 6, 10),
    "aug": (0, 4, 8),
    "+": (0, 4, 8),
    "sus2": (0, 2, 7),
    "sus4": (0, 5, 7),
    "6": (0, 4, 7, 9),
    "m6": (0, 3, 7, 9),
    "7": (0, 4, 7, 10),
    "maj7": (0, 4, 7, 11),
    "M7": (0, 4, 7, 11),
    "m7": (0, 3, 7, 10),
    "9": (0, 4, 7, 10, 14),
    "m9": (0, 3, 7, 10, 14),
}

#: 和弦后缀按长度倒序（解析时优先匹配长后缀，避免 "maj7" 被读成 "m"）
_SUFFIXES = sorted(CHORD_QUALITIES, key=len, reverse=True)


def parse_pitch_class(text: str) -> int:
    """``"C"`` / ``"F#"`` / ``"Bb"`` → 音级 0-11。"""
    token = str(text).strip()
    if not token or token[0].upper() not in NOTE_NAMES:
        raise ParameterError(f"无法解析音名: {text!r}")
    pitch = NOTE_NAMES[token[0].upper()]
    for char in token[1:]:
        if char == "#":
            pitch += 1
        elif char in ("b", "B"):
            pitch -= 1
        else:
            raise ParameterError(f"无法解析音名: {text!r}")
    return pitch % 12


@dataclass(frozen=True)
class Key:
    """调式：主音音级 + 调式名。"""

    tonic: int
    mode: str = "major"

    def __post_init__(self) -> None:
        if self.mode not in MODE_INTERVALS:
            raise ParameterError(
                f"未知调式 {self.mode!r}；可用: {', '.join(sorted(MODE_INTERVALS))}"
            )

    @property
    def pitch_classes(self) -> tuple[int, ...]:
        """音阶音级（升序，以主音为起点）。"""
        return tuple((self.tonic + step) % 12 for step in MODE_INTERVALS[self.mode])

    def pitches(self, low: int, high: int) -> list[int]:
        """``[low, high]`` 内的全部音阶音（升序）。"""
        members = set(self.pitch_classes)
        return [pitch for pitch in range(int(low), int(high) + 1) if pitch % 12 in members]

    def snap(self, pitch: int) -> int:
        """把任意音高吸附到最近的音阶音（距离相同取低音）。"""
        members = self.pitch_classes
        for delta in range(0, 13):
            for candidate in (pitch - delta, pitch + delta):
                if candidate % 12 in members:
                    return candidate
        return pitch  # pragma: no cover - 音阶必含 12 音级中的 7 个，不可能到这里


def parse_key(text: str) -> Key:
    """``"D minor"`` / ``"Bb major"`` / ``"C"``（默认大调）→ :class:`Key`。

    模式别名：``major/maj/M`` 与 ``minor/min/m``（大写 ``M`` 仍表示大调，
    和 ``m`` 区分开）。
    """
    parts = str(text).strip().split()
    if not parts:
        raise ParameterError("调性不能为空")
    tonic = parse_pitch_class(parts[0])
    if len(parts) == 1:
        return Key(tonic, "major")
    raw = parts[1].strip()
    if raw in ("major", "maj", "M") or raw.lower() in ("major", "maj"):
        mode = "major"
    elif raw.lower() in ("minor", "min", "m"):
        mode = "minor"
    else:
        mode = raw.lower()  # 交给 Key 校验（支持 dorian 等调式）
    return Key(tonic, mode)


@dataclass(frozen=True)
class Chord:
    """和弦：根音音级 + 性质 + 可选低音（转位）。"""

    root: int
    quality: str = ""
    bass: int | None = None

    def __post_init__(self) -> None:
        if self.quality not in CHORD_QUALITIES:
            raise ParameterError(
                f"未知和弦性质 {self.quality!r}；可用: {', '.join(sorted(CHORD_QUALITIES))}"
            )

    @property
    def intervals(self) -> tuple[int, ...]:
        return CHORD_QUALITIES[self.quality]

    @property
    def pitch_classes(self) -> tuple[int, ...]:
        """和弦音级（低音若指定且不在和弦内，则补在最前）。"""
        tones = [(self.root + step) % 12 for step in self.intervals]
        if self.bass is not None and self.bass not in tones:
            tones.insert(0, self.bass)
        return tuple(tones)

    def tones(self, low: int, high: int) -> list[int]:
        """``[low, high]`` 内的全部和弦音（升序）。"""
        members = set(self.pitch_classes)
        return [pitch for pitch in range(int(low), int(high) + 1) if pitch % 12 in members]

    def root_pitch(self, low: int, high: int) -> int:
        """区间内不低于 ``low`` 的最低位根音（低音声部用）。"""
        anchor = self.bass if self.bass is not None else self.root
        pitch = int(low) + ((anchor - int(low)) % 12)
        return pitch if pitch <= int(high) else int(low)


def parse_chord(symbol: str) -> Chord:
    """``"Dm"`` / ``"Bb"`` / ``"F/A"`` / ``"Cmaj7"`` → :class:`Chord`。"""
    text = str(symbol).strip()
    if not text:
        raise ParameterError("和弦不能为空")
    slash = text.find("/")
    bass: int | None = None
    if slash >= 0:
        bass = parse_pitch_class(text[slash + 1:])
        text = text[:slash]
    # 根音：1 个字母 + 任意个升降号
    length = 1
    while length < len(text) and text[length] in "#b":
        length += 1
    root = parse_pitch_class(text[:length])
    quality = text[length:]
    if quality not in CHORD_QUALITIES:
        # 允许省略 "maj" 前缀的大三和弦写法（如 "Cadd9" 暂不支持）
        raise ParameterError(
            f"无法解析和弦 {symbol!r}：未知和弦性质 {quality!r}"
        )
    return Chord(root=root, quality=quality, bass=bass)


def parse_progression(symbols: Iterable[str]) -> list[Chord]:
    """批量解析和弦进行（保持顺序）。"""
    return [parse_chord(symbol) for symbol in symbols]


def _spread(pitches: Sequence[int]) -> int:
    return int(pitches[-1] - pitches[0]) if pitches else 0


def voice_chord(
    chord: Chord,
    *,
    low: int,
    high: int,
    voices: int | None = None,
    previous: Sequence[int] | None = None,
    max_spread: int = 16,
) -> list[int]:
    """把和弦排成 ``voices`` 个音的密集排列（可选平滑声部连接）。

    做法：枚举区间内覆盖全部和弦音的组合，按"与上一和弦的总移动量 + 音域跨度惩罚"
    打分，取最小者；平局按音高排序打破（保证确定性）。

    Args:
        chord: 待排列的和弦。
        low / high: 允许的音域（MIDI）。
        voices: 声部数；``None`` 取和弦音数（三和弦 3 声部）。
        previous: 上一和弦的排列（用于声部平滑）。
        max_spread: 允许的最大跨度（半音）。

    Returns:
        升序音高列表。区间内找不到合适排列时退化为根音+邻近和弦音。
    """
    count = voices if voices is not None else len(chord.pitch_classes)
    count = max(1, count)
    candidates = chord.tones(low, high)
    if not candidates:
        raise ParameterError(f"和弦 {chord} 在音域 [{low}, {high}] 内没有可用音")

    best: tuple[float, list[int]] | None = None
    center = (int(low) + int(high)) / 2.0
    for combo in itertools.combinations(candidates, min(count, len(candidates))):
        present = {pitch % 12 for pitch in combo}
        missing = [pc for pc in chord.pitch_classes if pc not in present]
        cost = 10.0 * len(missing)
        if _spread(combo) > max_spread and len(combo) > 1:
            cost += 0.5 * (_spread(combo) - max_spread)
        # 轻微向音域中部靠拢：避免全部声部挤在音域下缘
        cost += 0.15 * abs((combo[0] + combo[-1]) / 2.0 - center)
        if previous:
            picked = list(combo)
            cost += sum(
                abs(picked[index] - previous[index])
                for index in range(min(len(picked), len(previous)))
            )
        key = (cost, list(combo))
        if best is None or key < (best[0], best[1]):
            best = key
    if best is None:  # pragma: no cover - combinations 非空
        return [candidates[0]]
    return best[1]
