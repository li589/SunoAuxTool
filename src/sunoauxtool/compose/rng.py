"""确定性随机：按 ``(song_seed, section, track, stage)`` 派生互不干扰的种子。

铁律（2.0 G8）：

- **禁止模块级/全局 random**，一切随机数来自 ``rng_for()`` 返回的独立生成器；
- 同一份清单 + 同一种子 → 逐字节相同的 MIDI（``tests/compose`` 用 sha256 断言）；
- 改 A 轨的密度不影响 B 轨；增删轨道不改变已有轨道内容（可增量编辑）。

派生公式（与 ``docs/refactor-2.0-multitrack.md`` 第 5 节一致）::

    seed = int.from_bytes(blake2b(f"{song}:{section}:{track}:{stage}", 8).digest(), "big")
"""
from __future__ import annotations

import hashlib
import random

#: 派生种子的字节数（64 bit）
DIGEST_SIZE = 8


def seed_for(song_seed: int, section_id: str = "", track_id: str = "", stage: str = "") -> int:
    """派生 64 位子种子（确定性，跨平台一致）。"""
    material = f"{int(song_seed)}:{section_id}:{track_id}:{stage}".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(material, digest_size=DIGEST_SIZE).digest(), "big")


def rng_for(
    song_seed: int, section_id: str = "", track_id: str = "", stage: str = ""
) -> random.Random:
    """派生独立随机数发生器（每次调用都是全新实例）。"""
    return random.Random(seed_for(song_seed, section_id, track_id, stage))
