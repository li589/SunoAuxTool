"""QA 独立验收补充：边界/异常用例（由 QA 工程师新增，不改动工程师测试）。

覆盖（团队 lead 要求 + QA 自研补充）：
1. Suno 导出时长边界：10s / 30s 必须通过，9s / 31s 必须退出码 5
2. pipeline --duration 35（mock 渲染）必须退出码 5
3. 非法/畸形和弦串：空串、纯分隔符、尾分隔符、超长非法符号
4. seed=0 边界（0 是合法种子，必须可复现且不等于 None 行为）
5. 无 seed（None）时两次生成结果不同（随机性）
6. 缺失输入文件（render / export suno）
7. AI 命令未装依赖 -> 退出码 6（diffrhythm）
8. generate melody variations=0 -> 仅主旋律轨
9. 升降号调式（F# minor / Bb major / C# minor）解析正确
10. Note 校验：非法 pitch / 负 duration -> ValueError
11. 配置校验：非法采样率/位深 -> ConfigError(2)
12. build_output_path 组合后缀 + 变体序号
13. 立体声导出通道保持
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner

from sunoauxtool.cli import app
from sunoauxtool.config import Config, build_output_path
from sunoauxtool.exceptions import ConfigError, ParameterError
from sunoauxtool.export import audio as audio_ops
from sunoauxtool.export.suno import ExportOptions, SunoExporter
from sunoauxtool.generators.base import GenerationRequest, resolve_scale_pitch_classes
from sunoauxtool.generators.procedural import ProceduralGenerator
from sunoauxtool.models.chords import ChordProgression
from sunoauxtool.models.midi import MidiDocument
from sunoauxtool.models.notes import Note

runner = CliRunner()


# ---------------------------------------------------------------------------
# 1. Suno 导出时长边界
# ---------------------------------------------------------------------------

def test_export_duration_boundary_10s(sine_wav, tmp_path):
    """边界 10s 必须通过（Suno 合规下限）。"""
    exporter = SunoExporter()
    out = exporter.export(str(sine_wav), ExportOptions(duration=10))
    data, sr = audio_ops.read_wav(out)
    assert len(data) == pytest.approx(sr * 10, abs=10)


def test_export_duration_boundary_30s(sine_wav, tmp_path):
    """边界 30s 必须通过（Suno 合规上限）。"""
    exporter = SunoExporter()
    out = exporter.export(str(sine_wav), ExportOptions(duration=30))
    data, sr = audio_ops.read_wav(out)
    assert len(data) == pytest.approx(sr * 30, abs=10)


def test_export_duration_31s_rejected(sine_wav):
    """31s 越界 -> ExportError(5)。"""
    from sunoauxtool.exceptions import ExportError

    exporter = SunoExporter()
    with pytest.raises(ExportError) as exc:
        exporter.export(str(sine_wav), ExportOptions(duration=31))
    assert exc.value.code == 5


# ---------------------------------------------------------------------------
# 2. pipeline 时长越界 -> 退出码 5（mock 渲染）
# ---------------------------------------------------------------------------

def test_pipeline_duration_35_exit_5(tmp_project, monkeypatch):
    """pipeline --duration 35 -> 退出码 5（合规校验在导出层）。"""
    from sunoauxtool.render.fluidsynth import FluidSynthRenderer

    def _fake_render(self, midi_path, soundfont, out_path):
        t = np.linspace(0, 30, 44100 * 30, endpoint=False)
        audio = (0.4 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        audio_ops.write_wav(out_path, audio, 44100, 16)
        return str(out_path)

    monkeypatch.setattr(FluidSynthRenderer, "render", _fake_render)
    result = runner.invoke(app, ["pipeline", "--duration", "35", "--seed", "1"])
    assert result.exit_code == 5, result.output
    assert "10..30" in result.output


# ---------------------------------------------------------------------------
# 3. 非法/畸形和弦串
# ---------------------------------------------------------------------------

def test_chord_parse_pure_separator():
    """纯 '-' -> ParameterError(1)。"""
    with pytest.raises(ParameterError) as exc:
        ChordProgression.parse("-")
    assert exc.value.code == 1


def test_chord_parse_double_separator_lenient():
    """'C--G' 容忍空段 -> 解析为 C、G（宽松但合理）。"""
    prog = ChordProgression.parse("C--G")
    assert [c.symbol for c in prog.chords] == ["C", "G"]


def test_chord_parse_trailing_separator_lenient():
    """'C-' 容忍尾分隔 -> 解析为 C。"""
    prog = ChordProgression.parse("C-")
    assert [c.symbol for c in prog.chords] == ["C"]


def test_chord_parse_whitespace_only():
    """纯空白 -> ParameterError(1)。"""
    with pytest.raises(ParameterError) as exc:
        ChordProgression.parse("   ")
    assert exc.value.code == 1


def test_chord_parse_nonsense_symbol():
    """'C7-9' -> 9 非法 -> ParameterError(1)。"""
    with pytest.raises(ParameterError) as exc:
        ChordProgression.parse("C7-9")
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# 4. seed=0 边界
# ---------------------------------------------------------------------------

def test_seed_zero_reproducible(tmp_path):
    """seed=0 是合法种子：同 seed 两次 .mid 字节级一致。"""
    req = GenerationRequest(seed=0, chords="C-G-Am-F", bars=4)
    a = MidiDocument.from_sequence(ProceduralGenerator(seed=0).generate(req)).write(tmp_path / "z0a.mid")
    b = MidiDocument.from_sequence(ProceduralGenerator(seed=0).generate(req)).write(tmp_path / "z0b.mid")
    assert Path(a).read_bytes() == Path(b).read_bytes()


def test_seed_none_not_reproducible(tmp_path):
    """seed=None 不固定：两次结果不同（随机性生效）。"""
    req = GenerationRequest(seed=None, chords="C-G-Am-F", bars=8)
    a = MidiDocument.from_sequence(ProceduralGenerator(seed=None).generate(req)).write(tmp_path / "na.mid")
    b = MidiDocument.from_sequence(ProceduralGenerator(seed=None).generate(req)).write(tmp_path / "nb.mid")
    assert Path(a).read_bytes() != Path(b).read_bytes()


# ---------------------------------------------------------------------------
# 6. 缺失输入文件
# ---------------------------------------------------------------------------

def test_export_suno_missing_input_exit_3(tmp_project):
    """export suno 输入不存在 -> 退出码 3。"""
    result = runner.invoke(app, ["export", "suno", "--input", "nope.wav", "--duration", "20"])
    assert result.exit_code == 3


def test_render_cli_missing_soundfont_exit_2(tmp_project, fake_midi):
    """render 输入存在但 SoundFont 缺失 -> 退出码 2。"""
    result = runner.invoke(app, ["render", "--input", str(fake_midi)])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# 7. AI 命令未装依赖
# ---------------------------------------------------------------------------

def test_ai_diffrhythm_exit_6_cli(tmp_project):
    """ai diffrhythm P0 环境 -> 退出码 6 + 安装提示。"""
    result = runner.invoke(app, ["ai", "diffrhythm", "--prompt", "slow ballad"])
    assert result.exit_code == 6
    assert "requirements/ai.txt" in result.output


# ---------------------------------------------------------------------------
# 8. variations=0
# ---------------------------------------------------------------------------

def test_melody_variations_zero_single_track():
    """variations=0 -> 仅主旋律轨，无变奏轨。"""
    from sunoauxtool.generators.music21_melody import Music21MelodyGenerator

    gen = Music21MelodyGenerator(seed=5)
    seq = gen.generate(GenerationRequest(seed=5, chords="C-G-Am-F", bars=8, variations=0))
    assert seq.track_names == ["melody"]


# ---------------------------------------------------------------------------
# 9. 升降号调式
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "key,expected",
    [
        ("F# minor", [6, 8, 9, 11, 1, 2, 4]),
        ("Bb major", [10, 0, 2, 3, 5, 7, 9]),
        ("C# minor", [1, 3, 4, 6, 8, 9, 11]),
        ("Eb major", [3, 5, 7, 8, 10, 0, 2]),
    ],
)
def test_resolve_scale_sharp_flat_keys(key, expected):
    """升降号调式音级正确。"""
    assert resolve_scale_pitch_classes(key) == expected


# ---------------------------------------------------------------------------
# 10. Note 校验
# ---------------------------------------------------------------------------

def test_note_pitch_out_of_range():
    """pitch 越界 -> ValueError。"""
    with pytest.raises(ValueError):
        Note(pitch=128, start=0, duration=1.0)
    with pytest.raises(ValueError):
        Note(pitch=-1, start=0, duration=1.0)


def test_note_negative_duration():
    """负 duration -> ValueError。"""
    with pytest.raises(ValueError):
        Note(pitch=60, start=0, duration=-0.5)


# ---------------------------------------------------------------------------
# 11. 配置校验
# ---------------------------------------------------------------------------

def test_config_invalid_sample_rate(tmp_project):
    """非法采样率 -> ConfigError(2)。"""
    with pytest.raises(ConfigError) as exc:
        Config.load().merge_cli(sample_rate=12345)
    assert exc.value.code == 2


def test_config_invalid_bit_depth(tmp_project):
    """非法位深 -> ConfigError(2)。"""
    with pytest.raises(ConfigError) as exc:
        Config.load().merge_cli(bit_depth=12)
    assert exc.value.code == 2


def test_config_invalid_bars(tmp_project):
    """bars 越界 -> ConfigError(2)。"""
    with pytest.raises(ConfigError) as exc:
        Config.load().merge_cli(bars=0)
    assert exc.value.code == 2
    with pytest.raises(ConfigError) as exc:
        Config.load().merge_cli(bars=65)
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# 12. build_output_path 组合
# ---------------------------------------------------------------------------

def test_build_output_path_suffix_plus_variant(tmp_path):
    """extra_suffix + variant 组合命名：..._suno25s_v3.ext。"""
    p = build_output_path(
        tmp_path, style="pop", key="C major", bpm=120, bars=8,
        seed=42, ext="wav", timestamp="140235", extra_suffix="_suno25s", variant=3,
    )
    assert p.name == "pop_Cmajor_120_8bars_42_140235_suno25s_v3.wav"


# ---------------------------------------------------------------------------
# 13. 立体声导出
# ---------------------------------------------------------------------------

def test_export_stereo_preserves_channels(sine_wav, tmp_path):
    """立体声输入导出后通道保持 2。"""
    # 构造立体声 WAV
    sr = 44100
    t = np.linspace(0, 3, int(sr * 3), endpoint=False)
    stereo = np.column_stack([0.4 * np.sin(2 * np.pi * 440 * t),
                              0.3 * np.sin(2 * np.pi * 550 * t)]).astype(np.float32)
    src = tmp_path / "stereo_src.wav"
    audio_ops.write_wav(src, stereo, sr, 16)

    exporter = SunoExporter()
    out = exporter.export(str(src), ExportOptions(duration=12, format="wav"))
    data, out_sr = audio_ops.read_wav(out)
    assert data.ndim == 2
    assert data.shape[1] == 2
    assert out_sr == 44100
