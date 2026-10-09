"""forensics.py 单元测试：熵 / 卡方 / 周期扫描 / 容器识别 / 判定结论。

核心断言基于判据常量而非硬编码数字，避免样本量微调就让测试失效。
"""

from __future__ import annotations

import math

import pytest

from sunoauxtool.download.forensics import (
    CHI2_UNIFORM_MAX,
    RANDOM_MATCH_BASE,
    Verdict,
    _detect_container,
    chi_square,
    classify,
    entropy,
    identify,
    periodicity_scan,
)


# -- entropy ----------------------------------------------------------------

def test_entropy_empty_is_zero():
    assert entropy(b"") == 0.0


def test_entropy_single_repeated_byte_is_zero():
    assert entropy(b"\x00" * 4096) == 0.0


def test_entropy_two_equal_values_is_one_bit():
    assert entropy(b"\x00\x01" * 512) == pytest.approx(1.0)


def test_entropy_uniform_all_256_bytes_is_eight_bits():
    assert entropy(bytes(range(256))) == pytest.approx(8.0)


def test_entropy_never_exceeds_eight_bits(encrypted_bytes):
    assert entropy(encrypted_bytes) <= 8.0


def test_entropy_random_sample_approaches_eight(encrypted_bytes):
    assert entropy(encrypted_bytes) > 7.99


# -- chi_square -------------------------------------------------------------

def test_chi_square_empty_is_zero():
    assert chi_square(b"") == 0.0


def test_chi_square_perfectly_uniform_is_zero():
    """每个字节恰好出现同样多次 → χ² 恰为 0。"""
    assert chi_square(bytes(range(256)) * 8) == pytest.approx(0.0)


def test_chi_square_skewed_is_large():
    assert chi_square(b"\x00" * 1000) > 100_000


def test_chi_square_random_sample_near_uniform_mean(encrypted_bytes):
    """均匀随机样本的 χ² 应落在 df=255 的均值附近（255 ± 若干 σ，σ≈22.6）。"""
    chi = chi_square(encrypted_bytes)
    assert 150 < chi < CHI2_UNIFORM_MAX


def test_chi_square_compressed_like_is_far_above_uniform(encrypted_bytes):
    """压缩明文（保留统计结构）的 χ² 应远高于同长度随机样本。"""
    skewed = bytes([i % 64 for i in range(len(encrypted_bytes))])
    assert chi_square(skewed) > chi_square(encrypted_bytes) * 10


# -- periodicity_scan -------------------------------------------------------

def test_periodicity_scan_too_short_returns_none():
    """数据短于 max_period 时无法扫描。"""
    assert periodicity_scan(b"\x00" * 10, max_period=64) == (None, 0.0)


def test_periodicity_scan_detects_repeating_key_period():
    """重复密钥 XOR 特征：在密钥长度处出现满匹配率。"""
    best_k, rate = periodicity_scan(b"KEY" * 20_000, max_period=64)
    assert best_k == 3
    assert rate == pytest.approx(1.0)


def test_periodicity_scan_random_stays_near_baseline(encrypted_bytes):
    """强加密样本不应检出周期——否则会把密文误判成弱加密。"""
    best_k, rate = periodicity_scan(encrypted_bytes, max_period=64)
    assert best_k is not None
    assert rate < RANDOM_MATCH_BASE * 3


# -- _detect_container ------------------------------------------------------

def test_detect_container_mp4_via_ftyp_at_offset_four():
    """ftyp 通常在偏移 4，不能只在开头找。"""
    assert _detect_container(b"\x00\x00\x00\x20ftypisom") == "mp4"


@pytest.mark.parametrize(
    "head, expected",
    [
        (b"OggS\x00\x02", "ogg"),
        (b"RIFF\x00\x00\x00\x00WAVE", "wav"),
        (b"ID3\x04\x00\x00", "mp3"),
        (b"fLaC\x00\x00\x00\x22", "flac"),
    ],
)
def test_detect_container_known_magics(head, expected):
    assert _detect_container(head + b"\x00" * 32) == expected


def test_detect_container_mp3_via_frame_sync():
    """无 ID3 的裸 MP3 靠帧同步字 0xFFE0 掩码识别。"""
    assert _detect_container(b"\xff\xfb\x90\x00" + b"\x00" * 32) == "mp3"


def test_detect_container_rejects_lookalike_frame_sync():
    """0xFF 后高 3 位不全为 1 → 不是帧同步。"""
    assert _detect_container(b"\xff\x1b\x90\x00" + b"\x00" * 32) is None


def test_detect_container_random_returns_none(encrypted_bytes):
    assert _detect_container(encrypted_bytes) is None


def test_detect_container_too_short_returns_none():
    assert _detect_container(b"ft") is None


# -- classify：明文分支 -----------------------------------------------------

def test_classify_fmp4_is_breakable(fmp4_bytes):
    v = classify(fmp4_bytes)
    assert v.kind == "fmp4"
    assert v.breakable is True
    assert v.is_encrypted is False


def test_classify_fmp4_evidence_mentions_fragments(fmp4_bytes):
    joined = " ".join(classify(fmp4_bytes).evidence)
    assert "容器魔数" in joined
    assert "moof/mdat" in joined


def test_classify_plain_mp4_without_fragments():
    from conftest import box

    data = box(b"ftyp", b"isom\x00\x00\x02\x00") + box(b"moov", b"\x00" * 16)
    v = classify(data)
    assert v.kind == "mp4"
    assert v.breakable is True
    assert any("非分片" in e for e in v.evidence)


def test_classify_ogg_container_is_breakable():
    v = classify(b"OggS\x00\x02" + bytes(range(256)) * 4)
    assert v.kind == "ogg"
    assert v.breakable is True


def test_classify_carries_path_through():
    assert classify(b"\x00" * 64, path="/tmp/x.bin").path == "/tmp/x.bin"


# -- classify：密文分支 -----------------------------------------------------

def test_classify_uniform_random_is_encrypted_and_unbreakable(encrypted_bytes):
    v = classify(encrypted_bytes)
    assert v.kind == "encrypted"
    assert v.is_encrypted is True
    assert v.breakable is False


def test_classify_encrypted_evidence_cites_chi_square_and_conclusion(encrypted_bytes):
    joined = " ".join(classify(encrypted_bytes).evidence)
    assert "未命中任何容器魔数" in joined
    assert str(int(CHI2_UNIFORM_MAX)) in joined
    assert "排除重复密钥 XOR" in joined
    assert "不可破" in joined


def test_classify_encrypted_records_period_scan_result(encrypted_bytes):
    v = classify(encrypted_bytes)
    assert v.periodic is False
    assert v.best_period is not None
    assert v.best_period_match > 0


def test_classify_periodic_payload_flagged_as_weak_encryption():
    """重复密钥 XOR 属于「可破解」的弱加密，必须与强加密区分开。"""
    v = classify(b"KEY" * 20_000)
    assert v.kind == "encrypted"
    assert v.periodic is True
    assert v.breakable is True


def test_classify_periodic_evidence_mentions_xor():
    joined = " ".join(classify(b"KEY" * 20_000).evidence)
    assert "重复密钥 XOR" in joined
    assert "可破解" in joined


def test_classify_non_uniform_unknown_stream_is_still_unbreakable():
    """分布不均匀但无周期、无容器头 → 未知裸流，不能声称可解。"""
    data = bytes([i % 200 for i in range(50_000)])
    v = classify(data)
    assert v.kind == "encrypted"
    assert v.breakable is False
    assert any("未知裸流" in e for e in v.evidence)


def test_classify_empty_input_does_not_crash():
    v = classify(b"")
    assert v.entropy == 0.0
    assert v.chi_square == 0.0
    assert v.kind == "encrypted"


# -- identify ---------------------------------------------------------------

def test_identify_reads_file_and_sets_path(fmp4_file):
    v = identify(str(fmp4_file))
    assert v.kind == "fmp4"
    assert v.path == str(fmp4_file)


def test_identify_encrypted_file(encrypted_file):
    v = identify(str(encrypted_file))
    assert v.is_encrypted is True
    assert v.breakable is False


def test_identify_missing_file_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        identify(str(tmp_path / "nope.m4a"))


# -- Verdict.render ---------------------------------------------------------

def test_verdict_is_encrypted_property():
    assert Verdict(kind="encrypted").is_encrypted is True
    assert Verdict(kind="fmp4").is_encrypted is False


def test_render_includes_core_fields(fmp4_bytes):
    text = classify(fmp4_bytes, path="a.mp3").render()
    assert "a.mp3" in text
    assert "判定    : fmp4（可解码）" in text
    assert "熵" in text
    assert "卡方" in text


def test_render_marks_unbreakable_as_not_decodable(encrypted_bytes):
    assert "（不可解码）" in classify(encrypted_bytes).render()


def test_render_shows_period_line_only_when_scanned(fmp4_bytes):
    """容器分支不做周期扫描，故不应出现周期性行。"""
    assert "周期性" not in classify(fmp4_bytes).render()


def test_render_shows_period_line_for_ciphertext(encrypted_bytes):
    text = classify(encrypted_bytes).render()
    assert "周期性" in text
    assert "随机基线" in text


def test_render_flags_weak_encryption_in_period_line():
    assert "检出周期，弱加密" in classify(b"KEY" * 20_000).render()


def test_entropy_of_uniform_sample_is_close_to_log2_256(encrypted_bytes):
    assert abs(entropy(encrypted_bytes) - math.log2(256)) < 0.01
