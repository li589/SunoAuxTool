"""commands/helpers.py 单元测试。

覆盖规则：错误守卫（_guard）的两条异常路径 × debug 开关、WAV 时长探测的
异常回落、单次元数据落盘的开关短路、版本对比的参数提取与异常分支、
交互式提示（_prompt / _config_prompt）的全部分支、以及
_apply_detected_to_config 的 bpm 无引号写入路径。

这些函数此前只有 CLI 端到端测试间接触碰，绝大多数分支从未被执行。
"""

from __future__ import annotations

import json

import pytest
import typer

from sunoauxtool.commands import helpers
from sunoauxtool.config import Config
from sunoauxtool.exceptions import ParameterError, RenderError
from sunoauxtool.output_manager import ArtifactMeta


# -- _guard 错误守卫 --------------------------------------------------------

def test_guard_passes_through_typer_exit():
    """typer.Exit 应原样抛出，不被转成通用错误。"""

    @helpers._guard
    def boom():
        raise typer.Exit(3)

    with pytest.raises(typer.Exit) as ei:
        boom()
    assert ei.value.exit_code == 3


def test_guard_returns_value_on_success():
    """正常返回时透传结果（不吞返回值）。"""

    @helpers._guard
    def ok():
        return "done"

    assert ok() == "done"


@pytest.mark.parametrize(
    "exc, expected_code",
    [(ParameterError("参数不对"), 1), (RenderError("渲染失败"), 4)],
)
def test_guard_maps_expected_error_to_exit_code(exc, expected_code, capsys):
    """SmartNoteGenError -> typer.Exit(exc.code)，并把错误打到 stderr。"""

    @helpers._guard
    def boom():
        raise exc

    with pytest.raises(typer.Exit) as ei:
        boom()
    assert ei.value.exit_code == expected_code
    assert f"错误 [{expected_code}]" in capsys.readouterr().err


def test_guard_expected_error_debug_uses_exception_log(monkeypatch, capsys):
    """debug 打开时走 logger.exception 分支，退出码语义不变。"""
    monkeypatch.setattr(helpers, "_DEBUG_ENABLED", True)

    @helpers._guard
    def boom():
        raise ParameterError("参数不对")

    with pytest.raises(typer.Exit) as ei:
        boom()
    assert ei.value.exit_code == 1
    assert "错误 [1]" in capsys.readouterr().err


def test_guard_unexpected_error_maps_to_exit_1(capsys):
    """非预期异常 -> 退出码 1，且提示为「意外错误」。"""

    @helpers._guard
    def boom():
        raise ValueError("boom")

    with pytest.raises(typer.Exit) as ei:
        boom()
    assert ei.value.exit_code == 1
    assert "意外错误" in capsys.readouterr().err


def test_guard_unexpected_error_debug_branch(monkeypatch, capsys):
    """debug 打开时的非预期异常路径。"""
    monkeypatch.setattr(helpers, "_DEBUG_ENABLED", True)

    @helpers._guard
    def boom():
        raise ValueError("boom")

    with pytest.raises(typer.Exit) as ei:
        boom()
    assert ei.value.exit_code == 1
    assert "意外错误" in capsys.readouterr().err


def test_set_debug_toggles_flag(monkeypatch):
    """set_debug 是模块级开关，影响 _guard 的日志分支。"""
    monkeypatch.setattr(helpers, "_DEBUG_ENABLED", False)
    helpers.set_debug(True)
    assert helpers._DEBUG_ENABLED is True
    helpers.set_debug(False)
    assert helpers._DEBUG_ENABLED is False


# -- _get_duration ----------------------------------------------------------

def test_get_duration_reads_real_wav(sine_wav):
    """合法 WAV 返回四舍五入后的时长。"""
    assert helpers._get_duration(sine_wav) == pytest.approx(2.0, abs=0.01)


def test_get_duration_returns_zero_on_unreadable_path(tmp_path):
    """不可读路径不抛异常，回落 0.0（对比表格仍可打印）。"""
    assert helpers._get_duration(tmp_path / "missing.wav") == 0.0


def test_get_duration_returns_zero_on_non_wav_content(tmp_path):
    """内容非 WAV 时同样回落 0.0。"""
    bad = tmp_path / "fake.wav"
    bad.write_text("not a wav at all", encoding="utf-8")
    assert helpers._get_duration(bad) == 0.0


# -- _write_single_metadata -------------------------------------------------

def _load_cfg(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    return Config.load()


def test_write_single_metadata_skips_when_disabled(monkeypatch, tmp_path):
    """[output] metadata = false 时直接短路，不产生 metadata.json。"""
    cfg = _load_cfg(monkeypatch, tmp_path)
    cfg.output.metadata = False

    helpers._write_single_metadata(cfg, "generate midi", 42, [])

    assert list(tmp_path.rglob("metadata.json")) == []


def test_write_single_metadata_writes_run_meta(monkeypatch, tmp_path):
    """开关打开时落盘 metadata.json，且记录 run 段与 artifacts 段。"""
    cfg = _load_cfg(monkeypatch, tmp_path)
    cfg.output.metadata = True

    helpers._write_single_metadata(
        cfg, "generate melody", 7,
        [ArtifactMeta(path="a.mid", kind="midi", seed=7)],
    )

    metas = list(tmp_path.rglob("metadata.json"))
    assert len(metas) == 1
    data = json.loads(metas[0].read_text(encoding="utf-8"))
    assert data["schema_version"] == "1.0"
    assert data["run"]["command"] == "generate melody"
    assert data["run"]["seed"] == 7
    assert data["run"]["version"]
    assert [a["kind"] for a in data["artifacts"]] == ["midi"]


# -- _diff_metadata ---------------------------------------------------------

def _write_meta(directory, params):
    """在目录下写一个含 wav artifact 的 metadata.json。"""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "metadata.json").write_text(
        json.dumps({"artifacts": [{"kind": "wav", "params": params}]}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_diff_metadata_prints_changed_params(tmp_path, capsys):
    """两侧参数不同 -> 打印「参数对比」并只列变化项。"""
    d1, d2 = tmp_path / "a", tmp_path / "b"
    _write_meta(d1, {"bpm": 120, "style": "pop"})
    _write_meta(d2, {"bpm": 140, "style": "pop"})

    helpers._diff_metadata(d1 / "x.wav", d2 / "y.wav")

    out = capsys.readouterr().out
    assert "参数对比" in out
    assert "bpm" in out
    assert "120" in out and "140" in out
    assert "style" not in out


def test_diff_metadata_handles_missing_files(tmp_path, capsys):
    """两侧都没有 metadata.json -> 静默返回。"""
    helpers._diff_metadata(tmp_path / "a" / "x.wav", tmp_path / "b" / "y.wav")
    assert capsys.readouterr().out == ""


def test_diff_metadata_handles_malformed_json(tmp_path, capsys):
    """metadata.json 非法 -> 归零参数，不抛异常也不打印对比。"""
    d1, d2 = tmp_path / "a", tmp_path / "b"
    for d in (d1, d2):
        d.mkdir(parents=True, exist_ok=True)
        (d / "metadata.json").write_text("{ not json", encoding="utf-8")

    helpers._diff_metadata(d1 / "x.wav", d2 / "y.wav")

    assert "参数对比" not in capsys.readouterr().out


def test_diff_metadata_skips_non_audio_artifacts(tmp_path, capsys):
    """artifact 的 kind 非 wav/suno 时不参与参数提取。"""
    d1, d2 = tmp_path / "a", tmp_path / "b"
    for d in (d1, d2):
        d.mkdir(parents=True, exist_ok=True)
        (d / "metadata.json").write_text(
            json.dumps({"artifacts": [{"kind": "midi", "params": {"bpm": 1}}]}),
            encoding="utf-8",
        )

    helpers._diff_metadata(d1 / "x.wav", d2 / "y.wav")

    assert "参数对比" not in capsys.readouterr().out


# -- _prompt / _config_prompt ----------------------------------------------

@pytest.mark.parametrize("fn", [helpers._prompt, helpers._config_prompt])
def test_prompt_returns_default_on_empty_input(fn, monkeypatch):
    """直接回车 -> 采用默认值。"""
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "")
    assert fn("和弦", "C-G-Am-F") == "C-G-Am-F"


@pytest.mark.parametrize("fn", [helpers._prompt, helpers._config_prompt])
def test_prompt_returns_typed_value(fn, monkeypatch):
    """有输入 -> 返回去掉首尾空白的输入值。"""
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "  Am-F-C-G  ")
    assert fn("和弦", "C-G-Am-F") == "Am-F-C-G"


@pytest.mark.parametrize("fn", [helpers._prompt, helpers._config_prompt])
def test_prompt_rejects_value_outside_choices(fn, monkeypatch, capsys):
    """超出枚举范围 -> 警告并回落默认值。"""
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "zzz")
    assert fn("风格", "pop", choices=["pop", "rock"]) == "pop"
    assert "可选值" in capsys.readouterr().out


@pytest.mark.parametrize("fn", [helpers._prompt, helpers._config_prompt])
def test_prompt_accepts_value_inside_choices(fn, monkeypatch):
    """枚举内的合法值正常返回。"""
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "rock")
    assert fn("风格", "pop", choices=["pop", "rock"]) == "rock"


# -- _apply_detected_to_config ---------------------------------------------

def test_apply_detected_writes_bpm_unquoted_and_paths_quoted(tmp_path):
    """bpm 写为无引号数字，其余键写为带引号字符串；未检测到的键保持原样。"""
    cfg_file = tmp_path / "u.toml"
    cfg_file.write_text(
        '[paths]\nsoundfont = "old.sf2"\n\n[defaults]\nbpm = 100\n',
        encoding="utf-8",
    )

    helpers._apply_detected_to_config(
        cfg_file,
        {"bpm": 128, "soundfont": "D:/sf/new.sf2", "style": None},
    )

    text = cfg_file.read_text(encoding="utf-8")
    assert "bpm = 128" in text
    assert 'soundfont = "D:/sf/new.sf2"' in text
    assert text.endswith("\n")


def test_apply_detected_escapes_backslashes(tmp_path):
    """Windows 路径中的反斜杠需转义，保证 TOML 可被再次解析。"""
    cfg_file = tmp_path / "u.toml"
    cfg_file.write_text('[paths]\nsoundfont = "old.sf2"\n', encoding="utf-8")

    helpers._apply_detected_to_config(cfg_file, {"soundfont": r"C:\sf\new.sf2"})

    assert r'soundfont = "C:\\sf\\new.sf2"' in cfg_file.read_text(encoding="utf-8")


def test_apply_detected_leaves_untouched_lines_intact(tmp_path):
    """未命中的行原样保留（含注释与空行）。"""
    cfg_file = tmp_path / "u.toml"
    original = '# 注释\n\n[defaults]\nchords = "C-G-Am-F"\n'
    cfg_file.write_text(original, encoding="utf-8")

    helpers._apply_detected_to_config(cfg_file, {"bpm": 90})

    assert cfg_file.read_text(encoding="utf-8") == original
