"""Python DSL：``SongBuilder``（2.0 PR-C）。

**只做收集，不做生成**：DSL 与 `song.toml` 编译到**同一个** ``SongSpec``，
之后走同一条校验/编译管线，因此两种写法的能力与报错完全一致。

DSL 是"人在 Python 里写清单"的便捷层；AI 走 TOML/JSON 即可::

    from sunoauxtool.compose.api import SongBuilder

    song = (
        SongBuilder("夜航", key="D minor", tempo=96, seed=20261024)
        .section("verse", bars=4, energy=0.45, progression="Dm-Bb-F-C")
        .section("chorus", bars=4, energy=0.85, progression="Bb-C-Dm-Dm")
        .track("piano", role="harmony", program=0, mix={"gain_db": -6.0, "pan": -0.15})
        .track("bass", role="bass", program=33, octave=2)
        .note("piano", beat=0, pitch=62, duration=2)
        .note("bass", beat=0, pitch=38, duration=4)
        .build()          # -> compose.song.Song
    )
"""
from __future__ import annotations

from typing import Any, Sequence

from sunoauxtool.compose.song import MixStrip, Song
from sunoauxtool.compose.spec import SCHEMA_VERSION, SongSpec, split_progression
from sunoauxtool.exceptions import ParameterError


class SongBuilder:
    """链式构建 ``SongSpec``（每个方法都返回 ``self``，便于流水式书写）。"""

    def __init__(
        self,
        title: str = "",
        *,
        key: str = "C major",
        tempo: float = 120.0,
        meter: str = "4/4",
        seed: int = 0,
        pickup_beats: float = 0.0,
    ) -> None:
        self._data: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "song": {
                "title": title,
                "key": key,
                "tempo": float(tempo),
                "meter": meter,
                "seed": int(seed),
            },
            "section": [],
            "track": [],
        }
        if pickup_beats:
            self._data["song"]["pickup_beats"] = float(pickup_beats)

    # -- 全曲参数 ---------------------------------------------------------

    def key(self, value: str) -> "SongBuilder":
        """调性（``"D minor"``）。"""
        self._data["song"]["key"] = value
        return self

    def tempo(self, bpm: float) -> "SongBuilder":
        """基准速度（BPM）。"""
        self._data["song"]["tempo"] = float(bpm)
        return self

    def meter(self, value: str) -> "SongBuilder":
        """名义拍号（``"4/4"`` / ``"3/4"`` / ``"6/8"``）。"""
        self._data["song"]["meter"] = value
        return self

    def seed(self, value: int) -> "SongBuilder":
        """随机种子（PR-D 编曲引擎的确定性来源）。"""
        self._data["song"]["seed"] = int(value)
        return self

    def pickup(self, beats: float) -> "SongBuilder":
        """弱起拍数。"""
        self._data["song"]["pickup_beats"] = float(beats)
        return self

    def tempo_change(self, beat: float, bpm: float, curve: str = "step") -> "SongBuilder":
        """追加速度变化点（beat 为全曲累计拍；首个点必须为 0）。"""
        self._data.setdefault("tempo", []).append(
            {"beat": float(beat), "bpm": float(bpm), "curve": curve}
        )
        return self

    # -- 段落与轨道 -------------------------------------------------------

    def section(
        self,
        section_id: str,
        *,
        name: str = "",
        bars: int = 4,
        energy: float = 0.5,
        progression: str | Sequence[str] | None = None,
        label: str = "",
    ) -> "SongBuilder":
        """追加段落（起始拍由小节累加自动决定，与 TOML 一致）。"""
        self._data["section"].append({
            "id": section_id,
            "name": name,
            "bars": int(bars),
            "energy": float(energy),
            "progression": split_progression(progression) if progression else [],
            "label": label,
        })
        return self

    def track(
        self,
        track_id: str,
        *,
        name: str = "",
        role: str = "harmony",
        instrument: str = "",
        bank: str = "gm",
        program: int | None = None,
        channel: int | None = None,
        octave: int = 4,
        range: tuple[int, int] | None = None,
        sections: Sequence[str] | None = None,
        mix: MixStrip | dict | None = None,
        muted: bool = False,
        density: float | None = None,
        groove: str | None = None,
    ) -> "SongBuilder":
        """追加轨道（``mix`` 可传 ``MixStrip`` 或字典）。"""
        payload: dict[str, Any] = {
            "id": track_id,
            "name": name,
            "role": role,
            "instrument": instrument,
            "bank": bank,
            "program": program,
            "channel": channel,
            "octave": int(octave),
            "sections": list(sections or []),
            "muted": bool(muted),
        }
        if range is not None:
            payload["range"] = [int(range[0]), int(range[1])]
        if mix is not None:
            payload["mix"] = mix.to_dict() if isinstance(mix, MixStrip) else dict(mix)
        if density is not None:
            payload["density"] = float(density)
        if groove is not None:
            payload["groove"] = groove
        self._data["track"].append(payload)
        return self

    def note(
        self,
        track_id: str,
        *,
        beat: float,
        pitch: int,
        duration: float,
        velocity: int = 64,
        release_velocity: int = 64,
        articulation: str | None = None,
        offset_ticks: int = 0,
    ) -> "SongBuilder":
        """给已声明的轨道追加显式音符（``beat`` 为全曲绝对拍）。"""
        track = self._find_track(track_id)
        track.setdefault("note", []).append({
            "beat": float(beat),
            "pitch": int(pitch),
            "duration": float(duration),
            "velocity": int(velocity),
            "release_velocity": int(release_velocity),
            "articulation": articulation,
            "offset_ticks": int(offset_ticks),
        })
        return self

    # -- 编译 -------------------------------------------------------------

    def to_dict(self) -> dict:
        """展开为清单字典（与 TOML 同构，可直接 ``json.dump``）。"""
        return {
            "schema": self._data["schema"],
            "song": dict(self._data["song"]),
            **({"tempo": list(self._data["tempo"])} if self._data.get("tempo") else {}),
            "section": [dict(item) for item in self._data["section"]],
            "track": [_clean_track(item) for item in self._data["track"]],
        }

    def to_spec(self) -> SongSpec:
        """编译为 ``SongSpec``（结构 + 语义校验在此发生）。"""
        return SongSpec.from_dict(self.to_dict())

    def validate(self) -> None:
        """只校验不编译（失败抛 ``SpecError``，带 JSON Pointer）。"""
        self.to_spec().validate()

    def build(self) -> Song:
        """编译为 ``Song``（唯一真源）。"""
        return self.to_spec().to_song()

    # -- 内部 -------------------------------------------------------------

    def _find_track(self, track_id: str) -> dict:
        for item in self._data["track"]:
            if item["id"] == track_id:
                return item
        known = ", ".join(repr(item["id"]) for item in self._data["track"]) or "（无）"
        raise ParameterError(f"未声明轨道 {track_id!r}；已声明: {known}")


def _clean_track(item: dict) -> dict:
    """去掉未显式给予的可选字段（避免清单里出现一堆 ``None``）。"""
    payload = {key: value for key, value in item.items() if value is not None}
    return payload
