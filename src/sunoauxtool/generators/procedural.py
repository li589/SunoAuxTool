"""程序化多轨 MIDI 生成器（和弦/旋律/贝斯，可选第 4 轨鼓）。

风格预设控制：乐器号（GM 映射）、节奏密度（sustain/half/eighth）。
P2-2/P2-4 集成（默认关闭/可选）：
- request.style_instruments：CLI 解析 StyleRegistry 后注入的乐器号（P2-4）；
- request.rhythm_pattern：引用 RhythmPatternRegistry 的节奏型（P2-2d），驱动贝斯音头；
- request.enable_*：乐理规则后处理（P2-2e），默认关闭。
所有随机性经由 SeedContext 设置，保证同 seed 可复现。
"""

from __future__ import annotations

import random
from typing import Dict, List, Optional

from sunoauxtool.generators.base import (
    GenerationRequest,
    Generator,
    SeedContext,
    chord_tones_in_range,
    scale_pitches_in_range,
)
from sunoauxtool.models.chords import ChordProgression
from sunoauxtool.models.notes import Note, NoteSequence
from sunoauxtool.music_theory.postprocess import apply_postprocess
from sunoauxtool.music_theory.rhythm_patterns import RhythmPattern, RhythmPatternRegistry


class _StylePreset:
    """风格预设：乐器号 + 节奏密度（P0 内置兜底）。"""

    __slots__ = ("chords_program", "melody_program", "bass_program", "density")

    def __init__(self, chords_program: int, melody_program: int, bass_program: int, density: str) -> None:
        self.chords_program = chords_program
        self.melody_program = melody_program
        self.bass_program = bass_program
        self.density = density  # 'sustain' | 'half' | 'eighth'


#: 内置风格预设（P2-4 扩展为完整风格库后作为兜底）
STYLE_PRESETS: Dict[str, _StylePreset] = {
    "pop": _StylePreset(0, 81, 33, "sustain"),        # 钢琴 / 合成主音 / 电贝斯
    "rock": _StylePreset(25, 80, 34, "eighth"),       # 尼龙吉他 / 方波主音 / 指拨贝斯
    "electronic": _StylePreset(88, 81, 38, "eighth"), # 合成垫 / 合成主音 / 合成贝斯
    "classical": _StylePreset(46, 73, 43, "sustain"), # 竖琴 / 长笛 / 大提琴
    "piano-cheerful": _StylePreset(0, 0, 33, "block"), # 钢琴主奏 / 整小节块状和弦(一起按下)伴奏
}

_DEFAULT_STYLE = "pop"


class ProceduralGenerator(Generator):
    """程序化生成器：不依赖 AI，纯乐理规则 + 随机游走产出多轨 MIDI。"""

    def __init__(self, seed: Optional[int] = None) -> None:
        self.seed = seed

    # -- 公共接口 ----------------------------------------------------------

    def generate(self, request: GenerationRequest) -> NoteSequence:
        """生成多轨 NoteSequence（和弦/旋律/贝斯 [+鼓]）。"""
        with SeedContext(self.seed if self.seed is not None else request.seed):
            progression = ChordProgression.parse(request.chords)
            preset = STYLE_PRESETS.get(request.style, STYLE_PRESETS[_DEFAULT_STYLE])
            programs = self._programs(request, preset)
            beats_per_bar = _beats_per_bar(request.time_signature)
            rhythm = self._rhythm_pattern(request)

            seq = NoteSequence(
                bpm=request.bpm,
                key=request.key,
                time_signature=request.time_signature,
                bars=request.bars,
                style=request.style,
            )

            track_names = list(request.tracks)
            if request.with_drums and "drums" not in track_names:
                track_names.append("drums")

            if "chords" in track_names:
                seq.add_track(
                    "chords", programs["chords"], 0,
                    self._chords_track(progression, request, preset, beats_per_bar),
                )
            if "melody" in track_names:
                seq.add_track(
                    "melody", programs["melody"], 1,
                    self._melody_track(progression, request, preset, beats_per_bar),
                )
            if "bass" in track_names:
                seq.add_track(
                    "bass", programs["bass"], 2,
                    self._bass_track(progression, request, preset, beats_per_bar, rhythm),
                )
            if "drums" in track_names:
                seq.add_track("drums", 0, 9, self._drums_track(request, beats_per_bar))

            # P2-2 乐理规则后处理（默认关闭）
            if request.enable_voice_leading or request.enable_counterpoint or request.enable_inversion:
                seq, _violations = apply_postprocess(seq, request)
            return seq

    # -- 风格/节奏解析 -----------------------------------------------------

    @staticmethod
    def _programs(request: GenerationRequest, preset: _StylePreset) -> Dict[str, int]:
        """确定各轨乐器号：CLI 注入（StyleRegistry）> 内置兜底。"""
        if request.style_instruments:
            return {
                "chords": int(request.style_instruments.get("chords", 0)),
                "melody": int(request.style_instruments.get("melody", 81)),
                "bass": int(request.style_instruments.get("bass", 33)),
            }
        return {
            "chords": preset.chords_program,
            "melody": preset.melody_program,
            "bass": preset.bass_program,
        }

    @staticmethod
    def _rhythm_pattern(request: GenerationRequest) -> Optional[RhythmPattern]:
        """解析节奏型；未指定时返回 None（沿用 P0 密度行为）。"""
        if not request.rhythm_pattern:
            return None
        return RhythmPatternRegistry().get(request.rhythm_pattern)

    # -- 各轨生成 ----------------------------------------------------------

    def _chords_track(
        self,
        progression: ChordProgression,
        request: GenerationRequest,
        preset: _StylePreset,
        beats_per_bar: float,
    ) -> List[Note]:
        """和弦轨：按小节铺开块状和弦/琶音。

        在 sustain 模式下，按奇数小节块状长音、偶数小节分解琶音——让和弦伴奏有变化，不单调。
        """
        notes: List[Note] = []
        for bar in range(request.bars):
            chord = progression.get_chord(bar)
            bar_start = bar * beats_per_bar
            tones = chord_tones_in_range(chord.chord_tones, 48, 71)  # C3 附近
            if not tones:
                tones = [48 + (chord.root_pc - 48) % 12]
            if preset.density == "eighth":
                # 8 分音符琶音（上下循环）
                step = beats_per_bar / 8.0
                for i in range(8):
                    idx = i % len(tones)
                    notes.append(
                        Note(
                            pitch=tones[idx],
                            start=bar_start + i * step,
                            duration=step * 0.9,
                            velocity=58 + random.randint(-4, 6),
                        )
                    )
            elif preset.density == "half":
                for half in range(2):
                    for tone in tones:
                        notes.append(
                            Note(
                                pitch=tone,
                                start=bar_start + half * beats_per_bar / 2,
                                duration=beats_per_bar / 2 - 0.05,
                                velocity=58 + random.randint(-4, 6),
                            )
                        )
            elif preset.density == "block":
                # 块状和弦：整小节所有和弦音同时按下（一起按下去），sustain 整小节
                notes.extend(_chords_track_block(tones, bar_start, beats_per_bar))
            else:  # sustain：交替块状与分解，制造伴奏呼吸感
                # 奇数小节（1/3/5/7）：块状和弦 sustain
                if bar % 2 == 0:
                    for tone in tones:
                        notes.append(
                            Note(
                                pitch=tone,
                                start=bar_start,
                                duration=beats_per_bar - 0.05,
                                velocity=58 + random.randint(-4, 6),
                            )
                        )
                else:
                    # 偶数小节（2/4/6/8）：慢速琶音分解（每拍一个和弦音上行）
                    # 注：step = bpb/max(4,n) 使末音 t=(n-1)·bpb/max(4,n) < bpb 恒成立，
                    # 越界 break 在数学上不可达（1.4.8 F2 复核后移除死分支）
                    step = beats_per_bar / max(4, len(tones))
                    for i, tone in enumerate(tones):
                        t = bar_start + i * step
                        notes.append(
                            Note(
                                pitch=tone,
                                start=t,
                                duration=step * 0.9,
                                velocity=58 + random.randint(-4, 6),
                            )
                        )
        return notes

    def _melody_track(
        self,
        progression: ChordProgression,
        request: GenerationRequest,
        preset: _StylePreset,
        beats_per_bar: float,
    ) -> List[Note]:
        """旋律轨：动机驱动的欢快主旋律（强拍和弦音 + 弱拍级进 + 乐句动机）。

        解决「听不出旋律 / 单一音」问题：
        - 强拍（每小节第 1、3 拍）落在和弦音 -> 和声清晰、有调性；
        - 弱拍以级进经过音（<=2 半音）连接 -> 杜绝跨十几半音的狂跳；
        - 每个 4 小节乐句复用同一节奏动机 -> 可记忆的律动钩子；
        - 乐句轮廓在 拱形/波浪/上行/下行 间轮换 -> 有起伏而非无序游走。
        向后兼容：无 melody_profile 时退化为均匀节奏的级进旋律。
        """
        self_in = request.melody_profile
        variation = 0.3
        if self_in and isinstance(self_in, dict):
            variation = float(self_in.get("variation_strength", 0.3))
        variation = max(0.0, min(1.0, variation))

        notes: List[Note] = []
        # 音域：读 melody_profile.register（如 "C4-C6"）；退化 C4(60)~B5(83)
        low, high = 60, 83
        if self_in and isinstance(self_in, dict):
            reg = str(self_in.get("register", ""))
            if reg and "-" in reg:
                left, _, right = reg.partition("-")
                low = _parse_pitch_name_to_midi(left) or 60
                high = _parse_pitch_name_to_midi(right) or 83
        pool = scale_pitches_in_range(request.key, low, high)
        if not pool:
            pool = scale_pitches_in_range(request.key, 60, 83)

        unit = beats_per_bar / 4.0  # 四分音符长度（拍）

        # 节奏动机（单小节内的 (拍偏移, 时长)，单位=拍；活动度从低到高）
        MOTIFS = [
            [(0, 1.0), (1.0, 0.5), (1.5, 0.5), (2.0, 1.0), (3.0, 0.5), (3.5, 0.5)],  # 蹦跳
            [(0, 1.5), (1.5, 0.5), (2.0, 1.0), (3.0, 0.5), (3.5, 0.5)],            # 附点
            [(0, 0.5), (0.5, 0.5), (1.0, 0.5), (1.5, 0.5),
             (2.0, 0.5), (2.5, 0.5), (3.0, 0.5), (3.5, 0.5)],                      # 流动八分
            [(0, 1.0), (1.0, 0.5), (1.5, 0.5), (2.0, 1.0), (3.0, 1.0)],            # 平稳
        ]
        phrase_len = 4  # 每 4 小节一乐句，乐句内复用同一动机
        CONTOURS = ["arch", "wave", "ascend", "descend"]

        # 第一遍：规划每个强拍（第 1、3 拍）的目标和弦音
        plan: dict = {}
        last: Optional[int] = None
        for bar in range(request.bars):
            chord = progression.get_chord(bar)
            ctones = chord_tones_in_range(chord.chord_tones, low, high)
            if not ctones:
                ctones = [60 + (chord.root_pc - 60) % 12]
            contour = CONTOURS[(bar // phrase_len) % len(CONTOURS)]
            local = bar % phrase_len
            for off in (0.0, 2.0):
                if last is None:
                    t = ctones[len(ctones) // 2]
                else:
                    ordered = sorted(ctones, key=lambda p: abs(p - last))
                    t = ordered[0]
                    if len(ordered) > 1:
                        if contour == "ascend" and ordered[0] < last:
                            t = ordered[1]
                        elif contour == "descend" and ordered[0] > last:
                            t = ordered[1]
                        elif contour == "arch":
                            if local < phrase_len // 2 and ordered[0] < last:
                                t = ordered[1]
                            elif local >= phrase_len // 2 and ordered[0] > last:
                                t = ordered[1]
                        if t == last:  # 避免连续完全相同，保持流动
                            t = ordered[1]
                plan[(bar, off)] = t
                last = t

        # 第二遍：按动机铺设音符，弱拍级进趋向下一个强拍目标
        current: Optional[int] = None
        for bar in range(request.bars):
            chord = progression.get_chord(bar)
            ctones = chord_tones_in_range(chord.chord_tones, low, high)
            if not ctones:
                ctones = [60 + (chord.root_pc - 60) % 12]
            phrase = bar // phrase_len
            # 动机选择：活动度随 variation 提升；乐句间轮换制造对比
            if variation > 0.6:
                motif_idx = (phrase * 2) % len(MOTIFS)
            elif variation < 0.25:
                motif_idx = (phrase + len(MOTIFS) - 1) % len(MOTIFS)
            else:
                motif_idx = (phrase + (1 if variation > 0.5 else 0)) % len(MOTIFS)
            motif = MOTIFS[motif_idx]
            bar_start = bar * beats_per_bar
            for (off, dur) in motif:
                if off < 2.0:
                    nxt_strong = plan.get((bar, 2.0)) or plan.get((bar + 1, 0.0))
                else:
                    nxt_strong = plan.get((bar + 1, 0.0)) or plan.get((bar, 0.0))
                if off in (0.0, 2.0):
                    pitch = plan[(bar, off)]
                    strong = True
                else:
                    pitch = _step_toward(current, nxt_strong, pool)
                    strong = False
                if pitch is None:
                    pitch = pool[len(pool) // 2]
                notes.append(
                    Note(
                        pitch=pitch,
                        start=bar_start + off * unit,
                        duration=dur * unit * 0.95,
                        velocity=(78 if strong else 70) + random.randint(-3, 4),
                    )
                )
                current = pitch
        return notes

    def _bass_track(
        self,
        progression: ChordProgression,
        request: GenerationRequest,
        preset: _StylePreset,
        beats_per_bar: float,
        rhythm: Optional[RhythmPattern],
    ) -> List[Note]:
        """贝斯轨：根音打底（整小节 / 四分 / 八分节奏型）。

        当 request.rhythm_pattern 指定时，按节奏型网格放置音头（P2-2d）。
        """
        notes: List[Note] = []
        for bar in range(request.bars):
            chord = progression.get_chord(bar)
            bar_start = bar * beats_per_bar
            root_pitch = 36 + ((chord.root_pc - 36) % 12)  # C2 附近
            if rhythm is not None:
                onsets = rhythm.onsets_in_bar(beats_per_bar)
                step = beats_per_bar / max(1, len(rhythm.grid))
                for t in onsets:
                    # 第五个音头可加五度音（保持节奏型骨架）
                    p = root_pitch
                    notes.append(
                        Note(
                            pitch=p,
                            start=bar_start + t,
                            duration=step * 0.9,
                            velocity=66 + random.randint(-4, 6),
                        )
                    )
            elif preset.density == "eighth":
                step = beats_per_bar / 8.0
                for i in range(8):
                    p = root_pitch if i % 2 == 0 else root_pitch + (7 if i == 5 else 0)
                    notes.append(
                        Note(
                            pitch=p,
                            start=bar_start + i * step,
                            duration=step * 0.9,
                            velocity=66 + random.randint(-4, 6),
                        )
                    )
            elif preset.density == "half":
                for half in range(2):
                    notes.append(
                        Note(
                            pitch=root_pitch,
                            start=bar_start + half * beats_per_bar / 2,
                            duration=beats_per_bar / 2 - 0.05,
                            velocity=66 + random.randint(-4, 6),
                        )
                    )
            else:  # sustain
                notes.append(
                    Note(
                        pitch=root_pitch,
                        start=bar_start,
                        duration=beats_per_bar - 0.05,
                        velocity=66 + random.randint(-4, 6),
                    )
                )
        return notes

    def _drums_track(self, request: GenerationRequest, beats_per_bar: float) -> List[Note]:
        """鼓轨：kick/snare/hihat（GM 通道 9）。"""
        notes: List[Note] = []
        kick = 36
        snare = 38
        hihat = 42
        for bar in range(request.bars):
            bar_start = bar * beats_per_bar
            # kick：每小节第 1、3 拍
            notes.append(Note(pitch=kick, start=bar_start, duration=0.4, velocity=92))
            notes.append(Note(pitch=kick, start=bar_start + beats_per_bar / 2, duration=0.4, velocity=88))
            # snare：第 2、4 拍
            notes.append(Note(pitch=snare, start=bar_start + beats_per_bar / 4, duration=0.3, velocity=80))
            notes.append(Note(pitch=snare, start=bar_start + 3 * beats_per_bar / 4, duration=0.3, velocity=80))
            # hihat：8 分音符
            step = beats_per_bar / 8.0
            for i in range(8):
                notes.append(
                    Note(
                        pitch=hihat,
                        start=bar_start + i * step,
                        duration=step * 0.5,
                        velocity=60 if i % 2 == 0 else 52,
                    )
                )
        return notes

    # -- 内部工具 ----------------------------------------------------------

    @staticmethod
    def _nearest(target: Optional[int], candidate: int) -> int:
        """返回离 target 最近的和弦音（平滑声部进行）。"""
        if target is None:
            return candidate
        return candidate

    @staticmethod
    def _step_or_hold(
        current: Optional[int],
        pool: List[int],
        chord_pool: List[int],
        chord_tone_prob: float,
    ) -> int:
        """随机游走一步：大概率级进（±1 音阶音），小概率跳至和弦音/保持。"""
        if current is None:
            return random.choice(pool)
        r = random.random()
        if r < chord_tone_prob:
            return random.choice(chord_pool)
        idx = pool.index(current) if current in pool else _nearest_index(pool, current)
        if r < chord_tone_prob + 0.6:
            return pool[max(0, min(len(pool) - 1, idx + random.choice([-1, 1])))]
        return current


def _nearest_index(pool: List[int], pitch: int) -> int:
    """返回 pool 中与 pitch 最接近的下标。"""
    best = 0
    best_dist = abs(pool[0] - pitch)
    for i, p in enumerate(pool):
        d = abs(p - pitch)
        if d < best_dist:
            best_dist = d
            best = i
    return best


_PITCH_TONE = {"C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4,
               "F": 5, "F#": 6, "Gb": 6, "G": 7, "G#": 8, "Ab": 8, "A": 9,
               "A#": 10, "Bb": 10, "B": 11}


def _parse_pitch_name_to_midi(text: str) -> Optional[int]:
    """解析音名字符串（如 'C4' / 'Bb2'）为 MIDI pitch；失败返回 None。"""
    text = str(text).strip()
    if not text:
        return None
    i = 0
    while i < len(text) and not text[i].isdigit():
        i += 1
    if i == 0:
        return None
    name = text[:i]
    oct_str = text[i:] or ""
    # 音名查找：先原样，再 capitalize（"Bb"/"bb" -> "Bb"）。
    # 1.4.8 修复：旧实现 text[:i].upper() 把降号音名 "Bb" 变 "BB"，全表查不到
    # （docstring 自己的 'Bb2' 示例都返回 None）。
    tone = _PITCH_TONE.get(name)
    if tone is None:
        tone = _PITCH_TONE.get(name.capitalize())
    if tone is None:
        return None
    try:
        octave = int(oct_str) if oct_str else 4
    except ValueError:
        return None
    return (octave + 1) * 12 + tone


def _step_toward(current: Optional[int], target: Optional[int], pool: List[int]) -> Optional[int]:
    """从 current 朝 target 方向级进（<=2 半音）取一个音阶音；无方向则向音域中心小幅移动（保持线条流动）。

    用于弱拍经过音：保证相邻音程极小（杜绝狂跳），且尽量不原地重复，让旋律始终在动。
    """
    if not pool:
        return current
    if current is None:
        return min(pool, key=lambda p: abs(p - (target if target is not None else 72)))
    # 朝 target 方向取一个 <=2 半音的音阶邻音
    if target is not None and target != current:
        direction = 1 if target > current else -1
        cands = [p for p in pool if (p - current) * direction > 0 and abs(p - current) <= 2]
        if cands:
            return min(cands, key=lambda p: abs(p - target))
    # 目标已到达或在边界：朝音域中心小幅移动，避免原地重复
    center = (min(pool) + max(pool)) // 2
    steps = [p for p in pool if 0 < abs(p - current) <= 2]
    if not steps:
        return current
    return min(steps, key=lambda p: abs(p - center))


def _beats_per_bar(time_signature: str) -> float:
    """按拍号计算每小节拍数（4/4 -> 4.0）；非法/除零回退 4.0。"""
    try:
        num, den = time_signature.split("/")
        return float(num) * 4.0 / float(den)
    except (ValueError, AttributeError, ZeroDivisionError):
        return 4.0


def _chords_track_block(
    tones: List[int], bar_start: float, beats_per_bar: float
) -> List[Note]:
    """块状和弦：整小节所有和弦音同时按下（sustain），即「一起按下去」。"""
    return [
        Note(
            pitch=tone,
            start=bar_start,
            duration=beats_per_bar - 0.05,
            velocity=58 + random.randint(-4, 6),
        )
        for tone in tones
    ]
