"""P2-5 输出管理单测：目录组织 / 命名 / 防覆盖 / 元数据 JSON。"""

from __future__ import annotations

import json
from pathlib import Path

from sunoauxtool.config import Config
from sunoauxtool.output_manager import ArtifactMeta, OutputManager, RunMeta


def _om(tmp_path, **cli) -> OutputManager:
    cfg = Config().merge_cli(output_dir=str(tmp_path), **cli)
    return OutputManager(cfg)


def test_root_project_date(tmp_path):
    """默认布局 project-date：<root>/<project>/<YYYYMMDD>/。"""
    om = _om(tmp_path, project="myproj")
    root = om.root()
    assert root.parent.name == "myproj"
    assert len(root.name) == 8 and root.name.isdigit()


def test_root_legacy(tmp_path):
    """legacy 布局：<root>/<YYYYMMDD>/（P0 兼容）。"""
    om = _om(tmp_path, project="myproj", layout="legacy")
    root = om.root()
    assert root.parent == tmp_path.resolve()
    assert len(root.name) == 8 and root.name.isdigit()


def test_root_default_project(tmp_path):
    """无项目名 -> 使用 default 目录。"""
    om = _om(tmp_path)
    assert om.root().parent.name == "default"


def test_plan_path_naming(tmp_path):
    """命名 {style}_{bpm}_{seed}_{seq}.{ext}。"""
    om = _om(tmp_path, project="myproj")
    p = om.plan_path(style="pop", bpm=120, seed=42, ext="mid", seq=1)
    assert p.name == "pop_120_42_1.mid"
    assert p.parent == om.root()


def test_plan_path_demo_seed(tmp_path):
    """seed=None -> demo。"""
    om = _om(tmp_path)
    p = om.plan_path(style="pop", bpm=120, seed=None, ext="mid", seq=3)
    assert p.name == "pop_120_demo_3.mid"


def test_plan_path_suno_suffix(tmp_path):
    """Suno 导出件追加 _suno{ds}s 后缀。"""
    om = _om(tmp_path)
    p = om.plan_path(style="pop", bpm=120, seed=7, ext="wav", seq=1, suffix="_suno25s")
    assert p.name == "pop_120_7_1_suno25s.wav"


def test_next_seq_anti_overwrite(tmp_path):
    """同参数重复运行不覆盖：seq 取已有最大 +1。"""
    om = _om(tmp_path, project="myproj")
    root = om.root()
    root.mkdir(parents=True)
    (root / "pop_120_42_1.mid").write_bytes(b"1")
    (root / "pop_120_42_2.mid").write_bytes(b"2")
    (root / "pop_120_42_1.wav").write_bytes(b"w")
    assert om.next_seq("pop", 120, 42, "mid") == 3
    assert om.next_seq("pop", 120, 42, "wav") == 2
    # 不同 style/seed 互不影响
    assert om.next_seq("rock", 120, 42, "mid") == 1
    assert om.next_seq("pop", 120, 43, "mid") == 1


def test_write_metadata_schema(tmp_path):
    """metadata.json 字段完整（schema_version/run/artifacts）。"""
    om = _om(tmp_path, project="myproj")
    run = RunMeta(command="sunoauxtool batch --count 3 --seed 42", seed=42,
                  started_at="2025-08-09T10:00:00", duration_s=3.2,
                  version="0.5.2", config_path=None)
    artifacts = [
        ArtifactMeta(path="output/myproj/20250809/pop_120_42001_1.mid", kind="midi",
                     params={"chords": "C-G-Am-F", "bpm": 120, "bars": 8, "style": "pop"},
                     seed=42001, seq=1, duration_s=16.0),
        ArtifactMeta(path="output/myproj/20250809/pop_120_42001_1_suno25s.wav", kind="suno",
                     params={"duration": 25, "format": "wav", "sample_rate": 44100, "bit_depth": 16},
                     seed=42001, seq=1, duration_s=25.0, sample_rate=44100),
    ]
    target = om.write_metadata(run, artifacts)
    data = json.loads(Path(target).read_text(encoding="utf-8"))
    assert data["schema_version"] == "1.0"
    assert data["run"]["seed"] == 42
    assert data["run"]["version"] == "0.5.2"
    assert len(data["artifacts"]) == 2
    assert data["artifacts"][0]["kind"] == "midi"
    assert data["artifacts"][0]["params"]["chords"] == "C-G-Am-F"
    assert data["artifacts"][1]["contains_vocals"] is False
