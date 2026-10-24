"""音源库注册表（2.0 PR-G）：发现 / 查询 / 校验 + soundbank 子命令。

音源文件用「假 SF2」（RIFF+sfbk 头 + 足够体积）构造，不依赖 30MB 真库，
也不依赖 fluidsynth——本轮只测结构校验与注册表行为。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sunoauxtool.aggregate import app as aggregate_app
from sunoauxtool.cli import app as core_app
from sunoauxtool.soundbank import SoundBank, SoundBankRegistry, build_registry, slugify
from sunoauxtool.soundbank.cli import app
from sunoauxtool.soundbank.exceptions import SoundBankError
from sunoauxtool.soundbank.registry import MIN_BYTES, VerifyResult

runner = CliRunner()


def _fake_sf2(path: Path, size: int = MIN_BYTES + 1024) -> Path:
    """写一个结构合法的假 SF2（RIFF....sfbk + 填充）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"RIFF" + (size - 8).to_bytes(4, "little") + b"sfbk" + b"\x00" * (size - 12)
    path.write_bytes(payload)
    return path


class TestSlugify:
    @pytest.mark.parametrize(("text", "expected"), [
        ("GeneralUser-GS", "generaluser-gs"),
        ("ColomboGMGS2", "colombogmgs2"),
        ("My Bank (v2)", "my-bank-v2"),
        ("  __x__  ", "x"),
        ("钢琴音源", "钢琴音源"),
        ("", "bank"),
        ("!!!", "bank"),
    ])
    def test_slugify(self, text: str, expected: str) -> None:
        assert slugify(text) == expected


class TestSoundBankModel:
    def test_defaults(self, tmp_path: Path) -> None:
        bank = SoundBank("gm", "GM", tmp_path / "x.sf2")
        assert bank.format == "sf2"
        assert bank.size_bytes == 0
        assert bank.exists is False

    def test_empty_id_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(SoundBankError) as excinfo:
            SoundBank("  ", "GM", tmp_path / "x.sf2")
        assert excinfo.value.code == 41

    def test_unknown_format_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(SoundBankError, match="未知音源格式"):
            SoundBank("gm", "GM", tmp_path / "x.sf2", format="mp3")

    def test_size_and_dict(self, tmp_path: Path) -> None:
        path = _fake_sf2(tmp_path / "bank.sf2")
        bank = SoundBank("bank", "Bank", path, license="MIT")
        assert bank.exists is True
        assert bank.size_mb > 0
        payload = bank.to_dict()
        assert payload["id"] == "bank" and payload["exists"] is True
        assert payload["license"] == "MIT"


class TestVerify:
    def test_valid_sf2(self, tmp_path: Path) -> None:
        result = SoundBank("b", "B", _fake_sf2(tmp_path / "b.sf2")).verify()
        assert result.ok is True and result.code == 0
        assert "通过" in result.describe()

    def test_missing_file(self, tmp_path: Path) -> None:
        result = SoundBank("b", "B", tmp_path / "nope.sf2").verify()
        assert result.ok is False and result.code == 41
        assert "不存在" in result.message

    def test_too_small(self, tmp_path: Path) -> None:
        path = tmp_path / "tiny.sf2"
        path.write_bytes(b"RIFF\x00\x00\x00\x00sfbk")
        result = SoundBank("b", "B", path).verify()
        assert result.ok is False and "过小" in result.message

    def test_bad_magic(self, tmp_path: Path) -> None:
        path = tmp_path / "fake.sf2"
        path.write_bytes(b"XXXX" + b"\x00" * (MIN_BYTES + 8))
        result = SoundBank("b", "B", path).verify()
        assert result.ok is False and "RIFF/sfbk" in result.message

    def test_sfz_valid(self, tmp_path: Path) -> None:
        path = tmp_path / "inst.sfz"
        path.write_text("<group> sample=a.wav </group>\n<region> key=60\n", encoding="utf-8")
        result = SoundBank("inst", "Inst", path, format="sfz").verify()
        assert result.ok is True

    def test_sfz_without_regions(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.sfz"
        path.write_text("# nothing here\n", encoding="utf-8")
        result = SoundBank("empty", "Empty", path, format="sfz").verify()
        assert result.ok is False and "region" in result.message

    def test_verify_result_describe(self) -> None:
        assert VerifyResult(True, 0, "ok").describe().startswith("✅")
        assert VerifyResult(False, 41, "bad").describe().startswith("❌")


class TestRegistry:
    def test_add_and_duplicate(self, tmp_path: Path) -> None:
        registry = SoundBankRegistry()
        registry.add(SoundBank("a", "A", tmp_path / "a.sf2"))
        with pytest.raises(SoundBankError, match="已存在") as excinfo:
            registry.add(SoundBank("a", "A2", tmp_path / "a2.sf2"))
        assert excinfo.value.code == 41

    def test_discover_nested(self, tmp_path: Path) -> None:
        _fake_sf2(tmp_path / "one" / "Bank-A.sf2")
        _fake_sf2(tmp_path / "two" / "deep" / "Bank-B.sf2")
        registry = SoundBankRegistry(roots=(tmp_path,))
        found = registry.discover()
        assert [bank.id for bank in found] == ["bank-a", "bank-b"]

    def test_discover_same_stem_gets_parent_suffix(self, tmp_path: Path) -> None:
        _fake_sf2(tmp_path / "x" / "gm.sf2")
        _fake_sf2(tmp_path / "y" / "gm.sf2")
        registry = SoundBankRegistry(roots=(tmp_path,))
        ids = [bank.id for bank in registry.discover()]
        assert ids == ["gm-x", "gm-y"]

    def test_discover_missing_root(self, tmp_path: Path) -> None:
        registry = SoundBankRegistry(roots=(tmp_path / "nope",))
        assert registry.discover() == []

    def test_discover_ignores_sfz(self, tmp_path: Path) -> None:
        (tmp_path / "inst.sfz").write_text("<region>", encoding="utf-8")
        assert SoundBankRegistry(roots=(tmp_path,)).discover() == []

    def test_get_unknown(self, tmp_path: Path) -> None:
        registry = SoundBankRegistry()
        registry.add(SoundBank("known", "K", tmp_path / "k.sf2"))
        with pytest.raises(SoundBankError) as excinfo:
            registry.get("ghost")
        assert excinfo.value.code == 41
        assert "known" in str(excinfo.value)

    def test_available_and_default(self, tmp_path: Path) -> None:
        good = _fake_sf2(tmp_path / "good.sf2")
        bad = tmp_path / "bad.sf2"
        bad.write_bytes(b"nope")
        registry = SoundBankRegistry()
        registry.add(SoundBank("a-bad", "Bad", bad))
        registry.add(SoundBank("z-good", "Good", good))
        assert [bank.id for bank in registry.available()] == ["z-good"]
        assert registry.default().id == "z-good"

    def test_default_none_when_empty(self) -> None:
        assert SoundBankRegistry().default() is None

    def test_verify_all(self, tmp_path: Path) -> None:
        registry = SoundBankRegistry()
        registry.add(SoundBank("ok", "OK", _fake_sf2(tmp_path / "ok.sf2")))
        registry.add(SoundBank("no", "No", tmp_path / "no.sf2"))
        results = registry.verify_all()
        assert results["ok"].ok is True
        assert results["no"].ok is False

    def test_list_is_sorted(self, tmp_path: Path) -> None:
        registry = SoundBankRegistry()
        registry.add(SoundBank("z", "Z", tmp_path / "z.sf2"))
        registry.add(SoundBank("a", "A", tmp_path / "a.sf2"))
        assert [bank.id for bank in registry.list()] == ["a", "z"]

    def test_build_registry_with_extra(self, tmp_path: Path) -> None:
        _fake_sf2(tmp_path / "root" / "bank.sf2")
        extra = SoundBank("extra", "Extra", _fake_sf2(tmp_path / "extra.sf2"))
        registry = build_registry([tmp_path / "root"], extra=[extra])
        assert [bank.id for bank in registry.list()] == ["bank", "extra"]

    def test_license_hints(self, tmp_path: Path) -> None:
        bank = _fake_sf2(tmp_path / "GeneralUser-GS.sf2")
        registry = build_registry([tmp_path])
        assert "GeneralUser" in registry.get("generaluser-gs").license
        assert bank.exists()


class TestCli:
    def _project(self, tmp_path: Path, monkeypatch) -> Path:
        """用 SUNOAUX_MODULE_DIR 把扫描根锁到临时目录（放两套假音源）。"""
        _fake_sf2(tmp_path / "module" / "Bank-A" / "Bank-A.sf2")
        _fake_sf2(tmp_path / "module" / "Bank-B" / "Bank-B.sf2")
        monkeypatch.setenv("SUNOAUX_MODULE_DIR", str(tmp_path / "module"))
        return tmp_path

    def test_list(self, tmp_path: Path, monkeypatch) -> None:
        self._project(tmp_path, monkeypatch)
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0, result.output
        assert "bank-a" in result.output and "bank-b" in result.output
        assert "共 2 套" in result.output

    def test_list_json(self, tmp_path: Path, monkeypatch) -> None:
        self._project(tmp_path, monkeypatch)
        result = runner.invoke(app, ["list", "--json"])
        payload = json.loads(result.output)
        assert [item["id"] for item in payload] == ["bank-a", "bank-b"]
        assert all(item["verify"] is True for item in payload)

    def test_list_empty(self, tmp_path: Path, monkeypatch) -> None:
        empty = tmp_path / "module"
        empty.mkdir()
        monkeypatch.setenv("SUNOAUX_MODULE_DIR", str(empty))
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        assert "未发现任何音源" in result.output

    def test_list_explicit_bank(self, tmp_path: Path, monkeypatch) -> None:
        empty = tmp_path / "module"
        empty.mkdir()
        monkeypatch.setenv("SUNOAUX_MODULE_DIR", str(empty))
        path = _fake_sf2(tmp_path / "custom.sf2")
        result = runner.invoke(app, ["list", "--bank", str(path)])
        assert result.exit_code == 0
        assert "custom" in result.output

    def test_info(self, tmp_path: Path, monkeypatch) -> None:
        self._project(tmp_path, monkeypatch)
        result = runner.invoke(app, ["info", "bank-a"])
        assert result.exit_code == 0, result.output
        assert "bank-a" in result.output and "校验" in result.output

    def test_info_json(self, tmp_path: Path, monkeypatch) -> None:
        self._project(tmp_path, monkeypatch)
        payload = json.loads(runner.invoke(app, ["info", "bank-a", "--json"]).output)
        assert payload["id"] == "bank-a" and payload["verify"]["ok"] is True

    def test_info_unknown(self, tmp_path: Path, monkeypatch) -> None:
        self._project(tmp_path, monkeypatch)
        result = runner.invoke(app, ["info", "ghost"])
        assert result.exit_code == 41

    def test_verify_all(self, tmp_path: Path, monkeypatch) -> None:
        self._project(tmp_path, monkeypatch)
        result = runner.invoke(app, ["verify", "--all"])
        assert result.exit_code == 0, result.output
        assert "失败 0 套" in result.output

    def test_verify_single(self, tmp_path: Path, monkeypatch) -> None:
        self._project(tmp_path, monkeypatch)
        result = runner.invoke(app, ["verify", "bank-b"])
        assert result.exit_code == 0
        assert "bank-b" in result.output

    def test_verify_failure_exit_41(self, tmp_path: Path, monkeypatch) -> None:
        self._project(tmp_path, monkeypatch)
        (tmp_path / "module" / "Bank-A" / "Bank-A.sf2").write_bytes(b"broken")
        result = runner.invoke(app, ["verify", "--all"])
        assert result.exit_code == 41
        assert "失败 1 套" in result.output

    def test_verify_json(self, tmp_path: Path, monkeypatch) -> None:
        self._project(tmp_path, monkeypatch)
        payload = json.loads(runner.invoke(app, ["verify", "--all", "--json"]).output)
        assert set(payload) == {"bank-a", "bank-b"}

    def test_registered_in_both_entries(self) -> None:
        for entry in (core_app, aggregate_app):
            output = runner.invoke(entry, ["soundbank", "--help"]).output
            for command in ("list", "info", "verify"):
                assert command in output, command

    def test_doctor_lists_soundbanks(self, tmp_path: Path, monkeypatch) -> None:
        self._project(tmp_path, monkeypatch)
        result = runner.invoke(core_app, ["doctor"])
        assert "音源库" in result.output
