"""M-1 渲染器新路径单测：dry_run / module 相对路径 / 双音色库回退 / 错误码 7。

渲染层 subprocess 用 mock（不依赖真实二进制）；module 路径解析与错误码是重点。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sunoauxtool import platform_paths
from sunoauxtool.exceptions import ConfigError, InputFileError, ModuleError, RenderError
from sunoauxtool.render.fluidsynth import FluidSynthRenderer


def _mk_module_env(tmp_path):
    """构造 module 环境：fluidsynth.exe + 主/备音色库。"""
    fs_bin = tmp_path / "module" / "fluidsynth" / "bin" / "fluidsynth.exe"
    fs_bin.parent.mkdir(parents=True)
    fs_bin.write_bytes(b"MZ")
    fs_bin.chmod(0o755)  # POSIX 需真实执行位，否则探测判 BROKEN（Windows 上为 no-op）
    primary = tmp_path / "module" / "sf" / "primary.sf2"
    primary.parent.mkdir(parents=True)
    primary.write_bytes(b"RIFFprimary")
    backup = tmp_path / "module" / "sf" / "backup.sf2"
    backup.write_bytes(b"RIFFbackup")
    return fs_bin, primary, backup


def test_render_dry_run_no_files(tmp_path, fake_midi):
    """dry_run：不调用 subprocess、不写产物，返回目标路径（M-1f）。"""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        raise AssertionError("dry-run 不应调用 subprocess")

    renderer = FluidSynthRenderer(fluidsynth_path="module/fs.exe")
    from sunoauxtool.render import fluidsynth as fs_mod

    import pytest

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fs_mod.subprocess, "run", fake_run)
        out = tmp_path / "dry.wav"
        path = renderer.render(str(fake_midi), "module/sf/x.sf2", str(out), dry_run=True)
    assert path == str(out.resolve())
    assert not out.exists()
    assert calls == []


def test_render_module_fluidsynth_missing_raises_7(tmp_path, fake_midi):
    """module 相对路径 fluidsynth 缺失 -> ModuleError(7)（音色库存在时）。"""
    sf = tmp_path / "x.sf2"
    sf.write_bytes(b"RIFFx")
    renderer = FluidSynthRenderer(
        fluidsynth_path="module/fluidsynth/bin/fluidsynth.exe",
        project_root=tmp_path,
    )
    with pytest.raises(ModuleError) as exc:
        renderer.render(str(fake_midi), str(sf), str(tmp_path / "o.wav"))
    assert exc.value.code == 7


def test_render_module_soundfont_missing_raises_7(tmp_path, fake_midi, monkeypatch):
    """module 主+备选音色库均缺失 -> ModuleError(7)。"""
    fs_bin = tmp_path / "module" / "fluidsynth" / "bin" / "fluidsynth.exe"
    fs_bin.parent.mkdir(parents=True)
    fs_bin.write_bytes(b"MZ")
    fs_bin.chmod(0o755)  # POSIX 需真实执行位，否则探测判 BROKEN（Windows 上为 no-op）
    from sunoauxtool.render import fluidsynth as fs_mod

    monkeypatch.setattr(fs_mod.subprocess, "run",
                        lambda *a, **k: _Result(0, "", ""))
    renderer = FluidSynthRenderer(
        fluidsynth_path=str(fs_bin),
        soundfont_backup="module/sf/backup.sf2",
        project_root=tmp_path,
    )
    with pytest.raises(ModuleError) as exc:
        renderer.render(str(fake_midi), "module/sf/primary.sf2", str(tmp_path / "o.wav"))
    assert exc.value.code == 7


class _Result:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_render_module_paths_ok(tmp_path, fake_midi, monkeypatch):
    """module 相对路径（fluidsynth + 主音色库）解析成功并渲染。"""
    fs_bin, primary, _backup = _mk_module_env(tmp_path)
    from sunoauxtool.render import fluidsynth as fs_mod

    def fake_run(cmd, **kw):
        out_idx = cmd.index("-F") + 1
        out = Path(cmd[out_idx])
        out.write_bytes(b"RIFFfake-wav")
        return _Result(0, "", "")

    monkeypatch.setattr(fs_mod.subprocess, "run", fake_run)
    renderer = FluidSynthRenderer(
        fluidsynth_path="module/fluidsynth/bin/fluidsynth.exe",
        soundfont_backup="module/sf/backup.sf2",
        project_root=tmp_path,
    )
    out = tmp_path / "o.wav"
    path = renderer.render(str(fake_midi), "module/sf/primary.sf2", str(out))
    assert Path(path).is_file()
    assert Path(path).read_bytes().startswith(b"RIFF")


def test_render_soundfont_backup_fallback(tmp_path, fake_midi, monkeypatch):
    """主音色库缺失、备选存在 -> 自动回退备选（M-1e）。"""
    # 直接构造「主库不存在 + 备选存在」的环境，避免运行期删除文件（Windows 文件锁竞态）
    fs_bin = tmp_path / "module" / "fluidsynth" / "bin" / "fluidsynth.exe"
    fs_bin.parent.mkdir(parents=True)
    fs_bin.write_bytes(b"MZ")
    fs_bin.chmod(0o755)  # POSIX 需真实执行位，否则探测判 BROKEN（Windows 上为 no-op）
    backup = tmp_path / "module" / "sf" / "backup.sf2"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"RIFFbackup")
    from sunoauxtool.render import fluidsynth as fs_mod

    def fake_run(cmd, **kw):
        out = Path(cmd[cmd.index("-F") + 1])
        out.write_bytes(b"RIFFfake-wav")
        return _Result(0, "", "")

    monkeypatch.setattr(fs_mod.subprocess, "run", fake_run)
    renderer = FluidSynthRenderer(
        fluidsynth_path=str(fs_bin),
        soundfont_backup=str(backup),
        project_root=tmp_path,
    )
    out = tmp_path / "o.wav"
    # 主库路径不存在（未创建），应回退到备选
    path = renderer.render(str(fake_midi), str(backup.parent / "primary.sf2"), str(out))
    assert Path(path).is_file()


def test_render_non_module_soundfont_missing_raises_2(tmp_path, fake_midi, monkeypatch):
    """非 module 音色库缺失 -> ConfigError(2)（P0 兼容，不误报 7）。"""
    fs_bin = tmp_path / "custom-fs.exe"
    fs_bin.write_bytes(b"MZ")
    fs_bin.chmod(0o755)  # POSIX 需真实执行位，否则探测判 BROKEN（Windows 上为 no-op）
    from sunoauxtool.render import fluidsynth as fs_mod

    monkeypatch.setattr(fs_mod.subprocess, "run",
                        lambda *a, **k: _Result(0, "", ""))
    renderer = FluidSynthRenderer(
        fluidsynth_path=str(fs_bin),
        project_root=tmp_path,
    )
    with pytest.raises(ConfigError) as exc:
        renderer.render(str(fake_midi), str(tmp_path / "nope.sf2"), str(tmp_path / "o.wav"))
    assert exc.value.code == 2


def test_render_dry_run_missing_midi_still_errors(tmp_path):
    """dry_run 仍校验输入存在 -> InputFileError(3)。"""
    renderer = FluidSynthRenderer()
    with pytest.raises(InputFileError) as exc:
        renderer.render(str(tmp_path / "nope.mid"), "x.sf2", str(tmp_path / "o.wav"), dry_run=True)
    assert exc.value.code == 3


def test_render_cli_dry_run(tmp_project, fake_midi):
    """CLI render --dry-run：退出码 0、输出 DRY-RUN 标注、不写产物。"""
    from typer.testing import CliRunner

    from sunoauxtool.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["render", "--input", str(fake_midi), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    assert not list(Path(".").glob("*_rendered.wav"))


# ---------------------------------------------------------------------------
# 跨平台回落：捆绑的 Windows 二进制在 POSIX 上不可执行 -> 改用系统 fluidsynth
# ---------------------------------------------------------------------------

def test_resolve_fluidsynth_falls_back_relative_posix(tmp_path, monkeypatch):
    """module 相对路径指向不可执行的 Windows 二进制 -> 回落到系统 fluidsynth。"""
    _fs_bin, _primary, _backup = _mk_module_env(tmp_path)
    renderer = FluidSynthRenderer(
        fluidsynth_path="module/fluidsynth/bin/fluidsynth.exe", project_root=tmp_path
    )
    monkeypatch.setattr(platform_paths, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_paths.shutil, "which", lambda name: "/usr/bin/fluidsynth")
    monkeypatch.setattr("sunoauxtool.render.fluidsynth.os.access", lambda p, mode: False)

    assert Path(renderer._resolve_fluidsynth()) == Path("/usr/bin/fluidsynth")


def test_resolve_fluidsynth_falls_back_absolute_posix(tmp_path, monkeypatch):
    """绝对路径指向不可执行的 Windows 二进制 -> 同样回落到系统 fluidsynth。"""
    fs_bin, _primary, _backup = _mk_module_env(tmp_path)
    renderer = FluidSynthRenderer(fluidsynth_path=str(fs_bin), project_root=tmp_path)
    monkeypatch.setattr(platform_paths, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_paths.shutil, "which", lambda name: "/usr/bin/fluidsynth")
    monkeypatch.setattr("sunoauxtool.render.fluidsynth.os.access", lambda p, mode: False)

    assert Path(renderer._resolve_fluidsynth()) == Path("/usr/bin/fluidsynth")


def test_resolve_fluidsynth_no_fallback_raises_module_error(tmp_path, monkeypatch):
    """不可执行且系统无 fluidsynth + module 路径 -> ModuleError(7)，不静默放过。"""
    _fs_bin, _primary, _backup = _mk_module_env(tmp_path)
    renderer = FluidSynthRenderer(
        fluidsynth_path="module/fluidsynth/bin/fluidsynth.exe", project_root=tmp_path
    )
    monkeypatch.setattr(platform_paths, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_paths.shutil, "which", lambda name: None)
    monkeypatch.setattr("sunoauxtool.render.fluidsynth.os.access", lambda p, mode: False)

    with pytest.raises(ModuleError) as exc:
        renderer._resolve_fluidsynth()
    assert exc.value.code == 7


def test_resolve_fluidsynth_windows_never_falls_back(tmp_path, monkeypatch):
    """Windows 平台不回落到系统 fluidsynth（既有语义逐字不变）。"""
    fs_bin, _primary, _backup = _mk_module_env(tmp_path)
    renderer = FluidSynthRenderer(fluidsynth_path=str(fs_bin), project_root=tmp_path)
    monkeypatch.setattr(platform_paths, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_paths.shutil, "which", lambda name: "/usr/bin/fluidsynth")

    # Windows 上 os.access(X_OK) 对存在的文件为真 -> 直接用捆绑二进制，不回落
    assert renderer._resolve_fluidsynth() == str(fs_bin)


# ---------------------------------------------------------------------------
# 回落不可用时的分级报错（保证不静默放过坏环境）
# ---------------------------------------------------------------------------

def test_resolve_absolute_module_no_fallback_raises_7(tmp_path, monkeypatch):
    """绝对路径指向 module 下不可执行的二进制、且系统无回落 -> ModuleError(7)。"""
    fs_bin, _primary, _backup = _mk_module_env(tmp_path)
    renderer = FluidSynthRenderer(fluidsynth_path=str(fs_bin), project_root=tmp_path)
    monkeypatch.setattr(platform_paths, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_paths.shutil, "which", lambda name: None)
    monkeypatch.setattr("sunoauxtool.render.fluidsynth.os.access", lambda p, mode: False)

    with pytest.raises(ModuleError) as exc:
        renderer._resolve_fluidsynth()
    assert exc.value.code == 7


def test_resolve_absolute_non_module_no_fallback_raises_4(tmp_path, monkeypatch):
    """绝对路径非 module、不可执行、无回落 -> RenderError(4)。"""
    fs_bin = tmp_path / "elsewhere" / "fluidsynth.exe"
    fs_bin.parent.mkdir()
    fs_bin.write_bytes(b"MZ")
    renderer = FluidSynthRenderer(fluidsynth_path=str(fs_bin), project_root=tmp_path)
    monkeypatch.setattr(platform_paths, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_paths.shutil, "which", lambda name: None)
    monkeypatch.setattr("sunoauxtool.render.fluidsynth.os.access", lambda p, mode: False)

    with pytest.raises(RenderError) as exc:
        renderer._resolve_fluidsynth()
    assert exc.value.code == 4


def test_resolve_relative_non_module_no_fallback_raises_4(tmp_path, monkeypatch):
    """相对路径非 module、不可执行、无回落 -> RenderError(4)。"""
    fs_bin = tmp_path / "sub" / "fluidsynth.exe"
    fs_bin.parent.mkdir()
    fs_bin.write_bytes(b"MZ")
    renderer = FluidSynthRenderer(fluidsynth_path="sub/fluidsynth.exe", project_root=tmp_path)
    monkeypatch.setattr(platform_paths, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_paths.shutil, "which", lambda name: None)
    monkeypatch.setattr("sunoauxtool.render.fluidsynth.os.access", lambda p, mode: False)

    with pytest.raises(RenderError) as exc:
        renderer._resolve_fluidsynth()
    assert exc.value.code == 4


def test_resolve_bare_name_uses_path_lookup(tmp_path, monkeypatch):
    """裸名（无路径分隔）文件不存在时做 PATH 查找。"""
    renderer = FluidSynthRenderer(fluidsynth_path="fluidsynth", project_root=tmp_path)
    monkeypatch.setattr(platform_paths.shutil, "which", lambda name: "/usr/bin/fluidsynth")

    assert Path(renderer._resolve_fluidsynth()) == Path("/usr/bin/fluidsynth")


def test_resolve_no_config_uses_path_lookup(tmp_path, monkeypatch):
    """未配置 fluidsynth_path 时直接查 PATH。"""
    renderer = FluidSynthRenderer(project_root=tmp_path)
    monkeypatch.setattr(platform_paths.shutil, "which", lambda name: "/usr/bin/fluidsynth")

    assert Path(renderer._resolve_fluidsynth()) == Path("/usr/bin/fluidsynth")


def test_resolve_no_config_and_not_on_path_raises_4(tmp_path, monkeypatch):
    """未配置且 PATH 上没有 fluidsynth -> RenderError(4)。"""
    renderer = FluidSynthRenderer(project_root=tmp_path)
    monkeypatch.setattr(platform_paths.shutil, "which", lambda name: None)

    with pytest.raises(RenderError) as exc:
        renderer._resolve_fluidsynth()
    assert exc.value.code == 4
