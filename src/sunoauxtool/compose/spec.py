"""SongSpec：`song.toml` 声明式清单（2.0 PR-C）。

三层职责，刻意分开：

1. **结构校验**（错误码 44）：字段是否存在、类型是否正确、有无未知字段；
2. **语义校验**（错误码 40）：小节数/段落衔接/音域/通道/音符边界等；
3. **编译为 `Song`**（唯一真源）：``SongSpec.to_song()``，之后一律走
   ``midi`` 包写 SMF。

清单是 AI 与编辑器的契约：所有错误都带 **JSON Pointer** 定位（``/track/1/program``），
便于机器读取后自动修复（``--json`` 输出 ``{"ok": false, "path", "code", "message"}``）。
静态 JSON Schema 随 2.0 收口（PR-H）发布。

示例（``compose init`` 会生成同类模板）::

    schema = 1

    [song]
    title = "夜航"
    key = "D minor"
    tempo = 96
    meter = "4/4"
    seed = 20261024

    [[section]]
    id = "verse"
    name = "主歌"
    bars = 4
    energy = 0.45
    progression = ["Dm", "Bb", "F", "C"]

    [[track]]
    id = "piano"
    name = "钢琴"
    role = "harmony"
    program = 0
    sections = ["verse"]
    mix = { gain_db = -6.0, pan = -0.15 }

    # 音符 beat 为全曲绝对拍（4/4 下 1 小节 = 4 拍）
    [[track.note]]
    beat = 0
    pitch = 62
    duration = 2
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from sunoauxtool.compose.expression import ARTICULATION_NAMES, DYNAMICS, RAMPS
from sunoauxtool.compose.harmony import parse_chord
from sunoauxtool.compose.song import (
    KNOWN_ROLES,
    Clip,
    InstrumentRef,
    MixStrip,
    Song,
    Track,
)
from sunoauxtool.exceptions import ParameterError
from sunoauxtool.midi.document import KeyPoint, MeterPoint, NoteEvent, TempoPoint

#: 当前清单 schema 版本
SCHEMA_VERSION = 1

#: 错误码 44：清单结构/schema 非法（缺字段、类型错误、未知字段）
SCHEMA_ERROR = 44
#: 错误码 40：清单语义/编曲非法（小节数、段落衔接、音域、通道…）
ARRANGEMENT_ERROR = 40

#: 合法速度曲线
CURVES = ("step", "linear")

#: 浮点比较容差（beat 累加）
_EPS = 1e-9


class SpecError(ParameterError):
    """清单错误：带 JSON Pointer 定位 + 分段错误码（44 结构 / 40 语义）。"""

    code = SCHEMA_ERROR

    def __init__(self, path: str, message: str, code: int = SCHEMA_ERROR) -> None:
        self.path = path
        self.detail = message
        super().__init__(f"{path or '/'}: {message}", code=code)

    def to_dict(self) -> dict:
        """结构化输出（供 `--json` 与 AI 自动修复）。"""
        return {"path": self.path or "/", "code": self.code, "message": self.detail}


class SpecGroupError(SpecError):
    """一次收集到的**多条**清单错误（``collect_all=True`` 时抛出）。

    ``path`` / ``code`` 取第一条错误的（保持"首错优先"的定位习惯），
    完整列表在 :attr:`errors`；``message`` 把全部错误串在一起，
    因此 ``str(exc)`` 永远包含第一条——既有断言写法不受影响。
    """

    def __init__(self, errors: Sequence["SpecError"]) -> None:
        if not errors:  # pragma: no cover - 调用方保证非空
            raise ValueError("SpecGroupError 需要至少一条错误")
        first = errors[0]
        super().__init__(
            first.path,
            f"共 {len(errors)} 处错误: " + "；".join(str(item) for item in errors),
            first.code,
        )
        self.errors: list[SpecError] = list(errors)


# ---------------------------------------------------------------------------
# 类型读取（所有错误带路径）
# ---------------------------------------------------------------------------

def _cast(value: Any, kind: str, path: str) -> Any:
    """按 ``kind`` 校验并归一化标量/容器。"""
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SpecError(path, f"必须是整数，实际为 {type(value).__name__}")
        return value
    if kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SpecError(path, f"必须是数值，实际为 {type(value).__name__}")
        return float(value)
    if kind == "str":
        if not isinstance(value, str):
            raise SpecError(path, f"必须是字符串，实际为 {type(value).__name__}")
        return value
    if kind == "bool":
        if not isinstance(value, bool):
            raise SpecError(path, f"必须是布尔值，实际为 {type(value).__name__}")
        return value
    if kind == "list":
        if not isinstance(value, list):
            raise SpecError(path, f"必须是数组，实际为 {type(value).__name__}")
        return value
    if kind == "dict":
        if not isinstance(value, dict):
            raise SpecError(path, f"必须是表（table），实际为 {type(value).__name__}")
        return value
    raise AssertionError(f"未知类型标记: {kind}")  # pragma: no cover - 编程错误


def _req(data: dict, key: str, kind: str, path: str) -> Any:
    """读取必填字段。"""
    if key not in data or data[key] is None:
        raise SpecError(f"{path}/{key}", f"缺少必填字段 {key!r}")
    return _cast(data[key], kind, f"{path}/{key}")


def _opt(data: dict, key: str, kind: str, default: Any, path: str) -> Any:
    """读取可选字段（``None`` 视为未提供）。"""
    if key not in data or data[key] is None:
        return default
    return _cast(data[key], kind, f"{path}/{key}")


def _only(data: dict, allowed: Iterable[str], path: str) -> None:
    """拒绝未知字段（拼写错误即刻暴露，AI 生成物友好）。"""
    extra = sorted(set(data) - set(allowed))
    if extra:
        raise SpecError(path or "/", f"未知字段: {', '.join(extra)}")


def _tables(data: dict, key: str, path: str) -> list[dict]:
    """读取 ``[[x]]`` 数组表（缺省为空数组）。"""
    raw = data.get(key, [])
    items = _cast(raw, "list", f"{path}/{key}")
    return [_cast(item, "dict", f"{path}/{key}/{index}") for index, item in enumerate(items)]


def _parse_meter(text: str, path: str = "/song/meter") -> tuple[int, int]:
    """``"4/4"`` → ``(4, 4)``；分母必须是 2 的幂。"""
    parts = str(text).split("/")
    if len(parts) != 2:
        raise SpecError(path, f"拍号必须形如 '4/4'，实际为 {text!r}", ARRANGEMENT_ERROR)
    try:
        numerator, denominator = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise SpecError(path, f"拍号必须形如 '4/4'，实际为 {text!r}", ARRANGEMENT_ERROR) from exc
    if numerator < 1:
        raise SpecError(path, f"拍号分子必须 >= 1，实际为 {numerator}", ARRANGEMENT_ERROR)
    if denominator not in (1, 2, 4, 8, 16, 32):
        raise SpecError(
            path, f"拍号分母必须是 2 的幂（1/2/4/8/16/32），实际为 {denominator}",
            ARRANGEMENT_ERROR,
        )
    return numerator, denominator


def split_progression(text: str | Sequence[str]) -> list[str]:
    """``"Dm-Bb-F-C"`` / ``"Dm Bb F C"`` / 列表 → 和弦列表。"""
    if isinstance(text, str):
        normalized = text.replace(",", "-").replace("|", "-")
        return [item.strip() for item in normalized.replace(" ", "-").split("-") if item.strip()]
    return [str(item).strip() for item in text if str(item).strip()]


# ---------------------------------------------------------------------------
# 清单子结构
# ---------------------------------------------------------------------------

@dataclass
class NoteSpec:
    """显式音符（PR-C 的手写路径；PR-D 由 Arranger 直接产出 ``NoteEvent``）。"""

    beat: float
    pitch: int
    duration: float
    velocity: int = 64
    release_velocity: int = 64
    articulation: str | None = None
    offset_ticks: int = 0

    def to_note(self) -> NoteEvent:
        return NoteEvent(
            pitch=self.pitch,
            start=self.beat,
            duration=self.duration,
            velocity=self.velocity,
            release_velocity=self.release_velocity,
            articulation=self.articulation,
            offset_ticks=self.offset_ticks,
        )

    def to_dict(self) -> dict:
        return {
            "beat": self.beat,
            "pitch": self.pitch,
            "duration": self.duration,
            "velocity": self.velocity,
            "release_velocity": self.release_velocity,
            "articulation": self.articulation,
            "offset_ticks": self.offset_ticks,
        }


@dataclass
class SectionSpec:
    """段落声明（小节数决定长度，能量驱动配器密度）。"""

    id: str
    name: str = ""
    bars: int = 4
    energy: float = 0.5
    progression: list[str] = field(default_factory=list)
    label: str = ""
    dynamic: str = ""
    ramp: str = ""
    tempo: float | None = None
    tempo_curve: str = "linear"

    def to_dict(self) -> dict:
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "bars": self.bars,
            "energy": self.energy,
            "progression": list(self.progression),
            "label": self.label,
            "dynamic": self.dynamic,
            "ramp": self.ramp,
        }
        if self.tempo is not None:
            payload["tempo"] = self.tempo
            payload["tempo_curve"] = self.tempo_curve
        return payload


@dataclass
class TrackSpec:
    """轨道声明：角色决定生成策略，音色决定音源。"""

    id: str
    name: str = ""
    role: str = "harmony"
    instrument: str = ""
    bank: str = "gm"
    program: int | None = None
    channel: int | None = None
    octave: int = 4
    transpose: int = 0
    articulation: str = ""
    swing: float = 0.0
    range: tuple[int, int] | None = None
    sections: list[str] = field(default_factory=list)
    mix: MixStrip = field(default_factory=MixStrip)
    muted: bool = False
    notes: list[NoteSpec] = field(default_factory=list)
    # PR-D 编曲参数（先收下，暂不生效，避免清单返工）
    density: float | None = None
    groove: str | None = None

    def to_dict(self) -> dict:
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "instrument": self.instrument,
            "bank": self.bank,
            "program": self.program,
            "channel": self.channel,
            "octave": self.octave,
            "transpose": self.transpose,
            "articulation": self.articulation,
            "swing": self.swing,
            "range": list(self.range) if self.range else None,
            "sections": list(self.sections),
            "mix": self.mix.to_dict(),
            "muted": self.muted,
            "note": [note.to_dict() for note in self.notes],
        }
        if self.density is not None:
            payload["density"] = self.density
        if self.groove is not None:
            payload["groove"] = self.groove
        return payload


# ---------------------------------------------------------------------------
# SongSpec
# ---------------------------------------------------------------------------

@dataclass
class SongSpec:
    """整首曲子的声明式清单。"""

    schema: int = SCHEMA_VERSION
    title: str = ""
    key: str = "C major"
    tempo: float = 120.0
    meter: str = "4/4"
    seed: int = 0
    pickup_beats: float = 0.0
    swing: float = 0.0
    bars: int | None = None
    sections: list[SectionSpec] = field(default_factory=list)
    tracks: list[TrackSpec] = field(default_factory=list)
    tempo_map: list[tuple[float, float, str]] = field(default_factory=list)

    # -- 解析 -------------------------------------------------------------

    @classmethod
    def from_toml(cls, path: str | Path) -> "SongSpec":
        """读 TOML；语法错误映射为 44（带行号）。"""
        file_path = Path(path)
        try:
            raw = file_path.read_bytes()
        except OSError as exc:
            raise ParameterError(f"无法读取清单 {file_path}: {exc}") from exc
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise SpecError("", f"清单必须是 UTF-8 编码: {exc}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise SpecError("", f"TOML 语法错误: {exc}") from exc
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict, *, collect_all: bool = False) -> "SongSpec":
        """从已解析字典构建（TOML/JSON/DSL 共用同一路径）。

        Args:
            data: 已解析的清单字典。
            collect_all: True 时逐**条目**收集全部结构错误并抛
                :class:`SpecGroupError`（AI 修复循环用——一轮看到所有问题）；
                False（默认）保持原行为：抛第一条错误。
        """
        data = _cast(data, "dict", "")
        _only(data, ("schema", "song", "tempo", "section", "track"), "")

        schema = _opt(data, "schema", "int", SCHEMA_VERSION, "")
        if schema != SCHEMA_VERSION:
            raise SpecError("/schema", f"不支持的清单版本 {schema}（当前只支持 {SCHEMA_VERSION}）")

        song = _cast(data.get("song", {}), "dict", "/song")
        _only(song, ("title", "key", "tempo", "meter", "seed", "pickup_beats",
                     "swing", "bars"), "/song")
        spec = cls(
            schema=schema,
            title=_opt(song, "title", "str", "", "/song"),
            key=_opt(song, "key", "str", "C major", "/song"),
            tempo=_opt(song, "tempo", "float", 120.0, "/song"),
            meter=_opt(song, "meter", "str", "4/4", "/song"),
            seed=_opt(song, "seed", "int", 0, "/song"),
            pickup_beats=_opt(song, "pickup_beats", "float", 0.0, "/song"),
            swing=_opt(song, "swing", "float", 0.0, "/song"),
            bars=_opt(song, "bars", "int", None, "/song"),
        )

        # 逐条目收集：一个坏段落/坏轨道不再遮住后面的错误（条目解析均为纯函数）
        problems: list[SpecError] = []
        for index, item in enumerate(_tables(data, "tempo", "")):
            path = f"/tempo/{index}"
            try:
                _only(item, ("beat", "bpm", "curve"), path)
                spec.tempo_map.append((
                    _req(item, "beat", "float", path),
                    _req(item, "bpm", "float", path),
                    _opt(item, "curve", "str", "step", path),
                ))
            except SpecError as exc:
                problems.append(exc)

        for index, item in enumerate(_tables(data, "section", "")):
            try:
                spec.sections.append(_section_from(item, f"/section/{index}"))
            except SpecError as exc:
                problems.append(exc)

        for index, item in enumerate(_tables(data, "track", "")):
            try:
                spec.tracks.append(_track_from(item, f"/track/{index}"))
            except SpecError as exc:
                problems.append(exc)

        if problems:
            raise SpecGroupError(problems) if collect_all else problems[0]
        return spec

    # -- 语义校验 ---------------------------------------------------------

    def validate(self, *, collect_all: bool = False) -> None:
        """语义校验（错误码 40）；不修改任何状态。

        Args:
            collect_all: True 时逐**条目**收集全部语义错误并抛
                :class:`SpecGroupError`；False（默认）抛第一条错误。
        """
        # 硬前置：没有段落/轨道时后续逐项检查没有意义，保持首错即停
        if not self.sections:
            raise SpecError("/section", "至少需要一个段落", ARRANGEMENT_ERROR)
        if not self.tracks:
            raise SpecError("/track", "至少需要一条轨道", ARRANGEMENT_ERROR)

        problems: list[SpecError] = []
        try:
            self._validate_song_table()
        except SpecError as exc:
            problems.append(exc)
        self._validate_tempo_map(problems)
        section_ids = self._validate_sections(problems)
        self._validate_tracks(section_ids, problems)
        if problems:
            raise SpecGroupError(problems) if collect_all else problems[0]

    def _validate_song_table(self) -> None:
        if not str(self.key).strip():
            raise SpecError("/song/key", "调性不能为空", ARRANGEMENT_ERROR)
        if self.tempo <= 0:
            raise SpecError("/song/tempo", f"速度必须 > 0，实际为 {self.tempo}", ARRANGEMENT_ERROR)
        _parse_meter(self.meter)
        if self.pickup_beats < 0:
            raise SpecError("/song/pickup_beats", "弱起拍不能为负", ARRANGEMENT_ERROR)
        if not 0.0 <= self.swing <= 0.75:
            raise SpecError("/song/swing", f"swing 必须在 0..0.75，实际为 {self.swing}",
                            ARRANGEMENT_ERROR)
        if self.bars is not None and self.bars != sum(section.bars for section in self.sections):
            raise SpecError(
                "/song/bars",
                f"声明的小节数 {self.bars} 与段落合计 "
                f"{sum(section.bars for section in self.sections)} 不一致",
                ARRANGEMENT_ERROR,
            )

    def _validate_tempo_map(self, problems: list[SpecError] | None = None) -> None:
        collect = problems is not None
        if not self.tempo_map:
            return  # 只用 [song].tempo 的单一速度
        previous = -1.0
        for index, (beat, bpm, curve) in enumerate(self.tempo_map):
            path = f"/tempo/{index}"
            try:
                if beat < 0:
                    raise SpecError(f"{path}/beat", "beat 不能为负", ARRANGEMENT_ERROR)
                if beat <= previous:
                    raise SpecError(f"{path}/beat", "beat 必须严格递增", ARRANGEMENT_ERROR)
                previous = beat
                if bpm <= 0:
                    raise SpecError(f"{path}/bpm", "bpm 必须 > 0", ARRANGEMENT_ERROR)
                if curve not in CURVES:
                    raise SpecError(
                        f"{path}/curve",
                        f"速度曲线只支持 {'/'.join(CURVES)}，实际为 {curve!r}",
                        ARRANGEMENT_ERROR,
                    )
            except SpecError as exc:
                if not collect:
                    raise
                problems.append(exc)
        if self.tempo_map[0][0] != 0:
            problem = SpecError("/tempo/0/beat", "速度表首点 beat 必须为 0", ARRANGEMENT_ERROR)
            if collect:
                problems.append(problem)
            else:
                raise problem

    def _validate_sections(self, problems: list[SpecError] | None = None) -> list[str]:
        collect = problems is not None
        seen: list[str] = []
        for index, section in enumerate(self.sections):
            if collect:
                try:
                    self._check_section(section, index, seen)
                except SpecError as exc:
                    problems.append(exc)
            else:
                self._check_section(section, index, seen)
        return seen

    def _check_section(self, section: SectionSpec, index: int, seen: list[str]) -> None:
        """单个段落的语义检查（collect 模式下由外层逐条目捕获）。"""
        path = f"/section/{index}"
        if not section.id.strip():
            raise SpecError(f"{path}/id", "段落 id 不能为空", ARRANGEMENT_ERROR)
        if section.id in seen:
            raise SpecError(f"{path}/id", f"段落 id 重复: {section.id!r}", ARRANGEMENT_ERROR)
        seen.append(section.id)
        if section.bars < 1:
            raise SpecError(f"{path}/bars", f"bars 必须 >= 1，实际为 {section.bars}",
                            ARRANGEMENT_ERROR)
        if not 0.0 <= section.energy <= 1.0:
            raise SpecError(f"{path}/energy", f"energy 必须在 0..1，实际为 {section.energy}",
                            ARRANGEMENT_ERROR)
        if section.dynamic and section.dynamic not in DYNAMICS:
            raise SpecError(
                f"{path}/dynamic",
                f"未知力度记号 {section.dynamic!r}；可用: {', '.join(DYNAMICS)}",
                ARRANGEMENT_ERROR,
            )
        if section.ramp and section.ramp not in RAMPS:
            raise SpecError(
                f"{path}/ramp",
                f"未知渐变 {section.ramp!r}；可用: {', '.join(RAMPS)}",
                ARRANGEMENT_ERROR,
            )
        if section.tempo is not None and section.tempo <= 0:
            raise SpecError(f"{path}/tempo", "tempo 必须 > 0", ARRANGEMENT_ERROR)
        if section.tempo_curve not in CURVES:
            raise SpecError(
                f"{path}/tempo_curve",
                f"速度曲线只支持 {'/'.join(CURVES)}，实际为 {section.tempo_curve!r}",
                ARRANGEMENT_ERROR,
            )
        for chord_index, chord in enumerate(section.progression):
            if not chord.strip():
                raise SpecError(f"{path}/progression/{chord_index}", "和弦不能为空",
                                ARRANGEMENT_ERROR)
            try:
                parse_chord(chord)
            except ParameterError as exc:
                raise SpecError(
                    f"{path}/progression/{chord_index}", str(exc), ARRANGEMENT_ERROR
                ) from exc

    def _validate_tracks(
        self, section_ids: Sequence[str], problems: list[SpecError] | None = None
    ) -> None:
        collect = problems is not None
        ids: set[str] = set()
        channels: dict[int, str] = {}
        drums = 0
        for index, track in enumerate(self.tracks):
            if collect:
                try:
                    drums += self._check_track(track, index, section_ids, ids, channels)
                except SpecError as exc:
                    problems.append(exc)
            else:
                drums += self._check_track(track, index, section_ids, ids, channels)
        if drums > 1:
            raise SpecError("/track", "一首曲子最多允许一条鼓轨（role='drums'）", ARRANGEMENT_ERROR)

    def _check_track(
        self,
        track: TrackSpec,
        index: int,
        section_ids: Sequence[str],
        ids: set[str],
        channels: dict[int, str],
    ) -> int:
        """单条轨道的语义检查；返回 1 表示鼓轨（供外层统计鼓轨数量）。"""
        path = f"/track/{index}"
        if not track.id.strip():
            raise SpecError(f"{path}/id", "轨道 id 不能为空", ARRANGEMENT_ERROR)
        if track.id in ids:
            raise SpecError(f"{path}/id", f"轨道 id 重复: {track.id!r}", ARRANGEMENT_ERROR)
        ids.add(track.id)
        if track.role not in KNOWN_ROLES:
            raise SpecError(
                f"{path}/role",
                f"未知角色 {track.role!r}；可用: {', '.join(sorted(KNOWN_ROLES))}",
                ARRANGEMENT_ERROR,
            )
        if track.program is not None and not 0 <= track.program <= 127:
            raise SpecError(f"{path}/program", f"program 必须在 0..127，实际为 {track.program}",
                            ARRANGEMENT_ERROR)
        if track.channel is not None:
            if not 0 <= track.channel <= 15:
                raise SpecError(f"{path}/channel",
                                f"channel 必须在 0..15，实际为 {track.channel}",
                                ARRANGEMENT_ERROR)
            if track.role == "drums" and track.channel != 9:
                raise SpecError(f"{path}/channel", "鼓轨必须使用通道 9", ARRANGEMENT_ERROR)
            if track.role != "drums" and track.channel == 9:
                raise SpecError(f"{path}/channel", "通道 9 保留给鼓轨", ARRANGEMENT_ERROR)
            if track.channel in channels and track.role != "drums":
                # 鼓轨之间共用通道 9 是必然的，由上方"最多一条鼓轨"统一报错
                raise SpecError(
                    f"{path}/channel",
                    f"通道 {track.channel} 已被轨道 {channels[track.channel]!r} 占用",
                    ARRANGEMENT_ERROR,
                )
            channels[track.channel] = track.id
        drums = 1 if track.role == "drums" else 0
        if track.articulation and track.articulation not in ARTICULATION_NAMES:
            raise SpecError(
                f"{path}/articulation",
                f"未知演奏法 {track.articulation!r}；"
                f"可用: {', '.join(sorted(ARTICULATION_NAMES))}",
                ARRANGEMENT_ERROR,
            )
        if not 0.0 <= track.swing <= 0.75:
            raise SpecError(f"{path}/swing",
                            f"swing 必须在 0..0.75，实际为 {track.swing}",
                            ARRANGEMENT_ERROR)
        if not -24 <= track.transpose <= 24:
            raise SpecError(f"{path}/transpose",
                            f"transpose 必须在 -24..24，实际为 {track.transpose}",
                            ARRANGEMENT_ERROR)
        if not 0 <= track.octave <= 9:
            raise SpecError(f"{path}/octave", f"octave 必须在 0..9，实际为 {track.octave}",
                            ARRANGEMENT_ERROR)
        if track.range is not None:
            low, high = track.range
            if not 0 <= low <= high <= 127:
                raise SpecError(f"{path}/range", f"音域 {track.range} 非法（需 0<=low<=high<=127）",
                                ARRANGEMENT_ERROR)
        for ref_index, ref in enumerate(track.sections):
            if ref not in section_ids:
                raise SpecError(f"{path}/sections/{ref_index}",
                                f"引用了不存在的段落 {ref!r}", ARRANGEMENT_ERROR)
        self._validate_notes(track, path)
        return drums

    def _validate_notes(self, track: TrackSpec, path: str) -> None:
        spans = self._section_spans()
        ends = {section_id: end for section_id, _start, end in spans}
        for index, note in enumerate(track.notes):
            note_path = f"{path}/note/{index}"
            if not 0 <= note.pitch <= 127:
                raise SpecError(f"{note_path}/pitch", f"pitch 必须在 0..127，实际为 {note.pitch}",
                                ARRANGEMENT_ERROR)
            if note.duration <= 0:
                raise SpecError(f"{note_path}/duration", "duration 必须 > 0", ARRANGEMENT_ERROR)
            for label, value in (("velocity", note.velocity),
                                 ("release_velocity", note.release_velocity)):
                if not 0 <= value <= 127:
                    raise SpecError(f"{note_path}/{label}", f"{label} 必须在 0..127，实际为 {value}",
                                    ARRANGEMENT_ERROR)
            section_id = self.section_at(note.beat)
            if section_id is None:
                end = spans[-1][2] if spans else 0.0
                raise SpecError(
                    f"{note_path}/beat",
                    f"beat {note.beat} 不在任何段落内（全曲 0..{end}）",
                    ARRANGEMENT_ERROR,
                )
            if track.sections and section_id not in track.sections:
                raise SpecError(
                    f"{note_path}/beat",
                    f"beat {note.beat} 属于段落 {section_id!r}，但它不在本轨的 sections 内",
                    ARRANGEMENT_ERROR,
                )
            section_end = ends[section_id]
            if note.beat + note.duration > section_end + _EPS:
                raise SpecError(
                    f"{note_path}/duration",
                    f"音符结束于 {note.beat + note.duration}，越过段落 {section_id!r} "
                    f"结尾 {section_end}（跨段长音需拆成两个音符）",
                    ARRANGEMENT_ERROR,
                )
            if track.range is not None and not track.range[0] <= note.pitch <= track.range[1]:
                raise SpecError(f"{note_path}/pitch",
                                f"pitch {note.pitch} 超出音域 {track.range}", ARRANGEMENT_ERROR)

    # -- 派生量 -----------------------------------------------------------

    def _meter_pair(self) -> tuple[int, int]:
        return _parse_meter(self.meter)

    def effective_tempo_map(self) -> list[tuple[float, float, str]]:
        """实际速度表：清单给了就用清单，否则用 ``[song].tempo`` 单点。"""
        return list(self.tempo_map) or [(0.0, self.tempo, "step")]

    def bar_length(self) -> float:
        """一小节的拍数（按名义拍号）。"""
        numerator, denominator = self._meter_pair()
        return numerator * 4.0 / denominator

    def _section_spans(self) -> list[tuple[str, float, float]]:
        """[(section_id, start, end)]，按清单顺序累加。"""
        bar_length = self.bar_length()
        spans: list[tuple[str, float, float]] = []
        cursor = self.pickup_beats
        for section in self.sections:
            start = cursor
            cursor = start + section.bars * bar_length
            spans.append((section.id, start, cursor))
        return spans

    def section_at(self, beat: float) -> str | None:
        """``beat`` 所在段落 id（右开区间，末段含右端点）。"""
        spans = self._section_spans()
        for index, (section_id, start, end) in enumerate(spans):
            last = index == len(spans) - 1
            if start <= beat < end or (last and beat == end):
                return section_id
        return None

    # -- 编译 -------------------------------------------------------------

    def to_song(self) -> Song:
        """编译为 ``Song``（唯一真源），并做一次完整校验。"""
        self.validate()
        song = Song(
            title=self.title,
            tempo_map=_tempo_points(self),
            meter_map=[MeterPoint(beat=0.0, numerator=self._meter_pair()[0],
                                  denominator=self._meter_pair()[1])],
            key_map=[KeyPoint(beat=0.0, key=self.key)],
            seed=self.seed,
            pickup_beats=self.pickup_beats,
            swing=self.swing,
        )
        for section in self.sections:
            song.add_section(
                section.id,
                name=section.name,
                bars=section.bars,
                energy=section.energy,
                progression=section.progression,
                label=section.label,
                dynamic=section.dynamic,
                ramp=section.ramp,
            )
        for spec in self.tracks:
            song.add_track(self._build_track(song, spec))
        try:
            song.validate()
        except ParameterError as exc:  # pragma: no cover - 兜底：语义校验已覆盖全部已知规则
            raise SpecError("", str(exc), ARRANGEMENT_ERROR) from exc
        return song

    def _build_track(self, song: Song, spec: TrackSpec) -> Track:
        """把清单轨道编译成 ``Track``：音符按段落切分为 ``Clip``。"""
        clips: list[Clip] = []
        for section in song.sections:
            if spec.sections and section.id not in spec.sections:
                continue
            start = section.start
            end = song.section_end(section)
            notes = [
                note.to_note()
                for note in spec.notes
                if self.section_at(note.beat) == section.id
            ]
            if not notes and not spec.sections:
                continue  # 未声明段落且此段无音符：不生成空片段
            clips.append(Clip(start=start, length=end - start, notes=notes,
                              section_id=section.id))
        return Track(
            id=spec.id,
            name=spec.name or spec.id,
            role=spec.role,
            instrument=InstrumentRef(bank=spec.bank, program=spec.program),
            channel=spec.channel,
            clips=clips,
            mix=spec.mix,
            octave=spec.octave,
            transpose=spec.transpose,
            articulation=spec.articulation,
            swing=spec.swing,
            range=spec.range,
            muted=spec.muted,
        )

    # -- 序列化 -----------------------------------------------------------

    def to_dict(self) -> dict:
        """展开为可回写的清单字典（``compose init`` / DSL 共用）。"""
        song: dict[str, Any] = {
            "title": self.title,
            "key": self.key,
            "tempo": self.tempo,
            "meter": self.meter,
            "seed": self.seed,
        }
        if self.pickup_beats:
            song["pickup_beats"] = self.pickup_beats
        if self.swing:
            song["swing"] = self.swing
        if self.bars is not None:
            song["bars"] = self.bars
        payload: dict[str, Any] = {"schema": self.schema, "song": song}
        if self.tempo_map:
            payload["tempo"] = [
                {"beat": beat, "bpm": bpm, "curve": curve}
                for beat, bpm, curve in self.tempo_map
            ]
        payload["section"] = [section.to_dict() for section in self.sections]
        payload["track"] = [track.to_dict() for track in self.tracks]
        return payload

    @property
    def total_bars(self) -> int:
        return sum(section.bars for section in self.sections)

    def summary(self) -> dict:
        """简要统计（CLI 输出/日志用）。"""
        song = self.to_song()
        return {
            "title": self.title,
            "key": self.key,
            "tempo": self.tempo,
            "meter": self.meter,
            "sections": len(self.sections),
            "tracks": len(self.tracks),
            "notes": sum(len(track.notes) for track in self.tracks),
            "bars": self.total_bars,
            "duration_beats": song.duration_beats,
            "duration_seconds": round(song.duration_seconds(), 3),
        }


# ---------------------------------------------------------------------------
# 速度表合成（显式 [[tempo]] + 段落级 rit./accel.）
# ---------------------------------------------------------------------------

def _tempo_points(spec: SongSpec) -> list[TempoPoint]:
    """合成速度表：显式速度点优先，段落 ``tempo`` 追加"渐变 + 到站"两点。

    段落 ``tempo = 84`` 的语义是：**该段从当前速度线性渐变到 84**，段末转到 84 继续。
    实现为 ``(段首, 当前速度, linear)`` + ``(段末, 84, step)``，段与段之间的衔接是"到站即停"。
    """
    points: list[tuple[float, float, str]] = list(spec.effective_tempo_map())
    if not any(section.tempo is not None for section in spec.sections):
        return [TempoPoint(beat=beat, bpm=bpm, curve=curve) for beat, bpm, curve in points]

    bar_length = spec.bar_length()
    cursor = spec.pickup_beats
    current = points[-1][1]
    merged: list[tuple[float, float, str]] = [(0.0, points[0][1], "step")]
    for section in spec.sections:
        start, end = cursor, cursor + section.bars * bar_length
        cursor = end
        if section.tempo is None:
            continue
        if start > 0:
            merged.append((start, current, section.tempo_curve))
        merged.append((end, float(section.tempo), "step"))
        current = float(section.tempo)
    # 去重（beat 必须严格递增）
    cleaned: list[tuple[float, float, str]] = []
    for beat, bpm, curve in merged:
        if cleaned and abs(cleaned[-1][0] - beat) < 1e-9:
            cleaned[-1] = (beat, bpm, curve)
        else:
            cleaned.append((beat, bpm, curve))
    # 显式速度点（beat>0）仍然生效：合并后按 beat 排序，冲突以显式点为准
    explicit = {round(beat, 6): (bpm, curve) for beat, bpm, curve in points if beat > 0}
    combined: dict[float, tuple[float, str]] = {beat: (bpm, curve) for beat, bpm, curve in cleaned}
    combined.update(explicit)
    ordered = sorted(combined.items())
    if ordered[0][0] != 0.0:
        ordered.insert(0, (0.0, (points[0][1], "step")))
    return [TempoPoint(beat=beat, bpm=bpm, curve=curve) for beat, (bpm, curve) in ordered]


# ---------------------------------------------------------------------------
# 子表构造器
# ---------------------------------------------------------------------------

def _section_from(item: dict, path: str) -> SectionSpec:
    _only(item, ("id", "name", "bars", "energy", "progression", "label",
                 "dynamic", "ramp", "tempo", "tempo_curve"), path)
    progression = item.get("progression", [])
    if isinstance(progression, str):
        chords = split_progression(progression)
    else:
        chords = [
            _cast(chord, "str", f"{path}/progression/{index}")
            for index, chord in enumerate(_cast(progression, "list", f"{path}/progression"))
        ]
    return SectionSpec(
        id=_req(item, "id", "str", path),
        name=_opt(item, "name", "str", "", path),
        bars=_opt(item, "bars", "int", 4, path),
        energy=_opt(item, "energy", "float", 0.5, path),
        progression=chords,
        label=_opt(item, "label", "str", "", path),
        dynamic=_opt(item, "dynamic", "str", "", path),
        ramp=_opt(item, "ramp", "str", "", path),
        tempo=_opt(item, "tempo", "float", None, path),
        tempo_curve=_opt(item, "tempo_curve", "str", "linear", path),
    )


def _track_from(item: dict, path: str) -> TrackSpec:
    _only(item, ("id", "name", "role", "instrument", "bank", "program", "channel",
                 "octave", "transpose", "articulation", "swing", "range", "sections",
                 "mix", "muted", "note", "density", "groove"), path)
    raw_range = item.get("range")
    if raw_range is None:
        narrow: tuple[int, int] | None = None
    else:
        values = _cast(raw_range, "list", f"{path}/range")
        if len(values) != 2:
            raise SpecError(f"{path}/range", "range 必须是 [low, high] 两个整数")
        narrow = (
            _cast(values[0], "int", f"{path}/range/0"),
            _cast(values[1], "int", f"{path}/range/1"),
        )
    raw_sections = item.get("sections", [])
    sections = [
        _cast(name, "str", f"{path}/sections/{index}")
        for index, name in enumerate(_cast(raw_sections, "list", f"{path}/sections"))
    ]
    mix_data = _cast(item.get("mix", {}), "dict", f"{path}/mix")
    _only(mix_data, ("gain_db", "pan", "mute", "solo", "reverb_send", "chorus_send"),
          f"{path}/mix")
    try:
        mix = MixStrip.from_dict(mix_data)
    except ParameterError as exc:  # 越界增益/声像等 → 定位到 mix 表
        raise SpecError(f"{path}/mix", str(exc), ARRANGEMENT_ERROR) from exc
    notes = [_note_from(note, f"{path}/note/{index}")
             for index, note in enumerate(_tables(item, "note", path))]
    return TrackSpec(
        id=_req(item, "id", "str", path),
        name=_opt(item, "name", "str", "", path),
        role=_opt(item, "role", "str", "harmony", path),
        instrument=_opt(item, "instrument", "str", "", path),
        bank=_opt(item, "bank", "str", "gm", path),
        program=_opt(item, "program", "int", None, path),
        channel=_opt(item, "channel", "int", None, path),
        octave=_opt(item, "octave", "int", 4, path),
        transpose=_opt(item, "transpose", "int", 0, path),
        articulation=_opt(item, "articulation", "str", "", path),
        swing=_opt(item, "swing", "float", 0.0, path),
        range=narrow,
        sections=sections,
        mix=mix,
        muted=_opt(item, "muted", "bool", False, path),
        notes=notes,
        density=_opt(item, "density", "float", None, path),
        groove=_opt(item, "groove", "str", None, path),
    )


def _note_from(item: dict, path: str) -> NoteSpec:
    _only(item, ("beat", "pitch", "duration", "velocity", "release_velocity",
                 "articulation", "offset_ticks"), path)
    return NoteSpec(
        beat=_req(item, "beat", "float", path),
        pitch=_req(item, "pitch", "int", path),
        duration=_req(item, "duration", "float", path),
        velocity=_opt(item, "velocity", "int", 64, path),
        release_velocity=_opt(item, "release_velocity", "int", 64, path),
        articulation=_opt(item, "articulation", "str", None, path),
        offset_ticks=_opt(item, "offset_ticks", "int", 0, path),
    )
