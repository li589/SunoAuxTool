"""compose 子命令端到端测试（2.0 PR-C）：init / validate / build。

覆盖三层：
- 命令行为（生成模板、校验、构建产物、`--dry-run` 不落盘）
- 错误码透传（40 语义 / 44 结构 / 1 参数）
- 与既有入口的注册关系（`sunoaux compose` 与 `sunoauxtool compose`）
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sunoauxtool.aggregate import app as aggregate_app
from sunoauxtool.cli import app as core_app
from sunoauxtool.compose.cli import app
from sunoauxtool.compose.spec import SongSpec
from sunoauxtool.midi.io import read_smf

runner = CliRunner()

BAD_SEMANTIC = """\
schema = 1
[song]
title = "bad"
tempo = 96

[[section]]
id = "a"
bars = 2

[[track]]
id = "piano"
program = 200
"""

BAD_SCHEMA = """\
schema = 1
[song]
title = "bad"
tmpo = 96
"""


def _init(tmp_path: Path, name: str = "night") -> Path:
    result = runner.invoke(app, ["init", name, "-o", str(tmp_path / name)])
    assert result.exit_code == 0, result.output
    return tmp_path / name / "song.toml"


class TestRegistration:
    def test_compose_group_listed(self) -> None:
        result = runner.invoke(core_app, ["compose", "--help"])
        assert result.exit_code == 0
        for command in ("init", "validate", "build"):
            assert command in result.output

    def test_aggregate_forwards(self) -> None:
        result = runner.invoke(aggregate_app, ["compose", "--help"])
        assert result.exit_code == 0
        assert "song.toml" in result.output

    def test_top_level_help_mentions_compose(self) -> None:
        assert "compose" in runner.invoke(core_app, ["--help"]).output


class TestInit:
    def test_writes_valid_template(self, tmp_path: Path) -> None:
        path = _init(tmp_path)
        assert path.exists()
        spec = SongSpec.from_toml(path)
        spec.validate()
        assert spec.total_bars == 8
        assert [track.role for track in spec.tracks] == ["harmony", "bass", "drums"]

    def test_template_has_notes(self, tmp_path: Path) -> None:
        spec = SongSpec.from_toml(_init(tmp_path))
        assert sum(len(track.notes) for track in spec.tracks) == 64
        assert spec.tracks[0].notes[0].pitch == 62

    def test_title_option(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app, ["init", "night", "-o", str(tmp_path / "night"), "--title", "夜航"]
        )
        assert result.exit_code == 0
        spec = SongSpec.from_toml(tmp_path / "night" / "song.toml")
        assert spec.title == "夜航"

    def test_refuses_overwrite(self, tmp_path: Path) -> None:
        _init(tmp_path)
        again = runner.invoke(app, ["init", "night", "-o", str(tmp_path / "night")])
        assert again.exit_code == 1
        assert "--force" in again.output or "--force" in (
            again.stderr if hasattr(again, "stderr") else ""
        )

    def test_force_overwrites(self, tmp_path: Path) -> None:
        path = _init(tmp_path)
        path.write_text("schema = 1\n", encoding="utf-8")
        result = runner.invoke(
            app, ["init", "night", "-o", str(tmp_path / "night"), "--force"]
        )
        assert result.exit_code == 0
        assert "[[track]]" in path.read_text(encoding="utf-8")


class TestValidate:
    def test_ok(self, tmp_path: Path) -> None:
        path = _init(tmp_path)
        result = runner.invoke(app, ["validate", str(path)])
        assert result.exit_code == 0, result.output
        assert "校验通过" in result.output
        assert "小节" in result.output

    def test_ok_json(self, tmp_path: Path) -> None:
        path = _init(tmp_path)
        result = runner.invoke(app, ["validate", str(path), "--json"])
        payload = json.loads(result.output)
        assert result.exit_code == 0
        assert payload["ok"] is True
        assert payload["summary"]["bars"] == 8

    def test_missing_file(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["validate", str(tmp_path / "nope.toml")])
        assert result.exit_code == 1
        assert "清单不存在" in result.output

    def test_semantic_error_code(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.toml"
        path.write_text(BAD_SEMANTIC, encoding="utf-8")
        result = runner.invoke(app, ["validate", str(path), "--json"])
        payload = json.loads(result.output)
        assert result.exit_code == 40
        assert payload == {
            "ok": False, "path": "/track/0/program",
            "code": 40, "message": "program 必须在 0..127，实际为 200",
        }

    def test_schema_error_code(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.toml"
        path.write_text(BAD_SCHEMA, encoding="utf-8")
        result = runner.invoke(app, ["validate", str(path), "--json"])
        assert result.exit_code == 44
        assert json.loads(result.output)["message"] == "未知字段: tmpo"

    def test_human_error_reports_pointer(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.toml"
        path.write_text(BAD_SEMANTIC, encoding="utf-8")
        result = runner.invoke(app, ["validate", str(path)])
        assert result.exit_code == 40
        assert "/track/0/program" in result.output
        assert "错误 [40]" in result.output  # 人读模式：带错误码前缀


class TestBuild:
    def test_build_default_out_dir(self, tmp_path: Path, monkeypatch) -> None:
        path = _init(tmp_path, "night")
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["build", str(path)])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "out" / "night" / "night.mid").exists()
        assert (tmp_path / "out" / "night" / "arrangement.json").exists()

    def test_build_explicit_out(self, tmp_path: Path) -> None:
        path = _init(tmp_path)
        target = tmp_path / "build"
        result = runner.invoke(app, ["build", str(path), "-o", str(target)])
        assert result.exit_code == 0, result.output
        document = read_smf(target / "night.mid")
        assert [(track.name, track.channel, track.program, track.is_drum)
                for track in document.tracks] == [
            ("钢琴", 0, 0, False), ("贝斯", 1, 33, False), ("鼓组", 9, 0, True),
        ]
        assert [point.bpm for point in document.tempo_map] == [96.0]
        assert document.markers == [(0.0, "主歌"), (16.0, "副歌")]
        assert "通道分配" in result.output

    def test_snapshot_matches_song(self, tmp_path: Path) -> None:
        path = _init(tmp_path)
        target = tmp_path / "build"
        runner.invoke(app, ["build", str(path), "-o", str(target)])
        snapshot = json.loads((target / "arrangement.json").read_text(encoding="utf-8"))
        assert snapshot["schema_version"] == 1
        assert [section["id"] for section in snapshot["sections"]] == ["verse", "chorus"]
        assert len(snapshot["tracks"]) == 3

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        path = _init(tmp_path)
        target = tmp_path / "build"
        result = runner.invoke(app, ["build", str(path), "-o", str(target), "--dry-run"])
        assert result.exit_code == 0
        assert "[dry-run]" in result.output
        assert not target.exists()

    def test_deterministic_output(self, tmp_path: Path) -> None:
        """G8：同一清单两次构建，MIDI 字节一致。"""
        path = _init(tmp_path)
        digests = []
        for index in range(2):
            target = tmp_path / f"build{index}"
            runner.invoke(app, ["build", str(path), "-o", str(target)])
            digests.append(hashlib.sha256((target / "night.mid").read_bytes()).hexdigest())
        assert digests[0] == digests[1]

    def test_build_rejects_bad_spec(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.toml"
        path.write_text(BAD_SEMANTIC, encoding="utf-8")
        result = runner.invoke(app, ["build", str(path), "-o", str(tmp_path / "o")])
        assert result.exit_code == 40
        assert not (tmp_path / "o").exists()

    def test_build_missing_file(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["build", str(tmp_path / "nope.toml")])
        assert result.exit_code == 1

    def test_build_via_aggregate(self, tmp_path: Path) -> None:
        path = _init(tmp_path)
        target = tmp_path / "agg"
        result = runner.invoke(
            aggregate_app, ["compose", "build", str(path), "-o", str(target)]
        )
        assert result.exit_code == 0, result.output
        assert (target / "night.mid").exists()


@pytest.mark.parametrize("name", ["夜航", "a b", "x/y"])
def test_slug_is_filesystem_safe(tmp_path: Path, name: str) -> None:
    """曲名带空格/斜杠/中文时输出文件名仍然安全。"""
    path = _init(tmp_path, "proj")
    content = path.read_text(encoding="utf-8").replace('title = "proj"', f'title = "{name}"')
    path.write_text(content, encoding="utf-8")
    result = runner.invoke(app, ["build", str(path), "-o", str(tmp_path / "out")])
    assert result.exit_code == 0, result.output
    produced = list((tmp_path / "out").glob("*.mid"))
    assert len(produced) == 1
    assert "/" not in produced[0].name
