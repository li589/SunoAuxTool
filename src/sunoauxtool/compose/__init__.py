"""编曲域（2.0 PR-B）：Song 模型 + 旧模型适配器。

公开 API::

    from sunoauxtool.compose import Song, Section, Track, Clip, MixStrip, InstrumentRef
    from sunoauxtool.compose import from_sequence, from_legacy_midi

生成引擎（arranger）与清单解析在后续 PR 接入。
"""

from __future__ import annotations

from sunoauxtool.compose.adapters import from_legacy_midi, from_sequence
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

__all__ = [
    "KNOWN_ROLES",
    "Clip",
    "InstrumentRef",
    "MixStrip",
    "Section",
    "Song",
    "Track",
    "flatten_notes",
    "from_legacy_midi",
    "from_sequence",
]
