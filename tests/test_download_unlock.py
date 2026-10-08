"""unlock 通用解密组件测试（1.6.3）：kwm / kgm / vpr / qmc 系列。

验证策略：
1. 官方测试向量锁定（unlock-music Rust 单元测试同源）：
   V1 transform 及 0x7FFF 边界、key_compress、QMC2Map@32760、qmc2_hash、
   get_segment_key、改型 RC4、QMC2RC4 256 字节、tc_tea 加解密；
2. KGM 用注入伪公钥 + 测试内独立朴素实现对拍（公式级自洽）；
   真实向量对（ghtz08 测试文件 + 公钥）本地存在时实弹校验（skipif 守护）；
3. 自打包端到端：kwm / qmc（V1 静态 + V2 EKey footer）往返逐字节还原；
4. CLI：unlock 命令 + batch --include-unlock + 聚合镜像。
"""

from __future__ import annotations

import base64
import struct
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sunoauxtool.download.unlock import (
    DecryptFailedError,
    KeyMissingError,
    UnknownFormatError,
    detect_format,
    unlock_bytes,
    unlock_file,
)
from sunoauxtool.download.unlock import kgm, kwm, qmc
from sunoauxtool.download.unlock._xutil import sniff_audio_ext, xor_repeating

_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


class TestXutil:
    def test_xor_repeating_phase(self):
        assert xor_repeating(b"\x00" * 4, b"\x01\x02") == b"\x01\x02\x01\x02"
        assert xor_repeating(b"\x00" * 4, b"\x01\x02", key_offset=1) == b"\x02\x01\x02\x01"

    def test_sniff_formats(self):
        assert sniff_audio_ext(b"fLaC" + b"\x00" * 32) == "flac"
        assert sniff_audio_ext(b"OggS" + b"\x00" * 32) == "ogg"
        id3 = b"ID3\x04\x00\x00\x00\x00\x00\x0b" + b"\x00" * 11 + b"fLaC"
        assert sniff_audio_ext(id3) == "flac"
        assert sniff_audio_ext(b"\xff\xfb\x90\x00" + b"\x00" * 32) == "mp3"
        assert sniff_audio_ext(bytes(range(256)) * 2) == "bin"


# ---------------------------------------------------------------------------
# KWM（酷我）
# ---------------------------------------------------------------------------


def _pack_kwm(seed: int, audio: bytes, magic: bytes = kwm.KWM_MAGIC_1) -> bytes:
    """测试打包器：用解密自逆特性实现加密。"""
    key = kwm.build_key_stream(seed)
    enc = xor_repeating(audio, key)
    header = bytearray(kwm._HEADER_LEN)
    header[:16] = magic
    header[24:32] = seed.to_bytes(8, "little")
    return bytes(header) + enc


class TestKwm:
    def test_key_stream_known_value(self):
        """seed 十进制串 8 位 < 32：padEnd(32, s) 语义 = 自身循环填充。"""
        key = kwm.build_key_stream(12345678)
        text = "12345678" * 4  # 循环填充到 32 位
        expected = bytes(
            ord(a) ^ ord(b) for a, b in zip(kwm._KEY_SEED_STRING, text)
        )
        assert key == expected
        assert len(key) == 32

    def test_roundtrip(self, tmp_path):
        audio = b"fLaC" + bytes(range(256)) * 8
        container = _pack_kwm(0xDEADBEEF, audio)
        assert kwm.is_kwm(container)
        out, ext = unlock_bytes(container, "kwm")
        assert out == audio
        assert ext == "flac"

    def test_tme_magic(self):
        audio = b"\xff\xfb" + bytes(64)
        container = _pack_kwm(42, audio, magic=kwm.KWM_MAGIC_2)
        out, ext = unlock_bytes(container, "kwm")
        assert out == audio and ext == "mp3"

    def test_bad_magic(self):
        with pytest.raises(UnknownFormatError):
            unlock_bytes(b"\x00" * 1100, "kwm")

    def test_cli_unlock(self, tmp_path):
        from sunoauxtool.download.cli import app

        audio = b"fLaC" + bytes(64)
        src = tmp_path / "song.kwm"
        src.write_bytes(_pack_kwm(7, audio))
        runner = CliRunner()
        result = runner.invoke(app, ["unlock", str(src), "-o", str(tmp_path / "out")])
        assert result.exit_code == 0, result.output
        out_file = tmp_path / "out" / "song.flac"
        assert out_file.read_bytes() == audio


# ---------------------------------------------------------------------------
# KGM / VPR（酷狗）
# ---------------------------------------------------------------------------

_NAIVE_FOLD = lambda v: v ^ ((v & 0x0F) << 4)  # noqa: E731


def _naive_kgm_decrypt(data: bytes, pub_key: bytes, vpr: bool) -> bytes:
    """测试内独立朴素实现（逐字节直算），与产品实现互为对拍。"""
    own = bytes(data[0x1C:0x2C]) + b"\x00"
    audio = bytes(data[1024:])
    out = bytearray()
    for i, b in enumerate(audio):
        t1 = _NAIVE_FOLD(own[i % 17] ^ b)
        t2 = _NAIVE_FOLD(kgm.PUB_KEY_MEND[i % 272] ^ pub_key[i // 16])
        v = t1 ^ t2
        if vpr:
            v ^= kgm.VPR_MASK_DIFF[i % 17]
        out.append(v)
    return bytes(out)


class TestKgm:
    def test_formula_vs_naive(self):
        """伪公钥下产品实现与朴素逐字节实现对拍一致（KGM + VPR）。"""
        data = bytearray(kgm.KGM_MAGIC + b"\x00" * 1008)
        data[0x10:0x14] = struct.pack("<I", 1024)
        data[0x1C:0x2C] = bytes(range(16))
        audio = b"fLaC" + bytes(range(256)) * 4
        fake_pub = bytes((i * 7 + 3) & 0xFF for i in range(128))
        container = bytes(data) + xor_repeating(
            audio, b"\x00" * 17
        )  # 载荷本身任意，解密公式与输入分布无关
        assert kgm.decrypt(container, pub_key=fake_pub) == _naive_kgm_decrypt(
            container, fake_pub, vpr=False
        )
        vpr_data = bytearray(kgm.VPR_MAGIC + bytes(data[16:]))
        assert kgm.decrypt(bytes(vpr_data), pub_key=fake_pub) == _naive_kgm_decrypt(
            bytes(vpr_data), fake_pub, vpr=True
        )

    def test_bad_magic(self):
        with pytest.raises(UnknownFormatError):
            kgm.decrypt(b"\x00" * 2000, pub_key=b"\x00" * 64)

    def test_key_missing(self, monkeypatch):
        monkeypatch.setattr(kgm, "locate_pub_key", lambda: None)
        data = kgm.KGM_MAGIC + b"\x00" * 2000
        with pytest.raises(KeyMissingError):
            kgm.decrypt(data)

    def test_key_length_mismatch(self, tmp_path, monkeypatch):
        bad = tmp_path / "kugou_key.bin"
        bad.write_bytes(b"\x00" * 100)
        monkeypatch.setattr(kgm, "locate_pub_key", lambda: bad)
        data = kgm.KGM_MAGIC + b"\x00" * 2000
        with pytest.raises(KeyMissingError, match="长度不符"):
            kgm.decrypt(data)

    def test_locate_env(self, tmp_path, monkeypatch):
        keyfile = tmp_path / "k.xz"
        keyfile.write_bytes(b"\xfd7zXZ\x00stub")
        monkeypatch.setenv("SUNO_KGM_KEY", str(keyfile))
        assert kgm.locate_pub_key() == keyfile

    def test_live_fire_real_vectors(self):
        """实弹校验：ghtz08 官方加密/解密对 + 真实公钥（仅本地存在时）。"""
        enc_p = _ROOT / "output/ref/test_kugou_kgm.dat"
        right_p = _ROOT / "output/ref/test_kugou_kgm_right.dat"
        key_p = _ROOT / "output/ref/kugou_key.xz"
        if not (enc_p.is_file() and right_p.is_file() and key_p.is_file()):
            pytest.skip("本地无 ghtz08 测试向量/公钥（output/ref/）")
        import lzma

        out = kgm.decrypt(enc_p.read_bytes(), pub_key=lzma.open(key_p).read())
        assert out == right_p.read_bytes()


# ---------------------------------------------------------------------------
# QMC V1 静态密钥（官方向量）
# ---------------------------------------------------------------------------

_GEN_KEY = bytes(range(1, 129))


class TestQmcV1:
    def test_official_start_vector(self):
        d = bytes(qmc.v1_transform(_GEN_KEY, b, i) for i, b in enumerate(b"igohj&pg{fo"))
        assert d == b"hello world"

    def test_official_boundary_vector(self):
        d2 = bytearray([0x13, 0x19, 0x11, 0x12, 0x10, 0xA0, 0x75, 0x6C, 0x76, 0x69, 0x62])
        for i in range(len(d2)):
            d2[i] = qmc.v1_transform(_GEN_KEY, d2[i], 0x7FFA + i)
        assert bytes(d2) == b"hello world"

    def test_official_whole_file_vector(self):
        v1 = qmc.v1_decrypt(
            bytes([0xAB, 0x2F, 0xBA, 0xA6, 0xFF, 0x47, 0x80, 0x3D, 0xAA, 0xCD, 0x02])
        )
        assert v1 == b"hello world"

    def test_involution(self):
        data = bytes(range(256)) * 3
        assert qmc.v1_decrypt(qmc.v1_decrypt(data)) == data

    def test_tkm_end_to_end(self, tmp_path):
        audio = b"ID3\x04\x00\x00\x00\x00\x00\x0b" + b"\x00" * 11 + b"\xff\xfb" + bytes(96)
        src = tmp_path / "cache.tkm"
        src.write_bytes(qmc.v1_decrypt(audio))  # 自逆 → 加密
        out, ext = unlock_bytes(src.read_bytes(), "tkm")
        assert out == audio and ext == "mp3"


# ---------------------------------------------------------------------------
# tc_tea + EKey
# ---------------------------------------------------------------------------

_TEA_KEY = bytes(b"12345678ABCDEFGH")
_TEA_CT = bytes([
    0x91, 0x09, 0x51, 0x62, 0xE3, 0xF5, 0xB6, 0xDC,
    0x6B, 0x41, 0x4B, 0x50, 0xD1, 0xA5, 0xB8, 0x4E,
    0xC5, 0x0D, 0x0C, 0x1B, 0x11, 0x96, 0xFD, 0x3C,
])
_TEA_SALT = bytes([0xA5, 0x6E, 0x35, 0xBC, 0x7C, 0x31, 0x04, 0x55, 0xA0, 0xBF])


class TestTea:
    def test_official_decrypt_vector(self):
        assert qmc.tea_cbc_decrypt(_TEA_CT, _TEA_KEY) == bytes([1, 2, 3, 4, 5, 6, 7, 8])

    def test_encrypt_roundtrip(self):
        enc = qmc.tea_cbc_encrypt(
            b"this is a test message.", b"43218765dcbahgfe", _TEA_SALT
        )
        assert qmc.tea_cbc_decrypt(enc, b"43218765dcbahgfe") == b"this is a test message."

    def test_invalid_length(self):
        with pytest.raises(DecryptFailedError):
            qmc.tea_cbc_decrypt(b"\x00" * 7, _TEA_KEY)


def _make_v1_ekey(master_key: bytes, header: bytes = b"12345678") -> str:
    """构造 V1 EKey（tea_key = interleave(simple_key, header)）。"""
    tea_key = bytearray()
    for sk, hk in zip(qmc.make_simple_key(), header):
        tea_key.append(sk)
        tea_key.append(hk)
    cipher = qmc.tea_cbc_encrypt(master_key, bytes(tea_key), _TEA_SALT)
    return base64.standard_b64encode(header + cipher).decode("latin-1")


class TestEkey:
    def test_simple_key_shape(self):
        sk = qmc.make_simple_key()
        assert len(sk) == 8 and all(0 <= v <= 255 for v in sk)

    def test_v1_roundtrip(self):
        master = bytes(range(24))
        ekey = _make_v1_ekey(master)
        assert qmc.ekey_decrypt(ekey.encode("latin-1"))[8:] == master

    def test_v2_roundtrip(self):
        master = bytes(range(16))
        inner = _make_v1_ekey(master).encode("latin-1")
        # V2 构造：enc(enc(inner, K2), K1) 的 base64，前缀为 EncV2 标记
        p1 = qmc.tea_cbc_encrypt(inner, qmc.EKEY_V2_KEY2, _TEA_SALT)
        p2 = qmc.tea_cbc_encrypt(p1, qmc.EKEY_V2_KEY1, _TEA_SALT)
        ekey = (qmc.EKEY_V2_PREFIX + base64.standard_b64encode(p2)).decode("latin-1")
        assert qmc.ekey_decrypt(ekey.encode("latin-1"))[8:] == master


# ---------------------------------------------------------------------------
# QMC V2 Map / RC4（官方向量）
# ---------------------------------------------------------------------------

_TEST_LONG_KEY = ((b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" * 6) * 10)[:325]
_KEY_COMPRESS_EXPECTED = bytes([
    0x79, 0xF4, 0x00, 0x75, 0x9E, 0x36, 0x00, 0x14, 0x8A, 0x63, 0x00, 0xB4, 0xBE, 0x77,
    0x00, 0x17, 0xBA, 0x00, 0x37, 0x00, 0x00, 0x00, 0xBF, 0x80, 0x41, 0xBF, 0x83, 0xDD,
    0xBC, 0x5C, 0x02, 0x43, 0x14, 0x82, 0x49, 0x02, 0x00, 0x55, 0xBE, 0x6D, 0xBF, 0x49,
    0x80, 0x8E, 0x43, 0x00, 0xFA, 0x41, 0x67, 0xA8, 0x17, 0xF4, 0xAE, 0x16, 0x15, 0x00,
    0xC1, 0x37, 0x82, 0xDD, 0x36, 0x21, 0x38, 0x55, 0x00, 0x79, 0x41, 0x9E, 0x42, 0xC1,
    0x36, 0xFA, 0xCF, 0x35, 0x00, 0x00, 0x41, 0xDD, 0x43, 0x42, 0x17, 0x4D, 0x8E, 0x8A,
    0xDD, 0x00, 0xBE, 0xF5, 0x38, 0xB4, 0xBF, 0x00, 0x7A, 0xCC, 0x4D, 0x02, 0x00, 0xCF,
    0xC1, 0xC1, 0x02, 0xA8, 0x00, 0x16, 0xC1, 0xBF, 0xC2, 0x42, 0x00, 0x49, 0x00, 0xC1,
    0xC2, 0xF5, 0x00, 0x17, 0x41, 0xDC, 0x83, 0xC2, 0x00, 0x9E, 0x41, 0xC1, 0x71, 0x36,
    0x00, 0x80,
])
_MAP_CT = bytes([
    0x00, 0x9E, 0x41, 0xC1, 0x71, 0x36, 0x00, 0x80, 0xF4, 0x00, 0x75, 0x9E, 0x36, 0x00,
    0x14, 0x8A,
])
_RC4_LONG_KEY = ((b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" * 9) * 10)[:512]


class TestQmcV2:
    def test_key_compress_vector(self):
        assert qmc.key_compress(_TEST_LONG_KEY) == _KEY_COMPRESS_EXPECTED

    def test_map_official_vector(self):
        cipher = qmc.QMC2Map(_TEST_LONG_KEY)
        assert cipher.decrypt(_MAP_CT, 32760) == b"\x00" * 16

    def test_map_involution_random_offsets(self):
        cipher = qmc.QMC2Map(b"k" * 24)
        data = bytes(range(256)) * 5
        for off in (0, 1, 127, 0x7FFF, 0x8000, 0x8001, 0x10000):
            assert cipher.decrypt(cipher.decrypt(data, off), off) == data

    def test_hash_vector(self):
        assert qmc.qmc2_hash(b"hello world") == 4045008896.0

    def test_segment_key_vectors(self):
        assert qmc.get_segment_key(1, 0, 12345.0) == 0
        assert qmc.get_segment_key(1, 123, 12345.0) == 5018
        assert qmc.get_segment_key(51, 35, 516402887.0) == 28373784
        assert qmc.get_segment_key(0, 66, 3908240000.0) == 5921575757

    def test_rc4_derive_vector(self):
        rc4 = qmc._ModifiedRC4(b"this is a test key")
        out = bytes(v ^ rc4.generate() for v in b"hello world")
        assert out == bytes([0x68, 0x75, 0x6B, 0x64, 0x64, 0x24, 0x7F, 0x60, 0x7C, 0x7D, 0x60])

    def test_rc4_256_zero_vector(self):
        """官方 QMC2RC4 测试：解密 256 字节密文得全零（覆盖首段+其他段）。"""
        ct = bytes([
            0x39, 0x5A, 0x4F, 0x75, 0x38, 0x71, 0x37, 0x6B, 0x36, 0x51, 0x53, 0x6D, 0x7A, 0x66,
            0x53, 0x4B, 0x66, 0x50, 0x69, 0x34, 0x67, 0x6C, 0x33, 0x7A, 0x55, 0x62, 0x35, 0x5A,
            0x32, 0x75, 0x4F, 0x68, 0x44, 0x52, 0x6D, 0x65, 0x75, 0x6E, 0x39, 0x52, 0x30, 0x7A,
            0x68, 0x62, 0x73, 0x59, 0x39, 0x48, 0x55, 0x57, 0x73, 0x32, 0x5A, 0x70, 0x64, 0x50,
            0x4E, 0x52, 0x6A, 0x63, 0x4D, 0x39, 0x37, 0x76, 0x72, 0x47, 0x64, 0x4D, 0x62, 0x6D,
            0x58, 0x68, 0x75, 0x47, 0x37, 0x56, 0x69, 0x6B, 0x4A, 0x79, 0x66, 0x63, 0x70, 0x39,
            0x59, 0x34, 0x43, 0x6B, 0x45, 0x32, 0x5A, 0x31, 0x38, 0x77, 0x70, 0x43, 0x51, 0x79,
            0x6A, 0x62, 0x32, 0x33, 0x65, 0x58, 0x4A, 0x4D, 0x33, 0x4E, 0x70, 0x62, 0x62, 0x67,
            0x4C, 0x54, 0x78, 0x64, 0x64, 0x77, 0x6E, 0x72, 0x37, 0x41, 0x54, 0x39, 0x42, 0x52,
            0x47, 0x32, 0x1A, 0xE4, 0x1B, 0x71, 0x68, 0x29, 0xB3, 0x6E, 0xAD, 0xC5, 0x28, 0x12,
            0xD6, 0xA4, 0x4B, 0x06, 0x7A, 0xDC, 0x90, 0x15, 0x99, 0xD6, 0xBF, 0x72, 0xA2, 0x30,
            0x37, 0x6B, 0x5C, 0xD6, 0x2F, 0x35, 0x14, 0x8A, 0xD6, 0xFB, 0x9F, 0xEE, 0x7D, 0x2D,
            0xB7, 0x37, 0xF2, 0x0B, 0x6E, 0x00, 0xFB, 0xA0, 0x3C, 0x40, 0xF3, 0x36, 0xB2, 0x76,
            0x20, 0x0F, 0x9E, 0xA5, 0xA3, 0x15, 0x60, 0x23, 0x15, 0x29, 0xA1, 0x91, 0xBF, 0xFB,
            0x12, 0x95, 0xAA, 0x8D, 0x92, 0xC6, 0x0B, 0x8D, 0x49, 0x99, 0xA5, 0xE0, 0x05, 0xCF,
            0xB6, 0xAC, 0x07, 0x54, 0x58, 0x28, 0xF9, 0x96, 0xD1, 0x9A, 0xFE, 0x0B, 0x3C, 0xFB,
            0x0B, 0x25, 0x7A, 0x43, 0x5A, 0x33, 0xC3, 0x7A, 0xFC, 0x33, 0xA3, 0xC2, 0x65, 0x48,
            0x29, 0x8D, 0x2C, 0x8F, 0x4E, 0x88, 0xFD, 0x44, 0xFD, 0xD5, 0xCA, 0xB9, 0x8D, 0x62,
            0x4A, 0x48, 0x20, 0x1D,
        ])
        cipher = qmc.QMC2RC4(_RC4_LONG_KEY)
        assert cipher.decrypt(ct, 0) == b"\x00" * 256

    def test_rc4_roundtrip_offsets(self):
        cipher = qmc.QMC2RC4(_RC4_LONG_KEY)
        data = bytes(range(256)) * 9
        for off in (0, 0x40, 0x80, 0x1400, 0x1401, 0x2800):
            assert cipher.decrypt(cipher.decrypt(data, off), off) == data


# ---------------------------------------------------------------------------
# Footer 解析 + QMC 端到端
# ---------------------------------------------------------------------------


class TestFooter:
    def test_pcv1_legacy(self):
        ekey = "YWJjZGVmZ2hpamtsbW5v"  # base64 文本
        tail = ekey.encode() + struct.pack("<I", len(ekey))
        footer = qmc.parse_footer(tail)
        assert footer is not None and footer.ftype == "PcV1Legacy"
        assert footer.ekey == ekey
        assert footer.size == len(ekey) + 4

    def test_qtag(self):
        ekey = "QUJDREVGRw=="
        csv = "%s,12345,2" % ekey
        payload = struct.pack(">I", len(csv)) + csv.encode()
        tail = payload + struct.pack(">I", len(csv)) + b"QTag"
        footer = qmc.parse_footer(tail)
        assert footer is not None and footer.ftype == "QTag"
        assert footer.ekey == ekey and footer.extra["resource_id"] == "12345"

    def test_stag(self):
        csv = "99999,2,AIM0001"
        payload = struct.pack(">I", len(csv)) + csv.encode()
        tail = payload + struct.pack(">I", len(csv)) + b"STag"
        footer = qmc.parse_footer(tail)
        assert footer is not None and footer.ftype == "STag"
        assert footer.ekey is None and footer.extra["media_mid"] == "AIM0001"

    def test_garbage_returns_none(self):
        assert qmc.parse_footer(b"\x00" * 64) is None
        assert qmc.parse_footer(b"ab") is None


class TestQmcEndToEnd:
    def test_qmcflac_v2_map(self, tmp_path):
        """自构造 PcV1Legacy footer + Map 密码端到端。"""
        audio = b"fLaC" + bytes(range(256)) * 10
        master = bytes(range(24))
        ekey = _make_v1_ekey(master)
        master_full = qmc.ekey_decrypt(ekey.encode("latin-1"))  # 官方语义：header+plain 即主密钥
        footer = ekey.encode() + struct.pack("<I", len(ekey))  # 长度在末尾（u32le）
        cipher = qmc.make_qmc2_cipher(master_full)
        enc_audio = cipher.decrypt(audio, 0)  # Map 自逆 → 加密
        src = tmp_path / "song.qmcflac"
        src.write_bytes(enc_audio + footer)
        out, ext = unlock_bytes(src.read_bytes(), "qmcflac")
        assert out == audio and ext == "flac"

    def test_qmc3_v1_static(self, tmp_path):
        audio = b"\xff\xfb" + bytes(200)
        src = tmp_path / "song.qmc3"
        src.write_bytes(qmc.v1_decrypt(audio))
        out, ext = unlock_bytes(src.read_bytes(), "qmc3")
        assert out == audio and ext == "mp3"

    def test_unrecognized_raises(self):
        with pytest.raises(DecryptFailedError):
            qmc.decrypt_qmc(bytes(range(256)) * 4, ext_hint="qmc3")

    def test_stag_needs_online_key(self):
        csv = "1,2,MID"
        payload = struct.pack(">I", len(csv)) + csv.encode()
        tail = payload + struct.pack(">I", len(csv)) + b"STag"
        with pytest.raises(KeyMissingError):
            qmc.decrypt_qmc(b"fLaC" + bytes(128) + tail, ext_hint="qmcflac")


# ---------------------------------------------------------------------------
# 分发 + CLI + 批量
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_detect_by_ext(self):
        assert detect_format("ncm", b"") == "ncm"
        assert detect_format("qmcflac", b"") == "qmc"
        assert detect_format("mflac", b"") == "qmc"
        assert detect_format("kgm", kgm.KGM_MAGIC + b"\x00" * 1100) == "kgm"
        assert detect_format("vpr", kgm.VPR_MAGIC + b"\x00" * 1100) == "vpr"

    def test_detect_by_magic_fallback(self):
        audio = b"fLaC" + bytes(128)
        assert detect_format("", _pack_kwm(1, audio)) == "kwm"
        assert detect_format("unknown", b"\x00" * 64) is None

    def test_unknown_format_error_code(self):
        with pytest.raises(UnknownFormatError) as exc_info:
            unlock_bytes(b"\x00" * 64, "xyz")
        assert exc_info.value.code == 27


class TestCliBatch:
    def test_batch_include_unlock(self, tmp_path):
        from sunoauxtool.download.cli import app

        src_dir = tmp_path / "in"
        src_dir.mkdir()
        audio = b"fLaC" + bytes(64)
        (src_dir / "a.kwm").write_bytes(_pack_kwm(3, audio))
        (src_dir / "b.txt").write_text("keep me out")
        out_dir = tmp_path / "out"
        runner = CliRunner()
        result = runner.invoke(
            app, ["batch", str(src_dir), "-o", str(out_dir), "--include-unlock"]
        )
        assert result.exit_code == 0, result.output
        assert "通用解密成功 1 个文件" in result.output
        assert (out_dir / "a.flac").read_bytes() == audio

    def test_batch_unlock_error_collected(self, tmp_path):
        from sunoauxtool.download.cli import app

        src_dir = tmp_path / "in"
        src_dir.mkdir()
        (src_dir / "bad.qmcflac").write_bytes(b"\x00" * 64)
        runner = CliRunner()
        result = runner.invoke(
            app, ["batch", str(src_dir), "-o", str(tmp_path / "out"), "--include-unlock"]
        )
        assert result.exit_code == 0
        assert "失败 1 个" in result.output

    def test_aggregate_mirror(self):
        """聚合入口 post unlock 已注册（薄转发）。"""
        from sunoauxtool.aggregate import post_app

        names = [c.name for c in post_app.registered_commands]
        assert "unlock" in names


# ---------------------------------------------------------------------------
# unlock_file 行为
# ---------------------------------------------------------------------------


class TestUnlockFile:
    def test_seq_no_overwrite(self, tmp_path):
        audio = b"fLaC" + bytes(64)
        src = tmp_path / "s.kwm"
        src.write_bytes(_pack_kwm(5, audio))
        out1 = unlock_file(src, tmp_path / "out")
        out2 = unlock_file(src, tmp_path / "out")
        assert out1.name == "s.flac" and out2.name == "s (1).flac"
        out3 = unlock_file(src, tmp_path / "out", overwrite=True)
        assert out3.name == "s.flac"

    def test_corrupt_result_raises(self, tmp_path):
        # QMC v1 静态解密后不是音频 → DecryptFailedError(28)
        src = tmp_path / "x.qmc3"
        src.write_bytes(bytes(range(256)) * 2)
        with pytest.raises(DecryptFailedError) as exc_info:
            unlock_file(src, tmp_path / "out")
        assert exc_info.value.code == 28
