"""编曲域（2.0）：Song 模型 + 清单 + 编排引擎 + 旧模型适配器。

公开 API::

    from sunoauxtool.compose import Song, Section, Track, Clip, MixStrip, InstrumentRef
    from sunoauxtool.compose import SongSpec, SpecError, SongBuilder
    from sunoauxtool.compose import arrange, ArrangeOptions
    from sunoauxtool.compose import from_sequence, from_legacy_midi

分层：
    song.py           编曲领域模型（Song 为唯一真源）
    spec.py           song.toml 清单：结构校验(44) + 语义校验(40) + 编译为 Song
    api.py            Python DSL（SongBuilder → SongSpec，与 TOML 同构）
    arranger.py       编排主流程（Spec + seed → Song，显式音符优先）
    harmony.py        调式/和弦解析 + 声部配置
    melody.py         动机生成与发展、终止式、对位
    rhythm.py         groove → 鼓组图案
    orchestration.py  角色 → 乐器/音域/力度、能量矩阵
    humanize.py       力度抖动 + 微时序
    rng.py            确定性种子派生（blake2b）
    cli.py            `sunoaux compose init/validate/build`
"""

from __future__ import annotations

from sunoauxtool.compose.adapters import from_legacy_midi, from_sequence
from sunoauxtool.compose.api import SongBuilder
from sunoauxtool.compose.arranger import ArrangeOptions, arrange
from sunoauxtool.compose.orchestration import roles_for_energy
from sunoauxtool.compose.rng import rng_for, seed_for
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
    "ArrangeOptions",
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
    "arrange",
    "flatten_notes",
    "from_legacy_midi",
    "from_sequence",
    "rng_for",
    "roles_for_energy",
    "seed_for",
]
