"""MIDI 交换层（2.0 PR-A）：全保真 SMF 读写 + 通道分配。

公开 API::

    from sunoauxtool.midi import MidiDocumentV2, MidiTrackV2, NoteEvent, write_smf, read_smf

旧 ``sunoauxtool.models.midi`` 保持不变；新代码请使用本包。
"""

from __future__ import annotations

from sunoauxtool.midi.channel import (
    MAX_MELODIC_CHANNELS,
    ChannelRequest,
    allocate_channels,
)
from sunoauxtool.midi.document import (
    DRUM_CHANNEL,
    ControlChange,
    KeyPoint,
    MeterPoint,
    MidiDocumentV2,
    MidiTrackV2,
    NoteEvent,
    RawEvent,
    TempoPoint,
    beat_to_seconds,
    iter_notes,
    seconds_between,
    total_bars,
    validate_timeline,
)
from sunoauxtool.midi.exceptions import ChannelAllocationError
from sunoauxtool.midi.io import read_smf, write_smf

__all__ = [
    "DRUM_CHANNEL",
    "MAX_MELODIC_CHANNELS",
    "ChannelAllocationError",
    "ChannelRequest",
    "ControlChange",
    "KeyPoint",
    "MeterPoint",
    "MidiDocumentV2",
    "MidiTrackV2",
    "NoteEvent",
    "RawEvent",
    "TempoPoint",
    "allocate_channels",
    "beat_to_seconds",
    "iter_notes",
    "read_smf",
    "seconds_between",
    "total_bars",
    "validate_timeline",
    "write_smf",
]
