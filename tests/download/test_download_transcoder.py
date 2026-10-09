"""transcoder.py 单元测试 + 真实 ffmpeg 端到端集成测试。

单元测试用 monkeypatch 拦掉 `_run`，不依赖外部程序；
集成测试（本文件末尾）真正调用 ffmpeg，缺少时自动 skip。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from sunoauxtool.download import transcoder
from sunoauxtool.download.exceptions import (
    EncryptedBlobError,
    FFmpegNotFoundError,
    NotFragmentedMP4Error,
    OutputWriteError,
    TranscodeError,
)
from sunoauxtool.download.forensics import identify
from sunoauxtool.download.transcoder import (
    _ensure_outdir,
    _sanitize,
    decode_fmp4,
    find_ffmpeg,
    probe,
    remux_opus,
    to_mp3,
)


@pytest.fixture
def fake_ffmpeg(monkeypatch, tmp_path):
    """把 ffmpeg 定位与进程调用都替换掉，返回记录调用的列表。"""
    exe = tmp_path / "ffmpeg"
    exe.write_bytes(b"stub")
    calls: list[list[str]] = []

    monkeypatch.setattr(transcoder, "find_ffmpeg", lambda path=None: exe)

    def fake_run(cmd):
        calls.append(cmd)
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(transcoder, "_run", fake_run)
    return calls


# -- find_ffmpeg ------------------------------------------------------------

def test_find_ffmpeg_explicit_existing_path(tmp_path):
    exe = tmp_path / "ffmpeg"
    exe.write_bytes(b"stub")
    assert find_ffmpeg(exe) == exe


def test_find_ffmpeg_explicit_missing_path_raises(tmp_path):
    with pytest.raises(FFmpegNotFoundError) as ei:
        find_ffmpeg(tmp_path / "nope")
    assert ei.value.code == 20


def test_find_ffmpeg_uses_path_lookup(monkeypatch, tmp_path):
    exe = tmp_path / "ffmpeg"
    exe.write_bytes(b"stub")
    monkeypatch.setattr(transcoder.shutil, "which", lambda name: str(exe))
    assert find_ffmpeg() == exe


def test_find_ffmpeg_falls_back_to_known_dirs(monkeypatch, tmp_path):
    """PATH 查不到时，回落到本机已知安装目录。"""
    exe = tmp_path / "ffmpeg.exe"
    exe.write_bytes(b"stub")
    monkeypatch.setattr(transcoder.shutil, "which", lambda name: None)
    monkeypatch.setattr(transcoder, "KNOWN_FFMPEG_DIRS", [str(tmp_path)])
    assert find_ffmpeg() == exe


def test_find_ffmpeg_raises_when_nowhere_found(monkeypatch, tmp_path):
    monkeypatch.setattr(transcoder.shutil, "which", lambda name: None)
    monkeypatch.setattr(transcoder, "KNOWN_FFMPEG_DIRS", [str(tmp_path / "empty")])
    with pytest.raises(FFmpegNotFoundError) as ei:
        find_ffmpeg()
    assert ei.value.code == 20


def test_find_ffmpeg_error_message_mentions_override():
    """报错要给出可执行的修复指引，而不是只说找不到。"""
    with pytest.raises(FFmpegNotFoundError) as ei:
        find_ffmpeg(Path("definitely-not-here"))
    assert "--ffmpeg-path" in ei.value.message


# -- find_ffmpeg：环境变量层（第 2 项：硬编码路径不再是唯一出路） ----------

def test_find_ffmpeg_env_var_pointing_at_file(monkeypatch, tmp_path):
    """SUNO_FFMPEG 直接指向可执行文件时优先于 PATH。"""
    exe = tmp_path / "ffmpeg.exe"
    exe.write_bytes(b"stub")
    monkeypatch.setenv("SUNO_FFMPEG", str(exe))
    # 故意让 PATH 上有一个「别的」ffmpeg：环境变量必须压过它
    other = tmp_path / "other" / "ffmpeg"
    other.parent.mkdir()
    other.write_bytes(b"stub")
    monkeypatch.setattr(transcoder.shutil, "which", lambda name: str(other))
    assert find_ffmpeg() == exe


def test_find_ffmpeg_env_var_pointing_at_directory(monkeypatch, tmp_path):
    """SUNO_FFMPEG 指向目录时，自动在其中找 ffmpeg.exe。"""
    exe = tmp_path / "ffmpeg.exe"
    exe.write_bytes(b"stub")
    monkeypatch.setenv("SUNO_FFMPEG", str(tmp_path))
    monkeypatch.setattr(transcoder.shutil, "which", lambda name: None)
    assert find_ffmpeg() == exe


def test_find_ffmpeg_alternate_env_var_name(monkeypatch, tmp_path):
    """SMARTNOTEGEN_FFMPEG 是同一约定的别名，两个都认。"""
    exe = tmp_path / "ffmpeg"
    exe.write_bytes(b"stub")
    monkeypatch.setenv("SMARTNOTEGEN_FFMPEG", str(exe))
    monkeypatch.setattr(transcoder.shutil, "which", lambda name: None)
    assert find_ffmpeg() == exe


def test_find_ffmpeg_invalid_env_var_falls_through_to_path(monkeypatch, tmp_path):
    """环境变量写了个不存在的路径 → 继续回落，而不是直接抛错。"""
    monkeypatch.setenv("SUNO_FFMPEG", str(tmp_path / "nope"))
    exe = tmp_path / "real" / "ffmpeg"
    exe.parent.mkdir()
    exe.write_bytes(b"stub")
    monkeypatch.setattr(transcoder.shutil, "which", lambda name: str(exe))
    assert find_ffmpeg() == exe


def test_find_ffmpeg_empty_env_var_is_ignored(monkeypatch, tmp_path):
    """空串等同于未设置（避免 CI 上 `FOO=` 这种传法把它当成合法路径）。"""
    monkeypatch.setenv("SUNO_FFMPEG", "")
    exe = tmp_path / "ffmpeg"
    exe.write_bytes(b"stub")
    monkeypatch.setattr(transcoder.shutil, "which", lambda name: str(exe))
    assert find_ffmpeg() == exe


def test_find_ffmpeg_env_dirs_searched(monkeypatch, tmp_path):
    """SUNO_FFMPEG_DIRS 提供追加搜索目录，绕过硬编码的本机路径。"""
    monkeypatch.setattr(transcoder.shutil, "which", lambda name: None)
    monkeypatch.setattr(transcoder, "KNOWN_FFMPEG_DIRS", [])
    portable = tmp_path / "portable" / "bin"
    portable.mkdir(parents=True)
    (portable / "ffmpeg").write_bytes(b"stub")
    monkeypatch.setenv("SUNO_FFMPEG_DIRS", str(portable))
    assert find_ffmpeg() == portable / "ffmpeg"


def test_find_ffmpeg_env_dirs_is_pathsep_separated(monkeypatch, tmp_path):
    """多目录用 os.pathsep 分隔，空项要跳过（Windows `;a;;b;` 这种写法很常见）。"""
    monkeypatch.setattr(transcoder.shutil, "which", lambda name: None)
    monkeypatch.setattr(transcoder, "KNOWN_FFMPEG_DIRS", [])
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (second / "ffmpeg.exe").write_bytes(b"stub")
    monkeypatch.setenv(
        "SUNO_FFMPEG_DIRS",
        os.pathsep.join(["", str(first), "", str(second), ""]),
    )
    assert find_ffmpeg() == second / "ffmpeg.exe"


def test_find_ffmpeg_env_dirs_take_precedence_over_hardcoded(monkeypatch, tmp_path):
    """环境变量目录排在硬编码目录之前，换机时无需改源码。"""
    monkeypatch.setattr(transcoder.shutil, "which", lambda name: None)
    hardcoded = tmp_path / "hardcoded"
    hardcoded.mkdir()
    (hardcoded / "ffmpeg.exe").write_bytes(b"stub")
    portable = tmp_path / "portable"
    portable.mkdir()
    (portable / "ffmpeg.exe").write_bytes(b"stub")
    monkeypatch.setattr(transcoder, "KNOWN_FFMPEG_DIRS", [str(hardcoded)])
    monkeypatch.setenv("SUNO_FFMPEG_DIRS", str(portable))
    assert find_ffmpeg() == portable / "ffmpeg.exe"


def test_find_ffmpeg_error_message_lists_all_routes():
    """报错要把四条修法都列出来（环境变量 / PATH / 目录列表 / 传参）。"""
    with pytest.raises(FFmpegNotFoundError) as ei:
        find_ffmpeg(Path("definitely-not-here"))
    msg = ei.value.message
    for hint in ("SUNO_FFMPEG=", "SUNO_FFMPEG_DIRS=", "--ffmpeg-path"):
        assert hint in msg


# -- _find_ffprobe ----------------------------------------------------------

def test_find_ffprobe_prefers_sibling_of_ffmpeg(tmp_path):
    """同目录有 ffprobe 时优先用它，避免依赖 PATH 上的另一个版本。"""
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"stub")
    ffprobe = tmp_path / "ffprobe.exe"
    ffprobe.write_bytes(b"stub")
    assert transcoder._find_ffprobe(ffmpeg) == ffprobe


def test_find_ffprobe_sibling_name_without_exe_suffix(tmp_path):
    """非 Windows 布局（无 .exe 后缀）也要能推导。"""
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_bytes(b"stub")
    ffprobe = tmp_path / "ffprobe"
    ffprobe.write_bytes(b"stub")
    assert transcoder._find_ffprobe(ffmpeg) == ffprobe


def test_find_ffprobe_falls_back_to_path(monkeypatch, tmp_path):
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_bytes(b"stub")
    other = tmp_path / "elsewhere" / "ffprobe"
    monkeypatch.setattr(transcoder.shutil, "which", lambda name: str(other))
    assert transcoder._find_ffprobe(ffmpeg) == other


def test_find_ffprobe_raises_when_unavailable(monkeypatch, tmp_path):
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_bytes(b"stub")
    monkeypatch.setattr(transcoder.shutil, "which", lambda name: None)
    with pytest.raises(FFmpegNotFoundError) as ei:
        transcoder._find_ffprobe(ffmpeg)
    assert ei.value.code == 20


# -- _sanitize --------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Suno _ AI Music", "Suno_AI_Music"),
        ("Suno _ AI Music (1)", "Suno_AI_Music_1"),
        ("track[2]{x}", "track2x"),
        ("  spaced  out  ", "spaced_out"),
        ("a-b_c", "a-b_c"),
        ("", "track"),
        ("()", "track"),
    ],
)
def test_sanitize(raw, expected):
    assert _sanitize(raw) == expected


# -- _ensure_outdir ---------------------------------------------------------

def test_ensure_outdir_creates_nested(tmp_path):
    target = tmp_path / "a" / "b" / "c"
    assert _ensure_outdir(target) == target
    assert target.is_dir()


def test_ensure_outdir_existing_is_idempotent(tmp_path):
    assert _ensure_outdir(tmp_path) == tmp_path


def test_ensure_outdir_wraps_oserror(monkeypatch, tmp_path):
    def boom(self, *a, **kw):
        raise OSError("denied")

    monkeypatch.setattr(Path, "mkdir", boom)
    with pytest.raises(OutputWriteError) as ei:
        _ensure_outdir(tmp_path / "x")
    assert ei.value.code == 24


# -- remux_opus / to_mp3 ----------------------------------------------------

def test_remux_opus_streams_copy_without_reencode(fake_ffmpeg, tmp_path):
    """无损路径必须是 -c copy —— 重编码会破坏「无损」承诺。"""
    dst = tmp_path / "out" / "t.opus"
    result = remux_opus(tmp_path / "in.mp3", dst)
    assert result == dst
    assert "-c" in fake_ffmpeg[0] and "copy" in fake_ffmpeg[0]
    assert "libmp3lame" not in fake_ffmpeg[0]


def test_remux_opus_creates_parent_dir(fake_ffmpeg, tmp_path):
    dst = tmp_path / "deep" / "nested" / "t.opus"
    remux_opus(tmp_path / "in.mp3", dst)
    assert dst.parent.is_dir()


def test_remux_opus_failure_raises_transcode_error(monkeypatch, tmp_path):
    monkeypatch.setattr(transcoder, "find_ffmpeg", lambda path=None: tmp_path / "ffmpeg")
    monkeypatch.setattr(
        transcoder, "_run", lambda cmd: CompletedProcess(cmd, 1, "", "boom")
    )
    with pytest.raises(TranscodeError) as ei:
        remux_opus(tmp_path / "in.mp3", tmp_path / "o.opus")
    assert ei.value.code == 23
    assert ei.value.exit_code == 1


def test_to_mp3_passes_bitrate(fake_ffmpeg, tmp_path):
    to_mp3(tmp_path / "in.mp3", tmp_path / "o.mp3", bitrate="256k")
    cmd = fake_ffmpeg[0]
    assert "libmp3lame" in cmd
    assert "256k" in cmd


def test_to_mp3_default_bitrate_is_192k(fake_ffmpeg, tmp_path):
    to_mp3(tmp_path / "in.mp3", tmp_path / "o.mp3")
    assert "192k" in fake_ffmpeg[0]


def test_to_mp3_failure_raises_transcode_error(monkeypatch, tmp_path):
    monkeypatch.setattr(transcoder, "find_ffmpeg", lambda path=None: tmp_path / "ffmpeg")
    monkeypatch.setattr(
        transcoder, "_run", lambda cmd: CompletedProcess(cmd, 1, "", "boom")
    )
    with pytest.raises(TranscodeError):
        to_mp3(tmp_path / "in.mp3", tmp_path / "o.mp3")


def test_transcode_error_truncates_stderr(monkeypatch, tmp_path):
    """stderr 可能极长，报错文本要截断，避免刷屏。"""
    monkeypatch.setattr(transcoder, "find_ffmpeg", lambda path=None: tmp_path / "ffmpeg")
    monkeypatch.setattr(
        transcoder, "_run", lambda cmd: CompletedProcess(cmd, 1, "", "E" * 5000)
    )
    with pytest.raises(TranscodeError) as ei:
        to_mp3(tmp_path / "in.mp3", tmp_path / "o.mp3")
    assert len(ei.value.message) < 1000


# -- decode_fmp4 ------------------------------------------------------------

def test_decode_fmp4_rejects_encrypted_blob(encrypted_file, tmp_path):
    with pytest.raises(EncryptedBlobError) as ei:
        decode_fmp4(encrypted_file, tmp_path / "out")
    assert ei.value.code == 22
    assert "无密钥不可解码" in ei.value.message


def test_decode_fmp4_encrypted_produces_no_output(encrypted_file, tmp_path):
    """拒绝时必须不留半成品垃圾文件。"""
    out = tmp_path / "out"
    with pytest.raises(EncryptedBlobError):
        decode_fmp4(encrypted_file, out)
    assert not any(out.glob("*"))


def test_decode_fmp4_rejects_non_fragmented_container(tmp_path):
    """合法容器但不是 fMP4（如 Ogg）→ 报「不是 fMP4」，退出码 21。"""
    ogg = tmp_path / "x.ogg"
    ogg.write_bytes(b"OggS\x00\x02" + bytes(range(256)) * 4)
    with pytest.raises(NotFragmentedMP4Error) as ei:
        decode_fmp4(ogg, tmp_path / "out")
    assert ei.value.code == 21


def test_decode_fmp4_rejects_plain_mp4_without_fragments(tmp_path):
    """标准 MP4（无 moof/mdat 分片）不是 Suno 缓存产物，应拒绝。"""
    from conftest import box

    plain = tmp_path / "plain.mp4"
    plain.write_bytes(
        box(b"ftyp", b"isom\x00\x00\x02\x00") + box(b"moov", b"\x00" * 16)
        + box(b"mdat", bytes(range(256)) * 8)
    )
    with pytest.raises(NotFragmentedMP4Error) as ei:
        decode_fmp4(plain, tmp_path / "out")
    assert ei.value.code == 21


def test_decode_fmp4_treats_unknown_non_uniform_blob_as_encrypted(tmp_path):
    """既非容器、又无周期的未知裸流按密文处理（宁可拒绝，不产垃圾）。"""
    blob = tmp_path / "y.bin"
    blob.write_bytes(bytes([i % 200 for i in range(50_000)]))
    with pytest.raises(EncryptedBlobError) as ei:
        decode_fmp4(blob, tmp_path / "out")
    assert ei.value.code == 22


def test_decode_fmp4_both_formats(fake_ffmpeg, fmp4_file, tmp_path):
    outs = decode_fmp4(fmp4_file, tmp_path / "out", fmt="both")
    assert [p.name for p in outs] == [
        "Suno_AI_Music_decoded.opus",
        "Suno_AI_Music_decoded.mp3",
    ]


@pytest.mark.parametrize(
    "fmt, expected_name",
    [("opus", "x_decoded.opus"), ("mp3", "x_decoded.mp3")],
)
def test_decode_fmp4_single_format(fake_ffmpeg, fmp4_file, tmp_path, fmt, expected_name):
    outs = decode_fmp4(fmp4_file, tmp_path / "out", fmt=fmt, stem="x")
    assert [p.name for p in outs] == [expected_name]


def test_decode_fmp4_stem_override(fake_ffmpeg, fmp4_file, tmp_path):
    outs = decode_fmp4(fmp4_file, tmp_path / "out", fmt="mp3", stem="my-track")
    assert outs[0].name == "my-track_decoded.mp3"


def test_decode_fmp4_creates_output_dir(fake_ffmpeg, fmp4_file, tmp_path):
    out = tmp_path / "a" / "b"
    decode_fmp4(fmp4_file, out, fmt="opus")
    assert out.is_dir()


def test_decode_fmp4_ignores_misleading_extension(fake_ffmpeg, tmp_path, make_fmp4):
    """扩展名是 .m4a 但内容是 fMP4 → 仍应解码（扩展名不可信）。"""
    misleading = tmp_path / "a1b2c3d4.m4a"
    misleading.write_bytes(make_fmp4())
    outs = decode_fmp4(misleading, tmp_path / "out", fmt="opus")
    assert outs[0].name == "a1b2c3d4_decoded.opus"


# -- 真实 ffmpeg 端到端 -----------------------------------------------------

@pytest.fixture
def real_opus_fmp4(tmp_path):
    """用本机 ffmpeg 现场生成一份真实的 Opus fMP4（复刻猫抓缓存产物）。"""
    try:
        ff = find_ffmpeg()
    except FFmpegNotFoundError:
        pytest.skip("本机无 ffmpeg")

    out = tmp_path / "Suno _ AI Music (1).mp3"
    cmd = [
        str(ff), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-c:a", "libopus", "-b:a", "96k",
        "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0 or not out.is_file():
        pytest.skip(f"本机 ffmpeg 无法生成 Opus fMP4: {(proc.stderr or '')[:200]}")
    return out


def test_real_ffmpeg_fixture_is_recognized_as_fmp4(real_opus_fmp4):
    v = identify(str(real_opus_fmp4))
    assert v.kind == "fmp4"
    assert v.breakable is True


def test_real_ffmpeg_decode_produces_playable_audio(real_opus_fmp4, tmp_path):
    outs = decode_fmp4(real_opus_fmp4, tmp_path / "out", fmt="both")
    assert len(outs) == 2
    for p in outs:
        assert p.is_file()
        assert p.stat().st_size > 1000


def test_real_ffmpeg_mp3_output_is_really_mp3(real_opus_fmp4, tmp_path):
    outs = decode_fmp4(real_opus_fmp4, tmp_path / "out", fmt="mp3")
    info = probe(outs[0])
    assert info.get("codec_name") == "mp3"
    assert info.get("codec_type") == "audio"


def test_real_ffmpeg_opus_output_is_ogg_container(real_opus_fmp4, tmp_path):
    """Opus 无损输出必须是 Ogg 封装——它不能进 MP4 容器。"""
    outs = decode_fmp4(real_opus_fmp4, tmp_path / "out", fmt="opus")
    assert outs[0].read_bytes()[:4] == b"OggS"


def test_real_ffmpeg_probe_reports_source_codec(real_opus_fmp4):
    info = probe(real_opus_fmp4)
    assert info.get("codec_name") == "opus"
    assert info.get("sample_rate") == "48000"


def test_real_ffmpeg_probe_bad_file_raises(tmp_path):
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"not media at all")
    with pytest.raises(TranscodeError):
        probe(bad)
