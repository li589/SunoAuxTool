"""通用转码组件测试（1.6.0）：音频互转矩阵 + 视频分离音轨。

源文件由 ffmpeg 合成（session 级，一次生成复用）：
    音频 = 2s 440Hz 正弦；视频 = 1s testsrc + AAC 音轨。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sunoauxtool.download.convert import (
    SUPPORTED_AUDIO_FMTS,
    convert_audio,
    extract_audio,
)
from sunoauxtool.download.convert.engine import probe_audio_stream
from sunoauxtool.download.exceptions import TranscodeError
from sunoauxtool.download.transcoder import find_ffmpeg

pytest.importorskip("shutil", reason="标准库")
_ffmpeg = find_ffmpeg()


def _ff(*args: str) -> None:
    subprocess.run([str(_ffmpeg), "-hide_banner", "-y", *args], check=True, capture_output=True)


def _probe_codec(path: Path) -> str:
    return (probe_audio_stream(str(path)) or {}).get("codec_name", "")


def _probe_rate(path: Path) -> int:
    return int((probe_audio_stream(str(path)) or {}).get("sample_rate") or 0)


@pytest.fixture(scope="module")
def sources(tmp_path_factory) -> dict[str, Path]:
    """session 级合成四种音频源 + 一个带 AAC 音轨的 mp4。"""
    d = tmp_path_factory.mktemp("conv_sources")
    wav = d / "src.wav"
    _ff("-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ar", "44100", str(wav))
    mp3 = d / "src.mp3"
    _ff("-i", str(wav), "-c:a", "libmp3lame", "-b:a", "128k", str(mp3))
    m4a = d / "src.m4a"
    _ff("-i", str(wav), "-c:a", "aac", "-b:a", "128k", str(m4a))
    flac = d / "src.flac"
    _ff("-i", str(wav), "-c:a", "flac", str(flac))
    mp4 = d / "src.mp4"
    _ff(
        "-f", "lavfi", "-i", "testsrc=duration=1:size=128x128:rate=15",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-shortest", str(mp4),
    )
    return {"wav": wav, "mp3": mp3, "m4a": m4a, "flac": flac, "mp4": mp4}


# 音频 codec -> 目标格式 -> 期望 codec（重编码分支的 ffprobe 断言依据）
_EXPECTED_CODEC = {
    "mp3": "mp3",
    "m4a": "aac",
    "flac": "flac",
    "wav": "pcm_s16le",
}


class TestConvertAudio:
    @pytest.mark.parametrize("src_fmt", SUPPORTED_AUDIO_FMTS)
    @pytest.mark.parametrize("dst_fmt", SUPPORTED_AUDIO_FMTS)
    def test_matrix(self, sources, tmp_path, src_fmt, dst_fmt):
        out = convert_audio(sources[src_fmt], tmp_path, fmt=dst_fmt, overwrite=True)
        assert out.is_file() and out.stat().st_size > 0
        assert out.suffix == f".{dst_fmt}"
        assert _probe_codec(out) == _EXPECTED_CODEC[dst_fmt]

    def test_sample_rate_override(self, sources, tmp_path):
        out = convert_audio(sources["mp3"], tmp_path, fmt="wav", sample_rate=22050, overwrite=True)
        assert _probe_rate(out) == 22050

    def test_wav_bit_depth_24(self, sources, tmp_path):
        out = convert_audio(sources["mp3"], tmp_path, fmt="wav", bit_depth=24, overwrite=True)
        assert _probe_codec(out) == "pcm_s24le"

    def test_profile_suno(self, sources, tmp_path):
        out = convert_audio(sources["mp3"], tmp_path, profile="suno", overwrite=True)
        assert out.suffix == ".wav"
        assert _probe_rate(out) == 44100
        assert _probe_codec(out) == "pcm_s16le"

    def test_profile_web(self, sources, tmp_path):
        out = convert_audio(sources["wav"], tmp_path, profile="web", overwrite=True)
        assert out.suffix == ".mp3"
        assert _probe_codec(out) == "mp3"

    def test_explicit_overrides_profile(self, sources, tmp_path):
        out = convert_audio(sources["mp3"], tmp_path, profile="suno", fmt="flac", overwrite=True)
        assert out.suffix == ".flac"

    def test_unknown_profile_rejected(self, sources, tmp_path):
        with pytest.raises(TranscodeError, match="未知预设"):
            convert_audio(sources["mp3"], tmp_path, profile="nope")

    def test_unsupported_input_rejected(self, tmp_path):
        txt = tmp_path / "a.txt"
        txt.write_text("x")
        with pytest.raises(TranscodeError, match="不支持的输入格式"):
            convert_audio(txt, tmp_path)

    def test_missing_input_rejected(self, tmp_path):
        with pytest.raises(TranscodeError, match="输入文件不存在"):
            convert_audio(tmp_path / "nope.mp3", tmp_path)

    def test_existing_output_requires_overwrite(self, sources, tmp_path):
        convert_audio(sources["mp3"], tmp_path, fmt="wav", overwrite=True)
        with pytest.raises(TranscodeError, match="目标已存在"):
            convert_audio(sources["mp3"], tmp_path, fmt="wav", overwrite=False)

    def test_stem(self, sources, tmp_path):
        out = convert_audio(sources["mp3"], tmp_path, fmt="flac", stem="custom", overwrite=True)
        assert out.name == "custom.flac"


class TestExtractAudio:
    def test_mp4_to_mp3_reencode(self, sources, tmp_path):
        out = extract_audio(sources["mp4"], tmp_path, fmt="mp3", overwrite=True)
        assert out.suffix == ".mp3"
        assert _probe_codec(out) == "mp3"

    def test_mp4_to_m4a_copy_passthrough(self, sources, tmp_path):
        """mp4 的 AAC 音轨请求 m4a：应走 -c:a copy 直通（codec 保持 aac）。"""
        out = extract_audio(sources["mp4"], tmp_path, fmt="m4a", overwrite=True)
        assert out.suffix == ".m4a"
        assert _probe_codec(out) == "aac"

    def test_no_copy_forces_reencode(self, sources, tmp_path):
        """--no-copy 强制重编码：m4a 输出的 aac 是重编码产物（时长对齐即可）。"""
        out = extract_audio(sources["mp4"], tmp_path, fmt="m4a", overwrite=True, copy_first=False)
        assert out.is_file()
        assert _probe_codec(out) == "aac"

    def test_mp4_profile_suno(self, sources, tmp_path):
        out = extract_audio(sources["mp4"], tmp_path, profile="suno", overwrite=True)
        assert out.suffix == ".wav"
        assert _probe_codec(out) == "pcm_s16le"
        assert _probe_rate(out) == 44100

    def test_missing_video_rejected(self, tmp_path):
        with pytest.raises(TranscodeError, match="输入文件不存在"):
            extract_audio(tmp_path / "nope.mp4", tmp_path)

    def test_non_video_ext_rejected(self, sources, tmp_path):
        with pytest.raises(TranscodeError, match="不支持的视频格式"):
            extract_audio(sources["mp3"], tmp_path)  # .mp3 不是视频扩展名

    def test_no_audio_stream_rejected(self, sources, tmp_path, monkeypatch):
        """无音轨且需重编码时给出明确错误。"""
        d = sources["mp4"].parent
        silent = d / "silent.mp4"
        _ff(
            "-f", "lavfi", "-i", "testsrc=duration=0.5:size=64x64:rate=10",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(silent),
        )
        with pytest.raises(TranscodeError, match="没有音频流|输出缺失"):
            extract_audio(silent, tmp_path, fmt="mp3", overwrite=True, copy_first=False)
