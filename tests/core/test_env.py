"""M-1 环境接入单测：ProjectRootResolver + PathResolver 三分支（OK/MISSING/BROKEN）。

路径探测通过 runner 注入，不依赖真实二进制（M-1 验收 6）。

跨平台注意：用作「可执行文件」的占位文件（`b"MZ"`）必须补可执行位
（`chmod(0o755)`）——POSIX 的 `os.access(X_OK)` 要求真实执行位，Windows 上则
近似等价于存在性检查。不补执行位会让本文件在 Linux/CI 上大面积误判为 BROKEN。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sunoauxtool import platform_paths
from sunoauxtool.config import Config
from sunoauxtool.env import PathResolver, ProbeStatus, ProjectRootResolver
from sunoauxtool.exceptions import ConfigError, ModuleError, RenderError

#: 注入 runner：模拟 fluidsynth 加载 SF2 成功（退出码 0，无错误文本）
def _OK_RUNNER(cmd):
    return SimpleNamespace(returncode=0, stdout="FluidSynth runtime version 2.5.7", stderr="")


#: 模拟 SF2 无法识别（退出码 0 但输出错误文本 —— Windows fluidsynth 实际行为）
def _BROKEN_RUNNER(cmd):
    return SimpleNamespace(
        returncode=0,
        stdout="",
        stderr="fluidsynth: error: fluid_is_soundfont(): fopen() failed: 'File does not exist.'\n"
        "Parameter '/tmp/x.sf2' not a SoundFont or MIDI file or error occurred identifying it.",
    )


def _make_cfg(tmp_path, *, fluidsynth=None, soundfont=None, soundfont_backup=None) -> Config:
    """构造测试用 Config（绝对路径注入）。"""
    cfg = Config()
    return cfg.merge_cli(
        fluidsynth=fluidsynth or str(tmp_path / "fluidsynth.exe"),
        soundfont=soundfont or str(tmp_path / "a.sf2"),
        soundfont_backup=soundfont_backup or str(tmp_path / "b.sf2"),
    )


# ---------------------------------------------------------------------------
# ProjectRootResolver
# ---------------------------------------------------------------------------

def test_project_root_base_injection(tmp_path):
    """显式注入 base 直接返回。"""
    root = ProjectRootResolver(base=tmp_path).resolve()
    assert root == tmp_path.resolve()


def test_project_root_upward_search(tmp_path):
    """向上查找 pyproject.toml。"""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    root = ProjectRootResolver(start=sub).resolve()
    assert root == tmp_path.resolve()


def test_project_root_module_dir_search(tmp_path):
    """向上查找 module/ 目录。"""
    (tmp_path / "module").mkdir()
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    root = ProjectRootResolver(start=sub).resolve()
    assert root == tmp_path.resolve()


def test_project_root_resolve_returns_path(tmp_path):
    """resolve 返回绝对 Path 且不抛错（找不到项目根时回退 start 的兼容语义）。"""
    root = ProjectRootResolver(start=tmp_path).resolve()
    assert isinstance(root, Path)
    assert root.is_absolute()


# ---------------------------------------------------------------------------
# PathResolver：OK 分支
# ---------------------------------------------------------------------------

def test_probe_all_ok(tmp_path):
    """fluidsynth + soundfont 均 OK，且 SF2 可被加载 -> ensure_ready 不抛错。"""
    fs_bin = tmp_path / "fluidsynth.exe"
    fs_bin.write_bytes(b"MZ")
    fs_bin.chmod(0o755)  # POSIX 需真实执行位，否则探测判 BROKEN（Windows 上为 no-op）
    sf = tmp_path / "good.sf2"
    sf.write_bytes(b"RIFFgood")
    cfg = _make_cfg(tmp_path, fluidsynth=str(fs_bin), soundfont=str(sf),
                    soundfont_backup=str(tmp_path / "backup.sf2"))
    resolver = PathResolver(cfg, project_root=tmp_path, runner=_OK_RUNNER)
    probes = resolver.probe_all()
    status = {p.component: p.status for p in probes}
    assert status["fluidsynth"] == ProbeStatus.OK
    assert status["soundfont"] == ProbeStatus.OK
    resolver.ensure_ready()  # 不应抛错


def test_resolve_soundfont_primary(tmp_path):
    """主音色库存在时返回主库。"""
    sf = tmp_path / "a.sf2"
    sf.write_bytes(b"RIFFa")
    cfg = _make_cfg(tmp_path, soundfont=str(sf))
    resolver = PathResolver(cfg, project_root=tmp_path)
    assert resolver.resolve_soundfont() == sf.resolve()


# ---------------------------------------------------------------------------
# PathResolver：MISSING / BROKEN 分支（module 默认路径 -> ModuleError 7）
# ---------------------------------------------------------------------------

def test_module_fluidsynth_missing_raises_7(tmp_path):
    """module/ 下 fluidsynth 缺失 -> ensure_ready 抛 ModuleError(7)。"""
    cfg = Config().merge_cli(
        fluidsynth="module/fluidsynth/bin/fluidsynth.exe",
        soundfont="module/GeneralUser_GS/GeneralUser-GS/GeneralUser-GS.sf2",
        soundfont_backup="module/GeneralUser_GS/ColomboGMGS2_SF2/ColomboGMGS2.sf2",
    )
    resolver = PathResolver(cfg, project_root=tmp_path)  # tmp 下无 module/
    with pytest.raises(ModuleError) as exc:
        resolver.ensure_ready()
    assert exc.value.code == 7
    assert "fluidsynth" in str(exc.value)


def test_module_soundfont_missing_raises_7(tmp_path):
    """module/ 下主+备选音色库均缺失 -> resolve_soundfont 抛 ModuleError(7)。"""
    fs_bin = tmp_path / "fluidsynth.exe"
    fs_bin.write_bytes(b"MZ")
    fs_bin.chmod(0o755)  # POSIX 需真实执行位，否则探测判 BROKEN（Windows 上为 no-op）
    cfg = Config().merge_cli(
        fluidsynth=str(fs_bin),
        soundfont="module/x/a.sf2",
        soundfont_backup="module/x/b.sf2",
    )
    resolver = PathResolver(cfg, project_root=tmp_path)
    with pytest.raises(ModuleError) as exc:
        resolver.resolve_soundfont()
    assert exc.value.code == 7


def test_soundfont_broken_load_raises_7(tmp_path):
    """SF2 存在但 fluidsynth 无法加载（BROKEN）-> ensure_ready 抛 ModuleError(7)。"""
    fs_bin = tmp_path / "fluidsynth.exe"
    fs_bin.write_bytes(b"MZ")
    fs_bin.chmod(0o755)  # POSIX 需真实执行位，否则探测判 BROKEN（Windows 上为 no-op）
    sf = tmp_path / "broken.sf2"
    sf.write_bytes(b"RIFFgarbage")
    cfg = Config().merge_cli(
        fluidsynth=str(fs_bin),
        soundfont="module/x/broken.sf2",  # module 相对路径，指向 project_root 下
        soundfont_backup="module/x/backup.sf2",
    )
    # 实际在 project_root/module/x/broken.sf2 放置文件
    (tmp_path / "module" / "x").mkdir(parents=True)
    (tmp_path / "module" / "x" / "broken.sf2").write_bytes(b"RIFFgarbage")
    resolver = PathResolver(cfg, project_root=tmp_path, runner=_BROKEN_RUNNER)
    probes = resolver.probe_all()
    sf_probe = next(p for p in probes if p.component == "soundfont")
    assert sf_probe.status == ProbeStatus.BROKEN
    with pytest.raises(ModuleError) as exc:
        resolver.ensure_ready()
    assert exc.value.code == 7


# ---------------------------------------------------------------------------
# PathResolver：双音色库回退（M-1e）
# ---------------------------------------------------------------------------

def test_soundfont_backup_fallback(tmp_path):
    """主音色库缺失、备选存在 -> resolve_soundfont 返回备选；ensure_ready 不抛错。"""
    fs_bin = tmp_path / "fluidsynth.exe"
    fs_bin.write_bytes(b"MZ")
    fs_bin.chmod(0o755)  # POSIX 需真实执行位，否则探测判 BROKEN（Windows 上为 no-op）
    backup = tmp_path / "module" / "x" / "backup.sf2"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"RIFFbackup")
    cfg = Config().merge_cli(
        fluidsynth=str(fs_bin),
        soundfont="module/x/primary.sf2",
        soundfont_backup="module/x/backup.sf2",
    )
    resolver = PathResolver(cfg, project_root=tmp_path, runner=_OK_RUNNER)
    assert resolver.resolve_soundfont() == backup.resolve()
    resolver.ensure_ready()  # 主库 MISSING + 备选 OK -> 不抛错


# ---------------------------------------------------------------------------
# PathResolver：非 module 路径 -> 保持 P0 语义（ConfigError 2 / RenderError 4）
# ---------------------------------------------------------------------------

def test_non_module_soundfont_missing_raises_2(tmp_path):
    """非 module 音色库缺失 -> ConfigError(2)，且 ensure_ready 不抛错（P0 兼容）。"""
    cfg = _make_cfg(tmp_path)  # 全部为绝对路径（非 module）
    resolver = PathResolver(cfg, project_root=tmp_path)
    resolver.ensure_ready()  # 仅记录分级日志
    with pytest.raises(ConfigError) as exc:
        resolver.resolve_soundfont()
    assert exc.value.code == 2


def test_non_module_fluidsynth_missing_raises_4(tmp_path):
    """非 module fluidsynth 缺失 -> resolve_fluidsynth 抛 RenderError(4)。"""
    cfg = Config().merge_cli(fluidsynth=str(tmp_path / "nope.exe"))
    resolver = PathResolver(cfg, project_root=tmp_path)
    with pytest.raises(RenderError) as exc:
        resolver.resolve_fluidsynth()
    assert exc.value.code == 4


def test_probe_sf2_loadable_true(tmp_path):
    """SF2 可加载：退出码 0 且无错误文本 -> True。"""
    fs_bin = tmp_path / "fs.exe"
    fs_bin.write_bytes(b"MZ")
    fs_bin.chmod(0o755)  # POSIX 需真实执行位，否则探测判 BROKEN（Windows 上为 no-op）
    sf = tmp_path / "ok.sf2"
    sf.write_bytes(b"RIFFok")
    cfg = _make_cfg(tmp_path, fluidsynth=str(fs_bin), soundfont=str(sf))
    resolver = PathResolver(cfg, project_root=tmp_path, runner=_OK_RUNNER)
    assert resolver._probe_sf2_loadable(fs_bin, sf) is True


def test_probe_sf2_loadable_false(tmp_path):
    """SF2 无法识别（含错误文本）-> False。"""
    fs_bin = tmp_path / "fs.exe"
    fs_bin.write_bytes(b"MZ")
    fs_bin.chmod(0o755)  # POSIX 需真实执行位，否则探测判 BROKEN（Windows 上为 no-op）
    sf = tmp_path / "bad.sf2"
    sf.write_bytes(b"garbage")
    cfg = _make_cfg(tmp_path, fluidsynth=str(fs_bin), soundfont=str(sf))
    resolver = PathResolver(cfg, project_root=tmp_path, runner=_BROKEN_RUNNER)
    assert resolver._probe_sf2_loadable(fs_bin, sf) is False


def test_probe_sf2_loadable_runner_exception(tmp_path):
    """runner 抛异常（启动失败）-> False。"""

    def _boom(cmd):
        raise OSError("no such binary")

    fs_bin = tmp_path / "fs.exe"
    fs_bin.write_bytes(b"MZ")
    fs_bin.chmod(0o755)  # POSIX 需真实执行位，否则探测判 BROKEN（Windows 上为 no-op）
    sf = tmp_path / "x.sf2"
    sf.write_bytes(b"RIFFx")
    cfg = _make_cfg(tmp_path, fluidsynth=str(fs_bin), soundfont=str(sf))
    resolver = PathResolver(cfg, project_root=tmp_path, runner=_boom)
    assert resolver._probe_sf2_loadable(fs_bin, sf) is False


# ---------------------------------------------------------------------------
# 跨平台回落（platform_paths）：捆绑的 Windows 二进制在 POSIX 上改用系统 fluidsynth
# ---------------------------------------------------------------------------

def test_system_fluidsynth_none_on_windows(monkeypatch):
    """Windows 上恒返回 None（永不回落），保持既有行为逐字不变。"""
    monkeypatch.setattr(platform_paths, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_paths.shutil, "which", lambda name: "/usr/bin/fluidsynth")
    assert platform_paths.system_fluidsynth() is None


def test_system_fluidsynth_uses_which_on_posix(monkeypatch):
    """POSIX 上返回 PATH 命中的 fluidsynth。"""
    monkeypatch.setattr(platform_paths, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_paths.shutil, "which", lambda name: "/usr/bin/fluidsynth")
    assert platform_paths.system_fluidsynth() == Path("/usr/bin/fluidsynth")


def test_system_fluidsynth_none_when_absent_on_posix(monkeypatch):
    """POSIX 上系统未安装 fluidsynth -> None（调用方维持原有不可用判定）。"""
    monkeypatch.setattr(platform_paths, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_paths.shutil, "which", lambda name: None)
    assert platform_paths.system_fluidsynth() is None


def test_probe_falls_back_to_system_on_posix(tmp_path, monkeypatch):
    """捆绑二进制存在但不可执行 + 系统有 fluidsynth -> 探测 OK 且指向系统版本。

    本地 Windows 上 os.access(X_OK) 对任意文件返回 True，故强制其为 False
    以复现 POSIX 语义；CI（Linux）上该 patch 只是让判定确定化。
    """
    fs_bin = tmp_path / "fluidsynth.exe"
    fs_bin.write_bytes(b"MZ")  # 故意不 chmod：模拟 PE 文件无执行位
    sf = tmp_path / "good.sf2"
    sf.write_bytes(b"RIFFgood")
    cfg = _make_cfg(tmp_path, fluidsynth=str(fs_bin), soundfont=str(sf),
                    soundfont_backup=str(tmp_path / "b.sf2"))
    monkeypatch.setattr(platform_paths, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_paths.shutil, "which", lambda name: "/usr/bin/fluidsynth")
    monkeypatch.setattr("sunoauxtool.env.os.access", lambda p, mode: False)

    resolver = PathResolver(cfg, project_root=tmp_path, runner=_OK_RUNNER)
    fs_probe = next(p for p in resolver.probe_all() if p.component == "fluidsynth")
    assert fs_probe.status == ProbeStatus.OK
    assert fs_probe.path == Path("/usr/bin/fluidsynth")
    resolver.ensure_ready()  # 回落可用 -> 不抛错


def test_probe_keeps_broken_when_no_system_fallback(tmp_path, monkeypatch):
    """不可执行且系统无 fluidsynth -> 维持 BROKEN（不静默放过坏环境）。"""
    fs_bin = tmp_path / "fluidsynth.exe"
    fs_bin.write_bytes(b"MZ")
    sf = tmp_path / "good.sf2"
    sf.write_bytes(b"RIFFgood")
    cfg = _make_cfg(tmp_path, fluidsynth=str(fs_bin), soundfont=str(sf),
                    soundfont_backup=str(tmp_path / "b.sf2"))
    monkeypatch.setattr(platform_paths, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_paths.shutil, "which", lambda name: None)
    monkeypatch.setattr("sunoauxtool.env.os.access", lambda p, mode: False)

    resolver = PathResolver(cfg, project_root=tmp_path, runner=_OK_RUNNER)
    fs_probe = next(p for p in resolver.probe_all() if p.component == "fluidsynth")
    assert fs_probe.status == ProbeStatus.BROKEN
    with pytest.raises(RenderError) as exc:  # 非 module 的 BROKEN -> RenderError(4)
        resolver.ensure_ready()
    assert exc.value.code == 4


def test_resolve_fluidsynth_falls_back_on_posix(tmp_path, monkeypatch):
    """resolve_fluidsynth 在不可执行时同样回落到系统版本。"""
    fs_bin = tmp_path / "fluidsynth.exe"
    fs_bin.write_bytes(b"MZ")
    cfg = _make_cfg(tmp_path, fluidsynth=str(fs_bin))
    monkeypatch.setattr(platform_paths, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_paths.shutil, "which", lambda name: "/usr/bin/fluidsynth")
    monkeypatch.setattr("sunoauxtool.env.os.access", lambda p, mode: False)

    resolver = PathResolver(cfg, project_root=tmp_path)
    assert resolver.resolve_fluidsynth() == Path("/usr/bin/fluidsynth")


def test_resolve_fluidsynth_module_no_fallback_raises_7(tmp_path, monkeypatch):
    """module 路径不可执行且系统无回落 -> resolve_fluidsynth 抛 ModuleError(7)。"""
    module_bin = tmp_path / "module" / "fluidsynth" / "bin"
    module_bin.mkdir(parents=True)
    fs_bin = module_bin / "fluidsynth.exe"
    fs_bin.write_bytes(b"MZ")
    cfg = Config().merge_cli(fluidsynth="module/fluidsynth/bin/fluidsynth.exe")
    monkeypatch.setattr(platform_paths, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_paths.shutil, "which", lambda name: None)
    monkeypatch.setattr("sunoauxtool.env.os.access", lambda p, mode: False)

    resolver = PathResolver(cfg, project_root=tmp_path)
    with pytest.raises(ModuleError) as exc:
        resolver.resolve_fluidsynth()
    assert exc.value.code == 7


def test_resolve_fluidsynth_non_module_no_fallback_raises_4(tmp_path, monkeypatch):
    """非 module 路径不可执行且系统无回落 -> RenderError(4)。"""
    fs_bin = tmp_path / "fluidsynth.exe"
    fs_bin.write_bytes(b"MZ")
    cfg = _make_cfg(tmp_path, fluidsynth=str(fs_bin))
    monkeypatch.setattr(platform_paths, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_paths.shutil, "which", lambda name: None)
    monkeypatch.setattr("sunoauxtool.env.os.access", lambda p, mode: False)

    resolver = PathResolver(cfg, project_root=tmp_path)
    with pytest.raises(RenderError) as exc:
        resolver.resolve_fluidsynth()
    assert exc.value.code == 4


# ---------------------------------------------------------------------------
# SF2 加载校验的音频驱动：无声卡环境（CI 容器）不能因音频设备失败而误判
# ---------------------------------------------------------------------------

def test_sf2_probe_audio_args_uses_file_driver():
    """统一用 file 写入驱动：绕开音频设备，且两平台都有（故无需平台分支）。"""
    assert platform_paths.sf2_probe_audio_args() == ["-a", "file"]


def test_probe_sf2_loadable_command_includes_file_driver(tmp_path):
    """SF2 校验命令确实带上 file 驱动参数（不依赖真实声卡）。"""
    fs_bin = tmp_path / "fluidsynth"
    fs_bin.write_bytes(b"MZ")
    sf = tmp_path / "x.sf2"
    sf.write_bytes(b"RIFFx")
    captured: dict = {}

    def _runner(cmd):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    cfg = _make_cfg(tmp_path, fluidsynth=str(fs_bin), soundfont=str(sf))
    resolver = PathResolver(cfg, project_root=tmp_path, runner=_runner)

    assert resolver._probe_sf2_loadable(fs_bin, sf) is True
    cmd = captured["cmd"]
    assert cmd[1] == "-ni"
    assert cmd[2:4] == ["-a", "file"]


def test_probe_sf2_loadable_uses_isolated_cwd(tmp_path):
    """真实执行路径须以临时目录为 cwd —— file 驱动会在 cwd 写出 fluidsynth.wav。"""
    fs_bin = tmp_path / "fluidsynth"
    fs_bin.write_bytes(b"MZ")
    sf = tmp_path / "x.sf2"
    sf.write_bytes(b"RIFFx")
    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    cfg = _make_cfg(tmp_path, fluidsynth=str(fs_bin), soundfont=str(sf))
    resolver = PathResolver(cfg, project_root=tmp_path)  # 不注入 runner -> 走真实 subprocess 分支
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("sunoauxtool.env.subprocess.run", _fake_run)
        assert resolver._probe_sf2_loadable(fs_bin, sf) is True

    assert captured.get("cwd"), "应以临时目录作为 cwd 以隔离 fluidsynth.wav"
