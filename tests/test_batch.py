"""P1-3 批量生成单测：随机化 / 可复现 / 失败隔离 / 退出码 / 链式 render / 元数据。"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sunoauxtool.batch import BatchOptions, BatchRunner, write_report
from sunoauxtool.cli import app
from sunoauxtool.config import Config
from sunoauxtool.exceptions import ParameterError
from sunoauxtool.generators import procedural as proc_mod

runner = CliRunner()


def _run(tmp_path, **opts) -> BatchRunner:
    cfg = Config().merge_cli(output_dir=str(tmp_path))
    return BatchRunner(BatchOptions(**opts), config=cfg).run()


def test_batch_count_and_seed_derivation(tmp_path):
    """count=3 + seed=42 -> 3 项，seed 派生为 42*1000+i（i 为 0 基序号）。"""
    result = _run(tmp_path, count=3, seed=42)
    assert len(result.items) == 3
    assert result.failed_count == 0
    seeds = [r.seed for r in result.items]
    assert seeds == [42000, 42001, 42002]
    assert result.actual_seed == 42


def test_batch_names_unique(tmp_path):
    """5 个变体命名唯一，且至少 2 个在和弦/风格维度与其余不同（P1-3 验收 1）。"""
    result = _run(tmp_path, count=5, seed=42)
    paths = [r.midi_path for r in result.items]
    assert len(set(paths)) == 5
    keys = [(r.params["style"], r.params["chords"], r.params["rhythm_pattern"]) for r in result.items]
    base = keys[0]
    distinct = sum(1 for k in keys[1:] if k != base)
    assert distinct >= 2, f"变体维度区分不足: {keys}"


def test_batch_reproducible_same_seed(tmp_path):
    """同 seed 两次运行 -> 对应项 MIDI 字节一致（P1-3 验收 2）。"""
    cfg = Config().merge_cli(output_dir=str(tmp_path))
    r1 = BatchRunner(BatchOptions(count=3, seed=42), config=cfg).run()
    r2 = BatchRunner(BatchOptions(count=3, seed=42), config=cfg).run()
    assert r1.actual_seed == r2.actual_seed == 42
    for a, b in zip(r1.items, r2.items):
        assert a.seed == b.seed
        assert Path(a.midi_path).read_bytes() == Path(b.midi_path).read_bytes()
    # 第二次运行不覆盖（seq 递增）
    assert r1.items[0].midi_path != r2.items[0].midi_path


def test_batch_no_seed_records_actual(tmp_path, caplog):
    """无 --seed 时日志记录实际 seed（P1-3 验收 3）。"""
    import logging

    cfg = Config().merge_cli(output_dir=str(tmp_path))
    with caplog.at_level(logging.INFO, logger="sunoauxtool.batch"):
        result = BatchRunner(BatchOptions(count=2), config=cfg).run()
    assert result.actual_seed is not None
    assert any("全局种子" in r.message for r in caplog.records)


def test_batch_failure_isolation(tmp_path, monkeypatch):
    """构造 1 个失败项：整体继续，成功项不受影响（P1-3 验收 4）。"""
    real_gen = proc_mod.ProceduralGenerator.generate

    def flaky(self, request):
        if request.seed is not None and request.seed % 1000 == 2:
            raise ParameterError("模拟和弦非法", code=1)
        return real_gen(self, request)

    monkeypatch.setattr(proc_mod.ProceduralGenerator, "generate", flaky)
    result = _run(tmp_path, count=5, seed=42)
    assert result.failed_count == 1
    assert result.ok_count == 4
    failed = next(r for r in result.items if r.status == "failed")
    assert failed.index == 2  # seed 42002
    assert "模拟" in failed.error


def test_batch_count_invalid(tmp_path):
    """count<1 -> ParameterError(1)。"""
    cfg = Config().merge_cli(output_dir=str(tmp_path))
    with pytest.raises(ParameterError) as exc:
        BatchRunner(BatchOptions(count=0), config=cfg).run()
    assert exc.value.code == 1


def test_batch_export_requires_render(tmp_path):
    """--export 未带 --render -> ParameterError(1)。"""
    cfg = Config().merge_cli(output_dir=str(tmp_path))
    with pytest.raises(ParameterError):
        BatchRunner(BatchOptions(export=True), config=cfg).run()


def test_batch_metadata_written(tmp_path):
    """批次清单复用 P2-5 元数据体系（P1-3 验收 6）。"""
    _run(tmp_path, count=3, seed=42)
    metadata = list(Path(tmp_path).rglob("metadata.json"))
    assert len(metadata) == 1
    import json

    data = json.loads(metadata[0].read_text(encoding="utf-8"))
    assert data["run"]["seed"] == 42
    assert len(data["artifacts"]) == 3
    assert all(a["kind"] == "midi" for a in data["artifacts"])


# ---------------------------------------------------------------------------
# CLI 退出码 0/8/9
# ---------------------------------------------------------------------------

def test_batch_cli_exit_0(tmp_project):
    """全部成功 -> 退出码 0。"""
    result = runner.invoke(app, ["batch", "--count", "3", "--seed", "42"])
    assert result.exit_code == 0, result.output
    assert "批量完成" in result.output


def test_batch_cli_electronic_seed1_all_success(tmp_project):
    """QA 缺陷 A 回归：electronic 预设含降号和弦 'Dm-Bb-F-C'，合法 seed 必须全成功。"""
    result = runner.invoke(
        app, ["batch", "--count", "6", "--seed", "1", "--style", "electronic"]
    )
    assert result.exit_code == 0, result.output
    assert "成功 6 / 失败 0" in result.output


def test_batch_electronic_pool_flat_chords(tmp_path):
    """BatchRunner 层回归：electronic 风格池随机采样不因 'Bb' 解析失败。"""
    result = _run(tmp_path, count=6, seed=1, style="electronic")
    assert result.failed_count == 0, [
        r.error for r in result.items if r.status == "failed"
    ]
    # 采样池应覆盖过含降号的 'Dm-Bb-F-C'（seed=1 确定性下验证可解析）
    assert any("Bb" in str(r.params.get("chords", "")) for r in result.items)


def test_batch_cli_partial_failure_exit_8(tmp_project, monkeypatch):
    """部分失败 -> 退出码 8（P1-3 验收 4）。"""
    real_gen = proc_mod.ProceduralGenerator.generate

    def flaky(self, request):
        if request.seed is not None and request.seed % 1000 == 1:
            raise ParameterError("模拟失败", code=1)
        return real_gen(self, request)

    monkeypatch.setattr(proc_mod.ProceduralGenerator, "generate", flaky)
    result = runner.invoke(app, ["batch", "--count", "3", "--seed", "42"])
    assert result.exit_code == 8
    assert "部分失败" in result.output


def test_batch_cli_all_failure_exit_9(tmp_project, monkeypatch):
    """全部失败 -> 退出码 9。"""
    def always_fail(self, request):
        raise ParameterError("模拟全失败", code=1)

    monkeypatch.setattr(proc_mod.ProceduralGenerator, "generate", always_fail)
    result = runner.invoke(app, ["batch", "--count", "3", "--seed", "42"])
    assert result.exit_code == 9
    assert "全部失败" in result.output


def test_batch_cli_render_chain(tmp_project, mock_fluidsynth, mock_path_resolver, mock_dsp):
    """--render 链式真实 render（mock 二进制）：产出 WAV + 元数据（P1-3 验收 5）。"""
    result = runner.invoke(
        app, ["batch", "--count", "2", "--seed", "42", "--render", "--project", "myproj"]
    )
    assert result.exit_code == 0, result.output
    wavs = list(Path("output").rglob("*.wav"))
    assert len(wavs) == 2
    mids = list(Path("output").rglob("*.mid"))
    assert len(mids) == 2
    metadata = list(Path("output").rglob("metadata.json"))
    assert len(metadata) == 1


# ---------------------------------------------------------------------------
# 1.5.0 F4：batch 结构化报告导出（JSON/CSV）
# ---------------------------------------------------------------------------


def test_batch_report_json(tmp_path):
    """JSON 报告：汇总 + 逐项明细 + 命令回放，字段完整。"""
    result = _run(tmp_path, count=2, seed=42)
    report = write_report(result, tmp_path / "report.json")
    assert report.exists()
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["summary"] == {"total": 2, "ok": 2, "failed": 0}
    assert data["actual_seed"] == 42
    assert "batch --count 2 --seed 42" in data["command"]
    assert data["version"]
    assert len(data["items"]) == 2
    item = data["items"][0]
    assert item["status"] == "ok"
    assert item["seed"] == 42000
    assert item["midi_path"]
    assert item["chords"] and item["style"] and item["bpm"]


def test_batch_report_csv(tmp_path):
    """CSV 报告：BOM 头 + 固定列 + 逐行对应。"""
    result = _run(tmp_path, count=3, seed=42)
    report = write_report(result, tmp_path / "report.csv")
    text = report.read_text(encoding="utf-8-sig")
    rows = list(csv.DictReader(text.splitlines()))
    assert len(rows) == 3
    assert rows[0]["seed"] == "42000"
    assert rows[0]["status"] == "ok"
    assert rows[0]["midi_path"]
    assert rows[0]["chords"]


def test_batch_report_includes_failed_items(tmp_path, monkeypatch):
    """部分失败：报告完整落盘，失败项含 error 字段（报告价值正在于此）。"""
    real_gen = proc_mod.ProceduralGenerator.generate

    def flaky(self, request):
        if request.seed is not None and request.seed % 1000 == 1:
            raise ParameterError("模拟失败", code=1)
        return real_gen(self, request)

    monkeypatch.setattr(proc_mod.ProceduralGenerator, "generate", flaky)
    result = _run(tmp_path, count=3, seed=42)
    assert result.failed_count == 1
    report = write_report(result, tmp_path / "r.json")
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["summary"] == {"total": 3, "ok": 2, "failed": 1}
    failed = [i for i in data["items"] if i["status"] == "failed"]
    assert len(failed) == 1
    assert "模拟失败" in failed[0]["error"]


def test_batch_report_invalid_extension(tmp_path):
    result = _run(tmp_path, count=1, seed=42)
    with pytest.raises(ParameterError):
        write_report(result, tmp_path / "report.txt")


def test_batch_cli_report_json_dry_run(tmp_project):
    """CLI --report：dry-run 下同样产出 JSON 报告（不写 MIDI 产物）。"""
    out = tmp_project / "rep" / "batch.json"
    result = runner.invoke(
        app, ["batch", "--count", "3", "--seed", "42", "--dry-run", "--report", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data["items"]) == 3
    assert data["summary"]["ok"] == 3
    assert "批次报告" in result.output


def test_batch_cli_report_csv_partial_failure(tmp_project, monkeypatch):
    """CLI --report：部分失败（退出码 8）时 CSV 报告仍完整落盘。"""
    real_gen = proc_mod.ProceduralGenerator.generate

    def flaky(self, request):
        if request.seed is not None and request.seed % 1000 == 1:
            raise ParameterError("模拟失败", code=1)
        return real_gen(self, request)

    monkeypatch.setattr(proc_mod.ProceduralGenerator, "generate", flaky)
    out = tmp_project / "batch.csv"
    result = runner.invoke(
        app, ["batch", "--count", "3", "--seed", "42", "--report", str(out)]
    )
    assert result.exit_code == 8
    assert out.exists()
    rows = list(csv.DictReader(out.read_text(encoding="utf-8-sig").splitlines()))
    assert len(rows) == 3
    assert sum(1 for r in rows if r["status"] == "failed") == 1
