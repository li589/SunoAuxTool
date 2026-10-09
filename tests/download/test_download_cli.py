"""cli.py 单元测试（Typer CliRunner）+ 一次真实端到端。

多数用例拦掉 ffmpeg/ffprobe，保证快速且确定；文件末尾用真实 ffmpeg
跑一次完整 `decode`，证明 CLI 确实接到了真实实现而非只在 mock 下成立。
"""

from __future__ import annotations

import subprocess

import pytest
from typer.testing import CliRunner

from sunoauxtool.download import cli
from sunoauxtool.download.cli import app
from sunoauxtool.download.exceptions import FFmpegNotFoundError, SunoError

runner = CliRunner()


def _text(result) -> str:
    """合并 stdout 与 stderr —— click 各版本对 stderr 的归并方式不一致。"""
    out = result.output or ""
    try:
        out += result.stderr or ""
    except (ValueError, AttributeError):
        pass
    return out


# -- version ----------------------------------------------------------------

def test_version_prints_package_and_ffmpeg_path(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "find_ffmpeg", lambda: tmp_path / "ffmpeg")
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "suno-cat-catch-resolve" in _text(result)
    assert "ffmpeg:" in _text(result)


def test_version_reports_missing_ffmpeg_without_failing(monkeypatch):
    def boom():
        raise FFmpegNotFoundError()

    monkeypatch.setattr(cli, "find_ffmpeg", boom)
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "ffmpeg: 未找到" in _text(result)


def test_help_lists_all_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("probe", "decode", "batch", "version"):
        assert cmd in _text(result)


# -- probe ------------------------------------------------------------------

def test_probe_plaintext_fmp4_reports_decodable(fmp4_file, monkeypatch):
    monkeypatch.setattr(cli, "ffprobe_info", lambda f, p: {})
    result = runner.invoke(app, ["probe", str(fmp4_file)])
    assert result.exit_code == 0
    text = _text(result)
    assert "fmp4" in text
    assert "（可解码）" in text


def test_probe_prints_media_info_keys(fmp4_file, monkeypatch):
    monkeypatch.setattr(
        cli, "ffprobe_info",
        lambda f, p: {"codec_name": "opus", "sample_rate": "48000", "duration": "3.0"},
    )
    text = _text(runner.invoke(app, ["probe", str(fmp4_file)]))
    assert "媒体信息" in text
    assert "opus" in text
    assert "48000" in text


def test_probe_skips_absent_media_keys(fmp4_file, monkeypatch):
    """ffprobe 未返回的键不应打印成空行。"""
    monkeypatch.setattr(cli, "ffprobe_info", lambda f, p: {"codec_name": "opus"})
    text = _text(runner.invoke(app, ["probe", str(fmp4_file)]))
    assert "bit_rate" not in text


def test_probe_survives_ffprobe_unavailable(fmp4_file, monkeypatch):
    """取证结论仍要输出，只提示 ffprobe 不可用。"""
    def boom(f, p):
        raise SunoError("ffprobe 未找到")

    monkeypatch.setattr(cli, "ffprobe_info", boom)
    result = runner.invoke(app, ["probe", str(fmp4_file)])
    assert result.exit_code == 0
    text = _text(result)
    assert "fmp4" in text
    assert "ffprobe 不可用" in text


def test_probe_ciphertext_reports_not_decodable(encrypted_file):
    result = runner.invoke(app, ["probe", str(encrypted_file)])
    assert result.exit_code == 0
    text = _text(result)
    assert "encrypted" in text
    assert "（不可解码）" in text


def test_probe_ciphertext_does_not_call_ffprobe(encrypted_file, monkeypatch):
    """密文没必要探测媒体信息。"""
    called = []
    monkeypatch.setattr(cli, "ffprobe_info", lambda f, p: called.append(1) or {})
    runner.invoke(app, ["probe", str(encrypted_file)])
    assert called == []


def test_probe_missing_file_raises(tmp_path):
    result = runner.invoke(app, ["probe", str(tmp_path / "nope.m4a")])
    assert result.exit_code != 0


# -- decode -----------------------------------------------------------------

def test_decode_rejects_invalid_fmt(fmp4_file):
    result = runner.invoke(app, ["decode", str(fmp4_file), "--fmt", "wav"])
    assert result.exit_code == 2
    assert "opus / mp3 / both" in _text(result)


def test_decode_encrypted_exits_22(encrypted_file, tmp_path):
    result = runner.invoke(app, ["decode", str(encrypted_file), "-o", str(tmp_path)])
    assert result.exit_code == 22
    assert "无密钥不可解码" in _text(result)


def test_decode_prints_generated_paths(monkeypatch, fmp4_file, tmp_path):
    out = tmp_path / "out"
    monkeypatch.setattr(
        cli, "decode_fmp4",
        lambda f, o, **kw: [out / "a.opus", out / "a.mp3"],
    )
    result = runner.invoke(app, ["decode", str(fmp4_file), "-o", str(out)])
    assert result.exit_code == 0
    assert _text(result).count("已生成:") == 2


def test_decode_forwards_options(monkeypatch, fmp4_file, tmp_path):
    seen = {}

    def fake(file, out, fmt="both", stem=None, bitrate="192k", ffmpeg=None):
        seen.update(fmt=fmt, stem=stem, bitrate=bitrate)
        return [tmp_path / "x.mp3"]

    monkeypatch.setattr(cli, "decode_fmp4", fake)
    runner.invoke(
        app,
        ["decode", str(fmp4_file), "-o", str(tmp_path),
         "--fmt", "mp3", "--stem", "s", "--bitrate", "256k"],
    )
    assert seen == {"fmt": "mp3", "stem": "s", "bitrate": "256k"}


def test_decode_generic_suno_error_uses_its_code(monkeypatch, fmp4_file, tmp_path):
    def boom(f, o, **kw):
        raise SunoError("转码炸了", code=23)

    monkeypatch.setattr(cli, "decode_fmp4", boom)
    result = runner.invoke(app, ["decode", str(fmp4_file), "-o", str(tmp_path)])
    assert result.exit_code == 23
    assert "错误[23]" in _text(result)


def test_decode_error_without_code_falls_back_to_exit_1(monkeypatch, fmp4_file, tmp_path):
    def boom(f, o, **kw):
        raise SunoError("没带错误码")

    monkeypatch.setattr(cli, "decode_fmp4", boom)
    assert runner.invoke(app, ["decode", str(fmp4_file), "-o", str(tmp_path)]).exit_code == 1


# -- batch ------------------------------------------------------------------

def _batch_dir(tmp_path, encrypted_bytes, make_fmp4):
    d = tmp_path / "downloads"
    d.mkdir()
    (d / "Suno _ AI Music.mp3").write_bytes(make_fmp4())
    (d / "aaaa-bbbb.m4a").write_bytes(encrypted_bytes)
    (d / "notes.txt").write_text("hello", encoding="utf-8")
    (d / "subdir").mkdir()
    return d


def test_batch_reports_decoded_and_skipped(tmp_path, encrypted_bytes, make_fmp4, monkeypatch):
    """报告逻辑：解出几个、跳过几个、分别是谁。刻意 mock 转码，使其与 ffmpeg 无关。"""
    d = _batch_dir(tmp_path, encrypted_bytes, make_fmp4)
    monkeypatch.setattr(cli, "decode_fmp4", lambda f, o, **kw: [o / "x_decoded.opus"])

    result = runner.invoke(app, ["batch", str(d)])
    assert result.exit_code == 0
    text = _text(result)
    assert "解码成功 1 个文件" in text
    assert "x_decoded.opus" in text
    assert "跳过" in text
    assert "aaaa-bbbb.m4a" in text
    assert "加密密文" in text


def test_batch_ignores_subdirectories(tmp_path, make_fmp4, monkeypatch):
    d = tmp_path / "d"
    d.mkdir()
    (d / "sub").mkdir()
    (d / "a.mp3").write_bytes(make_fmp4())
    monkeypatch.setattr(cli, "decode_fmp4", lambda f, o, **kw: [o / "a.opus"])
    result = runner.invoke(app, ["batch", str(d)])
    assert "解码成功 1 个文件" in _text(result)


def test_batch_reports_failures(tmp_path, make_fmp4, monkeypatch):
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.mp3").write_bytes(make_fmp4())

    def boom(*a, **kw):
        raise SunoError("转码失败", code=23)

    monkeypatch.setattr(cli, "decode_fmp4", boom)
    result = runner.invoke(app, ["batch", str(d)])
    assert "失败 1 个" in _text(result)


def test_batch_empty_directory(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    result = runner.invoke(app, ["batch", str(d)])
    assert result.exit_code == 0
    assert "解码成功 0 个文件" in _text(result)


def test_batch_skips_non_fmp4_containers_silently(tmp_path, monkeypatch):
    """Ogg/WAV 等合法但不是 fMP4 的文件不报错、也不算失败。"""
    d = tmp_path / "d"
    d.mkdir()
    (d / "x.ogg").write_bytes(b"OggS\x00\x02" + bytes(range(256)) * 4)
    called = []
    monkeypatch.setattr(cli, "decode_fmp4", lambda *a, **kw: called.append(1) or [])
    result = runner.invoke(app, ["batch", str(d)])
    text = _text(result)
    assert result.exit_code == 0
    assert "解码成功 0 个文件" in text
    assert "失败" not in text
    assert called == []


# -- batch：真实解码（用 ffmpeg 现场生成的真 fMP4）--------------------------

def test_batch_real_decode_creates_files(real_fmp4, encrypted_file, tmp_path):
    """batch 不拦 ffmpeg，真实 fMP4 应真的产出文件、密文应被跳过。"""
    import shutil

    d = tmp_path / "downloads"
    d.mkdir()
    shutil.copy(real_fmp4, d / real_fmp4.name)
    shutil.copy(encrypted_file, d / "aaaa-bbbb.m4a")

    result = runner.invoke(app, ["batch", str(d), "--fmt", "opus"])
    assert result.exit_code == 0, _text(result)
    produced = list(d.glob("*.opus"))
    assert len(produced) == 1
    assert produced[0].stat().st_size > 1000
    assert "加密密文" in _text(result)


def test_batch_real_decode_respects_output_dir(real_fmp4, tmp_path):
    import shutil

    d = tmp_path / "d"
    d.mkdir()
    shutil.copy(real_fmp4, d / real_fmp4.name)
    out = tmp_path / "out"

    result = runner.invoke(app, ["batch", str(d), "-o", str(out), "--fmt", "opus"])
    assert result.exit_code == 0, _text(result)
    assert list(out.glob("*.opus"))
    assert not list(d.glob("*.opus"))


# -- 真实端到端（不 mock）---------------------------------------------------

@pytest.fixture
def real_fmp4(tmp_path):
    from sunoauxtool.download.transcoder import find_ffmpeg

    try:
        ff = find_ffmpeg()
    except FFmpegNotFoundError:
        pytest.skip("本机无 ffmpeg")

    out = tmp_path / "Suno _ AI Music.mp3"
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


def test_cli_real_end_to_end_decode_produces_both_formats(real_fmp4, tmp_path):
    out = tmp_path / "out"
    result = runner.invoke(app, ["decode", str(real_fmp4), "-o", str(out)])
    assert result.exit_code == 0, _text(result)
    assert sorted(p.suffix for p in out.iterdir()) == [".mp3", ".opus"]


def test_cli_real_end_to_end_probe_reports_opus(real_fmp4):
    result = runner.invoke(app, ["probe", str(real_fmp4)])
    assert result.exit_code == 0, _text(result)
    text = _text(result)
    assert "fmp4" in text
    assert "opus" in text


def test_cli_real_end_to_end_decode_rejects_ciphertext(encrypted_file, tmp_path):
    """真实链路上密文也必须被拒（不依赖 mock 的行为）。"""
    out = tmp_path / "out"
    result = runner.invoke(app, ["decode", str(encrypted_file), "-o", str(out)])
    assert result.exit_code == 22
    assert not list(out.glob("*_decoded.*"))
