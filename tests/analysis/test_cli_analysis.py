"""``tempo`` / ``transcribe`` 子命令的 CLI 端到端测试（#13）。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from typer.testing import CliRunner

from sunoauxtool.cli import app

runner = CliRunner()

SR = 22050


def write_click_wav(path: Path, bpm: float = 120.0, beats: int = 24) -> None:
    n = int(beats * 60.0 / bpm * SR)
    x = np.zeros(n, dtype=np.float32)
    for k in range(beats):
        start = int(k * 60.0 / bpm * SR)
        dur = int(0.05 * SR)
        t = np.arange(dur) / SR
        x[start : start + dur] += (
            0.6 * np.sin(2 * np.pi * 880.0 * t) * np.exp(-t * 40.0)
        ).astype(np.float32)
    sf.write(path, x, SR, subtype="PCM_16")


def write_melody_wav(path: Path, pitches: list[int]) -> None:
    chunks = []
    for p in pitches:
        f = 440.0 * 2.0 ** ((p - 69) / 12.0)
        t = np.arange(int(0.5 * SR)) / SR
        env = np.minimum(1.0, np.minimum(t / 0.01, (0.5 - t) / 0.05))
        tone = (
            0.5 * np.sin(2 * np.pi * f * t)
            + 0.15 * np.sin(2 * np.pi * 2 * f * t)
            + 0.07 * np.sin(2 * np.pi * 3 * f * t)
        )
        chunks.append(tone * env)
    sf.write(path, np.concatenate(chunks).astype(np.float32), SR, subtype="PCM_16")


@pytest.fixture()
def click_wav(tmp_path: Path) -> Path:
    wav = tmp_path / "click.wav"
    write_click_wav(wav, 120.0)
    return wav


@pytest.fixture()
def melody_wav(tmp_path: Path) -> Path:
    wav = tmp_path / "mel.wav"
    write_melody_wav(wav, [72, 76, 79, 84])
    return wav


# ---------------------------------------------------------------------------
# tempo
# ---------------------------------------------------------------------------


def test_tempo_reports_bpm_and_confidence(click_wav: Path):
    result = runner.invoke(app, ["tempo", str(click_wav)])
    assert result.exit_code == 0, result.output
    assert "BPM:" in result.output
    assert "置信度" in result.output


def test_tempo_fast_tempo_prior_fold_hint(tmp_path: Path):
    """1.5.4：≥160BPM 素材被默认先验折半时，CLI 打印更快档提示。"""
    fast_wav = tmp_path / "fast.wav"
    write_click_wav(fast_wav, 180.0)
    result = runner.invoke(app, ["tempo", str(fast_wav)])
    assert result.exit_code == 0, result.output
    assert "更快档" in result.output
    assert "--prior-bpm 0" in result.output
    # 120BPM 常规素材不触发提示
    normal_wav = tmp_path / "normal.wav"
    write_click_wav(normal_wav, 120.0)
    result2 = runner.invoke(app, ["tempo", str(normal_wav)])
    assert result2.exit_code == 0, result2.output
    assert "更快档" not in result2.output
    # 关闭先验后（用户自行指定）不再提示
    result3 = runner.invoke(app, ["tempo", str(fast_wav), "--prior-bpm", "0"])
    assert result3.exit_code == 0, result3.output
    assert "更快档" not in result3.output
    assert "第一拍" in result.output


def test_tempo_missing_file_exits_3(tmp_path: Path):
    result = runner.invoke(app, ["tempo", str(tmp_path / "nope.wav")])
    assert result.exit_code == 3


def test_tempo_invalid_range_exits_1(click_wav: Path):
    result = runner.invoke(app, ["tempo", str(click_wav), "--min-bpm", "150", "--max-bpm", "100"])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# transcribe（内置后端）
# ---------------------------------------------------------------------------


def test_transcribe_default_output_next_to_wav(melody_wav: Path):
    result = runner.invoke(app, ["transcribe", str(melody_wav), "--bpm", "120"])
    assert result.exit_code == 0, result.output
    out = melody_wav.with_suffix(".transcribed.mid")
    assert out.is_file()
    assert "转谱完成" in result.output
    assert str(out) in result.output


def test_transcribe_output_override(melody_wav: Path, tmp_path: Path):
    target = tmp_path / "sub" / "out.mid"
    result = runner.invoke(
        app, ["transcribe", str(melody_wav), "--bpm", "120", "-o", str(target)]
    )
    assert result.exit_code == 0, result.output
    assert target.is_file()


def test_transcribe_grid_and_program_reach_result(melody_wav: Path, tmp_path: Path):
    out = tmp_path / "g.mid"
    result = runner.invoke(
        app,
        ["transcribe", str(melody_wav), "--bpm", "120", "--grid", "1/8",
         "--program", "4", "-o", str(out)],
    )
    assert result.exit_code == 0, result.output
    from sunoauxtool.models.midi import MidiDocument

    doc = MidiDocument.load(out)
    assert doc.tracks[0].program == 4


def test_transcribe_bad_bpm_string_exits_1(melody_wav: Path):
    result = runner.invoke(app, ["transcribe", str(melody_wav), "--bpm", "abc"])
    assert result.exit_code == 1
    assert "auto" in result.output


def test_transcribe_bad_grid_exits_1(melody_wav: Path):
    result = runner.invoke(app, ["transcribe", str(melody_wav), "--grid", "1/0"])
    assert result.exit_code == 1


def test_transcribe_missing_file_exits_3(tmp_path: Path):
    result = runner.invoke(app, ["transcribe", str(tmp_path / "nope.wav")])
    assert result.exit_code == 3


# ---------------------------------------------------------------------------
# transcribe 后端选择
# ---------------------------------------------------------------------------


def test_transcribe_basic_pitch_backend_exits_6_when_uninstalled(melody_wav: Path):
    """basic-pitch 未安装时退出码 6（AI 依赖不可用），且提示安装方式。"""
    import importlib.util

    if importlib.util.find_spec("basic_pitch") is not None:  # pragma: no cover
        pytest.skip("本机已安装 basic-pitch，无法测未装路径")
    result = runner.invoke(app, ["transcribe", str(melody_wav), "--backend", "basic-pitch"])
    assert result.exit_code == 6
    assert "basic-pitch" in result.output


def test_transcribe_unknown_backend_exits_1(melody_wav: Path):
    result = runner.invoke(app, ["transcribe", str(melody_wav), "--backend", "magia"])
    assert result.exit_code == 1
    assert "builtin" in result.output
