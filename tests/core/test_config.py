"""配置加载/合并/写回单元测试。"""

from __future__ import annotations

import pytest

from sunoauxtool.config import Config, build_output_path
from sunoauxtool.exceptions import ConfigError


def test_load_defaults(tmp_project):
    """无任何配置文件时使用内置默认值。"""
    cfg = Config.load()
    assert cfg.defaults.bpm == 120
    assert cfg.defaults.key == "C major"
    assert cfg.defaults.chords == "C-G-Am-F"
    assert cfg.defaults.bars == 8
    assert cfg.defaults.time_signature == "4/4"
    assert cfg.defaults.tracks == ["chords", "melody", "bass"]
    assert cfg.export.format == "wav"
    assert cfg.export.sample_rate == 44100
    assert cfg.export.bit_depth == 16
    assert cfg.export.duration == 25
    assert cfg.random.seed is None


def test_load_explicit_config(tmp_path):
    """显式指定配置文件可覆盖默认值。"""
    p = tmp_path / "user.toml"
    p.write_text("[defaults]\nbpm = 140\n[paths]\nsoundfont = 'D:/x.sf2'\n", encoding="utf-8")
    cfg = Config.load(path=p)
    assert cfg.defaults.bpm == 140
    assert cfg.paths.soundfont == "D:/x.sf2"
    assert cfg.defaults.key == "C major"  # 未覆盖字段保持默认
    assert cfg.config_path == p.resolve()


def test_load_missing_explicit_config(tmp_path):
    """显式指定不存在的配置文件 -> ConfigError(2)。"""
    with pytest.raises(ConfigError) as exc:
        Config.load(path=tmp_path / "nope.toml")
    assert exc.value.code == 2


def test_load_invalid_toml(tmp_path):
    """TOML 语法错误 -> ConfigError(2)。"""
    p = tmp_path / "bad.toml"
    p.write_text("this is not [ toml", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        Config.load(path=p)
    assert exc.value.code == 2


def test_load_unknown_section(tmp_path):
    """未知 section -> ConfigError(2)。"""
    p = tmp_path / "unknown.toml"
    p.write_text("[mystery]\nx = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        Config.load(path=p)
    assert exc.value.code == 2


def test_merge_cli(tmp_project):
    """CLI 覆盖优先级最高。"""
    cfg = Config.load().merge_cli(bpm=140, soundfont="D:/x.sf2", duration=20, seed=42)
    assert cfg.defaults.bpm == 140
    assert cfg.paths.soundfont == "D:/x.sf2"
    assert cfg.export.duration == 20
    assert cfg.random.seed == 42
    # 未覆盖字段保持默认
    assert cfg.defaults.key == "C major"


def test_merge_cli_none_ignored(tmp_project):
    """CLI 未提供（None）不覆盖。"""
    cfg = Config.load().merge_cli(bpm=None, seed=None)
    assert cfg.defaults.bpm == 120
    assert cfg.random.seed is None


def test_merge_cli_unknown_key(tmp_project):
    """未知覆盖键 -> ConfigError(2)。"""
    with pytest.raises(ConfigError) as exc:
        Config.load().merge_cli(unknown_thing=1)
    assert exc.value.code == 2


def test_write_template_roundtrip(tmp_path):
    """write_template 后再 load 回读，字段一致。"""
    cfg = Config().merge_cli(bpm=100, key="A minor", duration=20, seed=7)
    target = cfg.write_template(tmp_path / "cfg" / "sunoauxtool.toml")
    loaded = Config.load(path=target)
    assert loaded.defaults.bpm == 100
    assert loaded.defaults.key == "A minor"
    assert loaded.export.duration == 20
    assert loaded.random.seed == 7


def test_validate_bpm_range(tmp_project):
    """bpm 越界 -> ConfigError(2)。"""
    with pytest.raises(ConfigError) as exc:
        Config.load().merge_cli(bpm=5000)
    assert exc.value.code == 2


def test_validate_format(tmp_project):
    """导出格式非法 -> ConfigError(2)。"""
    with pytest.raises(ConfigError) as exc:
        Config.load().merge_cli(format="ogg")
    assert exc.value.code == 2


def test_build_output_path_uses_spec(tmp_path):
    """输出命名规范：output/{YYYYMMDD}/{style}_{key}_{bpm}_{bars}bars_{seed}_{ts}.{ext}"""
    p = build_output_path(
        tmp_path, style="pop", key="C major", bpm=120, bars=8,
        seed=42, ext="wav", timestamp="140235",
    )
    assert p.parent.name == "20250809" or len(p.parent.name) == 8
    assert p.name == "pop_Cmajor_120_8bars_42_140235.wav"


def test_build_output_path_demo_seed(tmp_path):
    """seed=None -> demo。"""
    p = build_output_path(
        tmp_path, style="pop", key="C major", bpm=120, bars=8,
        seed=None, ext="wav", timestamp="140235",
    )
    assert p.name == "pop_Cmajor_120_8bars_demo_140235.wav"


def test_build_output_path_suno_suffix(tmp_path):
    """Suno 导出件追加 _suno{ds}s。"""
    p = build_output_path(
        tmp_path, style="pop", key="C major", bpm=120, bars=8,
        seed=42, ext="wav", timestamp="140235", extra_suffix="_suno25s",
    )
    assert p.name == "pop_Cmajor_120_8bars_42_140235_suno25s.wav"
