"""``analysis.transcribe`` WAV→MIDI 转谱测试（#13）。

真值来源：合成旋律（正弦 + 弱 2/3 次谐波，10ms 起音 / 50ms 收尾）。
已实测并固化下来的边界（写新断言前先读模块文档）：
- 音高判断用谐波 salience + 八度鬼影扣除，C5-C6 旋律应逐音命中；
- 时间戳取帧中心（2048/256），量化后起拍应精确落在网格上；
- 重复同音靠 salience 相对凹谷切分（谷到两侧平台的 70% 以下）；
- **末音收尾按能量衰减判定，50ms 淡出素材的末音时长会略短（0.75 拍）**——
  这是 STFT 只能「看见」窗内能量的固有边界，不是缺陷。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from sunoauxtool.analysis.transcribe import (
    TranscribeOptions,
    parse_grid,
    transcribe_wav,
    write_transcribed_midi,
)
from sunoauxtool.exceptions import InputFileError, ParameterError
from sunoauxtool.models.midi import MidiDocument

SR = 22050
DUR = 0.5  # 每音 0.5s；显式 bpm=120 时 = 每音 1 拍


def write_melody_wav(path: Path, pitches: list[int], sr: int = SR) -> None:
    """首尾相接的旋律（每音 10ms 起音 / 50ms 收尾，含弱 2/3 次谐波锚定基频）。"""
    chunks = []
    for p in pitches:
        f = 440.0 * 2.0 ** ((p - 69) / 12.0)
        t = np.arange(int(DUR * sr)) / sr
        env = np.minimum(1.0, np.minimum(t / 0.01, (DUR - t) / 0.05))
        tone = (
            0.5 * np.sin(2 * np.pi * f * t)
            + 0.15 * np.sin(2 * np.pi * 2 * f * t)
            + 0.07 * np.sin(2 * np.pi * 3 * f * t)
        )
        chunks.append(tone * env)
    sf.write(path, np.concatenate(chunks).astype(np.float32), sr, subtype="PCM_16")


@pytest.fixture()
def melody_wav(tmp_path: Path) -> Path:
    wav = tmp_path / "mel.wav"
    write_melody_wav(wav, [72, 76, 79, 84, 76, 72])
    return wav


# ---------------------------------------------------------------------------
# 音高与量化
# ---------------------------------------------------------------------------


def test_melody_pitches_and_grid(melody_wav: Path):
    result = transcribe_wav(melody_wav, TranscribeOptions(bpm=120, grid="1/16"))
    notes = result.seq.tracks[0].notes
    assert [n.pitch for n in notes] == [72, 76, 79, 84, 76, 72]
    # 起拍精确落在 1 拍网格上（量化后是确定值，不是近似值）
    assert [n.start for n in notes] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    # 末音因收尾淡出会略短（见模块文档），其余应为整 1 拍
    for n in notes[:-1]:
        assert n.duration == pytest.approx(1.0)
    assert 0.5 <= notes[-1].duration <= 1.0
    assert result.seq.bpm == 120
    assert result.seq.track_names == ["transcribed"]


def test_repeated_notes_split_by_dips(tmp_path: Path):
    wav = tmp_path / "rep.wav"
    write_melody_wav(wav, [72, 72, 72])
    result = transcribe_wav(wav, TranscribeOptions(bpm=120, grid="1/16"))
    notes = result.seq.tracks[0].notes
    assert [n.pitch for n in notes] == [72, 72, 72]
    assert [n.start for n in notes] == [0.0, 1.0, 2.0]
    for n in notes:
        assert 0.5 <= n.duration <= 1.0


def test_legato_scale_low_region(tmp_path: Path):
    """A4-B4-C5-D5 连奏：音高逐音命中（低至 A4 的频率分辨率仍足够）。"""
    wav = tmp_path / "leg.wav"
    write_melody_wav(wav, [69, 71, 72, 74])
    result = transcribe_wav(wav, TranscribeOptions(bpm=120, grid="1/16"))
    notes = result.seq.tracks[0].notes
    assert [n.pitch for n in notes] == [69, 71, 72, 74]
    assert [n.start for n in notes] == [0.0, 1.0, 2.0, 3.0]


def test_auto_bpm_detection(melody_wav: Path):
    """不显式给 BPM 时自动测速：旋律每 0.5s 一个起音 → 120。"""
    result = transcribe_wav(melody_wav)  # 默认 1/16 量化
    assert abs(result.detected_bpm - 120.0) / 120.0 < 0.05
    assert 0.0 <= result.confidence <= 1.0
    # 自动测速后取整落进 NoteSequence
    assert result.seq.bpm == int(round(result.detected_bpm))


def test_velocity_within_midi_range(melody_wav: Path):
    result = transcribe_wav(melody_wav, TranscribeOptions(bpm=120))
    for n in result.seq.tracks[0].notes:
        assert 1 <= n.velocity <= 127


def test_explicit_bpm_used_verbatim(melody_wav: Path):
    result = transcribe_wav(melody_wav, TranscribeOptions(bpm=100.0))
    assert result.seq.bpm == 100
    assert result.detected_bpm == pytest.approx(120.0, abs=6.0)  # 仍然报告了测速值


def test_missing_file_raises_input_error(tmp_path: Path):
    with pytest.raises(InputFileError) as exc:
        transcribe_wav(tmp_path / "nope.wav")
    assert exc.value.code == 3


def test_silence_raises_parameter_error(tmp_path: Path):
    wav = tmp_path / "silence.wav"
    sf.write(wav, np.zeros(SR * 4, dtype=np.float32), SR, subtype="PCM_16")
    with pytest.raises(ParameterError):
        transcribe_wav(wav)


# ---------------------------------------------------------------------------
# 选项校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [("1/4", 1.0), ("1/8", 0.5), ("1/16", 0.25), ("1/32", 0.125), ("0.5", 0.5), ("2", 2.0)],
)
def test_parse_grid_valid(spec: str, expected: float):
    assert parse_grid(spec) == expected


@pytest.mark.parametrize("spec", ["", "abc", "0", "-0.5", "5", "1/0"])
def test_parse_grid_invalid(spec: str):
    with pytest.raises(ParameterError):
        parse_grid(spec)


def test_options_reject_bad_values():
    with pytest.raises(ParameterError):
        TranscribeOptions(bpm=10.0)
    with pytest.raises(ParameterError):
        TranscribeOptions(bpm=500.0)
    with pytest.raises(ParameterError):
        TranscribeOptions(program=200)
    with pytest.raises(ParameterError):
        TranscribeOptions(min_note_ms=0.0)
    with pytest.raises(ParameterError):
        TranscribeOptions(merge_gap_ms=-1.0)


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------


def test_write_transcribed_midi_roundtrip(melody_wav: Path, tmp_path: Path):
    result = transcribe_wav(melody_wav, TranscribeOptions(bpm=120))
    out = tmp_path / "out" / "t.mid"
    written = write_transcribed_midi(result, out)
    assert Path(written) == out.resolve()
    doc = MidiDocument.load(written)
    assert doc.bpm == 120
    assert doc.tracks[0].name == "transcribed"
    assert len(doc.tracks[0].notes) == result.note_count
