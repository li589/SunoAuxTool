"""MIDI 通道分配：显式优先、鼓轨固定 9、自动取最小空闲通道。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from sunoauxtool.exceptions import ParameterError
from sunoauxtool.midi.document import DRUM_CHANNEL
from sunoauxtool.midi.exceptions import ChannelAllocationError

#: 除鼓轨外的可用通道数（0-15 共 16 个，扣掉 9）
MAX_MELODIC_CHANNELS = 15


@dataclass(frozen=True)
class ChannelRequest:
    """一条轨道的通道申请。

    Attributes:
        track_id: 轨道标识（用于返回映射与报错定位）。
        channel: 显式通道 0-15；None 表示自动分配。
        is_drum: 是否鼓轨（鼓轨强制通道 9）。
    """

    track_id: str
    channel: int | None = None
    is_drum: bool = False


def allocate_channels(requests: Sequence[ChannelRequest]) -> dict[str, int]:
    """为轨道分配 MIDI 通道。

    规则（与重构文档 §4.3 一致）：

    1. 显式通道优先，但非鼓轨不得占用 9；
    2. 鼓轨固定 9，全曲最多一条；
    3. 其余轨道按申请顺序取最小空闲通道（跳过 9）；
    4. 超过 15 条旋律轨抛 ``ChannelAllocationError``（错误码 45）。

    Args:
        requests: 通道申请列表（顺序决定自动分配的先后）。

    Returns:
        ``{track_id: channel}``，保持入参顺序。

    Raises:
        ParameterError: track_id 为空/重复、显式通道越界。
        ChannelAllocationError: 鼓轨冲突或旋律通道耗尽。
    """
    if not requests:
        return {}

    seen_ids: set[str] = set()
    for request in requests:
        if not str(request.track_id).strip():
            raise ParameterError("track_id 不能为空")
        if request.track_id in seen_ids:
            raise ParameterError(f"track_id 重复: {request.track_id!r}")
        seen_ids.add(request.track_id)
        if request.channel is not None and not 0 <= int(request.channel) <= 15:
            raise ParameterError(
                f"显式通道超出范围 (0-15): {request.track_id}={request.channel}"
            )

    drums = [request for request in requests if request.is_drum]
    if len(drums) > 1:
        raise ChannelAllocationError(
            f"检测到 {len(drums)} 条鼓轨（{', '.join(r.track_id for r in drums)}）；"
            f"MIDI 只有一个打击通道 {DRUM_CHANNEL}，请合并为一条鼓轨"
        )
    if drums:
        explicit = drums[0].channel
        if explicit is not None and explicit != DRUM_CHANNEL:
            raise ChannelAllocationError(
                f"鼓轨 {drums[0].track_id!r} 必须使用通道 {DRUM_CHANNEL}，实为 {explicit}"
            )

    melodic_count = len(requests) - len(drums)
    if melodic_count > MAX_MELODIC_CHANNELS:
        raise ChannelAllocationError(
            f"旋律轨 {melodic_count} 条超过可用通道上限 {MAX_MELODIC_CHANNELS}"
            f"（通道 {DRUM_CHANNEL} 保留给鼓轨）；请合并分层或拆分为多个 MIDI 文件"
        )

    assigned: dict[str, int] = {}
    used: set[int] = set()

    # 第一轮：显式通道（先占位，避免被自动分配抢走）
    for request in requests:
        if request.channel is None:
            continue
        channel = int(request.channel)
        if request.is_drum:
            assigned[request.track_id] = DRUM_CHANNEL
            used.add(DRUM_CHANNEL)
            continue
        if channel == DRUM_CHANNEL:
            raise ChannelAllocationError(
                f"非鼓轨 {request.track_id!r} 不能占用打击通道 {DRUM_CHANNEL}"
            )
        if channel in used:
            owner = next(k for k, v in assigned.items() if v == channel)
            raise ChannelAllocationError(
                f"通道 {channel} 冲突：{owner!r} 与 {request.track_id!r} 同时申请"
            )
        assigned[request.track_id] = channel
        used.add(channel)

    # 第二轮：鼓轨（若未显式）
    for request in drums:
        if request.track_id not in assigned:
            if DRUM_CHANNEL in used:
                owner = next(k for k, v in assigned.items() if v == DRUM_CHANNEL)
                raise ChannelAllocationError(
                    f"通道 {DRUM_CHANNEL} 已被 {owner!r} 占用，鼓轨 {request.track_id!r} 无法分配"
                )
            assigned[request.track_id] = DRUM_CHANNEL
            used.add(DRUM_CHANNEL)

    # 第三轮：自动分配（按入参顺序取最小空闲，跳过 9）
    for request in requests:
        if request.track_id in assigned:
            continue
        channel = next(
            (c for c in range(16) if c != DRUM_CHANNEL and c not in used),
            None,
        )
        if channel is None:
            raise ChannelAllocationError(
                f"旋律通道已耗尽，无法为 {request.track_id!r} 分配通道"
            )
        assigned[request.track_id] = channel
        used.add(channel)

    # 按入参顺序返回，保证确定性
    return {request.track_id: assigned[request.track_id] for request in requests}
