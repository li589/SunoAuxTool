"""分页（2.6 F+）：``paginate_layout`` 切页、页高重算、页码与多文件导出。"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from sunoauxtool.compose.arranger import ArrangeOptions, arrange
from sunoauxtool.compose.cli import app as compose_app
from sunoauxtool.compose.score import ScoreOptions, export_score
from sunoauxtool.compose.spec import SongSpec
from sunoauxtool.score.layout import layout_score, paginate_layout
from sunoauxtool.score.model import Score
from sunoauxtool.score.svg import render_svg

_runner = CliRunner()


def _score() -> Score:
    data = {
        "schema": 1,
        "song": {"title": "分页测试", "key": "C major", "tempo": 100, "seed": 3},
        "section": [{"id": "a", "bars": 16, "energy": 0.8,
                     "progression": ["C", "Am", "F", "G"] * 4}],
        "track": [{"id": "lead", "role": "melody", "sections": ["a"]}],
    }
    spec = SongSpec.from_dict(data)
    return Score.from_song(arrange(spec, ArrangeOptions(humanize=False)),
                           grand_staff=False)


class TestPaginate:
    def test_no_split_returns_single_page(self) -> None:
        layout = layout_score(_score())
        assert paginate_layout(layout, 0) == [layout]
        assert paginate_layout(layout, len(layout.systems)) == [layout]

    def test_split_into_pages(self) -> None:
        layout = layout_score(_score())
        assert len(layout.systems) >= 2
        pages = paginate_layout(layout, 1)
        assert len(pages) == len(layout.systems)
        assert [p.page_number for p in pages] == list(range(1, len(pages) + 1))
        assert all(p.page_total == len(pages) for p in pages)

    def test_only_first_page_shows_title(self) -> None:
        pages = paginate_layout(layout_score(_score()), 1)
        assert pages[0].show_title is True
        assert all(page.show_title is False for page in pages[1:])

    def test_page_heights_recomputed(self) -> None:
        pages = paginate_layout(layout_score(_score()), 1)
        # 首页含标题区，应比后续页高
        assert pages[0].page_height > pages[1].page_height

    def test_system_positions_are_independent(self) -> None:
        layout = layout_score(_score())
        pages = paginate_layout(layout, 1)
        # 每页第一个系统都从页内顶部开始，说明 y 被重排过（不是共享原值）
        firsts = {round(page.systems[0].y, 1) for page in pages}
        assert len(firsts) <= 2  # 首页含标题偏移，后续页同值

    def test_page_footer_drawn(self) -> None:
        pages = paginate_layout(layout_score(_score()), 1)
        svg = render_svg(pages[1])
        assert "第 2 / " in svg

    def test_single_page_has_no_footer(self) -> None:
        layout = layout_score(_score())
        assert " / 1 页" not in render_svg(paginate_layout(layout, 0)[0])


class TestExport:
    def test_multi_page_files(self, tmp_path: Path) -> None:
        export_score(
            arrange(SongSpec.from_dict({
                "schema": 1,
                "song": {"title": "分页", "key": "C major", "tempo": 100, "seed": 3},
                "section": [{"id": "a", "bars": 16, "energy": 0.8,
                             "progression": ["C", "Am", "F", "G"] * 4}],
                "track": [{"id": "lead", "role": "melody", "sections": ["a"]}],
            }), ArrangeOptions(humanize=False)),
            out_dir=tmp_path,
            options=ScoreOptions(formats=("svg",), systems_per_page=1,
                                 grand_staff=False),
        )
        files = sorted(tmp_path.glob("*.svg"))
        assert len(files) >= 2
        assert files[0].name.endswith("-page01.svg")
        assert "第 1 / " in files[0].read_text(encoding="utf-8")

    def test_single_page_keeps_plain_name(self, tmp_path: Path) -> None:
        export_score(
            arrange(SongSpec.from_dict({
                "schema": 1,
                "song": {"title": "单页", "key": "C major", "tempo": 100, "seed": 3},
                "section": [{"id": "a", "bars": 4, "energy": 0.5,
                             "progression": ["C", "Am"]}],
                "track": [{"id": "lead", "role": "melody", "sections": ["a"]}],
            }), ArrangeOptions(humanize=False)),
            out_dir=tmp_path,
            options=ScoreOptions(formats=("svg",), grand_staff=False),
        )
        assert (tmp_path / "单页.svg").exists()


class TestCli:
    def test_systems_per_page_option(self, tmp_path: Path) -> None:
        toml = tmp_path / "song.toml"
        toml.write_text((
            'schema = 1\n\n[song]\ntitle = "命令行分页"\nkey = "C major"\n'
            'tempo = 100\nseed = 3\n\n[[section]]\nid = "a"\nbars = 16\n'
            'energy = 0.8\nprogression = ["C", "Am", "F", "G"]\n\n'
            '[[track]]\nid = "lead"\nrole = "melody"\nsections = ["a"]\n'
        ), encoding="utf-8")
        result = _runner.invoke(compose_app, [
            "score", str(toml), "-o", str(tmp_path / "out"),
            "--format", "svg", "--systems-per-page", "1",
        ])
        assert result.exit_code == 0, result.output
        assert len(list((tmp_path / "out").glob("*page*.svg"))) >= 2

    def test_grand_staff_flag(self, tmp_path: Path) -> None:
        toml = tmp_path / "piano.toml"
        toml.write_text((
            'schema = 1\n\n[song]\ntitle = "钢琴"\nkey = "C major"\ntempo = 100\n'
            'seed = 3\n\n[[section]]\nid = "a"\nbars = 4\nenergy = 0.6\n'
            'progression = ["C", "Am"]\n\n[[track]]\nid = "piano"\nrole = "harmony"\n'
            'program = 0\nsections = ["a"]\n'
        ), encoding="utf-8")
        out = tmp_path / "out"
        assert _runner.invoke(compose_app, [
            "score", str(toml), "-o", str(out), "--format", "musicxml",
        ]).exit_code == 0
        assert "<group-symbol>brace</group-symbol>" in (
            (out / "钢琴.musicxml").read_text(encoding="utf-8")
        )
        out2 = tmp_path / "out2"
        assert _runner.invoke(compose_app, [
            "score", str(toml), "-o", str(out2), "--format", "musicxml",
            "--no-grand-staff",
        ]).exit_code == 0
        assert "<group-symbol>brace</group-symbol>" not in (
            (out2 / "钢琴.musicxml").read_text(encoding="utf-8")
        )
