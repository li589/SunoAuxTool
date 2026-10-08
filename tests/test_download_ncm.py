"""NCM 解包组件测试（1.6.1）。

三层正确性保障：
1. AES-128 用 FIPS-197 附录 C.1 官方向量锁定（独立实现最易错处）；
2. RC4 keybox / 密钥流用自加密往返 + 公开规范的结构断言；
3. 端到端用「自打包 NCM」往返（pack helper 在本文件内实现，产品侧不提供
   加密能力），另用 examples/ncmdump/test/test.ncm 真实样本做开发期校验
   （该文件不入库、仅本地存在时启用断言）。
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from sunoauxtool.download.ncm import (
    CorruptNcmError,
    NotNcmError,
    build_key_box,
    parse,
    segment_key,
    suggested_filename,
    unpack_bytes,
    unpack_file,
)
from sunoauxtool.download.ncm.aes_ecb import (
    aes128_ecb_decrypt,
    aes128_ecb_encrypt,
    pkcs7_pad,
    pkcs7_unpad,
)

_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# AES-128：FIPS-197 官方测试向量
# ---------------------------------------------------------------------------


class TestAesEcb:
    def test_fips197_appendix_c1(self):
        """FIPS-197 C.1：key=000102...0f, pt=00112233...eeff → 69c4e0d8...。"""
        key = bytes(range(16))
        pt = bytes.fromhex("00112233445566778899aabbccddeeff")
        expected = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
        ct = aes128_ecb_encrypt(key, pt)
        assert ct == expected
        assert aes128_ecb_decrypt(key, ct) == pt

    def test_multiblock_roundtrip(self):
        key = b"SunoAuxToolTest!"  # 16 字节
        data = pkcs7_pad(b"hello ncm world" * 7)
        assert aes128_ecb_decrypt(key, aes128_ecb_encrypt(key, data)) == data

    def test_pkcs7_roundtrip_and_invalid(self):
        padded = pkcs7_pad(b"abc")
        assert len(padded) % 16 == 0
        assert pkcs7_unpad(padded) == b"abc"
        with pytest.raises(ValueError, match="填充非法"):
            pkcs7_unpad(b"x" * 16 + b"\x00" * 16)
        with pytest.raises(ValueError, match="填充非法"):
            pkcs7_unpad(b"x" * 15 + b"\x11")

    def test_invalid_key_length(self):
        with pytest.raises(ValueError, match="16 字节"):
            aes128_ecb_encrypt(b"short", b"\x00" * 16)

    def test_non_block_multiple_rejected(self):
        with pytest.raises(ValueError, match="16 字节倍数"):
            aes128_ecb_decrypt(b"\x00" * 16, b"\x00" * 15)


# ---------------------------------------------------------------------------
# RC4 定制流密码：keybox 结构 + 偏移无关性
# ---------------------------------------------------------------------------


class TestRc4Stream:
    def test_keybox_is_permutation(self):
        box = build_key_box(b"0123456789abcdef")
        assert sorted(box) == list(range(256))

    def test_segment_key_offset_deterministic(self):
        """同一偏移两次取值相同（流状态不推进——分块并行解密的依据）。"""
        box = build_key_box(b"keymaterial")
        assert segment_key(12345, box) == segment_key(12345, box)

    def test_payload_roundtrip(self):
        box = build_key_box(b"abc")
        payload = bytes(range(256)) * 3
        cipher = bytes(
            b ^ segment_key(i, box) for i, b in enumerate(payload)
        )
        restored = bytes(
            b ^ segment_key(i, box) for i, b in enumerate(cipher)
        )
        assert restored == payload


# ---------------------------------------------------------------------------
# 容器：自打包往返
# ---------------------------------------------------------------------------

_CORE_KEY = bytes.fromhex("687A4852416D736F356B496E62617857")
_META_KEY = bytes.fromhex("2333466C6A6B5F215C5D2630553C2728")


def _pack_ncm(rc4_key: bytes, meta: dict, image: bytes | None, payload: bytes) -> bytes:
    """测试专用 NCM 打包器（产品不提供加密能力；加密与解密共用同一实现）。"""
    from sunoauxtool.download.ncm.aes_ecb import aes128_ecb_encrypt, pkcs7_pad
    from sunoauxtool.download.ncm.stream import encrypt_payload

    key_cipher = bytes(
        b ^ 0x64
        for b in aes128_ecb_encrypt(_CORE_KEY, pkcs7_pad(b"neteasecloudmusic" + rc4_key))
    )
    meta_text = ("music:" + json.dumps(meta, ensure_ascii=False)).encode()
    meta_b64 = (
        b"163 key(Don't modify):"
        + base64_encode(aes128_ecb_encrypt(_META_KEY, pkcs7_pad(meta_text)))
    )
    meta_cipher = bytes(b ^ 0x63 for b in meta_b64)

    out = bytearray(b"CTENFDAM\x00\x00")
    out += struct.pack("<I", len(key_cipher)) + key_cipher
    out += struct.pack("<I", len(meta_cipher)) + meta_cipher
    out += struct.pack("<I", 0)  # crc32
    out += b"\x00" * 5
    out += struct.pack("<I", len(image)) + image if image else struct.pack("<I", 0)
    out += encrypt_payload(payload, rc4_key)
    return bytes(out)


def base64_encode(data: bytes) -> bytes:
    import base64

    return base64.standard_b64encode(data)


class TestContainerRoundtrip:
    def test_full_roundtrip(self, tmp_path):
        """自打包→解包：载荷逐字节还原，元数据与封面保留。"""
        rc4_key = bytes(range(1, 17))  # 16 字节密钥材料
        meta = {
            "musicName": "雨巷备忘录",
            "artist": [["齐见林", []]],
            "album": "全流程验证",
            "format": "flac",
        }
        image = b"\xff\xd8\xff\xe0fakejpeg"
        payload = b"fLaC" + bytes(range(256)) * 10

        container = _pack_ncm(rc4_key, meta, image, payload)
        content = parse(container)
        assert content.payload == payload
        assert content.fmt == "flac"
        assert content.music_name == "雨巷备忘录"
        assert content.artists == ["齐见林"]
        assert content.album == "全流程验证"
        assert content.image == image

        paths = unpack_bytes(container, tmp_path, write_cover=True)
        assert len(paths) == 2
        assert paths[0].name == "雨巷备忘录 - 齐见林.flac"
        assert paths[0].read_bytes() == payload
        assert paths[1].name == "雨巷备忘录 - 齐见林_cover.jpg"

    def test_no_image_no_cover(self, tmp_path):
        container = _pack_ncm(b"k" * 16, {"musicName": "x"}, None, b"ID3" + b"\x00" * 32)
        paths = unpack_bytes(container, tmp_path)
        assert len(paths) == 1 and paths[0].suffix == ".mp3"

    def test_stem_override(self, tmp_path):
        container = _pack_ncm(b"k" * 16, {}, None, b"fLaC" + b"\x00" * 16)
        paths = unpack_bytes(container, tmp_path, stem="custom")
        assert paths[0].name == "custom.flac"

    def test_suggested_filename_sanitized(self):
        from sunoauxtool.download.ncm import NcmContent

        content = NcmContent(payload=b"fLaC", fmt="flac", meta={"musicName": 'a/b:c*d?"<>|'})
        name = suggested_filename(content)
        assert "/" not in name and ":" not in name
        assert name.endswith(".flac")


class TestContainerErrors:
    def test_bad_magic(self):
        with pytest.raises(NotNcmError, match="CTENFDAM"):
            parse(b"NOTNCM!!" + b"\x00" * 32)

    def test_truncated_length_field(self):
        data = b"CTENFDAM\x00\x00\x01"  # 在 u32 长度字段处截断
        with pytest.raises(CorruptNcmError, match="截断"):
            parse(data)

    def test_wrong_core_key_prefix(self):
        """密钥段用错误 core key 加密 → 前缀校验失败（码 26）。"""
        from sunoauxtool.download.ncm.aes_ecb import aes128_ecb_encrypt, pkcs7_pad

        bad_cipher = bytes(
            b ^ 0x64 for b in aes128_ecb_encrypt(_CORE_KEY, pkcs7_pad(b"wrongprefix" + b"k" * 16))
        )
        data = bytearray(b"CTENFDAM\x00\x00")
        data += struct.pack("<I", len(bad_cipher)) + bad_cipher
        data += struct.pack("<I", 0) + struct.pack("<I", 0) + b"\x00" * 5
        data += struct.pack("<I", 0)
        with pytest.raises(CorruptNcmError, match="前缀"):
            parse(bytes(data))

    def test_empty_payload(self, tmp_path):
        container = _pack_ncm(b"k" * 16, {}, None, b"")
        with pytest.raises(CorruptNcmError, match="载荷"):
            parse(container)

    def test_unpack_existing_output(self, tmp_path):
        container = _pack_ncm(b"k" * 16, {"musicName": "dup"}, None, b"fLaC" + b"\x00" * 16)
        unpack_bytes(container, tmp_path)
        with pytest.raises(FileExistsError):
            unpack_bytes(container, tmp_path)


@pytest.mark.skipif(
    not (_ROOT / "examples/ncmdump/test/test.ncm").is_file(),
    reason="examples/ 真实样本仅本地存在（不入库），缺失时跳过",
)
class TestRealSample:
    """开发期校验：用 examples/ 的真实 .ncm 验证独立实现的互操作性。"""

    REAL = _ROOT / "examples/ncmdump/test/test.ncm"

    def test_real_file_unpacks_to_valid_audio(self, tmp_path):
        paths = unpack_file(self.REAL, tmp_path)
        audio = paths[0]
        assert audio.suffix in (".flac", ".mp3")
        assert audio.stat().st_size > 1024
        # 签名即格式判定：读头部魔数
        head = audio.read_bytes()[:4]
        assert head[:4] == b"fLaC" or head[:3] == b"ID3" or head[:2] == b"\xff\xfb"
