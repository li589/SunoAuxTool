"""CLI 端到端测试（Typer CliRunner；渲染用 mock）。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
from typer.testing import CliRunner

from sunoauxtool import __version__
from sunoauxtool.cli import app
from sunoauxtool.export.audio import write_wav

runner = CliRunner()


def test_help_lists_all_subcommands():
    """sunoauxtool --help 列出全部子命令。"""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ["generate", "render", "export", "pipeline", "batch", "config", "ai"]:
        assert cmd in result.output


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    # 动态比对 __version__（曾硬编码 1.0.0，bump 到 1.3.0 时假失败）
    assert __version__ in result.output


def test_config_init(tmp_project):
    result = runner.invoke(app, ["config", "init", "--path", "sunoauxtool.toml"])
    assert result.exit_code == 0, result.output
    assert Path("sunoauxtool.toml").is_file()
    content = Path("sunoauxtool.toml").read_text(encoding="utf-8")
    assert "[paths]" in content


def test_config_show(tmp_project):
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0, result.output
    assert "bpm" in result.output
    assert "C-G-Am-F" in result.output


def test_config_show_with_user_config(tmp_path):
    """用户配置覆盖后 show 展示生效值。"""
    p = tmp_path / "user.toml"
    p.write_text("[defaults]\nbpm = 150\n", encoding="utf-8")
    result = runner.invoke(app, ["--config", str(p), "config", "show"])
    assert result.exit_code == 0
    assert "150" in result.output


def test_generate_midi_cli(tmp_project):
    result = runner.invoke(app, ["generate", "midi", "--seed", "42"])
    assert result.exit_code == 0, result.output
    assert "MIDI 已生成" in result.output
    files = list(Path("output").rglob("*.mid"))
    assert len(files) == 1
    assert files[0].stat().st_size > 0


def test_generate_midi_reproducible_cli(tmp_project):
    r1 = runner.invoke(app, ["generate", "midi", "--seed", "42", "--output", "a.mid"])
    r2 = runner.invoke(app, ["generate", "midi", "--seed", "42", "--output", "b.mid"])
    assert r1.exit_code == 0 and r2.exit_code == 0
    assert Path("a.mid").read_bytes() == Path("b.mid").read_bytes()


def test_generate_midi_different_seed_cli(tmp_project):
    runner.invoke(app, ["generate", "midi", "--seed", "1", "--output", "c.mid"])
    runner.invoke(app, ["generate", "midi", "--seed", "2", "--output", "d.mid"])
    assert Path("c.mid").read_bytes() != Path("d.mid").read_bytes()


def test_generate_melody_cli(tmp_project):
    result = runner.invoke(
        app, ["generate", "melody", "--key", "C major", "--chords", "C-G-Am-F",
              "--variations", "3", "--seed", "5"]
    )
    assert result.exit_code == 0, result.output
    assert "旋律 MIDI 已生成" in result.output


def test_render_missing_input_exit_3(tmp_project):
    result = runner.invoke(app, ["render", "--input", "nope.mid"])
    assert result.exit_code == 3


def test_generate_invalid_chords_exit_1(tmp_project):
    result = runner.invoke(app, ["generate", "midi", "--chords", "H"])
    assert result.exit_code == 1
    assert "无法解析和弦" in result.output


def test_generate_rhythm_invalid_exit_1(tmp_project):
    """无 --style 时非法节奏型 -> 退出码 1（既有行为回归）。"""
    result = runner.invoke(app, ["generate", "midi", "--rhythm", "nonexistent", "--seed", "8"])
    assert result.exit_code == 1
    assert "未知节奏型" in result.output


def test_generate_style_rhythm_invalid_exit_1(tmp_project):
    """QA 缺陷 B 回归：--style + 显式非法 --rhythm 不再被预设静默覆盖，必须报错退出码 1。"""
    result = runner.invoke(
        app, ["generate", "midi", "--style", "pop", "--rhythm", "nonexistent", "--seed", "8"]
    )
    assert result.exit_code == 1, result.output
    assert "未知节奏型" in result.output


def test_generate_style_rhythm_explicit_wins(tmp_project):
    """QA 缺陷 B 回归：--style + 显式合法 --rhythm 以显式值为准（funk 覆盖 pop 预设）。"""
    from sunoauxtool.cli import _request_from_config
    from sunoauxtool.config import Config

    cfg = Config.load()
    req = _request_from_config(cfg, style="pop", rhythm="funk", seed=8)
    assert req.rhythm_pattern == "funk"
    # 预设其余字段仍注入
    assert req.style == "pop"


def test_generate_style_rhythm_preset_default(tmp_project):
    """--style 且未显式 --rhythm 时使用风格预设节奏型（pop）。"""
    from sunoauxtool.cli import _request_from_config
    from sunoauxtool.config import Config

    cfg = Config.load()
    req = _request_from_config(cfg, style="pop", seed=8)
    assert req.rhythm_pattern == "pop"


def test_bad_config_exit_2(tmp_project):
    result = runner.invoke(app, ["--config", "missing.toml", "config", "show"])
    assert result.exit_code == 2


def test_export_duration_out_of_range_exit_5(tmp_project, sine_wav):
    result = runner.invoke(
        app, ["export", "suno", "--input", str(sine_wav), "--duration", "35"]
    )
    assert result.exit_code == 5


def test_export_suno_cli(tmp_project, sine_wav):
    result = runner.invoke(
        app, ["export", "suno", "--input", str(sine_wav), "--duration", "20",
              "--output", "suno.wav"]
    )
    assert result.exit_code == 0, result.output
    assert Path("suno.wav").is_file()


def test_ai_musicgen_exit_6(tmp_project, monkeypatch):
    """AI 依赖未安装时 ai musicgen -> 明确提示安装 P1 依赖，退出码 6。

    通过 monkeypatch find_spec 保证与真实环境（是否已装 torch/audiocraft）无关。
    """
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    result = runner.invoke(
        app, ["ai", "musicgen", "--input", "x.wav", "--prompt", "upbeat pop"]
    )
    assert result.exit_code == 6
    assert "requirements/ai.txt" in result.output


def test_ai_musicgen_writes_metadata(tmp_project, monkeypatch, tmp_path):
    """ai musicgen 成功后落盘 metadata.json（kind=draft, contains_vocals=false, sample_rate=32000）。"""
    from sunoauxtool.ai.musicgen import MusicGenAdapter

    out_wav = tmp_path / "acc.wav"
    out_wav.write_bytes(b"RIFF\x00\x00\x00\x00fake-wav")

    def fake_generate(self, source_wav, prompt, **_kw):
        return str(out_wav)

    monkeypatch.setattr(MusicGenAdapter, "generate", fake_generate)
    result = runner.invoke(
        app, ["ai", "musicgen", "--input", "m.wav", "--prompt", "upbeat pop",
              "--output", str(out_wav)]
    )
    assert result.exit_code == 0, result.output
    metas = list(Path("output").rglob("metadata.json"))
    assert len(metas) == 1
    import json

    data = json.loads(metas[0].read_text(encoding="utf-8"))
    art = data["artifacts"][0]
    assert art["kind"] == "draft"
    assert art["contains_vocals"] is False
    assert art["sample_rate"] == 32000
    assert art["path"] == str(out_wav)


def test_ai_diffrhythm_exit_6(tmp_project, monkeypatch):
    """AI 依赖未安装时 ai diffrhythm -> 明确提示 + 退出码 6（环境无关）。"""
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    result = runner.invoke(app, ["ai", "diffrhythm", "--prompt", "slow ballad"])
    assert result.exit_code == 6


def test_ai_diffrhythm_writes_metadata(tmp_project, monkeypatch, tmp_path):
    """ai diffrhythm 成功后元数据标注 contains_vocals=true（草稿不进 Suno 链）。"""
    from sunoauxtool.ai.diffrhythm import DiffRhythmAdapter

    out_wav = tmp_path / "song.wav"
    out_wav.write_bytes(b"RIFF\x00\x00\x00\x00fake-wav")

    def fake_generate(self, source_wav, prompt, **_kw):
        return str(out_wav)

    monkeypatch.setattr(DiffRhythmAdapter, "generate", fake_generate)
    result = runner.invoke(
        app, ["ai", "diffrhythm", "--prompt", "slow ballad", "--output", str(out_wav)]
    )
    assert result.exit_code == 0, result.output
    metas = list(Path("output").rglob("metadata.json"))
    assert len(metas) == 1
    import json

    data = json.loads(metas[0].read_text(encoding="utf-8"))
    art = data["artifacts"][0]
    assert art["kind"] == "draft"
    assert art["contains_vocals"] is True
    assert art["sample_rate"] == 44100


def test_batch_full_cli(tmp_project):
    """batch 完整实现（P1-3）：--count 3 --seed 42 产出 3 个独立变体，退出码 0。"""
    result = runner.invoke(app, ["batch", "--count", "3", "--seed", "42"])
    assert result.exit_code == 0, result.output
    assert "批量完成" in result.output
    files = list(Path("output").rglob("*.mid"))
    assert len(files) == 3
    for f in files:
        assert f.stat().st_size > 0


def test_ai_adapters_unavailable_in_p0(monkeypatch):
    """AI 依赖缺失时 is_available() 均为 False（monkeypatch find_spec，环境无关）。"""
    import importlib.util

    from sunoauxtool.ai.diffrhythm import DiffRhythmAdapter
    from sunoauxtool.ai.musicgen import MusicGenAdapter

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert MusicGenAdapter().is_available() is False
    assert DiffRhythmAdapter().is_available() is False


def test_no_torch_import_on_cli(tmp_project):
    """导入 CLI 不触发任何 torch import（T05 验收）。"""
    src = Path(__file__).resolve().parents[2] / "src"
    env = {**os.environ, "PYTHONPATH": str(src)}
    code = (
        "import sys; import sunoauxtool.cli; "
        "assert 'torch' not in sys.modules, 'torch imported!'; print('OK')"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        env=env, cwd=str(tmp_project),
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


# ---------------------------------------------------------------------------
# pipeline（mock 渲染）
# ---------------------------------------------------------------------------

def _fake_render(self, midi_path, soundfont, out_path):
    """fake 渲染：直接写 20s 正弦 WAV。"""
    t = np.linspace(0, 20, 44100 * 20, endpoint=False)
    audio = (0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    write_wav(out_path, audio, 44100, 16)
    return str(out_path)


def test_pipeline_zero_arg_demo(tmp_project, monkeypatch):
    """零参数 sunoauxtool pipeline 完整跑通（mock 渲染）。"""
    from sunoauxtool.render.fluidsynth import FluidSynthRenderer

    monkeypatch.setattr(FluidSynthRenderer, "render", _fake_render)
    result = runner.invoke(app, ["pipeline"])
    assert result.exit_code == 0, result.output
    assert "Pipeline 完成" in result.output
    assert "C-G-Am-F" in result.output
    files = list(Path("output").rglob("*_suno25s.wav"))
    assert len(files) == 1
    assert files[0].stat().st_size > 0


def test_pipeline_with_params(tmp_project, monkeypatch):
    """pipeline 自定义参数：时长 20s。"""
    from sunoauxtool.render.fluidsynth import FluidSynthRenderer

    monkeypatch.setattr(FluidSynthRenderer, "render", _fake_render)
    result = runner.invoke(
        app, ["pipeline", "--chords", "C-G-Am-F", "--duration", "20", "--seed", "7"]
    )
    assert result.exit_code == 0, result.output
    files = list(Path("output").rglob("*_suno20s.wav"))
    assert len(files) == 1


def test_pipeline_cleans_tmp(tmp_project, monkeypatch):
    """pipeline 结束后 .tmp 中间产物清理。"""
    from sunoauxtool.render.fluidsynth import FluidSynthRenderer

    monkeypatch.setattr(FluidSynthRenderer, "render", _fake_render)
    result = runner.invoke(app, ["pipeline"])
    assert result.exit_code == 0
    tmp_dir = Path("output") / ".tmp"
    leftovers = list(tmp_dir.rglob("*")) if tmp_dir.exists() else []
    assert leftovers == []
