"""``sunoaux`` 聚合 CLI（R3）端到端测试。

覆盖三层：
- 注册结构：pre/post 组、子命令齐全、--version
- 薄转发正确性：pre *（→ sunoauxtool 既有命令）与 post *（→ downloadhelper /
  videomaker）走真实命令函数，错误码原样透传
- 后期命令：post dsp（R6 已交付：DSP 算子链）/ post enhance（R5 已交付：AudioSR 音质提升）

转发实现是「二次注册既有命令函数」——因此 mock 打在原模块
（如 ``sunoauxtool.download.cli.decode_fmp4``）依然生效：函数的
__globals__ 指向定义处模块。
"""

from __future__ import annotations

from pathlib import Path

import re

import numpy as np
import soundfile as sf
from typer.testing import CliRunner

from sunoauxtool import __version__
from sunoauxtool.aggregate import app
from sunoauxtool.download import cli as download_cli

runner = CliRunner()

SR = 22050


def _text(result) -> str:
    return result.output


def _write_melody_wav(path: Path, pitches: list[int]) -> None:
    """写一段简单旋律 WAV（复用 test_cli_analysis 的口径）。"""
    chunks = []
    for p in pitches:
        f = 440.0 * 2.0 ** ((p - 69) / 12.0)
        t = np.arange(int(0.5 * SR)) / SR
        env = np.minimum(1.0, np.minimum(t / 0.01, (0.5 - t) / 0.05))
        tone = (
            0.5 * np.sin(2 * np.pi * f * t)
            + 0.15 * np.sin(2 * np.pi * 2 * f * t)
            + 0.07 * np.sin(2 * np.pi * 3 * f * t)
        )
        chunks.append(tone * env)
    sf.write(path, np.concatenate(chunks).astype(np.float32), SR, subtype="PCM_16")


def _first_generated_midi(tmp_path: Path) -> Path:
    hits = sorted(tmp_path.glob("output/**/*.mid"))
    assert hits, "pre midi 应在 output/ 下产出 .mid"
    return hits[0]


# ---------------------------------------------------------------------------
# 结构：版本 + 分组
# ---------------------------------------------------------------------------


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    # 动态比对 __version__（曾硬编码 1.0.0，bump 到 1.3.0 时假失败）
    assert f"sunoaux {__version__}" in _text(result)


def test_pre_group_lists_five_commands():
    result = runner.invoke(app, ["pre", "--help"])
    assert result.exit_code == 0
    for name in ("melody", "midi", "score", "render", "transcribe"):
        assert name in _text(result)


def test_post_group_lists_all_capabilities():
    result = runner.invoke(app, ["post", "--help"])
    assert result.exit_code == 0
    for name in ("probe", "convert", "fetch", "video", "dsp", "enhance"):
        assert name in _text(result)


# ---------------------------------------------------------------------------
# pre：创作侧转发（真实引擎）
# ---------------------------------------------------------------------------


def test_pre_melody_generates_midi(tmp_project):
    result = runner.invoke(
        app,
        ["pre", "melody", "--key", "C major", "--chords", "C-G-Am-F",
         "--variations", "2", "--seed", "5"],
    )
    assert result.exit_code == 0, result.output
    assert "旋律 MIDI 已生成" in result.output
    assert _first_generated_midi(tmp_project).is_file()


def test_pre_melody_invalid_chords_exit_1(tmp_project):
    result = runner.invoke(app, ["pre", "melody", "--chords", "H"])
    assert result.exit_code == 1
    assert "无法解析和弦" in result.output


def test_pre_midi_generates_file(tmp_project):
    result = runner.invoke(app, ["pre", "midi", "--chords", "C-G-Am-F", "--seed", "3"])
    assert result.exit_code == 0, result.output
    assert "MIDI 已生成" in result.output
    assert _first_generated_midi(tmp_project).is_file()


def test_pre_midi_invalid_chords_exit_1(tmp_project):
    result = runner.invoke(app, ["pre", "midi", "--chords", "H"])
    assert result.exit_code == 1
    assert "无法解析和弦" in result.output


def test_pre_score_renders_svg(tmp_project):
    midi = _first_generated_midi(_run_pre_midi(tmp_project))
    result = runner.invoke(app, ["pre", "score", str(midi)])
    assert result.exit_code == 0, result.output
    svgs = list(tmp_path_svg(tmp_project))
    assert svgs, "pre score 应产出 SVG 谱面"


def _run_pre_midi(tmp_path: Path) -> Path:
    result = runner.invoke(app, ["pre", "midi", "--chords", "C-G-Am-F", "--seed", "3"])
    assert result.exit_code == 0, result.output
    return tmp_path


def tmp_path_svg(tmp_path: Path):
    return tmp_path.glob("output/**/*.svg")


def test_pre_score_invalid_key_exit_1(tmp_project):
    midi = _first_generated_midi(_run_pre_midi(tmp_project))
    result = runner.invoke(app, ["pre", "score", str(midi), "--key", "H major"])
    assert result.exit_code == 1


def test_pre_render_mock_engine(tmp_project, mock_fluidsynth, mock_path_resolver, mock_dsp):
    midi = _first_generated_midi(_run_pre_midi(tmp_project))
    result = runner.invoke(app, ["pre", "render", "--input", str(midi)])
    assert result.exit_code == 0, result.output
    wavs = list(tmp_project.glob("output/**/*.wav"))
    assert wavs, "pre render 应产出 WAV"


def test_pre_render_missing_input_exit_3(tmp_project):
    result = runner.invoke(app, ["pre", "render", "--input", "nope.mid"])
    assert result.exit_code == 3


def test_pre_transcribe(tmp_path):
    wav = tmp_path / "mel.wav"
    _write_melody_wav(wav, [72, 76, 79, 84])
    result = runner.invoke(app, ["pre", "transcribe", str(wav), "--bpm", "120"])
    assert result.exit_code == 0, result.output
    out = wav.with_suffix(".transcribed.mid")
    assert out.is_file()
    assert "转谱完成" in result.output


def test_pre_transcribe_missing_input_exit_3(tmp_path):
    result = runner.invoke(app, ["pre", "transcribe", str(tmp_path / "nope.wav")])
    assert result.exit_code == 3


# ---------------------------------------------------------------------------
# post：取回 / 转码（mock 打在 download_cli 模块上，转发后依然生效）
# ---------------------------------------------------------------------------


def test_post_probe_plaintext(fmp4_file, monkeypatch):
    monkeypatch.setattr(download_cli, "ffprobe_info", lambda f, p: {})
    result = runner.invoke(app, ["post", "probe", str(fmp4_file)])
    assert result.exit_code == 0, result.output
    assert "fmp4" in result.output
    assert "（可解码）" in result.output


def test_post_probe_encrypted_reports_cipher(encrypted_file):
    result = runner.invoke(app, ["post", "probe", str(encrypted_file)])
    assert result.exit_code == 0, result.output
    assert "强加密" in result.output


def test_post_convert_invalid_fmt_exit_2(fmp4_file):
    result = runner.invoke(app, ["post", "convert", str(fmp4_file), "--fmt", "wav"])
    assert result.exit_code == 2
    assert "opus / mp3 / both" in result.output


def test_post_convert_encrypted_exit_22(encrypted_file, tmp_path):
    result = runner.invoke(app, ["post", "convert", str(encrypted_file), "-o", str(tmp_path)])
    assert result.exit_code == 22
    assert "无密钥不可解码" in result.output


def test_post_fetch_reports_decoded_and_skipped(tmp_path, encrypted_bytes, make_fmp4, monkeypatch):
    d = tmp_path / "cache"
    d.mkdir()
    (d / "aaaa-bbbb.m4a").write_bytes(encrypted_bytes)
    (d / "c.mp3").write_bytes(make_fmp4())
    out = d / "out"
    monkeypatch.setattr(
        download_cli, "decode_fmp4", lambda f, o, **kw: [out / "c_decoded.opus"]
    )

    result = runner.invoke(app, ["post", "fetch", str(d), "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert "解码成功 1 个文件" in result.output
    assert "跳过" in result.output
    assert "aaaa-bbbb.m4a" in result.output


def test_post_fetch_empty_directory(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    result = runner.invoke(app, ["post", "fetch", str(d)])
    assert result.exit_code == 0
    assert "解码成功 0 个文件" in result.output


# ---------------------------------------------------------------------------
# post video：videomaker 转发
# ---------------------------------------------------------------------------


_ANSI_RE = re.compile(chr(27) + r"\[[0-9;]*m")


def test_post_video_render_help():
    result = runner.invoke(app, ["post", "video", "render", "--help"])
    assert result.exit_code == 0
    # Linux CI 下 rich 输出带 ANSI 样式码，先剥离再断言
    plain = _ANSI_RE.sub("", result.output)
    assert "--preset" in plain
    assert "--style" in plain


def test_post_video_render_missing_audio_fails_clean(tmp_path):
    result = runner.invoke(
        app, ["post", "video", "render", str(tmp_path / "nope.wav"), "--fps", "5"]
    )
    assert result.exit_code != 0


def test_post_video_multi_help():
    result = runner.invoke(app, ["post", "video", "multi", "--help"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# 后期命令：dsp（R6 已交付）/ enhance（R5 已交付）
# ---------------------------------------------------------------------------


def test_post_dsp_requires_ops():
    """R6 转正后 --ops 必填（缺失 -> 用法错误 2）。"""
    result = runner.invoke(app, ["post", "dsp"])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# post enhance：AudioSR 适配器（R5，推理路径 mock，不触发真实模型）
# ---------------------------------------------------------------------------


def test_post_enhance_missing_input_exit_3(tmp_path):
    """输入检查前置（不依赖 audiosr 是否就位，CI 上也可跑）。"""
    result = runner.invoke(app, ["post", "enhance", str(tmp_path / "nope.wav")])
    assert result.exit_code == 3


def test_post_enhance_dependency_unavailable_exit_6(tmp_path, monkeypatch):
    from sunoauxtool.ai.audiosr import AudioSRAdapter

    src = tmp_path / "a.wav"
    src.write_bytes(b"RIFF")  # 输入检查先行，必须真实存在才能到达依赖检查
    monkeypatch.setattr(AudioSRAdapter, "is_available", lambda self: False)
    result = runner.invoke(app, ["post", "enhance", str(tmp_path / "a.wav")])
    assert result.exit_code == 6
    assert "audiosr 不可用" in result.output


def test_post_enhance_mock_success(tmp_path, monkeypatch):
    from sunoauxtool.ai.audiosr import AudioSRAdapter

    src = tmp_path / "a.wav"
    src.write_bytes(b"RIFF")

    def fake_enhance(self, input_path, output_path):
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"RIFF")
        return str(out)

    monkeypatch.setattr(AudioSRAdapter, "is_available", lambda self: True)
    monkeypatch.setattr(AudioSRAdapter, "enhance", fake_enhance)
    result = runner.invoke(app, ["post", "enhance", str(src)])
    assert result.exit_code == 0, result.output
    assert "音质提升完成" in result.output
    assert (tmp_path / "a_enhanced.wav").is_file()


def test_post_enhance_forwards_params(tmp_path, monkeypatch):
    from sunoauxtool.ai.audiosr import AudioSRAdapter

    src = tmp_path / "b.wav"
    src.write_bytes(b"RIFF")
    seen = {}

    def fake_enhance(self, input_path, output_path):
        seen.update(model=self.model_name, seed=self.seed, steps=self.ddim_steps)
        Path(output_path).write_bytes(b"RIFF")
        return str(output_path)

    monkeypatch.setattr(AudioSRAdapter, "is_available", lambda self: True)
    monkeypatch.setattr(AudioSRAdapter, "enhance", fake_enhance)
    result = runner.invoke(
        app,
        ["post", "enhance", str(src), "--model", "speech", "--seed", "7",
         "--steps", "30", "-o", str(tmp_path / "out.wav")],
    )
    assert result.exit_code == 0, result.output
    assert seen == {"model": "speech", "seed": 7, "steps": 30}
    assert (tmp_path / "out.wav").is_file()
