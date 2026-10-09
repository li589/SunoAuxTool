"""生成器单元测试：程序化多轨 MIDI + music21 乐理旋律。

断言：
- 音符 pitch 均在指定调式音阶内
- 强拍/句尾目标音与和弦音对齐率 >= 80%
- 相同 seed 输出字节级一致；不同 seed 结果不同
"""

from __future__ import annotations

import random as _random
from pathlib import Path

import pytest

from sunoauxtool.generators.base import (
    GenerationRequest,
    resolve_scale_pitch_classes,
)
from sunoauxtool.generators.music21_melody import Music21MelodyGenerator
from sunoauxtool.generators.procedural import (
    ProceduralGenerator,
    _StylePreset,
    _beats_per_bar,
    _chords_track_block,
    _nearest_index,
    _parse_pitch_name_to_midi,
    _step_toward,
)
from sunoauxtool.models.chords import Chord, ChordProgression
from sunoauxtool.models.midi import MidiDocument


def _write_midi(seq, path: Path) -> Path:
    p = Path(MidiDocument.from_sequence(seq).write(path))
    return p


# ---------------------------------------------------------------------------
# 程序化 MIDI 生成
# ---------------------------------------------------------------------------

def test_procedural_three_tracks():
    """默认 3 轨：chords/melody/bass，乐器号与 pop 预设一致。"""
    gen = ProceduralGenerator(seed=1)
    seq = gen.generate(GenerationRequest(seed=1))
    assert seq.track_names == ["chords", "melody", "bass"]
    programs = {t.name: t.program for t in seq.tracks}
    assert programs["chords"] == 0   # 钢琴
    assert programs["melody"] == 81  # 合成主音
    assert programs["bass"] == 33    # 电贝斯
    assert seq.duration_seconds() == pytest.approx(8 * (60 / 120) * 4, abs=0.01)


def test_procedural_with_drums():
    """with_drums=True -> 第 4 轨鼓，channel 9。"""
    gen = ProceduralGenerator(seed=1)
    seq = gen.generate(GenerationRequest(seed=1, with_drums=True))
    assert len(seq.tracks) == 4
    drums = seq.tracks[-1]
    assert drums.name == "drums"
    assert drums.channel == 9
    assert all(36 <= n.pitch <= 42 for n in drums.notes)


def test_procedural_reproducible_same_seed(tmp_path):
    """相同 seed -> .mid 字节级一致。"""
    req = GenerationRequest(seed=42, chords="C-G-Am-F", bars=8)
    a = _write_midi(ProceduralGenerator(seed=42).generate(req), tmp_path / "a.mid")
    b = _write_midi(ProceduralGenerator(seed=42).generate(req), tmp_path / "b.mid")
    assert a.read_bytes() == b.read_bytes()


def test_procedural_different_seed_differs(tmp_path):
    """不同 seed -> 结果不同。"""
    req = GenerationRequest(seed=1, bars=8)
    a = _write_midi(ProceduralGenerator(seed=1).generate(req), tmp_path / "c.mid")
    b = _write_midi(ProceduralGenerator(seed=2).generate(req), tmp_path / "d.mid")
    assert a.read_bytes() != b.read_bytes()


def test_procedural_notes_in_scale():
    """旋律/和弦/贝斯音符均在调式音阶内。"""
    gen = ProceduralGenerator(seed=7)
    seq = gen.generate(GenerationRequest(seed=7, key="C major", chords="C-G-Am-F"))
    scale_pcs = set(resolve_scale_pitch_classes("C major"))
    for n in seq.notes:
        assert n.pitch % 12 in scale_pcs


# ---------------------------------------------------------------------------
# music21 乐理旋律生成
# ---------------------------------------------------------------------------

def test_melody_notes_in_scale():
    """music21 旋律音符全部落在调式音阶内。"""
    gen = Music21MelodyGenerator(seed=3)
    seq = gen.generate(GenerationRequest(seed=3, key="C major", chords="C-G-Am-F", bars=8))
    scale_pcs = set(resolve_scale_pitch_classes("C major"))
    melody_notes = [t.notes for t in seq.tracks if t.name == "melody"][0]
    for n in melody_notes:
        assert n.pitch % 12 in scale_pcs, f"pitch {n.pitch} 不在 C major 音阶内"


def test_melody_chord_tone_alignment():
    """强拍（第 1、3 拍）与句尾目标音对齐和弦音 >= 80%。"""
    gen = Music21MelodyGenerator(seed=3)
    request = GenerationRequest(seed=3, key="C major", chords="C-G-Am-F", bars=8)
    seq = gen.generate(request)
    prog = ChordProgression.parse(request.chords)
    melody_notes = [t.notes for t in seq.tracks if t.name == "melody"][0]

    checked = 0
    aligned = 0
    for i, n in enumerate(melody_notes):
        bar = int(n.start // 4)
        beat = round(n.start % 4)
        chord = prog.get_chord(bar)
        phrase_end = i == len(melody_notes) - 1
        if beat in (0, 2) or phrase_end:
            checked += 1
            if n.pitch % 12 in chord.chord_tones:
                aligned += 1
    assert checked > 0
    ratio = aligned / checked
    assert ratio >= 0.8, f"对齐率 {ratio:.2%} < 80%"


def test_melody_variations_produced():
    """--variations 3 -> 主旋律 + 3 个变奏轨。"""
    gen = Music21MelodyGenerator(seed=5)
    seq = gen.generate(GenerationRequest(seed=5, chords="C-G-Am-F", bars=8, variations=3))
    assert len(seq.tracks) == 4
    names = seq.track_names
    assert names[0] == "melody"
    assert any("var_rhythm" in n for n in names)
    assert any("var_ornament" in n for n in names)
    assert any("var_retrograde" in n for n in names)


def test_melody_variations_distinguishable():
    """变奏与主旋律音符序列不同（可辨识）。"""
    gen = Music21MelodyGenerator(seed=5)
    seq = gen.generate(GenerationRequest(seed=5, chords="C-G-Am-F", bars=8, variations=3))
    main = seq.tracks[0].notes
    for t in seq.tracks[1:]:
        assert [n.pitch for n in t.notes] != [n.pitch for n in main]


def test_melody_reproducible_same_seed(tmp_path):
    """music21 旋律同 seed -> 字节级一致。"""
    req = GenerationRequest(seed=9, chords="C-G-Am-F", bars=8, variations=2)
    a = _write_midi(Music21MelodyGenerator(seed=9).generate(req), tmp_path / "e.mid")
    b = _write_midi(Music21MelodyGenerator(seed=9).generate(req), tmp_path / "f.mid")
    assert a.read_bytes() == b.read_bytes()


# ---------------------------------------------------------------------------
# 1.4.8 F2：procedural.py 边界/辅助函数补测（81% -> 目标 95%+）
# ---------------------------------------------------------------------------


_HALF = _StylePreset(0, 0, 33, "half")
_BLOCK = _StylePreset(0, 0, 33, "block")
_SUSTAIN = _StylePreset(0, 81, 33, "sustain")


def test_chords_track_half_density():
    """half 密度：每半小节铺一次全和弦音，时长 = bpb/2 - 0.05。"""
    prog = ChordProgression.parse("C-Am")
    req = GenerationRequest(bars=2, key="C major")
    _random.seed(7)
    notes = ProceduralGenerator()._chords_track(prog, req, _HALF, 4.0)
    # C 和弦音在 48-71 有 6 个音，2 小节 x 2 半小节 x 6 = 24
    assert len(notes) == 24
    assert all(n.duration == pytest.approx(1.95) for n in notes)


def test_chords_track_block_density():
    """block 密度：整小节所有和弦音同时按下（start 相同）。"""
    prog = ChordProgression.parse("C-G")
    req = GenerationRequest(bars=2, key="C major")
    _random.seed(7)
    notes = ProceduralGenerator()._chords_track(prog, req, _BLOCK, 4.0)
    assert len(notes) == 12  # 6 音 x 2 小节
    starts = {n.start for n in notes}
    assert starts == {0.0, 4.0}


def test_chords_track_empty_tones_fallback():
    """和弦音全在音域外：回退到根音音级最近 C3 附近的单音。"""
    prog = ChordProgression(chords=[Chord(symbol="X", root_pc=7, chord_tones=[])], beats_per_chord=4.0)
    req = GenerationRequest(bars=1, key="C major")
    _random.seed(7)
    notes = ProceduralGenerator()._chords_track(prog, req, _SUSTAIN, 4.0)
    assert len(notes) == 1
    assert notes[0].pitch == 55  # 48 + (7-48) % 12


def test_chords_track_block_helper():
    notes = _chords_track_block([60, 64, 67], 8.0, 4.0)
    assert [n.pitch for n in notes] == [60, 64, 67]
    assert all(n.start == 8.0 and n.duration == pytest.approx(3.95) for n in notes)


@pytest.mark.parametrize("variation,expect_branch", [(0.9, "fast"), (0.1, "slow"), (0.4, "mid")])
def test_melody_variation_motif_branches(variation, expect_branch):
    """variation 三档动机选择分支（>0.6 / <0.25 / 中间）。"""
    prog = ChordProgression.parse("C-Am-F-G")
    req = GenerationRequest(
        bars=4, key="C major", seed=3,
        melody_profile={"variation_strength": variation},
    )
    _random.seed(3)
    notes = ProceduralGenerator()._melody_track(prog, req, _SUSTAIN, 4.0)
    assert notes
    assert all(60 <= n.pitch <= 83 for n in notes)


def test_melody_register_out_of_range_fallback():
    """register 音域内无音阶音：回退默认 C4-B5（pool/ctones 双回退路径）。"""
    prog = ChordProgression.parse("C-Am")
    req = GenerationRequest(
        bars=2, key="C major", seed=5,
        melody_profile={"register": "C#4-C#4"},
    )
    _random.seed(5)
    notes = ProceduralGenerator()._melody_track(prog, req, _SUSTAIN, 4.0)
    assert notes
    assert all(60 <= n.pitch <= 83 for n in notes)


def test_melody_contour_rotation_covers_all_contours():
    """16 小节轮换 arch/wave/ascend/descend 四种轮廓（相邻声部校正分支）。"""
    prog = ChordProgression.parse("C-Am-F-G")
    for seed in (1, 2, 3):
        req = GenerationRequest(bars=16, key="C major", seed=seed)
        _random.seed(seed)
        notes = ProceduralGenerator()._melody_track(prog, req, _SUSTAIN, 4.0)
        assert len(notes) > 30


def test_bass_eighth_and_half_density():
    """贝斯 eighth（第 5 个音头加五度）与 half（每半小节根音）两条分支。"""
    prog = ChordProgression.parse("C-G")
    req = GenerationRequest(bars=2, key="C major")
    _random.seed(11)
    eighth = ProceduralGenerator()._bass_track(prog, req, _StylePreset(25, 80, 34, "eighth"), 4.0, None)
    assert len(eighth) == 16  # 8 x 2 小节
    assert any(n.pitch - 36 in (7, 19) for n in eighth)  # 存在五度音
    half = ProceduralGenerator()._bass_track(prog, req, _HALF, 4.0, None)
    assert len(half) == 4  # 2 半小节 x 2 小节
    assert all(n.duration == pytest.approx(1.95) for n in half)


def test_nearest_helper():
    assert ProceduralGenerator._nearest(None, 60) == 60
    assert ProceduralGenerator._nearest(62, 60) == 60  # 恒返回 candidate（语义=直接采用）


def test_step_or_hold_all_branches():
    pool = list(range(60, 73))
    chord_pool = [60, 64, 67]
    _random.seed(42)
    # current=None -> 从 pool 随机起音
    assert ProceduralGenerator._step_or_hold(None, pool, chord_pool, 0.5) in pool
    seen_chord = seen_step = seen_hold = False
    for _ in range(500):
        r = ProceduralGenerator._step_or_hold(62, pool, chord_pool, 0.2)
        assert r in pool
        if r in chord_pool and r != 62:
            seen_chord = True
        elif r != 62:
            seen_step = True
        else:
            seen_hold = True
    # prob=0.2 时三件套（跳和弦/级进/保持）都应出现过
    assert seen_chord and seen_step and seen_hold


def test_nearest_index_takes_closest_first_on_tie():
    assert _nearest_index([60, 64, 67], 66) == 2
    assert _nearest_index([60, 64, 67], 61) == 0  # 60 与 64 等距，取先出现
    assert _nearest_index([60], 100) == 0


@pytest.mark.parametrize(
    "text,expect",
    [("C4", 60), ("c4", 60), ("Bb2", 46), ("bb2", 46), ("F#3", 54), ("Gb2", 42), ("C", 60),
     ("", None), ("4C", None), ("Xyz4", None), ("C2x", None)],
)
def test_parse_pitch_name_to_midi(text, expect):
    assert _parse_pitch_name_to_midi(text) == expect


def test_step_toward_edges():
    # 空 pool：返回 current（可能为 None）
    assert _step_toward(70, 64, []) == 70
    assert _step_toward(None, 64, []) is None
    # current=None：取 pool 中离 target 最近者
    assert _step_toward(None, 64, [60, 64, 67]) == 64
    assert _step_toward(None, None, [60, 64, 67]) == 67  # pool 内离 72（默认中心）最近
    # 正常级进：<=2 半音内取离 target 最近
    assert _step_toward(60, 64, [60, 61, 62, 63, 64]) == 62
    # 无邻音（pool 只有自身）：原地保持
    assert _step_toward(60, 64, [60]) == 60


def test_beats_per_bar_valid_and_fallback():
    assert _beats_per_bar("4/4") == 4.0
    assert _beats_per_bar("3/4") == 3.0
    assert _beats_per_bar("6/8") == 3.0
    assert _beats_per_bar("abc") == 4.0
    assert _beats_per_bar("4/0") == 4.0  # 除零防护（1.4.8 F2 补）
    assert _beats_per_bar("garbage/x") == 4.0
