"""compose score / compose render 的 CLI 与导出层测试（2.0 PR-E/F）。"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sunoauxtool.aggregate import app as aggregate_app
from sunoauxtool.cli import app as core_app
from sunoauxtool.compose.cli import app
from sunoauxtool.compose.score import (
    SCORE_FORMATS,
    ScoreOptions,
    ScoreResult,
    build_score,
    describe_score,
    export_score,
)
from sunoauxtool.compose.spec import SongSpec

runner = CliRunner()

SPEC = """\
schema = 1

[song]
title = "夜航"
key = "D minor"
tempo = 96
seed = 7

[[section]]
id = "verse"
bars = 2
energy = 0.9
progression = ["Dm", "Bb"]

[[track]]
id = "piano"
name = "钢琴"
role = "harmony"

[[track]]
id = "clarinet"
name = "Bb单簧管"
role = "melody"
transpose = 2

[[track]]
id = "bass"
name = "贝斯"
role = "bass"
"""


@pytest.fixture()
def song_path(tmp_path: Path) -> Path:
    path = tmp_path / "song.toml"
    path.write_text(SPEC, encoding="utf-8")
    return path


def _song(song_path: Path):
    from sunoauxtool.compose.arranger import arrange

    return arrange(SongSpec.from_toml(song_path))


class TestBuildScore:
    def test_tracks_and_metadata(self, song_path: Path) -> None:
        score = build_score(_song(song_path), composer="齐见林")
        assert score.title == "夜航"
        assert score.composer == "齐见林"
        # 2.6 起钢琴自动拆两行大谱表：两轨同名、brace 相同，移调轨紧随其后
        assert [track.name for track in score.tracks] == ["钢琴", "钢琴", "Bb单簧管", "贝斯"]
        assert score.tracks[0].brace == score.tracks[1].brace == "piano"
        assert score.tracks[2].transpose == 2

    def test_part_groups_disabled(self, song_path: Path) -> None:
        score = build_score(_song(song_path), part_groups=False)
        assert all(track.part_group == "" for track in score.tracks)


class TestFormats:
    def test_formats_table(self) -> None:
        assert set(SCORE_FORMATS) == {"svg", "png", "musicxml"}

    def test_normalized_formats(self) -> None:
        options = ScoreOptions(formats=("SVG", "xml", "svg"))
        assert options.normalized_formats() == ("svg", "musicxml")

    def test_unknown_format(self) -> None:
        with pytest.raises(ValueError, match="不支持的总谱格式"):
            ScoreOptions(formats=("pdf",)).normalized_formats()


class TestExport:
    def test_svg_only(self, song_path: Path, tmp_path: Path) -> None:
        result = export_score(_song(song_path), out_dir=tmp_path,
                              options=ScoreOptions(formats=("svg",)))
        assert list(result.full) == ["svg"]
        svg = result.full["svg"]
        assert svg.exists() and svg.read_text(encoding="utf-8").startswith("<svg")
        assert result.parts == {}

    def test_all_formats(self, song_path: Path, tmp_path: Path) -> None:
        result = export_score(
            _song(song_path), out_dir=tmp_path,
            options=ScoreOptions(formats=("svg", "png", "musicxml")),
        )
        assert set(result.full) == {"svg", "png", "musicxml"}
        assert result.full["png"].read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        root = ET.fromstring(result.full["musicxml"].read_text(encoding="utf-8"))
        assert root.tag == "score-partwise"

    def test_parts(self, song_path: Path, tmp_path: Path) -> None:
        result = export_score(
            _song(song_path), out_dir=tmp_path / "score",
            options=ScoreOptions(formats=("musicxml",), parts=True),
        )
        assert result.part_count == 3
        assert set(result.parts) == {"钢琴", "Bb单簧管", "贝斯"}
        for files in result.parts.values():
            root = ET.fromstring(files["musicxml"].read_text(encoding="utf-8"))
            assert len(root.findall("part")) == 1
        assert (tmp_path / "score" / "parts").is_dir()

    def test_paths_helper(self, song_path: Path, tmp_path: Path) -> None:
        result = export_score(_song(song_path), out_dir=tmp_path,
                              options=ScoreOptions(formats=("svg",), parts=True))
        assert len(result.paths()) == 4  # 总谱 1 + 分谱 3

    def test_describe(self, song_path: Path, tmp_path: Path) -> None:
        result = export_score(_song(song_path), out_dir=tmp_path,
                              options=ScoreOptions(formats=("svg",), parts=True))
        text = "\n".join(describe_score(result))
        assert "3 声部" in text and "移调声部" in text and "分谱 3 份" in text

    def test_result_dataclass_defaults(self, tmp_path: Path) -> None:
        result = ScoreResult(out_dir=tmp_path)
        assert result.full == {} and result.parts == {} and result.part_count == 0


class TestScoreCli:
    def test_score_default_svg(self, song_path: Path, tmp_path: Path) -> None:
        target = tmp_path / "score"
        result = runner.invoke(app, ["score", str(song_path), "-o", str(target)])
        assert result.exit_code == 0, result.output
        assert (target / "夜航.svg").exists()
        assert "已导出总谱" in result.output

    def test_score_all_formats_with_parts(self, song_path: Path, tmp_path: Path) -> None:
        target = tmp_path / "score"
        result = runner.invoke(app, [
            "score", str(song_path), "-o", str(target),
            "--format", "svg,png,musicxml", "--parts", "--composer", "齐见林",
        ])
        assert result.exit_code == 0, result.output
        assert (target / "夜航.musicxml").exists()
        assert (target / "parts" / "钢琴.svg").exists()
        xml = (target / "夜航.musicxml").read_text(encoding="utf-8")
        assert "<transpose>" in xml and "键盘组" in xml
        assert "齐见林" in xml

    def test_score_bad_format(self, song_path: Path, tmp_path: Path) -> None:
        result = runner.invoke(app, ["score", str(song_path), "--format", "pdf"])
        assert result.exit_code == 1
        assert "不支持的格式" in result.output

    def test_score_missing_file(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["score", str(tmp_path / "nope.toml")])
        assert result.exit_code == 1

    def test_score_bad_spec(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.toml"
        path.write_text("schema = 1\n[song]\ntmpo = 1\n", encoding="utf-8")
        result = runner.invoke(app, ["score", str(path)])
        assert result.exit_code == 44

    def test_score_no_part_groups(self, song_path: Path, tmp_path: Path) -> None:
        target = tmp_path / "score"
        result = runner.invoke(app, [
            "score", str(song_path), "-o", str(target), "--format", "musicxml",
            "--no-part-groups",
        ])
        assert result.exit_code == 0
        assert "<part-group" not in (target / "夜航.musicxml").read_text(encoding="utf-8")

    def test_registered_commands(self) -> None:
        for entry in (core_app, aggregate_app):
            output = runner.invoke(entry, ["compose", "--help"]).output
            for command in ("init", "validate", "build", "render", "score"):
                assert command in output, command

    def test_score_via_aggregate(self, song_path: Path, tmp_path: Path) -> None:
        target = tmp_path / "agg"
        result = runner.invoke(aggregate_app, [
            "compose", "score", str(song_path), "-o", str(target), "--format", "svg",
        ])
        assert result.exit_code == 0, result.output
        assert (target / "夜航.svg").exists()


class TestRenderCli:
    def test_render_dry_run(self, song_path: Path, tmp_path: Path) -> None:
        target = tmp_path / "out"
        result = runner.invoke(app, ["render", str(song_path), "-o", str(target),
                                     "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output
        assert not (target / "stems").exists()

    def test_render_bad_format(self, song_path: Path, tmp_path: Path) -> None:
        result = runner.invoke(app, ["render", str(song_path), "--format", "mp3"])
        assert result.exit_code == 1

    def test_render_missing_file(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["render", str(tmp_path / "nope.toml")])
        assert result.exit_code == 1
