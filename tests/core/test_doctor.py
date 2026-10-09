"""环境诊断测试（P3-E3）。"""

from __future__ import annotations

from typer.testing import CliRunner



from sunoauxtool.cli import _doctor_item


def test_doctor_item_format(capsys):
    """_doctor_item 输出格式正确。"""
    _doctor_item("Python", "✅ 3.12.0")
    captured = capsys.readouterr()
    assert "Python" in captured.out
    assert "✅ 3.12.0" in captured.out


def test_doctor_item_alignment(capsys):
    """不同长度的名称对齐（宽度差异不超过 2）。"""
    _doctor_item("Python", "✅ OK")
    _doctor_item("fluidsynth", "✅ OK")
    captured = capsys.readouterr()
    lines = captured.out.strip().split("\n")
    assert len(lines) == 2
    col1 = lines[0].index("✅")
    col2 = lines[1].index("✅")
    # 中文字符占 2 宽度，允许 2 字符偏差
    assert abs(col1 - col2) <= 2, f"状态图标列偏移过大: {col1} vs {col2}"


def test_doctor_via_typer():
    """doctor 子命令可通过 CLI 调用（不崩溃）。"""
    from typer.testing import CliRunner
    from sunoauxtool.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code in (0, 1, 2)
    assert "SunoAuxTool 环境诊断" in result.output
    assert "Python" in result.output

# ---------------------------------------------------------------------------
# 1.4.7 F1：doctor 新增 ffmpeg/AudioSR/basic_pitch 三项探测
# ---------------------------------------------------------------------------

_STUB = object()  # find_spec 返回值替身（只做 is not None 判断）


def _invoke_doctor(monkeypatch, find_ffmpeg=None, find_ffprobe=None,
                   audiosr_dir=None, specs=None):
    from sunoauxtool.cli import app

    if find_ffmpeg is not None:
        monkeypatch.setattr(
            "sunoauxtool.download.transcoder.find_ffmpeg", find_ffmpeg
        )
    if find_ffprobe is not None:
        monkeypatch.setattr(
            "sunoauxtool.download.transcoder._find_ffprobe", find_ffprobe
        )
    if audiosr_dir is not None or specs is not None:
        monkeypatch.setattr(
            "sunoauxtool.ai.audiosr.resolve_audiosr_dir", lambda: audiosr_dir
        )
    if specs is not None:
        import importlib.util

        monkeypatch.setattr(
            importlib.util,
            "find_spec",
            lambda name, *a, **k: _STUB if name in specs else None,
        )
    result = CliRunner().invoke(app, ["doctor"])
    return result


def test_doctor_ffmpeg_found(monkeypatch, tmp_path):
    """ffmpeg 命中：显示路径 + 版本号，ffprobe 同目录命中。"""
    result = _invoke_doctor(
        monkeypatch,
        find_ffmpeg=lambda: tmp_path / "ffmpeg",
        find_ffprobe=lambda _p: tmp_path / "ffprobe",
        audiosr_dir=None,
        specs={"torch": _STUB, "basic_pitch": _STUB, "onnxruntime": _STUB},
    )
    assert result.exit_code in (0, 1, 2), result.output
    assert "✅" in result.output
    assert str(tmp_path / "ffmpeg") in result.output
    assert str(tmp_path / "ffprobe") in result.output
    assert "basic_pitch" in result.output
    assert "推理后端: onnxruntime" in result.output


def test_doctor_ffmpeg_missing(monkeypatch, tmp_path):
    """ffmpeg 未命中：❌ + 修法提示，计入 errors（退出码 2）。"""
    from sunoauxtool.download.transcoder import FFmpegNotFoundError

    def _raise():
        raise FFmpegNotFoundError()

    result = _invoke_doctor(
        monkeypatch, find_ffmpeg=_raise, find_ffprobe=_raise,
        audiosr_dir=tmp_path,
        specs={"torch": _STUB, "audiocraft": None, "diffrhythm": None},
    )
    assert "❌ 未找到" in result.output
    assert "SUNO_FFMPEG" in result.output
    assert result.exit_code == 2  # ffmpeg 缺失是 error 级


def test_doctor_audiosr_dir_missing_hint(monkeypatch, tmp_path):
    """AudioSR 目录未就位：⚠️ + AUDIOSR_DIR 提示，不记 error。"""
    result = _invoke_doctor(
        monkeypatch, audiosr_dir=None,
        specs={"torch": _STUB, "basic_pitch": _STUB, "onnxruntime": _STUB},
    )
    assert "AUDIOSR_DIR" in result.output
    assert "AudioSR" in result.output


def test_doctor_basic_pitch_no_backend(monkeypatch, tmp_path):
    """basic_pitch 已装但四后端全缺：⚠️ + onnxruntime 修法提示。"""
    result = _invoke_doctor(
        monkeypatch, audiosr_dir=tmp_path,
        specs={"torch": _STUB, "basic_pitch": _STUB},  # 无任何推理后端
    )
    assert "无可用推理后端" in result.output
    assert "pip install onnxruntime" in result.output


def test_doctor_doctor_still_lists_new_sections(monkeypatch, tmp_path):
    """无论探测结果如何，三个新区块都在输出中。"""
    result = _invoke_doctor(monkeypatch, audiosr_dir=tmp_path)
    for name in ("ffmpeg", "ffprobe", "AudioSR", "basic_pitch"):
        assert name in result.output


def test_doctor_diffrhythm_source_mode_wording():
    """1.7.0：doctor 的 diffrhythm 项与源码目录模式口径对齐。

    DiffRhythm 走 DIFFRHYTHM_DIR / module/diffrhythm 源码模式（非 pip 包），
    不得再输出 find_spec 口径的「未安装」。
    """
    from typer.testing import CliRunner

    from sunoauxtool.cli import app

    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "diffrhythm" in result.output
    assert "源码模式就位" in result.output or "未就位" in result.output
    assert "diffrhythm  ✅ 已安装" not in result.output
