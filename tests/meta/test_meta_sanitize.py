"""meta 清理子包测试（R9）：WAV chunk 重写器 + mutagen 容器 + MOTW + CLI。

WAV 用例手搓 RIFF chunk（复刻 Suno 官方 WAV 结构：fmt + LIST/INFO[ICMT/ISFT]
+ data + C2PA）；MP3/FLAC/OGG 由 libsndfile 合成，M4A 由 ffmpeg 合成（缺失则跳过）。
"""

from __future__ import annotations

import os
import re
import struct
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sunoauxtool.aggregate import app
from sunoauxtool.exceptions import ParameterError
from sunoauxtool.meta import (
    MODES,
    SanitizeSpec,
    sanitize_file,
    strip_motw,
)
from sunoauxtool.meta.exceptions import UnsupportedContainerError
from sunoauxtool.meta.sanitize import _ape_tag_len

runner = CliRunner()

# Linux CI 下 typer 的 OptionHighlighter 给选项名上色时会把 `--mode` 拆成
# `-` + `-mode` 两段（中间插 ANSI 码），直接子串匹配必败——先剥再断言
# （与 tests/core/test_aggregate_cli.py 同模式）。
_ANSI_RE = re.compile(chr(27) + r"\[[0-9;]*m")


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _chunk(cid: bytes, body: bytes) -> bytes:
    out = cid + struct.pack("<I", len(body)) + body
    if len(body) & 1:
        out += b"\x00"
    return out


def _info_sub(cid: bytes, value: bytes) -> bytes:
    val = value + b"\x00"
    return cid + struct.pack("<I", len(val)) + val + (b"\x00" if len(val) & 1 else b"")


def make_suno_like_wav(path: Path, *, with_c2pa: bool = True, audio_bytes: int = 4096) -> Path:
    """复刻 Suno 官方下载 WAV 的容器结构（取证结论见 output/ref 报告 3.7 节）。"""
    fmt = _chunk(b"fmt ", struct.pack("<HHIIHH", 1, 2, 48000, 48000 * 4, 4, 16))
    info = b"INFO"
    info += _info_sub(b"ICMT", b"made with suno; created=2026-10-20T09:33:49Z; id=90c5e692-29ab")
    info += _info_sub(b"ISFT", b"Lavf60.16.100\x00")
    info += _info_sub(b"INAM", b"Life")
    info += _info_sub(b"IART", b"Suno AI")
    data = _chunk(b"data", b"\x00" * audio_bytes)
    body = b"WAVE" + fmt + _chunk(b"LIST", info) + data
    if with_c2pa:
        body += _chunk(b"C2PA", b"\x00\x00\x00\x00jumb\x00\x00\x00\x1e" + b"x" * 60)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return path


def make_plain_wav(path: Path, audio_bytes: int = 1024) -> Path:
    fmt = _chunk(b"fmt ", struct.pack("<HHIIHH", 1, 2, 48000, 48000 * 4, 4, 16))
    body = b"WAVE" + fmt + _chunk(b"data", b"\x00" * audio_bytes)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return path


def make_tagged_mp3(path: Path) -> Path:
    """ID3v2（含 TXXX 溯源帧 + 封面）+ 尾部 ID3v1 + APEv2 的 MP3。"""
    import numpy as np
    import soundfile as sf

    x = (0.1 * np.sin(2 * np.pi * 440 * np.arange(8000) / 8000)).astype("float32")
    sf.write(str(path), x, 8000, format="MP3")
    from mutagen.id3 import APIC, COMM, ID3, TALB, TCON, TIT2, TPE1, TXXX, USLT

    t = ID3()
    t.add(TIT2(encoding=3, text="SunoSong"))
    t.add(TPE1(encoding=3, text="Suno AI"))
    t.add(TALB(encoding=3, text="Alb"))
    t.add(TCON(encoding=3, text="Pop"))
    t.add(USLT(encoding=3, lang="eng", desc="", text="lyrics here"))
    t.add(COMM(encoding=3, lang="eng", desc="", text="my own comment"))
    t.add(APIC(encoding=3, mime="image/png", type=3, desc="cover", data=b"\x89PNG\r\n\x1a\n" + b"\x00" * 64))
    t.add(TXXX(encoding=3, desc="SUNO_CLIP_ID", text=["abc-123"]))
    t.save(str(path))
    raw = bytearray(path.read_bytes())
    item = (6).to_bytes(4, "little") + (0).to_bytes(4, "little") + b"TITLE" + b"\x00" + b"apev" + b"\x00" * 2
    ape = (
        b"APETAGEX"
        + (2000).to_bytes(4, "little")
        + len(item).to_bytes(4, "little")
        + (1).to_bytes(4, "little")
        + (0).to_bytes(4, "little")
        + b"\x00" * 8
    )
    v1 = (
        b"TAG"
        + b"sunotitle".ljust(30, b"\x00")
        + b"sunoartist".ljust(30, b"\x00")
        + b"alb".ljust(30, b"\x00")
        + b"2026"
        + b"cmt".ljust(30, b"\x00")
        + b"\x00"
    )
    path.write_bytes(bytes(raw + item + ape + v1))
    return path


def make_tagged_flac(path: Path) -> Path:
    import numpy as np
    import soundfile as sf

    x = (0.1 * np.sin(2 * np.pi * 440 * np.arange(8000) / 8000)).astype("float32")
    sf.write(str(path), x, 8000, format="FLAC")
    from mutagen.flac import FLAC, Picture

    f = FLAC(str(path))
    f["TITLE"] = ["SunoSong"]
    f["ARTIST"] = ["Suno AI"]
    f["LYRICS"] = ["la la"]
    f["WEIRD_TRACE"] = ["suno_id_123"]
    p = Picture()
    p.type = 3
    p.mime = "image/png"
    p.data = b"\x89PNG" + b"\x00" * 32
    f.add_picture(p)
    f.save()
    return path


def make_tagged_ogg(path: Path) -> Path:
    import numpy as np
    import soundfile as sf

    x = (0.1 * np.sin(2 * np.pi * 440 * np.arange(8000) / 8000)).astype("float32")
    sf.write(str(path), x, 8000, format="OGG")
    from mutagen.oggvorbis import OggVorbis

    o = OggVorbis(str(path))
    o["TITLE"] = ["SunoSong"]
    o["ARTIST"] = ["Suno AI"]
    o["LYRICS"] = ["la la"]
    o["TRACE"] = ["x"]
    o.save()
    return path


def make_tagged_m4a(path: Path) -> Path:
    import numpy as np
    import soundfile as sf

    x = (0.1 * np.sin(2 * np.pi * 440 * np.arange(8000) / 8000)).astype("float32")
    wav = path.with_suffix(".src.wav")
    sf.write(str(wav), x, 8000)
    _ffmpeg = _find_ffmpeg()
    subprocess.run(
        [str(_ffmpeg), "-hide_banner", "-y", "-i", str(wav), "-c:a", "aac", "-b:a", "128k", str(path)],
        check=True,
        capture_output=True,
    )
    from mutagen.mp4 import MP4

    m = MP4(str(path))
    m["\xa9nam"] = ["SunoSong"]
    m["\xa9ART"] = ["Suno AI"]
    m["\xa9lyr"] = ["la la"]
    m["cprt"] = ["suno"]
    m["covr"] = [b"\x89PNG" + b"\x00" * 32]
    m.save()
    return path


def _find_ffmpeg():
    from sunoauxtool.download.transcoder import find_ffmpeg

    try:
        return find_ffmpeg()
    except Exception:
        return None


_ffmpeg = _find_ffmpeg()
needs_ffmpeg = pytest.mark.skipif(_ffmpeg is None, reason="ffmpeg 不可用（M4A 夹具合成）")


def _riff_size_ok(path: Path) -> bool:
    b = path.read_bytes()
    return len(b) >= 12 and struct.unpack("<I", b[4:8])[0] + 8 == len(b)


# ---------------------------------------------------------------------------
# WAV：chunk 重写器
# ---------------------------------------------------------------------------


class TestWavSanitize:
    def test_all_mode_strips_info_and_c2pa(self, tmp_path):
        wav = make_suno_like_wav(tmp_path / "Life.wav")
        rep = sanitize_file(wav, SanitizeSpec(mode="all"))
        b = wav.read_bytes()
        assert rep.container == "wav"
        assert b"C2PA" not in b
        assert b"made with suno" not in b
        assert b"Lavf" not in b
        assert b"INAM" not in b
        assert _riff_size_ok(wav)
        assert rep.bytes_after < rep.bytes_before
        assert any("Content Credentials" in r for r in rep.removed)

    def test_all_mode_keeps_audio_decodable(self, tmp_path):
        import soundfile as sf

        wav = make_suno_like_wav(tmp_path / "Life.wav", audio_bytes=8192)
        sanitize_file(wav, SanitizeSpec(mode="all"))
        info = sf.info(str(wav))
        assert info.samplerate == 48000
        assert info.channels == 2
        assert info.frames == 8192 // 4

    def test_keep_basic_keeps_title_artist_drops_traces(self, tmp_path):
        wav = make_suno_like_wav(tmp_path / "Life.wav")
        sanitize_file(wav, SanitizeSpec(mode="keep-basic"))
        b = wav.read_bytes()
        assert b"INAM" in b and b"Life" in b
        assert b"IART" in b and b"Suno AI" in b
        # ICMT（Suno 溯源串惯常位）与 ISFT（编码器痕迹）必须剔除
        assert b"made with suno" not in b
        assert b"Lavf" not in b
        assert b"C2PA" not in b

    def test_custom_overwrites_info_and_id3(self, tmp_path):
        from mutagen.wave import WAVE

        wav = make_suno_like_wav(tmp_path / "Life.wav")
        rep = sanitize_file(
            wav,
            SanitizeSpec(
                mode="custom",
                title="MySong",
                artist="Me",
                album="Alb",
                year="2024",
                genre="Pop",
                comment="hi",
                lyrics="la la la",
            ),
        )
        b = wav.read_bytes()
        assert b"MySong" in b and b"Alb" in b and b"la la la" in b
        assert b"made with suno" not in b and b"C2PA" not in b
        assert rep.written
        # id3 chunk 可被 mutagen 读回（4 字节 chunk ID 含尾空格的规范形态）
        tags = WAVE(str(wav)).tags
        assert tags is not None
        assert str(tags["TIT2"]) == "MySong"
        assert str(tags["USLT::eng"]) == "la la la"

    def test_custom_on_plain_wav_creates_list(self, tmp_path):
        wav = make_plain_wav(tmp_path / "plain.wav")
        sanitize_file(wav, SanitizeSpec(mode="custom", title="T", artist="A"))
        b = wav.read_bytes()
        assert b"LIST" in b and b"INFO" in b
        assert _riff_size_ok(wav)

    def test_clean_wav_not_rewritten(self, tmp_path):
        wav = make_plain_wav(tmp_path / "clean.wav")
        before = wav.read_bytes()
        rep = sanitize_file(wav, SanitizeSpec(mode="all"))
        assert wav.read_bytes() == before
        assert rep.bytes_after == rep.bytes_before

    def test_missing_data_chunk_raises_35(self, tmp_path):
        fmt = _chunk(b"fmt ", struct.pack("<HHIIHH", 1, 2, 48000, 48000 * 4, 4, 16))
        body = b"WAVE" + fmt + _chunk(b"LIST", b"INFO" + _info_sub(b"INAM", b"x"))
        p = tmp_path / "nodata.wav"
        p.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
        with pytest.raises(UnsupportedContainerError) as exc:
            sanitize_file(p, SanitizeSpec(mode="all"))
        assert exc.value.code == 35

    def test_not_riff_raises_35(self, tmp_path):
        p = tmp_path / "fake.wav"
        p.write_bytes(b"NOTRIFF" + b"\x00" * 64)
        with pytest.raises(UnsupportedContainerError) as exc:
            sanitize_file(p, SanitizeSpec(mode="all"))
        assert exc.value.code == 35

    def test_truncated_tail_chunk_tolerated(self, tmp_path):
        wav = make_suno_like_wav(tmp_path / "Life.wav")
        raw = wav.read_bytes()
        # 砍掉最后 20 字节（C2PA chunk 声明长度超出文件尾）
        wav.write_bytes(raw[:-20])
        rep = sanitize_file(wav, SanitizeSpec(mode="all"))
        assert rep.container == "wav"
        assert _riff_size_ok(wav)

    def test_dry_run_does_not_write(self, tmp_path):
        wav = make_suno_like_wav(tmp_path / "Life.wav")
        before = wav.read_bytes()
        rep = sanitize_file(wav, SanitizeSpec(mode="all"), dry_run=True)
        assert rep.dry_run
        assert wav.read_bytes() == before
        assert any("C2PA" in r for r in rep.removed)


# ---------------------------------------------------------------------------
# MP3 / FLAC / OGG / M4A：mutagen + 尾部 legacy 标签
# ---------------------------------------------------------------------------


class TestTaggedContainers:
    def test_mp3_keep_basic(self, tmp_path):
        mp3 = make_tagged_mp3(tmp_path / "a.mp3")
        rep = sanitize_file(mp3, SanitizeSpec(mode="keep-basic"))
        raw = mp3.read_bytes()
        assert raw[-128:-125] != b"TAG", "ID3v1 未剥离"
        assert raw[-32:-24] != b"APETAGEX", "APEv2 未剥离"
        assert b"SUNO_CLIP_ID" not in raw, "TXXX 溯源帧残留"
        assert b"cover" not in raw, "封面残留"
        assert b"lyrics here" in raw and b"SunoSong" in raw and b"my own comment" in raw
        assert "ID3v1" in rep.removed and "APEv2" in rep.removed
        # MP3 封面是 ID3 帧（APIC），随帧过滤移除，体现在帧数差里
        assert any("ID3v2" in r and "帧" in r for r in rep.removed)
        from mutagen.mp3 import MP3

        frames = set(MP3(str(mp3)).tags.keys())
        assert "TIT2" in frames and "USLT::eng" in frames
        assert "TXXX:SUNO_CLIP_ID" not in frames

    def test_mp3_all(self, tmp_path):
        from mutagen.mp3 import MP3

        mp3 = make_tagged_mp3(tmp_path / "a.mp3")
        sanitize_file(mp3, SanitizeSpec(mode="all"))
        raw = mp3.read_bytes()
        assert b"SunoSong" not in raw and b"my own comment" not in raw
        assert raw[-128:-125] != b"TAG"
        assert not MP3(str(mp3)).tags

    def test_flac_keep_basic(self, tmp_path):
        from mutagen.flac import FLAC

        flac = make_tagged_flac(tmp_path / "a.flac")
        rep = sanitize_file(flac, SanitizeSpec(mode="keep-basic"))
        f = FLAC(str(flac))
        assert dict(f.tags) == {"title": ["SunoSong"], "artist": ["Suno AI"], "lyrics": ["la la"]}
        assert not f.pictures
        assert any("封面" in r for r in rep.removed)

    def test_ogg_custom_overwrite(self, tmp_path):
        from mutagen.oggvorbis import OggVorbis

        ogg = make_tagged_ogg(tmp_path / "a.ogg")
        sanitize_file(ogg, SanitizeSpec(mode="custom", title="NewTitle", lyrics="new lyrics"))
        assert dict(OggVorbis(str(ogg)).tags) == {
            "title": ["NewTitle"],
            "lyrics": ["new lyrics"],
        }

    @needs_ffmpeg
    def test_m4a_all_clears_lavf_and_cover(self, tmp_path):
        from mutagen.mp4 import MP4

        m4a = make_tagged_m4a(tmp_path / "a.m4a")
        before = MP4(str(m4a)).tags
        assert "\xa9too" in before, "ffmpeg 合成应带 Lavf 编码器痕迹"
        sanitize_file(m4a, SanitizeSpec(mode="all"))
        after = MP4(str(m4a)).tags
        assert not after


# ---------------------------------------------------------------------------
# 错误路径 / MOTW / spec 校验
# ---------------------------------------------------------------------------


class TestErrorsAndMotw:
    def test_unsupported_suffix_raises_35(self, tmp_path):
        p = tmp_path / "x.txt"
        p.write_text("hi")
        with pytest.raises(UnsupportedContainerError) as exc:
            sanitize_file(p, SanitizeSpec(mode="all"))
        assert exc.value.code == 35
        assert ".txt" in exc.value.message

    def test_missing_file_raises_3(self, tmp_path):
        from sunoauxtool.exceptions import InputFileError

        with pytest.raises(InputFileError) as exc:
            sanitize_file(tmp_path / "nope.wav", SanitizeSpec(mode="all"))
        assert exc.value.code == 3

    def test_bad_mode_raises_parameter_error(self):
        with pytest.raises(ParameterError) as exc:
            SanitizeSpec(mode="bogus")
        assert exc.value.code == 1
        assert MODES == ("all", "keep-basic", "custom")

    def test_ape_tag_len_variants(self):
        items = b"\x00" * 10
        footer_only = b"APETAGEX" + (2000).to_bytes(4, "little") + len(items).to_bytes(4, "little") + b"\x00" * 16
        with_header = footer_only + items + b"APETAGEX" + (2000).to_bytes(4, "little") + len(items).to_bytes(4, "little") + b"\x00" * 16
        assert _ape_tag_len(items + footer_only) == len(items) + 32
        assert _ape_tag_len(with_header) == len(with_header)
        assert _ape_tag_len(b"\x00" * 128) == 0
        assert _ape_tag_len(b"APETAGEX" + b"\x00" * 8) == 0

    def test_strip_motw_non_windows_returns_false(self, monkeypatch, tmp_path):
        monkeypatch.setattr(os, "name", "posix")
        assert strip_motw(tmp_path / "a.wav") is False

    def test_strip_motw_calls_unlink_on_windows(self, monkeypatch, tmp_path):
        calls = []

        def fake_unlink(p):
            calls.append(p)
            raise FileNotFoundError(p)

        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setattr(os, "unlink", fake_unlink)
        target = tmp_path / "a.wav"
        assert strip_motw(target) is False
        assert calls == [f"{target}:Zone.Identifier"]

    @pytest.mark.skipif(os.name != "nt", reason="ADS 仅 Windows NTFS 可写")
    def test_strip_motw_removes_real_ads(self, tmp_path):
        p = tmp_path / "a.wav"
        p.write_bytes(b"RIFF")
        ads = f"{p}:Zone.Identifier"
        with open(ads, "w") as fh:
            fh.write("[ZoneTransfer]\nHostUrl=https://suno.com\n")
        if not strip_motw(p):
            # 沙箱 delete 钩子会失败关闭地拦截 ADS 删除；真实 Windows 上此处为全断言
            pytest.skip("当前环境无法删除 ADS（沙箱 delete 钩子拦截）")
        with pytest.raises(FileNotFoundError):
            open(ads)


# ---------------------------------------------------------------------------
# CLI：post strip-meta
# ---------------------------------------------------------------------------


class TestStripMetaCli:
    def test_help_lists_options(self):
        r = runner.invoke(app, ["post", "strip-meta", "--help"])
        assert r.exit_code == 0
        plain = _ANSI_RE.sub("", r.output)
        for opt in ("--mode", "--title", "--lyrics", "--no-motw", "--dry-run", "--yes"):
            assert opt in plain

    def test_dry_run_no_write(self, tmp_path):
        wav = make_suno_like_wav(tmp_path / "Life.wav")
        before = wav.read_bytes()
        r = runner.invoke(app, ["post", "strip-meta", str(wav), "--dry-run"])
        assert r.exit_code == 0, r.output
        assert "C2PA" in r.output
        assert wav.read_bytes() == before

    def test_decline_confirmation_no_write(self, tmp_path):
        wav = make_suno_like_wav(tmp_path / "Life.wav")
        before = wav.read_bytes()
        r = runner.invoke(app, ["post", "strip-meta", str(wav)], input="n\n")
        assert r.exit_code == 0
        assert "已取消" in r.output
        assert wav.read_bytes() == before

    def test_yes_runs_and_strips(self, tmp_path):
        wav = make_suno_like_wav(tmp_path / "Life.wav")
        r = runner.invoke(app, ["post", "strip-meta", str(wav), "--yes"])
        assert r.exit_code == 0, r.output
        assert "✅" in r.output
        assert b"C2PA" not in wav.read_bytes()

    def test_custom_mode_cli(self, tmp_path):
        from mutagen.wave import WAVE

        wav = make_suno_like_wav(tmp_path / "Life.wav")
        r = runner.invoke(
            app,
            [
                "post", "strip-meta", str(wav), "--yes",
                "--mode", "custom", "--title", "T", "--artist", "A", "--lyrics", "L",
            ],
        )
        assert r.exit_code == 0, r.output
        tags = WAVE(str(wav)).tags
        assert str(tags["TIT2"]) == "T" and str(tags["USLT::eng"]) == "L"

    def test_bad_mode_exit_1(self, tmp_path):
        wav = make_suno_like_wav(tmp_path / "Life.wav")
        r = runner.invoke(app, ["post", "strip-meta", str(wav), "--mode", "bogus", "--yes"])
        assert r.exit_code == 1

    def test_missing_file_exit_3(self, tmp_path):
        r = runner.invoke(app, ["post", "strip-meta", str(tmp_path / "nope.wav"), "--yes"])
        assert r.exit_code == 3

    def test_unsupported_single_file_exit_35(self, tmp_path):
        p = tmp_path / "x.txt"
        p.write_text("hi")
        r = runner.invoke(app, ["post", "strip-meta", str(p), "--yes"])
        assert r.exit_code == 35
        assert "35" in r.output

    def test_batch_partial_exit_8(self, tmp_path):
        wav = make_suno_like_wav(tmp_path / "Life.wav")
        bad = tmp_path / "y.txt"
        bad.write_text("x")
        r = runner.invoke(app, ["post", "strip-meta", str(wav), str(bad), "--yes"])
        assert r.exit_code == 8
        assert b"C2PA" not in wav.read_bytes()

    def test_batch_all_fail_multi_exit_9(self, tmp_path):
        bad = tmp_path / "y.txt"
        bad.write_text("x")
        bad2 = tmp_path / "z.txt"
        bad2.write_text("x")
        r = runner.invoke(app, ["post", "strip-meta", str(bad), str(bad2), "--yes"])
        assert r.exit_code == 9
