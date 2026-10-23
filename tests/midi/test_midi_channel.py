"""MIDI 通道分配（channel.py）单测。"""

from __future__ import annotations

import pytest

from sunoauxtool.exceptions import ParameterError
from sunoauxtool.midi import (
    DRUM_CHANNEL,
    MAX_MELODIC_CHANNELS,
    ChannelAllocationError,
    ChannelRequest,
    allocate_channels,
)


class TestAllocateChannels:
    def test_empty(self) -> None:
        assert allocate_channels([]) == {}

    def test_auto_allocation_skips_drum_channel(self) -> None:
        result = allocate_channels([
            ChannelRequest("a"),
            ChannelRequest("b"),
            ChannelRequest("c"),
        ])
        assert result == {"a": 0, "b": 1, "c": 2}

    def test_drum_gets_channel_nine(self) -> None:
        result = allocate_channels([
            ChannelRequest("piano"),
            ChannelRequest("kit", is_drum=True),
            ChannelRequest("bass"),
        ])
        assert result["kit"] == DRUM_CHANNEL
        assert result["piano"] == 0
        assert result["bass"] == 1

    def test_explicit_channels_preserved(self) -> None:
        result = allocate_channels([
            ChannelRequest("a", channel=5),
            ChannelRequest("b", channel=2),
            ChannelRequest("c"),
        ])
        assert result == {"a": 5, "b": 2, "c": 0}

    def test_explicit_wins_over_auto(self) -> None:
        result = allocate_channels([
            ChannelRequest("auto"),
            ChannelRequest("explicit", channel=0),
        ])
        assert result["explicit"] == 0
        assert result["auto"] == 1

    def test_order_is_preserved(self) -> None:
        requests = [ChannelRequest("x"), ChannelRequest("y"), ChannelRequest("z")]
        assert list(allocate_channels(requests)) == ["x", "y", "z"]

    def test_fifteen_melodic_tracks_fit(self) -> None:
        requests = [ChannelRequest(f"t{i}") for i in range(MAX_MELODIC_CHANNELS)]
        result = allocate_channels(requests)
        assert len(result) == MAX_MELODIC_CHANNELS
        assert DRUM_CHANNEL not in result.values()

    def test_sixteen_melodic_tracks_rejected(self) -> None:
        requests = [ChannelRequest(f"t{i}") for i in range(MAX_MELODIC_CHANNELS + 1)]
        with pytest.raises(ChannelAllocationError) as excinfo:
            allocate_channels(requests)
        assert excinfo.value.code == 45

    def test_duplicate_explicit_channel_rejected(self) -> None:
        with pytest.raises(ChannelAllocationError):
            allocate_channels([
                ChannelRequest("a", channel=4),
                ChannelRequest("b", channel=4),
            ])

    def test_non_drum_cannot_take_channel_nine(self) -> None:
        with pytest.raises(ChannelAllocationError):
            allocate_channels([ChannelRequest("pad", channel=DRUM_CHANNEL)])

    def test_drum_with_wrong_explicit_channel(self) -> None:
        with pytest.raises(ChannelAllocationError):
            allocate_channels([ChannelRequest("kit", channel=2, is_drum=True)])

    def test_two_drum_tracks_rejected(self) -> None:
        with pytest.raises(ChannelAllocationError):
            allocate_channels([
                ChannelRequest("kit1", is_drum=True),
                ChannelRequest("kit2", is_drum=True),
            ])

    def test_explicit_channel_out_of_range(self) -> None:
        with pytest.raises(ParameterError):
            allocate_channels([ChannelRequest("a", channel=16)])

    def test_duplicate_track_id_rejected(self) -> None:
        with pytest.raises(ParameterError):
            allocate_channels([ChannelRequest("a"), ChannelRequest("a")])

    def test_empty_track_id_rejected(self) -> None:
        with pytest.raises(ParameterError):
            allocate_channels([ChannelRequest("  ")])

    def test_error_code_constant(self) -> None:
        assert ChannelAllocationError("x").code == 45
