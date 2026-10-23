"""编曲域（2.0）：Song 模型 + 清单 + 旧模型适配器。

公开 API::

    from sunoauxtool.compose import Song, Section, Track, Clip, MixStrip, InstrumentRef
    from sunoauxtool.compose import SongSpec, SpecError, SongBuilder
    from sunoauxtool.compose import from_sequence, from_legacy_midi

分层：
    song.py    编曲领域模型（Song 为唯一真源）
    spec.py    song.toml 清单：结构校验(44) + 语义校验(40) + 编译为 Song
    api.py     Python DSL（SongBuilder → SongSpec，与 TOML 同构）
    cli.py     `sunoaux compose init/validate/build`

生成引擎（arranger/harmony/melody…）在 2.0 PR-D 接入。
"""

from __future__ import annotations

from sunoauxtool.compose.adapters import from_legacy_midi, from_sequence
from sunoauxtool.compose.api import SongBuilder
from sunoauxtool.compose.song import (
    KNOWN_ROLES,
    Clip,
    InstrumentRef,
    MixStrip,
    Section,
    Song,
    Track,
    flatten_notes,
)
from sunoauxtool.compose.spec import (
    ARRANGEMENT_ERROR,
    SCHEMA_ERROR,
    SCHEMA_VERSION,
    NoteSpec,
    SectionSpec,
    SongSpec,
    SpecError,
    TrackSpec,
)

__all__ = [
    "ARRANGEMENT_ERROR",
    "KNOWN_ROLES",
    "SCHEMA_ERROR",
    "SCHEMA_VERSION",
    "Clip",
    "InstrumentRef",
    "MixStrip",
    "NoteSpec",
    "Section",
    "SectionSpec",
    "Song",
    "SongBuilder",
    "SongSpec",
    "SpecError",
    "Track",
    "TrackSpec",
    "flatten_notes",
    "from_legacy_midi",
    "from_sequence",
]
