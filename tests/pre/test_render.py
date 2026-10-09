"""渲染单元测试：mock fluidsynth，不依赖真实 SoundFont/二进制。"""

from __future__ import annotations

import pytest

from sunoauxtool.exceptions import ConfigError, InputFileError, RenderError
from sunoauxtool.render.fluidsynth import FluidSynthRenderer


def test_render_creates_wav(mock_fluidsynth, fake_midi, fake_soundfont, tmp_path):
    """mock 渲染成功，输出 WAV 存在。"""
    renderer = FluidSynthRenderer()
    out = tmp_path / "out.wav"
    path = renderer.render(str(fake_midi), str(fake_soundfont), str(out))
    assert path == str(out.resolve())
    assert out.is_file()
    assert out.stat().st_size > 0


def test_render_missing_midi(mock_fluidsynth, fake_soundfont, tmp_path):
    """MIDI 不存在 -> InputFileError(3)。"""
    renderer = FluidSynthRenderer()
    with pytest.raises(InputFileError) as exc:
        renderer.render(str(tmp_path / "nope.mid"), str(fake_soundfont), str(tmp_path / "o.wav"))
    assert exc.value.code == 3


def test_render_missing_soundfont(mock_fluidsynth, fake_midi, tmp_path):
    """SoundFont 不存在 -> ConfigError(2)。"""
    renderer = FluidSynthRenderer()
    with pytest.raises(ConfigError) as exc:
        renderer.render(str(fake_midi), str(tmp_path / "nope.sf2"), str(tmp_path / "o.wav"))
    assert exc.value.code == 2


def test_render_fluidsynth_not_found(monkeypatch, fake_midi, fake_soundfont, tmp_path):
    """fluidsynth 未安装/找不到 -> RenderError(4)。"""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _: None)
    renderer = FluidSynthRenderer(fluidsynth_path=None)
    with pytest.raises(RenderError) as exc:
        renderer.render(str(fake_midi), str(fake_soundfont), str(tmp_path / "o.wav"))
    assert exc.value.code == 4
    assert "fluidsynth" in str(exc.value)


def test_render_subprocess_failure(mock_fluidsynth, fake_midi, fake_soundfont, tmp_path, monkeypatch):
    """渲染进程非零退出 -> RenderError(4)。"""
    from sunoauxtool.render import fluidsynth as fs_mod

    class _Fail:
        returncode = 1
        stdout = ""
        stderr = "audio driver error"

    monkeypatch.setattr(fs_mod.subprocess, "run", lambda *a, **k: _Fail())
    renderer = FluidSynthRenderer()
    with pytest.raises(RenderError) as exc:
        renderer.render(str(fake_midi), str(fake_soundfont), str(tmp_path / "o.wav"))
    assert exc.value.code == 4


def test_render_subprocess_oserror(mock_fluidsynth, fake_midi, fake_soundfont, tmp_path, monkeypatch):
    """subprocess.run 抛 OSError -> RenderError(4)。"""
    from sunoauxtool.render import fluidsynth as fs_mod

    def _raise_oserror(*a, **k):
        raise OSError("file not found")

    monkeypatch.setattr(fs_mod.subprocess, "run", _raise_oserror)
    renderer = FluidSynthRenderer()
    with pytest.raises(RenderError) as exc:
        renderer.render(str(fake_midi), str(fake_soundfont), str(tmp_path / "o.wav"))
    assert exc.value.code == 4
    assert "无法启动 fluidsynth" in str(exc.value)


def test_render_output_not_created(mock_fluidsynth, fake_midi, fake_soundfont, tmp_path, monkeypatch):
    """返回码 0 但输出文件未生成 -> RenderError(4)。"""
    from sunoauxtool.render import fluidsynth as fs_mod

    class _Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(fs_mod.subprocess, "run", lambda *a, **k: _Ok())
    renderer = FluidSynthRenderer()
    with pytest.raises(RenderError) as exc:
        renderer.render(str(fake_midi), str(fake_soundfont), str(tmp_path / "o.wav"))
    assert exc.value.code == 4


def test_render_custom_fluidsynth_path(mock_fluidsynth, fake_midi, fake_soundfont, tmp_path, monkeypatch):
    """配置绝对路径的 fluidsynth 可被解析（文件存在）。"""
    from sunoauxtool.render import fluidsynth as fs_mod

    binary = tmp_path / "fluidsynth-custom.exe"
    binary.write_bytes(b"mock")

    def fake_resolve(self):
        return str(binary)

    monkeypatch.setattr(fs_mod.FluidSynthRenderer, "_resolve_fluidsynth", fake_resolve)
    renderer = FluidSynthRenderer(fluidsynth_path=str(binary))
    out = tmp_path / "custom.wav"
    renderer.render(str(fake_midi), str(fake_soundfont), str(out))
    assert out.is_file()


def test_render_dry_run(mock_fluidsynth, fake_midi, fake_soundfont, tmp_path):
    """dry_run 时不调用 subprocess，返回路径但不写文件。"""
    renderer = FluidSynthRenderer()
    out = tmp_path / "dry.wav"
    path = renderer.render(str(fake_midi), str(fake_soundfont), str(out), dry_run=True)
    assert path == str(out.resolve())
    # dry_run 不写文件
    assert not out.is_file()


def test_render_dry_run_missing_midi(mock_fluidsynth, fake_soundfont, tmp_path):
    """dry_run 时 MIDI 不存在仍抛错（前置校验优先）。"""
    renderer = FluidSynthRenderer()
    from sunoauxtool.exceptions import InputFileError
    with pytest.raises(InputFileError):
        renderer.render(str(tmp_path / "nope.mid"), str(fake_soundfont), str(tmp_path / "o.wav"), dry_run=True)
