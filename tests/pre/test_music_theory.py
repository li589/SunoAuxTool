"""P2-2 乐理规则单测：平行五度/八度检测、对位、转位、节奏型库。"""

from __future__ import annotations

import json

import pytest

from sunoauxtool.exceptions import ParameterError
from sunoauxtool.models.notes import Note, NoteSequence
from sunoauxtool.music_theory.counterpoint import CounterpointEngine
from sunoauxtool.music_theory.inversion import InversionResolver
from sunoauxtool.music_theory.rhythm_patterns import RhythmPatternRegistry
from sunoauxtool.music_theory.voice_leading import VoiceLeadingChecker


# ---------------------------------------------------------------------------
# 节奏型库（P2-2 验收 4）
# ---------------------------------------------------------------------------

def test_builtin_patterns_at_least_six():
    """内置节奏型 >= 6 种。"""
    reg = RhythmPatternRegistry()
    assert len(reg.BUILTIN) >= 6
    for name in ("pop", "rock", "electronic", "classical", "waltz", "funk"):
        pat = reg.get(name)
        assert pat.grid
        assert all(g in (0, 1) for g in pat.grid)


def test_get_unknown_pattern_raises():
    """未知节奏型 -> ParameterError(1)。"""
    with pytest.raises(ParameterError) as exc:
        RhythmPatternRegistry().get("nope")
    assert exc.value.code == 1


def test_from_string():
    """自定义节奏型字符串 '10010010'。"""
    pat = RhythmPatternRegistry.from_string("10010010")
    assert pat.grid == (1, 0, 0, 1, 0, 0, 1, 0)


def test_from_string_invalid():
    """非法字符串 -> ParameterError(1)。"""
    with pytest.raises(ParameterError):
        RhythmPatternRegistry.from_string("abc")


def test_from_json(tmp_path):
    """自定义节奏型 JSON 注册。"""
    p = tmp_path / "mygroove.json"
    p.write_text(json.dumps({"name": "mygroove", "grid": [1, 0, 1, 0, 1, 1, 0, 0],
                             "style_tags": ["pop"]}), encoding="utf-8")
    pat = RhythmPatternRegistry.from_json(p)
    assert pat.name == "mygroove"
    assert pat.grid == (1, 0, 1, 0, 1, 1, 0, 0)


def test_extra_patterns_injected():
    """extra_patterns 注入后可被 get。"""
    from sunoauxtool.music_theory import RhythmPattern

    extra = RhythmPattern("custom1", (1, 0, 0, 0, 0, 0, 0, 0), ("x",))
    reg = RhythmPatternRegistry(extra_patterns=[extra])
    assert reg.get("custom1").name == "custom1"


def test_onsets_in_bar():
    """onets 按拍换算正确（pop 网格 8 半拍 -> 步长 0.5 拍）。"""
    pat = RhythmPatternRegistry().get("pop")
    onsets = pat.onsets_in_bar(4.0)
    assert onsets == [0.0, 1.0, 2.0, 2.5]


def test_pattern_density_high():
    """密度高 -> eighth。"""
    from sunoauxtool.music_theory import RhythmPattern
    pat = RhythmPattern("dense", (1, 1, 1, 1, 1, 1, 1, 1))
    assert pat.density == "eighth"


def test_pattern_density_medium():
    """密度中等 -> half。"""
    from sunoauxtool.music_theory import RhythmPattern
    pat = RhythmPattern("mid", (1, 0, 1, 0, 1, 0, 0, 0))
    assert pat.density == "half"


def test_pattern_density_low():
    """密度低 -> sustain。"""
    from sunoauxtool.music_theory import RhythmPattern
    pat = RhythmPattern("sparse", (1, 0, 0, 0, 0, 0, 0, 0))
    assert pat.density == "sustain"


def test_pattern_density_empty():
    """空网格 -> sustain。"""
    from sunoauxtool.music_theory import RhythmPattern
    pat = RhythmPattern("empty", ())
    assert pat.density == "sustain"


def test_onsets_in_bar_empty_grid():
    """空网格 onets 为空列表。"""
    from sunoauxtool.music_theory import RhythmPattern
    pat = RhythmPattern("empty", ())
    assert pat.onsets_in_bar(4.0) == []


def test_from_json_missing_file():
    """from_json 不存在的文件 -> InputFileError(3)。"""
    from sunoauxtool.exceptions import InputFileError
    with pytest.raises(InputFileError):
        RhythmPatternRegistry.from_json("/nonexistent/x.json")


def test_from_json_invalid_json(tmp_path):
    """from_json 非法 JSON -> ParameterError(1)。"""
    p = tmp_path / "bad.json"
    p.write_text("{invalid", encoding="utf-8")
    with pytest.raises(ParameterError):
        RhythmPatternRegistry.from_json(p)


def test_from_json_invalid_grid(tmp_path):
    """from_json grid 含非 0/1 -> ParameterError(1)。"""
    p = tmp_path / "badgrid.json"
    p.write_text('{"name": "x", "grid": [1, 2, 3]}', encoding="utf-8")
    with pytest.raises(ParameterError):
        RhythmPatternRegistry.from_json(p)


def test_from_json_empty_grid(tmp_path):
    """from_json grid 为空 -> ParameterError(1)。"""
    p = tmp_path / "emptygrid.json"
    p.write_text('{"name": "x", "grid": []}', encoding="utf-8")
    with pytest.raises(ParameterError):
        RhythmPatternRegistry.from_json(p)


def test_from_string_empty():
    """from_string 空/无 0/1 -> ParameterError(1)。"""
    with pytest.raises(ParameterError):
        RhythmPatternRegistry.from_string("")


def test_names_includes_custom():
    """names() 包含自定义节奏型。"""
    from sunoauxtool.music_theory import RhythmPattern
    extra = RhythmPattern("myx", (1, 0, 0, 0, 0, 0, 0, 0))
    reg = RhythmPatternRegistry(extra_patterns=[extra])
    assert "myx" in reg.names()
    assert "pop" in reg.names()


# ---------------------------------------------------------------------------
# 平行五度/八度检测（P2-2 验收 1）
# ---------------------------------------------------------------------------

def _two_voice_seq(voice_a, voice_b):
    """构造双声部序列（每拍一个音符）。"""
    seq = NoteSequence(bpm=120, bars=2, time_signature="4/4")
    seq.add_track("upper", 73, 1, [Note(pitch=p, start=i, duration=1.0, velocity=70)
                                   for i, p in enumerate(voice_a)])
    seq.add_track("lower", 43, 2, [Note(pitch=p, start=i, duration=1.0, velocity=70)
                                   for i, p in enumerate(voice_b)])
    return seq


def test_detect_parallel_fifths():
    """构造平行五度输入可被检出（P2-2 验收 1）。"""
    # upper: C5(72) -> D5(74)   lower: F3(53) -> G3(55)：音程恒为 19 个半音（P5+八度）
    seq = _two_voice_seq([72, 74, 72, 74], [53, 55, 53, 55])
    violations = VoiceLeadingChecker().detect_parallel_fifths_octaves(seq)
    assert len(violations) >= 1
    assert violations[0].kind == "parallel_fifth_or_octave"


def test_detect_parallel_octaves():
    """平行八度检出。"""
    seq = _two_voice_seq([72, 74, 72, 74], [60, 62, 60, 62])  # 恒为八度（12）
    violations = VoiceLeadingChecker().detect_parallel_fifths_octaves(seq)
    assert len(violations) >= 1


def test_no_violation_for_contrary_motion():
    """反向进行不误报。"""
    seq = _two_voice_seq([72, 74, 72, 74], [55, 53, 55, 53])  # 反方向
    violations = VoiceLeadingChecker().detect_parallel_fifths_octaves(seq)
    assert len(violations) == 0


def test_detect_crossing():
    """声部交叉检出。"""
    # upper 轨（按平均音域判定的高音轨）在拍 0 低于 lower 轨 -> 交叉
    seq = _two_voice_seq([55, 55, 55, 55], [60, 48, 60, 48])
    crossings = VoiceLeadingChecker().detect_crossing(seq)
    assert len(crossings) >= 1
    assert crossings[0].kind == "voice_crossing"


# ---------------------------------------------------------------------------
# 二声部对位（P2-2 验收 2）
# ---------------------------------------------------------------------------

def test_counterpoint_enforce_consonant():
    """开启对位后，强拍音程 ∈ 协和允许集合。"""
    seq = _two_voice_seq([60, 61, 60, 61, 60, 61, 60, 61], [48, 48, 48, 48, 48, 48, 48, 48])
    # 初始强拍音程：60-48=12（八度，协和）；但 beat1/3/5/7 为二度（不协和）
    out = CounterpointEngine(strictness=1).enforce(seq)
    upper = next(t for t in out.tracks if t.name == "upper")
    lower = next(t for t in out.tracks if t.name == "lower")
    allowed = {0, 3, 4, 7, 8, 9, 12}
    low_map = {round(n.start): n.pitch for n in lower.notes}
    for n in upper.notes:
        beat = round(n.start)
        if beat % 4 in (0, 2):  # 强拍
            assert abs(n.pitch - low_map[beat]) % 12 in allowed


def test_counterpoint_invalid_strictness():
    """strictness 越界 -> ParameterError(1)。"""
    with pytest.raises(ParameterError):
        CounterpointEngine(strictness=5)


def test_counterpoint_single_track_noop():
    """单轨序列不触发对位（原样返回）。"""
    seq = NoteSequence(bpm=120, bars=2)
    seq.add_track("melody", 73, 1, [Note(pitch=60, start=0, duration=4.0, velocity=70)])
    out = CounterpointEngine(strictness=1).enforce(seq)
    assert len(out.tracks) == 1
    assert out.tracks[0].notes[0].pitch == 60


def test_counterpoint_drums_only_noop():
    """仅鼓轨时不触发对位（channel 9 跳过）。"""
    seq = NoteSequence(bpm=120, bars=2)
    seq.add_track("drums", 0, 9, [Note(pitch=36, start=0, duration=1.0, velocity=60)])
    out = CounterpointEngine(strictness=1).enforce(seq)
    assert len(out.tracks) == 1


def test_counterpoint_strictness_2_allowed():
    """strictness=2 允许小六度（8 semitones）。"""
    seq = _two_voice_seq([60, 68, 60, 68, 60, 68, 60, 68], [48, 48, 48, 48, 48, 48, 48, 48])
    # 60-48=12（八度）OK；68-48=20→8（小六度，strictness=2 允许）
    out = CounterpointEngine(strictness=2).enforce(seq)
    upper = next(t for t in out.tracks if t.name == "upper")
    assert len(upper.notes) > 0


def test_counterpoint_same_pitch_range_noop():
    """上下声部音域相同（lower is upper）时不调整。"""
    seq = _two_voice_seq([60, 60, 60, 60], [60, 60, 60, 60])
    out = CounterpointEngine(strictness=1).enforce(seq)
    upper = next(t for t in out.tracks if t.name == "upper")
    # 音域相同 -> 不触发调整，所有音符保持原样
    assert all(n.pitch == 60 for n in upper.notes)


def test_counterpoint_nearest_consonant():
    """_nearest_consonant 就近找协和音。"""
    from sunoauxtool.music_theory.counterpoint import CONSONANT_SETS
    engine = CounterpointEngine(strictness=1)
    allowed = CONSONANT_SETS[1]
    # bass=48(C3)，pitch=61(C#4) 二度不协和 -> 就近找协和（C4=60）
    result = engine._nearest_consonant(61, 48, allowed)
    assert abs(result - 48) % 12 in allowed
    assert result == 60  # 就近：60 与 48 差 12（八度，协和）


def test_counterpoint_nearest_consonant_boundary():
    """_nearest_consonant 在音域边界不越界。"""
    from sunoauxtool.music_theory.counterpoint import CONSONANT_SETS
    engine = CounterpointEngine(strictness=1)
    allowed = CONSONANT_SETS[1]
    # 极高音：127 附近找协和，不越界
    result = engine._nearest_consonant(127, 60, allowed)
    assert 0 <= result <= 127
    # 极低音：0 附近找协和，不越界
    result2 = engine._nearest_consonant(0, 60, allowed)
    assert 0 <= result2 <= 127


# ---------------------------------------------------------------------------
# 和弦转位（P2-2 验收 3）
# ---------------------------------------------------------------------------

def _chord_seq():
    """构造两小节 C 大调 -> F 大调和弦轨（根音位置），bass 变化大。"""
    seq = NoteSequence(bpm=120, bars=2, time_signature="4/4")
    seq.add_track(
        "chords", 0, 0,
        [Note(pitch=48 + t, start=bar * 4, duration=4.0, velocity=60)
         for bar, tones in enumerate(((0, 4, 7), (5, 9, 0))) for t in tones],
    )
    seq.add_track("melody", 73, 1, [Note(pitch=72, start=0, duration=4.0, velocity=70)])
    return seq


def test_inversion_smoother_bass():
    """转位后低音声部相邻音程差 <= 未开启基线，且和弦功能不变。"""
    baseline = _chord_seq()
    resolved = InversionResolver().resolve(_chord_seq())

    def bass_pitches(seq):
        chords = next(t for t in seq.tracks if t.name == "chords")
        # 每小节最低音
        bars = {}
        for n in chords.notes:
            bar = int(n.start // 4)
            bars.setdefault(bar, n.pitch)
            bars[bar] = min(bars[bar], n.pitch)
        return [bars[b] for b in sorted(bars)]

    base_bass = bass_pitches(baseline)   # C2(48) -> F2(53)：移动 5
    new_bass = bass_pitches(resolved)    # 转位选择使移动最小
    assert sum(abs(b - a) for a, b in zip(base_bass, base_bass[1:])) >= sum(
        abs(a - b) for a, b in zip(new_bass, new_bass[1:])
    )
    # 和弦功能不变：每小节音级集合与原始一致
    def pcs(seq):
        chords = next(t for t in seq.tracks if t.name == "chords")
        by_bar = {}
        for n in chords.notes:
            by_bar.setdefault(int(n.start // 4), set()).add(n.pitch % 12)
        return by_bar

    for bar in range(2):
        assert pcs(resolved)[bar] == pcs(baseline)[bar]
