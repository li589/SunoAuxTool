"""fmp4.py 单元测试：ISO BMFF 原子解析与 mdat 提取。

全部用合成的字节串，不依赖 ffmpeg、不依赖任何真实音频文件。
"""

from __future__ import annotations

import struct

from sunoauxtool.download.fmp4 import (
    Atom,
    concatenate_mdat,
    is_fragmented_mp4,
    iter_mdat,
    parse_atoms,
    summarize,
)

from conftest import box


# -- parse_atoms 基本行为 ---------------------------------------------------

def test_parse_atoms_reads_top_level_sequence(fmp4_bytes):
    """按出现顺序解析顶层原子。"""
    atoms = parse_atoms(fmp4_bytes)
    assert [a.type for a in atoms] == ["ftyp", "moov", "moof", "mdat"]


def test_parse_atoms_offsets_are_contiguous(fmp4_bytes):
    """每个原子的 offset 紧接前一个的尾部。"""
    atoms = parse_atoms(fmp4_bytes)
    cursor = 0
    for atom in atoms:
        assert atom.offset == cursor
        cursor += atom.size
    assert cursor == len(fmp4_bytes)


def test_parse_atoms_32bit_header_size(fmp4_bytes):
    """常规原子头部 8 字节。"""
    assert all(a.header_size == 8 for a in parse_atoms(fmp4_bytes))


def test_parse_atoms_64bit_size_uses_16_byte_header():
    """长度字段=1 时，后续 8 字节为 64 位真实长度，头部占 16 字节。"""
    payload = b"\xab" * 40
    big = struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", len(payload) + 16) + payload
    atoms = parse_atoms(big)
    assert len(atoms) == 1
    assert atoms[0].type == "mdat"
    assert atoms[0].header_size == 16
    assert atoms[0].size == len(payload) + 16
    assert atoms[0].payload_size == len(payload)
    assert atoms[0].payload_offset == 16


def test_parse_atoms_64bit_size_truncated_stops():
    """声称 64 位长度但字节不足 → 停止解析，不抛异常。"""
    truncated = struct.pack(">I", 1) + b"mdat" + b"\x00\x00"
    assert parse_atoms(truncated) == []


def test_parse_atoms_size_zero_extends_to_eof():
    """长度字段=0 表示原子延伸到文件末尾。"""
    tail = b"\xcd" * 25
    data = struct.pack(">I", 0) + b"mdat" + tail
    atoms = parse_atoms(data)
    assert len(atoms) == 1
    assert atoms[0].size == len(data)
    assert atoms[0].payload_size == len(tail)


def test_parse_atoms_stops_on_non_ascii_type():
    """类型字段不是可打印 ASCII → 认定结构损坏，返回已解析部分。"""
    data = box(b"ftyp", b"payload") + struct.pack(">I", 16) + b"\x00\x01\x02\x03" + b"\x00" * 8
    atoms = parse_atoms(data)
    assert [a.type for a in atoms] == ["ftyp"]


def test_parse_atoms_stops_on_trailing_bytes_below_header():
    """尾部不足 8 字节 → 不再解析。"""
    data = box(b"ftyp", b"x" * 8) + b"\x00\x01\x02"
    assert [a.type for a in parse_atoms(data)] == ["ftyp"]


def test_parse_atoms_records_illegal_size_then_stops():
    """长度 < 8 且非 0/1 → 该原子被记录后立即停止（避免死循环）。"""
    data = box(b"ftyp", b"x" * 8) + struct.pack(">I", 4) + b"moof" + b"\x00" * 8
    assert [a.type for a in parse_atoms(data)] == ["ftyp", "moof"]


def test_parse_atoms_empty_input():
    assert parse_atoms(b"") == []


def test_parse_atoms_short_input():
    assert parse_atoms(b"\x00\x00\x00") == []


# -- Atom 属性 --------------------------------------------------------------

def test_atom_payload_properties():
    a = Atom(type="mdat", offset=100, size=50, header_size=8)
    assert a.payload_offset == 108
    assert a.payload_size == 42


def test_atom_payload_size_never_negative():
    """头部比整体还长时载荷为 0，不出现负数。"""
    assert Atom(type="x", offset=0, size=4, header_size=8).payload_size == 0


# -- is_fragmented_mp4 ------------------------------------------------------

def test_is_fragmented_mp4_true(fmp4_bytes):
    assert is_fragmented_mp4(fmp4_bytes) is True


def test_is_fragmented_mp4_false_without_moof():
    """缺 moof → 不是分片 MP4（普通 MP4）。"""
    data = box(b"ftyp", b"isom\x00\x00\x02\x00") + box(b"moov", b"\x00" * 16)
    data += box(b"mdat", b"\x00" * 32)
    assert is_fragmented_mp4(data) is False


def test_is_fragmented_mp4_false_without_ftyp():
    data = box(b"moov", b"\x00" * 8) + box(b"moof", b"\x00" * 8) + box(b"mdat", b"\x00" * 8)
    assert is_fragmented_mp4(data) is False


def test_is_fragmented_mp4_false_on_random_bytes(encrypted_bytes):
    assert is_fragmented_mp4(encrypted_bytes) is False


# -- mdat 提取 --------------------------------------------------------------

def test_iter_mdat_yields_payloads_in_order(make_fmp4):
    data = make_fmp4(mdat_payloads=(b"AAA", b"BBB", b"CCC"))
    assert list(iter_mdat(data)) == [b"AAA", b"BBB", b"CCC"]


def test_concatenate_mdat_joins_all_fragments(make_fmp4):
    """Suno 场景：多片 mdat 拼起来才是完整码流。"""
    data = make_fmp4(mdat_payloads=(b"\x01\x02", b"\x03", b"\x04\x05\x06"))
    assert concatenate_mdat(data) == b"\x01\x02\x03\x04\x05\x06"


def test_concatenate_mdat_empty_when_no_mdat():
    data = box(b"ftyp", b"isom\x00\x00\x02\x00")
    assert concatenate_mdat(data) == b""


def test_mdat_payload_excludes_header(make_fmp4):
    """载荷必须不含 8 字节头部——误含会污染音频码流。"""
    data = make_fmp4(mdat_payloads=(b"PAYLOAD!",))
    assert concatenate_mdat(data) == b"PAYLOAD!"


# -- summarize --------------------------------------------------------------

def test_summarize_counts_atoms_and_fragments(make_fmp4):
    data = make_fmp4(mdat_payloads=(b"x" * 10, b"y" * 20))
    s = summarize(data)
    assert s["moof_count"] == 2
    assert s["mdat_count"] == 2
    assert s["mdat_total_bytes"] == 30
    assert s["atom_types"] == ["ftyp", "mdat", "moof", "moov"]
    assert s["atom_count"] == 6


def test_summarize_overhead_is_total_minus_mdat(fmp4_bytes):
    s = summarize(fmp4_bytes)
    assert s["container_overhead_bytes"] == len(fmp4_bytes) - s["mdat_total_bytes"]
