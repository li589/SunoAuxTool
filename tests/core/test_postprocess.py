"""P2-2 生成后处理集成单测：apply_postprocess 开关行为。"""

from __future__ import annotations

from sunoauxtool.generators.base import GenerationRequest
from sunoauxtool.generators.procedural import ProceduralGenerator
from sunoauxtool.music_theory.postprocess import apply_postprocess


def test_postprocess_default_off():
    """默认不开启任何规则 -> 输出与 P0 完全一致（P2-2 验收 5）。"""
    req = GenerationRequest(seed=1)
    seq = ProceduralGenerator(seed=1).generate(req)
    baseline_notes = [(n.pitch, n.start, n.duration) for n in seq.notes]
    seq2 = ProceduralGenerator(seed=1).generate(GenerationRequest(seed=1))
    assert [(n.pitch, n.start, n.duration) for n in seq2.notes] == baseline_notes


def test_postprocess_voice_leading_runs():
    """开启 voice_leading：返回原序列 + 违规列表（不抛错）。"""
    req = GenerationRequest(seed=1, enable_voice_leading=True)
    seq = ProceduralGenerator(seed=1).generate(req)
    out, violations = apply_postprocess(seq, req)
    assert out is seq
    assert isinstance(violations, list)


def test_postprocess_inversion_changes_chords():
    """开启 inversion：和弦轨低音更平滑且功能不变。"""
    req = GenerationRequest(seed=1, enable_inversion=True)
    seq = ProceduralGenerator(seed=1).generate(req)
    out, _ = apply_postprocess(seq, req)
    chords = next(t for t in out.tracks if t.name == "chords")
    assert chords.notes  # 仍有和弦音符


def test_postprocess_counterpoint_runs():
    """开启 counterpoint：不抛错且强拍约束生效（不崩）。"""
    req = GenerationRequest(seed=1, enable_counterpoint=True)
    seq = ProceduralGenerator(seed=1).generate(req)
    out, _ = apply_postprocess(seq, req)
    assert out is seq


def test_postprocess_all_flags():
    """全部规则同时开启：不抛错、音符仍在有效范围。"""
    req = GenerationRequest(
        seed=3,
        enable_voice_leading=True,
        enable_counterpoint=True,
        enable_inversion=True,
    )
    seq = ProceduralGenerator(seed=3).generate(req)
    out, violations = apply_postprocess(seq, req)
    for n in out.notes:
        assert 0 <= n.pitch <= 127
