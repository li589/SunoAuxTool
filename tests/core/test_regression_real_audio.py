# -*- coding: utf-8 -*-
"""真实引擎音频回归语料（1.5.5，批次 #4）。

链路：程序化真值 MIDI → 真 FluidSynth + GeneralUser GS 渲染 → tempo/transcribe
无回归断言。区别于 conftest 的 mock_fluidsynth / 合成正弦 fixture，本文件把
「真实 SF2 音色 + 渲染子进程 + 分析链」整体纳入回归保护。

语料基线（2026-09-22 实测：win fluidsynth 2.x + GeneralUser-GS，gain 0.5）：
- tempo: 119.94 / 89.94 BPM（绝对误差 ≤ 0.1），拍相位误差 ≤ 8ms，置信度 ≥ 0.87
- transcribe: ±2 半音容差多重集合匹配 100%，检出音符数 truth ~ truth+11

断言阈值 = 实测基线 × 数倍余量，目的在于捕捉回归而非精确量化精度；
已知边界：内置转谱后端在真实钢琴音色上无容差直接匹配仅 ~50%（谐波重叠所致），
因此音高断言采用 ±2 半音容差匹配。环境不完整（fluidsynth 缺失 / SF2 为
LFS 指针未拉实体）时整文件 skip，CI（apt fluidsynth + git lfs pull sf2）可跑。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sunoauxtool.analysis.tempo import estimate_bpm
from sunoauxtool.analysis.transcribe import TranscribeOptions, transcribe_wav
from sunoauxtool.render.fluidsynth import FluidSynthRenderer

ROOT = Path(__file__).resolve().parents[2]
SF2_REL = Path("module/GeneralUser_GS/GeneralUser-GS/GeneralUser-GS.sf2")

# 语料定义：name -> (bpm, 真值音高序列；第 i 个音落在第 i 拍，四分音符断奏)
CORPUS: dict[str, tuple[float, list[int]]] = {
    "a120": (120.0, [60, 62, 64, 65, 67, 69, 71, 72, 71, 69, 67, 65, 64, 62, 60, 60]),
    "b090": (90.0, [57, 60, 64, 69, 67, 64, 60, 57, 55, 59, 62, 64]),
}


def _write_melody_midi(path: Path, bpm: float, pitches: list[int]) -> None:
    """单旋律真值 MIDI：第 i 个音从第 i 拍开始，0.5 拍断奏（留间隙降谐波重叠）。"""
    import pretty_midi

    pm = pretty_midi.PrettyMIDI(initial_tempo=bpm)
    inst = pretty_midi.Instrument(program=0, name="melody")
    beat = 60.0 / bpm
    for i, p in enumerate(pitches):
        start = i * beat
        inst.notes.append(
            pretty_midi.Note(velocity=90, pitch=p, start=start, end=start + 0.5 * beat)
        )
    pm.instruments.append(inst)
    pm.write(str(path))


@pytest.fixture(scope="session")
def real_engine():
    """定位真实渲染环境：module 内置二进制优先，其次 PATH；SF2 须为 RIFF 实体。"""
    sf2 = ROOT / SF2_REL
    if not sf2.is_file():
        pytest.skip("SoundFont 缺失（module/GeneralUser_GS 未就位）")
    if sf2.read_bytes()[:4] != b"RIFF":
        pytest.skip("SoundFont 是 Git LFS 指针文件（实体未拉取）")
    exe = ROOT / "module/fluidsynth/bin/fluidsynth.exe"
    if exe.is_file():
        fs = str(exe)
    else:
        fs = shutil.which("fluidsynth")
        if fs is None:
            pytest.skip("fluidsynth 不可用（module/ 与 PATH 均无）")
    return fs, str(sf2)


@pytest.fixture(scope="session")
def real_corpus(real_engine, tmp_path_factory):
    """渲染整个语料库（session 级，两段共约 22s 音频，渲染 <2s）。"""
    fs, sf2 = real_engine
    out_dir = tmp_path_factory.mktemp("real_corpus")
    renderer = FluidSynthRenderer(fluidsynth_path=fs, gain=0.5, project_root=ROOT)
    corpus: dict[str, dict] = {}
    for name, (bpm, pitches) in CORPUS.items():
        midi = out_dir / f"{name}.mid"
        wav = out_dir / f"{name}.wav"
        _write_melody_midi(midi, bpm, pitches)
        renderer.render(str(midi), sf2, str(wav))
        corpus[name] = {"wav": wav, "bpm": bpm, "pitches": pitches}
    return corpus


def _tolerant_match(det: list[int], truth: list[int], tol: int) -> int:
    """容差多重集合匹配：每个检出音最多消费一个 |det-truth|<=tol 的真值音。"""
    used = [False] * len(truth)
    matched = 0
    for d in det:
        best = None
        for i, t in enumerate(truth):
            if not used[i] and abs(d - t) <= tol and (best is None or abs(d - t) < abs(d - truth[best])):
                best = i
        if best is not None:
            used[best] = True
            matched += 1
    return matched


class TestTempoRealEngine:
    """真渲染音频上的测速回归：基线误差 ≤0.1 BPM / 8ms，阈值放宽数倍。"""

    @pytest.mark.parametrize("name", list(CORPUS))
    def test_bpm_accuracy(self, real_corpus, name):
        case = real_corpus[name]
        est = estimate_bpm(str(case["wav"]))
        assert abs(est.bpm - case["bpm"]) <= max(0.6, case["bpm"] * 0.005)
        assert est.confidence >= 0.5

    @pytest.mark.parametrize("name", list(CORPUS))
    def test_beat_phase(self, real_corpus, name):
        """真值第一拍在 t=0；相位误差对拍周期取模后应远小于半拍。"""
        case = real_corpus[name]
        est = estimate_bpm(str(case["wav"]))
        period = 60.0 / case["bpm"]
        err = abs((est.beat_offset + period / 2) % period - period / 2)
        assert err <= 0.03, f"拍相位误差 {err * 1000:.1f}ms 超出 30ms 基线余量"


class TestTranscribeRealEngine:
    """真渲染音频上的转谱回归。

    内置后端在真实钢琴音色上无容差直配仅 ~50%（谐波重叠），因此：
    - 音高断言用 ±2 半音容差匹配（基线 100%）；
    - 数量断言允许 spurious 拆分（基线 truth ~ truth+11）；
    - 网格与测速一致性按领域模型（Note.start 单位为拍）断言。
    """

    @pytest.mark.parametrize("name", list(CORPUS))
    def test_pitch_tolerance_match(self, real_corpus, name):
        case = real_corpus[name]
        res = transcribe_wav(str(case["wav"]), TranscribeOptions(grid="1/8"))
        det = [n.pitch for n in res.seq.notes]
        matched = _tolerant_match(det, case["pitches"], tol=2)
        assert matched >= 0.9 * len(case["pitches"]), (
            f"±2 半音容差匹配 {matched}/{len(case['pitches'])} 低于 90% 基线余量；"
            f"det={det}"
        )

    @pytest.mark.parametrize("name", list(CORPUS))
    def test_note_count_bounds(self, real_corpus, name):
        case = real_corpus[name]
        res = transcribe_wav(str(case["wav"]), TranscribeOptions(grid="1/8"))
        n = len(res.seq.notes)
        truth_n = len(case["pitches"])
        assert truth_n <= n <= truth_n + 12, f"检出音符数 {n} 超出基线区间 [{truth_n}, {truth_n + 12}]"

    @pytest.mark.parametrize("name", list(CORPUS))
    def test_onset_grid_alignment(self, real_corpus, name):
        """量化网格 1/8 拍：所有音符起始拍须落在 0.5 拍倍数上。"""
        case = real_corpus[name]
        res = transcribe_wav(str(case["wav"]), TranscribeOptions(grid="1/8"))
        assert res.seq.notes, "转谱结果为空"
        for note in res.seq.notes:
            cells = note.start / 0.5
            assert abs(cells - round(cells)) < 1e-6, (
                f"音符起始拍 {note.start} 未对齐 1/8 拍网格"
            )

    @pytest.mark.parametrize("name", list(CORPUS))
    def test_detected_bpm_consistent(self, real_corpus, name):
        """转谱内部复用的测速结果须与独立测速一致（同一素材）。"""
        case = real_corpus[name]
        res = transcribe_wav(str(case["wav"]), TranscribeOptions(grid="1/8"))
        assert abs(res.detected_bpm - case["bpm"]) <= max(0.6, case["bpm"] * 0.005)
