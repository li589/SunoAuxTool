"""compose 子命令：清单 → MIDI/编排快照（2.0 PR-C）。

子命令：
    compose init      生成可构建的 song.toml 模板 + 目录骨架
    compose validate  校验清单（结构 44 / 语义 40，错误带 JSON Pointer）
    compose build     song.toml → <name>.mid + arrangement.json（`--dry-run` 只打印计划）

`compose render`（音频/stems）随 2.0 PR-E 落地，`compose score` 随 PR-F 落地。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

import typer

from sunoauxtool.commands.helpers import _guard
from sunoauxtool.compose.spec import SpecError, SongSpec
from sunoauxtool.logging_setup import get_logger
from sunoauxtool.midi.io import write_smf

logger = get_logger("compose.cli")

app = typer.Typer(
    help="代码驱动编曲：song.toml → MIDI + 编排快照（数字轴入口）",
    no_args_is_help=True,
    add_completion=False,
)

#: 文件名安全化白名单（保留中文，只替换路径分隔符等危险字符）
_UNSAFE = re.compile(r'[\\/:*?"<>|\s]+')

_DRUM_CHANNEL = 9


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _slug(text: str, fallback: str = "song") -> str:
    """曲名 → 文件名主干（中文保留，危险字符折叠为 ``_``）。"""
    cleaned = _UNSAFE.sub("_", str(text).strip()).strip("._")
    return cleaned or fallback


def _toml_string(value: str) -> str:
    """最小 TOML 基本字符串转义。"""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _note_block(beat: float, pitch: int, duration: float, velocity: int = 72) -> list[str]:
    return [
        "[[track.note]]",
        f"beat = {beat:g}",
        f"pitch = {pitch}",
        f"duration = {duration:g}",
        f"velocity = {velocity}",
        "",
    ]


def _template(title: str) -> str:
    """生成 8 小节（主歌 4 + 副歌 4）三轨的可构建模板。"""
    lines = [
        "# SunoAuxTool 2.0 编曲清单（song.toml）",
        "# 音符 beat 为全曲绝对拍：4/4 下 1 小节 = 4 拍",
        "schema = 1",
        "",
        "[song]",
        f"title = {_toml_string(title)}",
        'key = "D minor"',
        "tempo = 96",
        'meter = "4/4"',
        "seed = 20261024",
        "",
        "[[section]]",
        'id = "verse"',
        'name = "主歌"',
        "bars = 4",
        "energy = 0.45",
        'progression = ["Dm", "Bb", "F", "C"]',
        "",
        "[[section]]",
        'id = "chorus"',
        'name = "副歌"',
        "bars = 4",
        "energy = 0.85",
        'progression = ["Bb", "C", "Dm", "Dm"]',
        "",
        "# ---- 钢琴：每小节柱式和弦 -------------------------------------------",
        "[[track]]",
        'id = "piano"',
        'name = "钢琴"',
        'role = "harmony"',
        "program = 0",
        'sections = ["verse", "chorus"]',
        "mix = { gain_db = -6.0, pan = -0.15, reverb_send = 0.18 }",
        "",
    ]
    # 每小节一个三和弦（根音上 3 音），副歌整体上移八度增强
    chords = [
        (0.0, (62, 65, 69)), (4.0, (58, 62, 65)), (8.0, (57, 60, 65)), (12.0, (55, 60, 64)),
        (16.0, (70, 74, 77)), (20.0, (72, 74, 79)), (24.0, (74, 77, 81)), (28.0, (74, 77, 81)),
    ]
    for beat, pitches in chords:
        for pitch in pitches:
            lines += _note_block(beat, pitch, 3.5, 76)

    lines += [
        "# ---- 贝斯：每小节根音 -----------------------------------------------",
        "[[track]]",
        'id = "bass"',
        'name = "贝斯"',
        'role = "bass"',
        "program = 33",
        "octave = 2",
        'sections = ["verse", "chorus"]',
        "mix = { gain_db = -3.0 }",
        "",
    ]
    roots = [(0.0, 38), (4.0, 34), (8.0, 41), (12.0, 36),
             (16.0, 34), (20.0, 36), (24.0, 38), (28.0, 38)]
    for beat, pitch in roots:
        lines += _note_block(beat, pitch, 3.8, 84)

    lines += [
        "# ---- 鼓组：固定通道 9（role = \"drums\" 自动占用）---------------------",
        "[[track]]",
        'id = "drums"',
        'name = "鼓组"',
        'role = "drums"',
        'instrument = "standard_kit"',
        f"channel = {_DRUM_CHANNEL}",
        'sections = ["verse", "chorus"]',
        "mix = { gain_db = -4.0 }",
        "",
    ]
    for bar in range(8):
        base = bar * 4.0
        lines += _note_block(base, 36, 0.5, 100)       # 底鼓
        lines += _note_block(base + 2.0, 38, 0.5, 92)  # 军鼓
        lines += _note_block(base + 1.0, 42, 0.5, 64)  # 闭镲
        lines += _note_block(base + 3.0, 42, 0.5, 64)  # 闭镲
    return "\n".join(lines).rstrip() + "\n"


def _describe(spec: SongSpec) -> list[str]:
    """人类可读摘要（validate / build 共用）。"""
    summary = spec.summary()
    return [
        f"曲名 {summary['title'] or '(未命名)'}｜调性 {summary['key']}｜"
        f"速度 {summary['tempo']:g} BPM｜拍号 {summary['meter']}",
        f"段落 {summary['sections']} 个 / {summary['bars']} 小节｜"
        f"轨道 {summary['tracks']} 条｜音符 {summary['notes']} 个",
        f"时长 {summary['duration_beats']:g} 拍 = {summary['duration_seconds']:.2f} 秒",
    ]


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------

@app.command("init")
@_guard
def init(
    name: str = typer.Argument(..., help="工程名（默认生成 <name>/song.toml）"),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="输出目录（默认当前目录下的 <name>/）"
    ),
    title: Optional[str] = typer.Option(None, "--title", help="曲名（默认同工程名）"),
    force: bool = typer.Option(False, "--force", help="覆盖已存在的 song.toml"),
) -> None:
    """生成可构建的 song.toml 模板（8 小节、钢琴/贝斯/鼓三轨）。"""
    target_dir = Path(out) if out is not None else Path(name)
    target = target_dir / "song.toml"
    if target.exists() and not force:
        typer.echo(f"已存在 {target}（如需覆盖加 --force）", err=True)
        raise typer.Exit(1)
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(_template(title or name), encoding="utf-8")

    spec = SongSpec.from_toml(target)  # 自检：模板必须能过校验
    typer.echo(f"已生成 {target}")
    for line in _describe(spec):
        typer.echo(f"  {line}")
    typer.echo(f"  下一步：sunoaux compose build {target}")


@app.command("validate")
@_guard
def validate_cmd(
    path: Path = typer.Argument(..., help="song.toml 路径"),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出结果（AI/CI 友好）"),
) -> None:
    """校验清单的语法、结构与语义。"""
    if not path.exists():
        typer.echo(f"清单不存在: {path}", err=True)
        raise typer.Exit(1)
    if as_json:
        try:
            spec = SongSpec.from_toml(path)
            spec.validate()
        except SpecError as exc:
            typer.echo(json.dumps({"ok": False, **exc.to_dict()}, ensure_ascii=False))
            raise typer.Exit(exc.code) from exc
        typer.echo(json.dumps({"ok": True, "summary": spec.summary()}, ensure_ascii=False))
        return

    spec = SongSpec.from_toml(path)
    spec.validate()
    typer.echo(f"校验通过: {path}")
    for line in _describe(spec):
        typer.echo(f"  {line}")


@app.command("build")
@_guard
def build(
    path: Path = typer.Argument(..., help="song.toml 路径"),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="输出目录（默认 out/<曲名>/）"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印计划，不写文件"),
) -> None:
    """构建成品 MIDI（format 1）+ 编排快照（arrangement.json）。"""
    if not path.exists():
        typer.echo(f"清单不存在: {path}", err=True)
        raise typer.Exit(1)

    spec = SongSpec.from_toml(path)
    song = spec.to_song()
    document = song.to_midi()

    base = _slug(song.title or path.parent.name)
    out_dir = Path(out) if out is not None else Path("out") / base
    midi_path = out_dir / f"{base}.mid"
    snapshot_path = out_dir / "arrangement.json"

    channels = "、".join(
        f"{track.name}={track.channel}" for track in document.tracks
    ) or "（无轨道）"
    plan = [
        *_describe(spec),
        f"通道分配 {channels}",
        f"输出 {midi_path}｜{snapshot_path}",
    ]
    if dry_run:
        typer.echo("[dry-run] 计划：")
        for line in plan:
            typer.echo(f"  {line}")
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    write_smf(document, midi_path)
    snapshot_path.write_text(
        json.dumps(song.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    typer.echo(f"已构建 {midi_path}")
    typer.echo(f"  编排快照 {snapshot_path}")
    for line in plan[:-1]:
        typer.echo(f"  {line}")
