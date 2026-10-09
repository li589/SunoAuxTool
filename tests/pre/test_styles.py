"""P2-4 预设风格库单测：加载 / 校验 / 自定义注册 / CLI 联动。"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from sunoauxtool.cli import app
from sunoauxtool.styles import StyleError, StyleRegistry

runner = CliRunner()


def test_builtin_four_styles_loaded():
    """内置 4 基线全部加载且字段完整（P2-4 验收 2）。"""
    reg = StyleRegistry()
    presets = reg.load_all()
    assert set(reg.BUILTIN_NAMES) == {"pop", "rock", "electronic", "classical"}
    for name in reg.BUILTIN_NAMES:
        p = presets[name]
        assert p.name == name
        lo, hi = p.bpm_range
        assert 20 <= lo <= hi <= 300
        assert "chords" in p.instruments and "melody" in p.instruments
        assert all(0 <= v <= 127 for v in p.instruments.values())
        assert p.rhythm_pattern  # 引用 RhythmPatternRegistry
        assert isinstance(p.melody_profile, dict)
        assert p.chord_preference
        assert isinstance(p.dsp_defaults, dict)


def test_pop_preset_fields():
    """pop 预设关键字段与 P0 生成器一致。"""
    p = StyleRegistry().get("pop")
    assert p.instruments == {"chords": 0, "melody": 81, "bass": 33, "drums": 0}
    assert p.rhythm_pattern == "pop"


def test_get_unknown_style_raises():
    """未知风格 -> StyleError(1)（P2-4 验收 3）。"""
    with pytest.raises(StyleError) as exc:
        StyleRegistry().get("nope")
    assert exc.value.code == 1


def test_register_custom_toml(tmp_path):
    """自定义 TOML 注册后可被引用（P2-4 验收 3）。"""
    p = tmp_path / "jazz.toml"
    p.write_text(
        "name = 'jazz'\n"
        "bpm_range = [90, 120]\n"
        "instruments = { chords = 4, melody = 73, bass = 33, drums = 0 }\n"
        "rhythm_pattern = 'funk'\n"
        "melody_profile = { register = 'C4-C6' }\n"
        "chord_preference = ['C-G-Am-F', 'Dm7-G7-Cmaj7']\n"
        "dsp_defaults = { fade_in_ms = 100 }\n",
        encoding="utf-8",
    )
    reg = StyleRegistry()
    preset = reg.register(p)
    assert preset.name == "jazz"
    assert reg.get("jazz").bpm_range == (90, 120)
    assert reg.get("jazz").instruments["chords"] == 4


def test_register_custom_dir(tmp_path):
    """自定义目录（styles/ dir）自动加载。"""
    custom = tmp_path / "styles"
    custom.mkdir()
    (custom / "lofi.toml").write_text(
        "name = 'lofi'\nbpm_range = [70, 90]\ninstruments = { chords = 4, melody = 81, bass = 33 }\n"
        "rhythm_pattern = 'pop'\nmelody_profile = {}\nchord_preference = []\ndsp_defaults = {}\n",
        encoding="utf-8",
    )
    reg = StyleRegistry(extra_dirs=[custom])
    assert reg.get("lofi").name == "lofi"


def test_register_invalid_style_skipped(tmp_path):
    """非法风格文件（缺 rhythm_pattern）加载时跳过，不抛错。"""
    custom = tmp_path / "styles"
    custom.mkdir()
    (custom / "bad.toml").write_text(
        "name = 'bad'\nbpm_range = [100, 120]\ninstruments = {}\n", encoding="utf-8"
    )
    reg = StyleRegistry(extra_dirs=[custom])
    assert "bad" not in reg.load_all()


# ---------------------------------------------------------------------------
# CLI 联动（P2-4 验收 1）
# ---------------------------------------------------------------------------

def test_generate_midi_style_pop_cli(tmp_project):
    """generate midi --style pop（零额外参数）：BPM/节奏型与预设一致。"""
    result = runner.invoke(app, ["generate", "midi", "--style", "pop", "--seed", "5"])
    assert result.exit_code == 0, result.output
    # pop bpm_range [100,128] -> 中点 114；命名 {style}_{bpm}_{seed}_{seq}
    files = list(Path("output").rglob("pop_114_5_*.mid"))
    assert len(files) == 1
    assert files[0].stat().st_size > 0


def test_generate_midi_unknown_style_raises(tmp_project):
    """未知风格名 -> 明确错误（P2-4 验收 3）。"""
    result = runner.invoke(app, ["generate", "midi", "--style", "nope"])
    assert result.exit_code == 1
    assert "未知风格" in result.output
