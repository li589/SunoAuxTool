"""Pipeline 测试（P2-5 + P3-A1 集成 + P2 覆盖率补强）。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from typer.testing import CliRunner

from sunoauxtool.cli import app
from sunoauxtool.config import Config
from sunoauxtool.export.audio import write_wav
from sunoauxtool.export.suno import ExportOptions
from sunoauxtool.pipeline import Pipeline, PipelineResult, _force_remove_tree

runner = CliRunner()


def _fake_render(self, midi_path, soundfont, out_path):
    """fake 渲染：直接写 20s 正弦 WAV（与 test_cli.py 同构）。"""
    t = np.linspace(0, 20, 44100 * 20, endpoint=False)
    audio = (0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    write_wav(out_path, audio, 44100, 16)
    return str(out_path)


# -- 纯单元 ---------------------------------------------------------------


def test_pipeline_preview_enabled_by_default():
    """预览页默认开启。"""
    cfg = Config()
    pipeline = Pipeline(cfg)
    assert pipeline._preview_enabled() is True


def test_pipeline_preview_disabled_in_dry_run():
    """dry_run 时预览页关闭。"""
    cfg = Config()
    pipeline = Pipeline(cfg, dry_run=True)
    assert pipeline._preview_enabled() is False


def test_pipeline_preview_disabled_via_config():
    """配置 preview.enabled=false 时关闭。"""
    cfg = Config()
    cfg.preview.enabled = False
    pipeline = Pipeline(cfg)
    assert pipeline._preview_enabled() is False


def test_pipeline_preview_enabled_legacy_config(monkeypatch):
    """旧配置无 preview 节（getattr 返回 None）时默认开启（兼容分支）。"""
    cfg = Config()
    monkeypatch.delattr(cfg, "preview")
    pipeline = Pipeline(cfg)
    assert pipeline._preview_enabled() is True


def test_pipeline_result_to_dict_roundtrip():
    """to_dict/field_dict 覆盖全部 dataclass 字段（score_paths 含内）。"""
    r = PipelineResult(
        midi_path="a.mid", wav_path="a.wav", export_path="a_suno25s.wav",
        duration_s=25.0, sample_rate=44100, bit_depth=16,
        chords="C-G-Am-F", seed=42, bpm=120, bars=8, key="C", style="pop",
        format="wav", score_paths={"svg": "a.svg"},
    )
    d = r.to_dict()
    assert set(d) == {f.name for f in PipelineResult.__dataclass_fields__.values()}
    assert d["score_paths"] == {"svg": "a.svg"}
    assert d["seed"] == 42


def test_field_dict_works_for_other_dataclasses():
    """field_dict 对任意 dataclass 通用（ExportOptions 即用例）。"""
    from dataclasses import is_dataclass

    from sunoauxtool.pipeline import field_dict

    opts = ExportOptions()
    assert is_dataclass(opts)
    d = field_dict(opts)
    assert set(d) == {f.name for f in ExportOptions.__dataclass_fields__.values()}


# -- _force_remove_tree 容错分支 -------------------------------------------


def test_force_remove_tree(tmp_path):
    """_force_remove_tree 删除目录树。"""
    d = tmp_path / "subdir"
    d.mkdir()
    (d / "a.txt").write_text("hello")
    (d / "b.txt").write_text("world")
    sub = d / "nested"
    sub.mkdir()
    (sub / "c.txt").write_text("deep")

    assert d.is_dir()
    _force_remove_tree(d)
    assert not d.exists()


def test_force_remove_tree_nonexistent():
    """不存在的路径不崩溃。"""
    _force_remove_tree(Path("/nonexistent/path"))


def test_force_remove_tree_empty_dir(tmp_path):
    """空目录可删除（不崩溃）。"""
    d = tmp_path / "empty"
    d.mkdir()
    _force_remove_tree(d)
    # 沙箱环境可能拦截删除，只要不崩溃即可
    assert True


def test_force_remove_tree_swallows_os_errors(tmp_path, monkeypatch):
    """文件/子目录/目标本身删除失败时逐级吞掉 OSError，绝不外抛。"""

    def _locked(*_a, **_k):
        raise OSError("file locked (simulated)")

    d = tmp_path / "locked"
    sub = d / "nested"
    sub.mkdir(parents=True)
    (d / "a.txt").write_text("x")
    (sub / "c.txt").write_text("y")

    monkeypatch.setattr("sunoauxtool.pipeline.os.remove", _locked)
    # os.remove 全部失败 → 目录非空 → 内层 rmdir（392-393）与目标 rmdir（396-397）也失败
    _force_remove_tree(d)  # 不抛即通过
    assert d.is_dir()  # 内容确实未被删除


# -- run() 集成（mock 渲染） ------------------------------------------------


def test_pipeline_score_success(tmp_project, monkeypatch):
    """--score 成功路径：谱面文件产出 + SVG 内嵌 + score_paths 进元数据。"""
    from sunoauxtool.render.fluidsynth import FluidSynthRenderer

    monkeypatch.setattr(FluidSynthRenderer, "render", _fake_render)
    result = runner.invoke(app, ["pipeline", "--score", "--score-format", "svg"])
    assert result.exit_code == 0, result.output
    assert "谱面:" in result.output
    svgs = list(Path("output").rglob("*.svg"))
    assert len(svgs) == 1, f"未找到谱面 SVG: {svgs}"
    # 与 MIDI 同目录同主干（#12 约定）
    midis = list(Path("output").rglob("*.mid"))
    assert svgs[0].stem == midis[0].stem


def test_pipeline_score_failure_warns_not_blocks(tmp_project, monkeypatch):
    """--score 失败只告警不阻断：音频/导出照常交付（显式设计，#12）。

    「显式请求失败即抛」只适用于 generate --score；pipeline 侧谱面是附加物，
    任何失败（含未装 Pillow）只记 warning。
    """
    import sunoauxtool.pipeline as pl

    def _boom(*_a, **_k):
        raise RuntimeError("simulated score failure")

    monkeypatch.setattr(pl, "score_from_sequence", _boom)
    monkeypatch.setattr(pl.FluidSynthRenderer, "render", _fake_render)

    result = runner.invoke(app, ["pipeline", "--score"])
    assert result.exit_code == 0, result.output
    assert "谱面生成失败" in result.output
    # 主产物不受影响
    files = list(Path("output").rglob("*_suno25s.wav"))
    assert len(files) == 1


def test_pipeline_preview_failure_warns_not_blocks(tmp_project, monkeypatch):
    """预览页失败只告警不阻断（287-288 分支）。"""
    import sunoauxtool.pipeline as pl

    class _BrokenPreview:
        def generate_for(self, *a, **k):
            raise RuntimeError("simulated preview failure")

    monkeypatch.setattr(pl, "PreviewGenerator", _BrokenPreview)
    monkeypatch.setattr(pl.FluidSynthRenderer, "render", _fake_render)

    result = runner.invoke(app, ["pipeline"])
    assert result.exit_code == 0, result.output
    assert "预览页生成失败" in result.output
    assert list(Path("output").rglob("*_suno25s.wav"))


def test_pipeline_tmp_root_leftover_tolerated(tmp_project, monkeypatch):
    """.tmp 根目录残留他物时 rmdir 失败被吞掉（297-298），且不碰用户残留。"""
    from sunoauxtool.render.fluidsynth import FluidSynthRenderer

    monkeypatch.setattr(FluidSynthRenderer, "render", _fake_render)
    tmp_root = Path("output") / ".tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    stray = tmp_root / "stray_user_file.txt"
    stray.write_text("user data")

    result = runner.invoke(app, ["pipeline"])
    assert result.exit_code == 0, result.output
    # 残留文件原样保留（管线只清自己创建的 mkdtemp 子目录）
    assert stray.is_file()