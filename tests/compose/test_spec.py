"""SongSpec 清单（compose/spec.py）单测：结构 44 / 语义 40 / 编译为 Song。"""

from __future__ import annotations

from pathlib import Path

import pytest

from sunoauxtool.compose.spec import (
    ARRANGEMENT_ERROR,
    SCHEMA_ERROR,
    SCHEMA_VERSION,
    SongSpec,
    SpecError,
    split_progression,
)
from sunoauxtool.exceptions import ParameterError


def _spec_dict() -> dict:
    """两轨两段的基准清单（verse 2 小节 = 8 拍，4/4）。"""
    return {
        "schema": 1,
        "song": {"title": "夜航", "key": "D minor", "tempo": 96, "meter": "4/4", "seed": 7},
        "section": [
            {"id": "verse", "name": "主歌", "bars": 2, "energy": 0.4,
             "progression": ["Dm", "Bb"]},
            {"id": "chorus", "name": "副歌", "bars": 2, "energy": 0.9,
             "progression": ["Bb", "C"]},
        ],
        "track": [
            {"id": "piano", "name": "钢琴", "role": "harmony", "program": 0,
             "sections": ["verse"],
             "note": [{"beat": 0.0, "pitch": 62, "duration": 2.0}]},
            {"id": "bass", "name": "贝斯", "role": "bass", "program": 33, "octave": 2},
        ],
    }


def _parse_and_validate(data: dict) -> SongSpec:
    spec = SongSpec.from_dict(data)
    spec.validate()
    return spec


def _raises(data: dict, path: str, code: int, match: str) -> SpecError:
    with pytest.raises(SpecError) as excinfo:
        _parse_and_validate(data)
    error = excinfo.value
    assert error.path == path, f"期望路径 {path}，实际 {error.path}（{error}）"
    assert error.code == code, f"期望错误码 {code}，实际 {error.code}"
    assert match in str(error)
    return error


class TestSpecError:
    def test_message_has_pointer(self) -> None:
        error = SpecError("/track/0/program", "program 必须在 0..127", ARRANGEMENT_ERROR)
        assert str(error) == "/track/0/program: program 必须在 0..127"
        assert error.code == ARRANGEMENT_ERROR
        assert isinstance(error, ParameterError)

    def test_to_dict(self) -> None:
        assert SpecError("/song/tempo", "必须 > 0").to_dict() == {
            "path": "/song/tempo", "code": SCHEMA_ERROR, "message": "必须 > 0",
        }

    def test_root_pointer_renders_slash(self) -> None:
        assert str(SpecError("", "坏文件")).startswith("/: ")


class TestParseDefaults:
    def test_minimal_defaults(self) -> None:
        spec = SongSpec.from_dict({
            "section": [{"id": "a"}],
            "track": [{"id": "t", "note": [{"beat": 0, "pitch": 60, "duration": 1}]}],
        })
        assert spec.schema == SCHEMA_VERSION
        assert (spec.title, spec.key, spec.tempo, spec.meter, spec.seed) == (
            "", "C major", 120.0, "4/4", 0,
        )
        assert spec.tempo_map == [] and spec.bars is None
        spec.validate()
        assert spec.effective_tempo_map() == [(0.0, 120.0, "step")]

    def test_from_toml_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "song.toml"
        path.write_text(_TOML, encoding="utf-8")
        spec = SongSpec.from_toml(path)
        assert spec.title == "夜航"
        assert [section.id for section in spec.sections] == ["verse", "chorus"]
        assert spec.sections[0].progression == ["Dm", "Bb", "F", "C"]
        assert spec.tracks[0].mix.gain_db == -6.0
        assert spec.tracks[0].notes[0].velocity == 72
        assert spec.tempo_map == [(0.0, 96.0, "step"), (16.0, 104.0, "linear")]
        spec.validate()

    def test_from_dict_to_dict_round_trip(self) -> None:
        spec = _parse_and_validate(_spec_dict())
        again = SongSpec.from_dict(spec.to_dict())
        assert again.to_dict() == spec.to_dict()
        again.validate()

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ParameterError, match="无法读取清单"):
            SongSpec.from_toml(tmp_path / "nope.toml")

    def test_toml_syntax_error(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.toml"
        path.write_text("schema = 1\n[song\n", encoding="utf-8")
        with pytest.raises(SpecError) as excinfo:
            SongSpec.from_toml(path)
        assert excinfo.value.code == SCHEMA_ERROR
        assert "TOML 语法错误" in str(excinfo.value)

    def test_non_utf8_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "gbk.toml"
        path.write_bytes("title = \"夜航\"\n".encode("gbk"))
        with pytest.raises(SpecError, match="UTF-8"):
            SongSpec.from_toml(path)


class TestSchemaErrors:
    """错误码 44：缺字段 / 类型不符 / 未知字段 / 版本不符。"""

    def test_unknown_top_level_key(self) -> None:
        data = _spec_dict()
        data["tracks"] = []
        _raises(data, "/", SCHEMA_ERROR, "未知字段: tracks")

    def test_unknown_song_key(self) -> None:
        data = _spec_dict()
        data["song"]["tmpo"] = 96
        _raises(data, "/song", SCHEMA_ERROR, "未知字段: tmpo")

    def test_unknown_section_key(self) -> None:
        data = _spec_dict()
        data["section"][0]["engery"] = 0.4
        _raises(data, "/section/0", SCHEMA_ERROR, "未知字段: engery")

    def test_unknown_track_key(self) -> None:
        data = _spec_dict()
        data["track"][0]["gain_db"] = -6.0
        _raises(data, "/track/0", SCHEMA_ERROR, "未知字段: gain_db")

    def test_unknown_mix_key(self) -> None:
        data = _spec_dict()
        data["track"][0]["mix"] = {"gain": -6.0}
        _raises(data, "/track/0/mix", SCHEMA_ERROR, "未知字段: gain")

    def test_unknown_note_key(self) -> None:
        data = _spec_dict()
        data["track"][0]["note"][0]["velocty"] = 90
        _raises(data, "/track/0/note/0", SCHEMA_ERROR, "未知字段: velocty")

    def test_missing_track_id(self) -> None:
        data = _spec_dict()
        data["track"][0].pop("id")
        _raises(data, "/track/0/id", SCHEMA_ERROR, "缺少必填字段 'id'")

    def test_missing_note_pitch(self) -> None:
        data = _spec_dict()
        data["track"][0]["note"][0].pop("pitch")
        _raises(data, "/track/0/note/0/pitch", SCHEMA_ERROR, "缺少必填字段")

    def test_wrong_type(self) -> None:
        data = _spec_dict()
        data["section"][0]["bars"] = "2"
        _raises(data, "/section/0/bars", SCHEMA_ERROR, "必须是整数")

    def test_bool_is_not_int(self) -> None:
        data = _spec_dict()
        data["song"]["seed"] = True
        _raises(data, "/song/seed", SCHEMA_ERROR, "必须是整数")

    def test_track_must_be_table(self) -> None:
        data = _spec_dict()
        data["track"].append("piano")
        _raises(data, "/track/2", SCHEMA_ERROR, "必须是表")

    def test_schema_version_unsupported(self) -> None:
        data = _spec_dict()
        data["schema"] = 2
        _raises(data, "/schema", SCHEMA_ERROR, "不支持的清单版本 2")

    def test_mix_gain_out_of_range(self) -> None:
        data = _spec_dict()
        data["track"][0]["mix"] = {"gain_db": 30.0}
        _raises(data, "/track/0/mix", ARRANGEMENT_ERROR, "gain_db 超出范围")


class TestSemanticErrors:
    """错误码 40：语义/编曲非法。"""

    def test_no_sections(self) -> None:
        data = _spec_dict()
        data["section"] = []
        _raises(data, "/section", ARRANGEMENT_ERROR, "至少需要一个段落")

    def test_no_tracks(self) -> None:
        data = _spec_dict()
        data["track"] = []
        _raises(data, "/track", ARRANGEMENT_ERROR, "至少需要一条轨道")

    def test_bars_mismatch(self) -> None:
        data = _spec_dict()
        data["song"]["bars"] = 3
        _raises(data, "/song/bars", ARRANGEMENT_ERROR, "不一致")

    def test_bars_match_ok(self) -> None:
        data = _spec_dict()
        data["song"]["bars"] = 4
        _parse_and_validate(data)

    def test_tempo_not_positive(self) -> None:
        data = _spec_dict()
        data["song"]["tempo"] = 0
        _raises(data, "/song/tempo", ARRANGEMENT_ERROR, "速度必须 > 0")

    def test_empty_key(self) -> None:
        data = _spec_dict()
        data["song"]["key"] = "  "
        _raises(data, "/song/key", ARRANGEMENT_ERROR, "调性不能为空")

    def test_meter_without_slash(self) -> None:
        data = _spec_dict()
        data["song"]["meter"] = "4-4"
        _raises(data, "/song/meter", ARRANGEMENT_ERROR, "拍号必须形如")

    def test_meter_bad_denominator(self) -> None:
        data = _spec_dict()
        data["song"]["meter"] = "4/3"
        _raises(data, "/song/meter", ARRANGEMENT_ERROR, "2 的幂")

    def test_meter_zero_numerator(self) -> None:
        data = _spec_dict()
        data["song"]["meter"] = "0/4"
        _raises(data, "/song/meter", ARRANGEMENT_ERROR, "分子必须 >= 1")

    def test_negative_pickup(self) -> None:
        data = _spec_dict()
        data["song"]["pickup_beats"] = -1.0
        _raises(data, "/song/pickup_beats", ARRANGEMENT_ERROR, "弱起拍不能为负")

    def test_duplicate_section_id(self) -> None:
        data = _spec_dict()
        data["section"][1]["id"] = "verse"
        _raises(data, "/section/1/id", ARRANGEMENT_ERROR, "段落 id 重复")

    def test_blank_section_id(self) -> None:
        data = _spec_dict()
        data["section"][0]["id"] = " "
        _raises(data, "/section/0/id", ARRANGEMENT_ERROR, "段落 id 不能为空")

    def test_section_bars_zero(self) -> None:
        data = _spec_dict()
        data["section"][0]["bars"] = 0
        _raises(data, "/section/0/bars", ARRANGEMENT_ERROR, "bars 必须 >= 1")

    def test_section_energy_out_of_range(self) -> None:
        data = _spec_dict()
        data["section"][0]["energy"] = 1.5
        _raises(data, "/section/0/energy", ARRANGEMENT_ERROR, "energy 必须在 0..1")

    def test_blank_chord(self) -> None:
        data = _spec_dict()
        data["section"][0]["progression"] = ["Dm", " "]
        _raises(data, "/section/0/progression/1", ARRANGEMENT_ERROR, "和弦不能为空")

    def test_tempo_map_not_ascending(self) -> None:
        data = _spec_dict()
        data["tempo"] = [{"beat": 0, "bpm": 96}, {"beat": 0, "bpm": 100}]
        _raises(data, "/tempo/1/beat", ARRANGEMENT_ERROR, "严格递增")

    def test_tempo_map_negative_beat(self) -> None:
        data = _spec_dict()
        data["tempo"] = [{"beat": -1, "bpm": 96}]
        _raises(data, "/tempo/0/beat", ARRANGEMENT_ERROR, "beat 不能为负")

    def test_tempo_map_must_start_at_zero(self) -> None:
        data = _spec_dict()
        data["tempo"] = [{"beat": 4, "bpm": 96}]
        _raises(data, "/tempo/0/beat", ARRANGEMENT_ERROR, "首点 beat 必须为 0")

    def test_tempo_map_bad_bpm(self) -> None:
        data = _spec_dict()
        data["tempo"] = [{"beat": 0, "bpm": -96}]
        _raises(data, "/tempo/0/bpm", ARRANGEMENT_ERROR, "bpm 必须 > 0")

    def test_tempo_map_bad_curve(self) -> None:
        data = _spec_dict()
        data["tempo"] = [{"beat": 0, "bpm": 96, "curve": "ease"}]
        _raises(data, "/tempo/0/curve", ARRANGEMENT_ERROR, "速度曲线只支持")

    def test_duplicate_track_id(self) -> None:
        data = _spec_dict()
        data["track"][1]["id"] = "piano"
        _raises(data, "/track/1/id", ARRANGEMENT_ERROR, "轨道 id 重复")

    def test_blank_track_id(self) -> None:
        data = _spec_dict()
        data["track"][0]["id"] = ""
        _raises(data, "/track/0/id", ARRANGEMENT_ERROR, "轨道 id 不能为空")

    def test_unknown_role(self) -> None:
        data = _spec_dict()
        data["track"][0]["role"] = "lead-guitar"
        _raises(data, "/track/0/role", ARRANGEMENT_ERROR, "未知角色")

    def test_program_out_of_range(self) -> None:
        data = _spec_dict()
        data["track"][0]["program"] = 200
        _raises(data, "/track/0/program", ARRANGEMENT_ERROR, "program 必须在 0..127")

    def test_channel_out_of_range(self) -> None:
        data = _spec_dict()
        data["track"][0]["channel"] = 16
        _raises(data, "/track/0/channel", ARRANGEMENT_ERROR, "channel 必须在 0..15")

    def test_drum_must_use_channel_9(self) -> None:
        data = _spec_dict()
        data["track"][1].update({"role": "drums", "channel": 3})
        _raises(data, "/track/1/channel", ARRANGEMENT_ERROR, "鼓轨必须使用通道 9")

    def test_channel_9_reserved(self) -> None:
        data = _spec_dict()
        data["track"][1]["channel"] = 9
        _raises(data, "/track/1/channel", ARRANGEMENT_ERROR, "通道 9 保留给鼓轨")

    def test_two_drum_tracks(self) -> None:
        data = _spec_dict()
        data["track"][0].update({"role": "drums", "channel": 9})
        data["track"][1].update({"role": "drums", "channel": 9})
        _raises(data, "/track", ARRANGEMENT_ERROR, "最多允许一条鼓轨")

    def test_octave_out_of_range(self) -> None:
        data = _spec_dict()
        data["track"][1]["octave"] = 12
        _raises(data, "/track/1/octave", ARRANGEMENT_ERROR, "octave 必须在 0..9")

    def test_bad_range(self) -> None:
        data = _spec_dict()
        data["track"][1]["range"] = [72, 48]
        _raises(data, "/track/1/range", ARRANGEMENT_ERROR, "音域")

    def test_range_wrong_length(self) -> None:
        data = _spec_dict()
        data["track"][1]["range"] = [48]
        _raises(data, "/track/1/range", SCHEMA_ERROR, "[low, high]")

    def test_unknown_section_ref(self) -> None:
        data = _spec_dict()
        data["track"][0]["sections"] = ["verse", "bridge"]
        _raises(data, "/track/0/sections/1", ARRANGEMENT_ERROR, "不存在的段落")

    def test_note_pitch_out_of_range(self) -> None:
        data = _spec_dict()
        data["track"][0]["note"][0]["pitch"] = 200
        _raises(data, "/track/0/note/0/pitch", ARRANGEMENT_ERROR, "pitch 必须在 0..127")

    def test_note_duration_not_positive(self) -> None:
        data = _spec_dict()
        data["track"][0]["note"][0]["duration"] = 0
        _raises(data, "/track/0/note/0/duration", ARRANGEMENT_ERROR, "duration 必须 > 0")

    def test_note_velocity_out_of_range(self) -> None:
        data = _spec_dict()
        data["track"][0]["note"][0]["velocity"] = 200
        _raises(data, "/track/0/note/0/velocity", ARRANGEMENT_ERROR, "velocity 必须在 0..127")

    def test_note_outside_any_section(self) -> None:
        data = _spec_dict()
        data["track"][0]["note"][0]["beat"] = 40.0
        _raises(data, "/track/0/note/0/beat", ARRANGEMENT_ERROR, "不在任何段落内")

    def test_note_in_undeclared_section(self) -> None:
        data = _spec_dict()
        data["track"][0]["note"] = [{"beat": 8.0, "pitch": 62, "duration": 1.0}]
        _raises(data, "/track/0/note/0/beat", ARRANGEMENT_ERROR, "不在本轨的 sections 内")

    def test_note_outside_track_range(self) -> None:
        data = _spec_dict()
        data["track"][0]["range"] = [72, 96]
        _raises(data, "/track/0/note/0/pitch", ARRANGEMENT_ERROR, "超出音域")


class TestDerived:
    def test_section_spans_and_lookup(self) -> None:
        spec = _parse_and_validate(_spec_dict())
        assert spec._section_spans() == [("verse", 0.0, 8.0), ("chorus", 8.0, 16.0)]
        assert spec.section_at(0.0) == "verse"
        assert spec.section_at(7.9) == "verse"
        assert spec.section_at(8.0) == "chorus"
        assert spec.section_at(16.0) == "chorus"  # 末段含右端点
        assert spec.section_at(-1.0) is None

    def test_pickup_shifts_sections(self) -> None:
        data = _spec_dict()
        data["song"]["pickup_beats"] = 1.0
        data["track"][0]["note"][0]["beat"] = 1.0
        spec = _parse_and_validate(data)
        assert spec._section_spans()[0][1] == 1.0
        assert spec.section_at(0.5) is None
        assert spec.to_song().sections[0].start == 1.0

    def test_bar_length_three_four(self) -> None:
        data = _spec_dict()
        data["song"]["meter"] = "3/4"
        spec = _parse_and_validate(data)
        assert spec.bar_length() == 3.0
        assert spec.total_bars == 4

    def test_summary(self) -> None:
        summary = _parse_and_validate(_spec_dict()).summary()
        assert summary["tracks"] == 2
        assert summary["notes"] == 1
        assert summary["bars"] == 4
        assert summary["duration_beats"] == 16.0
        assert summary["duration_seconds"] == pytest.approx(10.0)  # 16 拍 @96 => 10s


class TestToSong:
    def test_sections_and_maps(self) -> None:
        song = _parse_and_validate(_spec_dict()).to_song()
        assert [section.id for section in song.sections] == ["verse", "chorus"]
        assert [section.start for section in song.sections] == [0.0, 8.0]
        assert song.tempo_map[0].bpm == 96
        assert song.key_map[0].key == "D minor"
        assert song.seed == 7
        assert song.duration_beats == 16.0

    def test_clips_grouped_by_section(self) -> None:
        data = _spec_dict()
        data["track"][0]["sections"] = ["verse", "chorus"]
        data["track"][0]["note"] = [
            {"beat": 0.0, "pitch": 62, "duration": 2.0},
            {"beat": 8.0, "pitch": 65, "duration": 2.0},
        ]
        song = _parse_and_validate(data).to_song()
        piano = song.track_by_id("piano")
        assert [(clip.section_id, len(clip.notes)) for clip in piano.clips] == [
            ("verse", 1), ("chorus", 1),
        ]
        assert [note.pitch for clip in piano.clips for note in clip.notes] == [62, 65]

    def test_undeclared_sections_skipped(self) -> None:
        song = _parse_and_validate(_spec_dict()).to_song()
        piano = song.track_by_id("piano")
        assert [clip.section_id for clip in piano.clips] == ["verse"]
        # 既未声明 sections、也没有音符：不留空片段（轨道仍存在，渲染期静音）
        bass = song.track_by_id("bass")
        assert bass.clips == []

    def test_tempo_map_compiled(self) -> None:
        data = _spec_dict()
        data["tempo"] = [{"beat": 0, "bpm": 96}, {"beat": 8, "bpm": 120, "curve": "linear"}]
        song = _parse_and_validate(data).to_song()
        assert [(point.beat, point.bpm, point.curve) for point in song.tempo_map] == [
            (0.0, 96.0, "step"), (8.0, 120.0, "linear"),
        ]

    def test_midi_export(self) -> None:
        document = _parse_and_validate(_spec_dict()).to_song().to_midi()
        assert [(track.name, track.channel, track.program) for track in document.tracks] == [
            ("钢琴", 0, 0), ("贝斯", 1, 33),
        ]
        assert document.duration_beats == 16.0
        assert document.markers == [(0.0, "主歌"), (8.0, "副歌")]


class TestHelpers:
    @pytest.mark.parametrize(("text", "expected"), [
        ("Dm-Bb-F-C", ["Dm", "Bb", "F", "C"]),
        ("Dm, Bb, F", ["Dm", "Bb", "F"]),
        ("Dm Bb|F", ["Dm", "Bb", "F"]),
        ("F/A", ["F/A"]),
        ("", []),
    ])
    def test_split_progression(self, text: str, expected: list[str]) -> None:
        assert split_progression(text) == expected

    def test_split_progression_list(self) -> None:
        assert split_progression(["Dm", " Bb "]) == ["Dm", "Bb"]


_TOML = """\
schema = 1

[song]
title = "夜航"
key = "D minor"
tempo = 96
meter = "4/4"
seed = 20261024
bars = 4

[[tempo]]
beat = 0
bpm = 96

[[tempo]]
beat = 16
bpm = 104
curve = "linear"

[[section]]
id = "verse"
name = "主歌"
bars = 2
energy = 0.45
progression = ["Dm", "Bb", "F", "C"]

[[section]]
id = "chorus"
name = "副歌"
bars = 2
energy = 0.85
progression = ["Bb", "C"]

[[track]]
id = "piano"
name = "钢琴"
role = "harmony"
program = 0
sections = ["verse", "chorus"]
mix = { gain_db = -6.0, pan = -0.15, reverb_send = 0.18 }

[[track.note]]
beat = 0.0
pitch = 62
duration = 3.5
velocity = 72

[[track]]
id = "drums"
name = "鼓组"
role = "drums"
channel = 9
groove = "pop-half-time"
density = 0.8
"""


class TestTypeCastBranch:
    """`_cast` 的其余类型分支（错误信息里带实际类型名）。"""

    def test_float_field_given_string(self) -> None:
        data = _spec_dict()
        data["song"]["tempo"] = "fast"
        _raises(data, "/song/tempo", SCHEMA_ERROR, "必须是数值")

    def test_str_field_given_number(self) -> None:
        data = _spec_dict()
        data["song"]["title"] = 42
        _raises(data, "/song/title", SCHEMA_ERROR, "必须是字符串")

    def test_bool_field_given_number(self) -> None:
        data = _spec_dict()
        data["track"][0]["muted"] = 1
        _raises(data, "/track/0/muted", SCHEMA_ERROR, "必须是布尔值")

    def test_list_field_given_table(self) -> None:
        data = _spec_dict()
        data["track"][0]["sections"] = {"verse": True}
        _raises(data, "/track/0/sections", SCHEMA_ERROR, "必须是数组")

    def test_meter_non_numeric(self) -> None:
        data = _spec_dict()
        data["song"]["meter"] = "a/4"
        _raises(data, "/song/meter", ARRANGEMENT_ERROR, "拍号必须形如")

    def test_progression_as_string(self) -> None:
        data = _spec_dict()
        data["section"][0]["progression"] = "Dm Bb"
        spec = _parse_and_validate(data)
        assert spec.sections[0].progression == ["Dm", "Bb"]

    def test_density_and_groove_survive_round_trip(self) -> None:
        data = _spec_dict()
        data["track"][1].update({"density": 0.75, "groove": "funk"})
        spec = _parse_and_validate(data)
        payload = spec.to_dict()
        assert payload["track"][1]["density"] == 0.75
        assert payload["track"][1]["groove"] == "funk"
        assert SongSpec.from_dict(payload).to_dict() == payload


class TestNotesAndChannels:
    def test_note_crossing_section_end(self) -> None:
        data = _spec_dict()
        data["track"][0]["note"][0].update({"beat": 7.0, "duration": 2.0})
        _raises(data, "/track/0/note/0/duration", ARRANGEMENT_ERROR, "越过段落")

    def test_duplicate_explicit_channel(self) -> None:
        data = _spec_dict()
        data["track"][0]["channel"] = 3
        data["track"][1]["channel"] = 3
        _raises(data, "/track/1/channel", ARRANGEMENT_ERROR, "已被轨道")

    def test_to_dict_keeps_optional_fields(self) -> None:
        data = _spec_dict()
        data["song"]["pickup_beats"] = 1.0
        data["song"]["bars"] = 4
        data["track"][0]["note"][0]["beat"] = 1.0
        data["tempo"] = [{"beat": 0, "bpm": 96}]
        payload = _parse_and_validate(data).to_dict()
        assert payload["song"]["pickup_beats"] == 1.0
        assert payload["song"]["bars"] == 4
        assert payload["tempo"] == [{"beat": 0, "bpm": 96, "curve": "step"}]
