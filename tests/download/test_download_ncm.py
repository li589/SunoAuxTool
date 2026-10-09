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
import subprocess
import struct
from pathlib import Path

import pytest

from sunoauxtool.download.transcoder import find_ffmpeg
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

_ROOT = Path(__file__).resolve().parents[2]

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

# 密钥常量直接从产品模块导入（单一事实来源，防止 pack/unpack 密钥漂移）
from sunoauxtool.download.ncm.unpack import _CORE_KEY, _META_KEY  # noqa: E402


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


class TestMetaKey:
    def test_meta_key_matches_reference(self):
        """锁定 _META_KEY 字节：对齐 ncmdump 参考实现 sModifyKey（ncmcrypt.cpp）。

        曾因字节 3-4 抄错（3346 ≠ 3134）导致真实 NCM 元数据解密报
        「PKCS#7: 填充非法」、文件名退化为 untitled。此测试防止回归。
        """
        from sunoauxtool.download.ncm.unpack import _CORE_KEY, _META_KEY

        assert _META_KEY == bytes.fromhex("2331346C6A6B5F215C5D2630553C2728")
        # 核心密钥一并锁定：对应参考实现 sCoreKey
        assert _CORE_KEY == bytes.fromhex("687A4852416D736F356B496E62617857")

    def test_real_style_meta_decrypts(self):
        """用参考密钥加密的元数据段必须能被 parse 解出（加密端同用 _META_KEY，
        故此测试验证 roundtrip 一致性；密钥错位时解密在 pkcs7_unpad 处爆炸）。"""
        rc4_key = bytes(range(1, 17))
        meta = {"musicName": "真实验证", "format": "mp3"}
        container = _pack_ncm(rc4_key, meta, None, b"ID3" + b"\x00" * 16)
        content = parse(container)
        assert content.music_name == "真实验证"


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


# ---------------------------------------------------------------------------
# 1.6.2：batch 混合扫描 + 标签/封面嵌入
# ---------------------------------------------------------------------------


class TestBatchIncludeNcm:
    def test_batch_mixed_directory(self, tmp_path, capsys):
        """目录里同时有 fMP4 与 .ncm：--include-ncm 时两类都处理。"""
        from typer.testing import CliRunner
        from sunoauxtool.download.cli import app

        # 布置：1 个 .ncm（自打包）+ 1 个无关文件
        container = _pack_ncm(b"k" * 16, {"musicName": "batchtest"}, None, b"fLaC" + b"\x00" * 64)
        (tmp_path / "song.ncm").write_bytes(container)
        (tmp_path / "readme.txt").write_text("x")
        outdir = tmp_path / "out"

        runner = CliRunner()
        result = runner.invoke(app, ["batch", str(tmp_path), "-o", str(outdir), "--include-ncm"])
        assert result.exit_code == 0, result.output
        assert "NCM 解包成功 1" in result.output
        assert (outdir / "batchtest.flac").is_file()

    def test_batch_without_flag_ignores_ncm(self, tmp_path):
        from typer.testing import CliRunner
        from sunoauxtool.download.cli import app

        container = _pack_ncm(b"k" * 16, {"musicName": "ignored"}, None, b"fLaC" + b"\x00" * 16)
        (tmp_path / "song.ncm").write_bytes(container)
        outdir = tmp_path / "out"

        runner = CliRunner()
        result = runner.invoke(app, ["batch", str(tmp_path), "-o", str(outdir)])
        assert result.exit_code == 0
        assert not (outdir / "ignored.flac").exists()


class TestEmbedTags:
    def test_embed_metadata_flac(self, tmp_path):
        pytest.importorskip("sunoauxtool.download.transcoder")
        from sunoauxtool.download.ncm.meta import embed_metadata

        # 嵌入需真实可读音频：ffmpeg 合成 1s 正弦真 FLAC 作为载荷
        import subprocess as _sp

        real = tmp_path / "_real.flac"
        _sp.run(
            [str(Path(find_ffmpeg())), "-hide_banner", "-y", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=1", str(real)],
            check=True, capture_output=True,
        )
        real_payload = real.read_bytes()
        container = _pack_ncm(
            b"k" * 16,
            {"musicName": "标签曲", "artist": [["甲", []], ["乙", []]], "album": "专辑X"},
            None, real_payload,
        )
        content = parse(container)
        paths = unpack_bytes(container, tmp_path, write_cover=False)
        audio = paths[0]
        assert audio.read_bytes() == real_payload
        out = embed_metadata(audio, content)
        assert out == audio
        # ffprobe 验证标签写入
        probe = Path(str(find_ffmpeg())).with_name("ffprobe.exe")
        if not probe.is_file():
            probe = Path(str(find_ffmpeg()).replace("ffmpeg", "ffprobe"))
        res = subprocess.run(
            [str(probe), "-v", "error", "-show_entries", "format_tags", "-of", "json", str(audio)],
            capture_output=True, text=True,
        )
        tags = json.loads(res.stdout or "{}").get("format", {}).get("tags", {})
        assert tags.get("title") == "标签曲"
        assert tags.get("album") == "专辑X"
        assert "甲" in tags.get("artist", "") and "乙" in tags.get("artist", "")
