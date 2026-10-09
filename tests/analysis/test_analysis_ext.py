"""分析子系统扩展（R13）测试：调性 / 和弦 / 结构。

全部用**合成素材**（正弦叠加），numpy-only，不依赖 librosa / 真实录音。
"""

from __future__ import annotations

import numpy as np
import pytest

from sunoauxtool.analysis.chords import (
    ChordSegment,
    chord_templates,
    chromagram,
    estimate_chords,
)
from sunoauxtool.analysis.key import (
    KeyEstimate,
    chroma_from_mono,
    chroma_from_pitches,
    estimate_key,
    estimate_key_from_chroma,
)
from sunoauxtool.analysis.structure import (
    foote_novelty,
    pick_peaks,
    self_similarity,
    estimate_structure,
)

SR = 44100

# 音高（Hz）
C4, E4, G4 = 261.63, 329.63, 392.00
A3, C4n, E4n = 220.00, 261.63, 329.63
G3, B3, D4 = 196.00, 246.94, 293.66
FS4, AS4, CS5 = 369.99, 466.16, 554.37  # 远关系调（对比段用）


def _tone(freq: float, dur: float, amp: float = 0.5, sr: int = SR) -> np.ndarray:
    t = np.arange(int(sr * dur), dtype=np.float64) / sr
    return amp * np.sin(2.0 * np.pi * freq * t)


def _chord(freqs, dur: float, sr: int = SR) -> np.ndarray:
    parts = [_tone(f, dur, 1.0 / len(freqs), sr) for f in freqs]
    return sum(parts)


# ---------------------------------------------------------------------------
# key
# ---------------------------------------------------------------------------


def test_chroma_from_mono_cmajor():
    sig = _chord([C4, E4, G4], 2.0)
    chroma = chroma_from_mono(sig, SR)
    assert chroma.shape == (12,)
    assert abs(chroma.sum() - 1.0) < 1e-9
    # C(0) / E(4) / G(7) 三个音级应显著
    for pc in (0, 4, 7):
        assert chroma[pc] > 0.15, f"音级 {pc} 能量偏低: {chroma}"


def test_estimate_key_cmajor():
    est = estimate_key(_chord([C4, E4, G4], 2.0), SR)
    assert isinstance(est, KeyEstimate)
    assert est.key == "C"
    assert est.mode == "major"
    assert est.confidence > 0.5


def test_estimate_key_aminor():
    est = estimate_key(_chord([A3, C4n, E4n], 2.0), SR)
    assert est.key == "A"
    assert est.mode == "minor"
    assert est.confidence > 0.5


def test_estimate_key_silence_zero_confidence():
    est = estimate_key(np.zeros(SR, dtype=np.float64), SR)
    assert est.confidence == 0.0


def test_estimate_key_from_chroma_requires_energy():
    est = estimate_key_from_chroma(np.zeros(12))
    assert est.confidence == 0.0


def test_chroma_from_pitches():
    chroma = chroma_from_pitches([60, 64, 67])  # C4 E4 G4
    assert chroma[0] == pytest.approx(1 / 3)
    assert chroma[4] == pytest.approx(1 / 3)
    assert chroma.sum() == pytest.approx(1.0)
    assert chroma_from_pitches([]).sum() == 0.0


def test_chroma_from_pitches_weighted():
    chroma = chroma_from_pitches([60, 64], weights=[3.0, 1.0])
    assert chroma[0] == pytest.approx(0.75)
    assert chroma[4] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# chords
# ---------------------------------------------------------------------------


def test_chord_templates_shape():
    tmpl, labels = chord_templates()
    # 12 根音 × 4 质量
    assert tmpl.shape == (48, 12)
    assert len(labels) == 48
    assert labels[0] == (0, "maj")
    assert tmpl[0][[0, 4, 7]].sum() == 3.0


def test_chromagram_shape_and_hop():
    sig = _chord([C4, E4, G4], 2.0)
    gram, hop_sec = chromagram(sig, SR, win_s=0.5, hop_s=0.25)
    # (2.0 - 0.5) / 0.25 + 1 = 7 窗
    assert gram.shape[0] == 7
    assert gram.shape[1] == 12
    assert hop_sec == pytest.approx(0.25)


def test_chromagram_too_short_returns_empty():
    gram, _ = chromagram(np.zeros(100), SR, win_s=0.5, hop_s=0.25)
    assert gram.shape == (0, 12)


def test_estimate_chords_progression():
    """Cmaj(1s) → Amin(1s) → Gmaj(1s)：首段应为 Cmaj，且出现 Amin。"""
    sig = np.concatenate(
        [
            _chord([C4, E4, G4], 1.0),
            _chord([A3, C4n, E4n], 1.0),
            _chord([G3, B3, D4], 1.0),
        ]
    )
    segs = estimate_chords(sig, SR, win_s=0.5, hop_s=0.25)
    assert isinstance(segs[0], ChordSegment)
    assert segs[0].root == "C"
    assert segs[0].quality == "maj"
    labels = [s.label for s in segs]
    assert "Amin" in labels, f"实际进行: {labels}"


def test_estimate_chords_silence_yields_nothing():
    assert estimate_chords(np.zeros(SR), SR) == []


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------


def test_self_similarity_diagonal_is_one():
    gram, _ = chromagram(_chord([C4, E4, G4], 2.0), SR)
    ssm = self_similarity(gram)
    assert np.allclose(np.diag(ssm), 1.0, atol=1e-9)
    assert ssm.shape[0] == ssm.shape[1]


def test_foote_novelty_length_and_nonneg_peak():
    gram, _ = chromagram(_chord([C4, E4, G4], 2.0), SR)
    nov = foote_novelty(self_similarity(gram), k=4)
    assert nov.shape == (gram.shape[0],)
    assert nov.size > 0


def test_pick_peaks_respects_min_distance():
    x = np.array([0.0, 0.0, 5.0, 0.0, 4.0, 0.0, 0.0, 9.0, 0.0])
    peaks = pick_peaks(x, min_distance=3, threshold_scale=0.5)
    assert 7 in peaks  # 最强峰必在
    assert all(abs(a - b) >= 3 for a, b in zip(peaks, peaks[1:]))


def test_pick_peaks_empty():
    assert pick_peaks(np.zeros(0)) == []


def test_estimate_structure_contrasting_sections():
    """A(4s) → B(4s, 远关系调) → A(4s)：至少切出 2 段。"""
    sig = np.concatenate(
        [
            _chord([C4, E4, G4], 4.0),
            _chord([FS4, AS4, CS5], 4.0),
            _chord([C4, E4, G4], 4.0),
        ]
    )
    segs = estimate_structure(sig, SR, win_s=0.5, hop_s=0.25, min_section_s=4.0)
    assert len(segs) >= 2, f"分段过少: {segs}"
    assert segs[0].start == 0.0
    assert segs[-1].end > 0.0
    assert all(s.end > s.start for s in segs)


def test_estimate_structure_too_short_returns_empty():
    assert estimate_structure(np.zeros(1000), SR) == []


# ---------------------------------------------------------------------------
# CLI: analyze
# ---------------------------------------------------------------------------


def _write_wav(tmp_path, sig, name="a.wav"):
    import soundfile as sf

    p = tmp_path / name
    sf.write(str(p), np.asarray(sig, dtype=np.float32), SR)
    return p


def test_cli_analyze_all_writes_json(tmp_path):
    import json

    from typer.testing import CliRunner

    from sunoauxtool.cli import app

    wav = _write_wav(tmp_path, _chord([C4, E4, G4], 2.0))
    out_json = tmp_path / "a.json"
    result = CliRunner().invoke(app, ["analyze", str(wav), "--json", str(out_json)])

    assert result.exit_code == 0, result.output
    assert "调性" in result.output and "和弦" in result.output and "结构" in result.output
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["key"]["key"] == "C"
    assert data["sample_rate"] == SR
    assert isinstance(data["chords"], list)
    assert isinstance(data["structure"], list)


def test_cli_analyze_key_only(tmp_path):
    from typer.testing import CliRunner

    from sunoauxtool.cli import app

    wav = _write_wav(tmp_path, _chord([C4, E4, G4], 2.0))
    result = CliRunner().invoke(app, ["analyze", str(wav), "--key"])

    assert result.exit_code == 0, result.output
    assert "调性" in result.output
    assert "和弦" not in result.output  # 只跑调性


def test_cli_analyze_missing_file_exit_3(tmp_path):
    from typer.testing import CliRunner

    from sunoauxtool.cli import app

    result = CliRunner().invoke(app, ["analyze", str(tmp_path / "nope.wav")])
    assert result.exit_code == 3
