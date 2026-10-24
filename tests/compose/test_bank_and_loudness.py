"""2.1 音源库：每轨独立选库 + 分轨响度对齐。

不依赖 fluidsynth / 真实 .sf2：注册表用假 SF2，渲染用 ``FakeBackend`` 记录
"这一轨用了哪个音源库"，混音用合成正弦测响度。
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from typer.testing import CliRunner

from sunoauxtool.compose.cli import app
from sunoauxtool.compose.render import (
    DEFAULT_BANK_IDS,
    RenderOptions,
    describe_result,
    render_song,
    resolve_banks,
)
from sunoauxtool.compose.spec import SongSpec
from sunoauxtool.render.backend import RenderCapabilities, RenderRequest
from sunoauxtool.render.mixer import align_banks, align_loudness, stem_loudness
from sunoauxtool.soundbank import SoundBank, SoundBankRegistry, SoundBankError
from sunoauxtool.soundbank.registry import MIN_BYTES

runner = CliRunner()
SR = 44100

#: Linux CI 下 rich/typer 会开色，选项名还被 OptionHighlighter 拆成 "-"+"-bank"
#: 两段上色 —— 断言 help 文本前必须先剥 ANSI（1.9.1 起的老坑）
_ANSI_RE = re.compile(chr(27) + r"\[[0-9;]*m")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _fake_sf2(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    size = MIN_BYTES + 512
    path.write_bytes(b"RIFF" + (size - 8).to_bytes(4, "little") + b"sfbk"
                     + b"\x00" * (size - 12))
    return path


def _tone(path: Path, *, amp: float = 0.5, seconds: float = 1.0,
          freq: float = 440.0, sr: int = SR) -> Path:
    t = np.arange(int(sr * seconds)) / sr
    wave = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    data = np.column_stack([wave, wave])
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), data, sr)
    return path


def _spec(banks: dict[str, str] | None = None) -> SongSpec:
    tracks = []
    for track_id, bank in (banks or {}).items():
        tracks.append({"id": track_id, "role": "harmony", "bank": bank})
    if not tracks:
        tracks = [{"id": "piano", "role": "harmony"}]
    return SongSpec.from_dict({
        "schema": 1,
        "song": {"title": "bank", "key": "D minor", "tempo": 120, "seed": 3},
        "section": [{"id": "a", "bars": 1, "energy": 0.9, "progression": ["Dm"]}],
        "track": tracks,
    })


def _song(banks: dict[str, str] | None = None):
    from sunoauxtool.compose.arranger import arrange

    return arrange(_spec(banks))


class FakeBackend:
    capabilities = RenderCapabilities(name="fake")

    def __init__(self) -> None:
        self.requests: list[RenderRequest] = []

    def available(self) -> bool:
        return True

    def render(self, request: RenderRequest) -> Path:
        self.requests.append(request)
        out = request.out_path.with_suffix(".wav")
        _tone(out, amp=0.2, seconds=1.0)
        return out


# ---------------------------------------------------------------------------
# resolve_banks
# ---------------------------------------------------------------------------

class TestResolveBanks:
    def _registry(self, tmp_path: Path) -> SoundBankRegistry:
        registry = SoundBankRegistry()
        registry.add(SoundBank("gm-alt", "Alt", _fake_sf2(tmp_path / "alt.sf2")))
        return registry

    def test_default_ids_use_config_soundfont(self, tmp_path: Path) -> None:
        for value in DEFAULT_BANK_IDS:
            spec = _spec() if value == "" else _spec({"piano": value})
            result = resolve_banks(spec, self._registry(tmp_path),
                                   default_soundfont="/cfg/default.sf2")
            assert result == {"piano": "/cfg/default.sf2"}

    def test_explicit_bank_resolved(self, tmp_path: Path) -> None:
        spec = _spec({"piano": "gm-alt"})
        result = resolve_banks(spec, self._registry(tmp_path),
                               default_soundfont="/cfg/default.sf2")
        assert Path(result["piano"]).name == "alt.sf2"

    def test_mixed_banks(self, tmp_path: Path) -> None:
        spec = _spec({"piano": "gm-alt", "bass": ""})
        result = resolve_banks(spec, self._registry(tmp_path),
                               default_soundfont="/cfg/default.sf2")
        assert Path(result["piano"]).name == "alt.sf2"
        assert result["bass"] == "/cfg/default.sf2"

    def test_unknown_bank_is_41(self, tmp_path: Path) -> None:
        with pytest.raises(SoundBankError) as excinfo:
            resolve_banks(_spec({"piano": "ghost"}), self._registry(tmp_path),
                          default_soundfont="/cfg/d.sf2")
        assert excinfo.value.code == 41

    def test_broken_bank_is_41(self, tmp_path: Path) -> None:
        registry = SoundBankRegistry()
        broken = tmp_path / "broken.sf2"
        broken.write_bytes(b"nope")
        registry.add(SoundBank("broken", "Broken", broken))
        with pytest.raises(SoundBankError) as excinfo:
            resolve_banks(_spec({"piano": "broken"}), registry,
                          default_soundfont="/cfg/d.sf2")
        assert excinfo.value.code == 41
        assert "校验失败" in str(excinfo.value)

    def test_override_wins(self, tmp_path: Path) -> None:
        spec = _spec({"piano": "gm-alt", "bass": "gm-alt"})
        result = resolve_banks(spec, self._registry(tmp_path),
                               default_soundfont="/cfg/d.sf2", override="/x/one.sf2")
        assert set(result.values()) == {"/x/one.sf2"}

    def test_dedupes_registry_lookup(self, tmp_path: Path) -> None:
        """多轨共用同一 bank 时只查一次（结果一致即可）。"""
        spec = _spec({"a": "gm-alt", "b": "gm-alt"})
        result = resolve_banks(spec, self._registry(tmp_path), default_soundfont="/d")
        assert result["a"] == result["b"]


# ---------------------------------------------------------------------------
# 每轨独立渲染
# ---------------------------------------------------------------------------

class TestPerTrackBank:
    def test_each_track_uses_its_bank(self, tmp_path: Path) -> None:
        backend = FakeBackend()
        banks = {"piano": "/banks/alt.sf2", "bass": "/banks/default.sf2"}
        result = render_song(
            _song({"piano": "x", "bass": "y"}), out_dir=tmp_path, backend=backend,
            soundfont="/banks/default.sf2", bank_paths=banks,
            options=RenderOptions(align_loudness=False),
        )
        by_track = {request.meta["track"]: request.soundfont for request in backend.requests}
        assert by_track == banks
        assert result.banks == banks

    def test_missing_bank_entry_falls_back(self, tmp_path: Path) -> None:
        backend = FakeBackend()
        render_song(_song(), out_dir=tmp_path, backend=backend,
                    soundfont="/banks/default.sf2", bank_paths={},
                    options=RenderOptions(align_loudness=False))
        assert {request.soundfont for request in backend.requests} == {"/banks/default.sf2"}

    def test_describe_mentions_banks(self, tmp_path: Path) -> None:
        result = render_song(_song({"piano": "x"}), out_dir=tmp_path, backend=FakeBackend(),
                             soundfont="/s.sf2", bank_paths={"piano": "/s.sf2"})
        text = "\n".join(describe_result(result))
        assert "音源库 1 套" in text


# ---------------------------------------------------------------------------
# 响度对齐
# ---------------------------------------------------------------------------

class TestLoudness:
    def test_measure(self, tmp_path: Path) -> None:
        loud = _tone(tmp_path / "loud.wav", amp=0.5)
        quiet = _tone(tmp_path / "quiet.wav", amp=0.05)
        loud_lufs = stem_loudness(loud)
        quiet_lufs = stem_loudness(quiet)
        assert loud_lufs is not None and quiet_lufs is not None
        assert loud_lufs > quiet_lufs + 15  # 20 dB 幅度差 ≈ 20 LU

    def test_silent_and_short_return_none(self, tmp_path: Path) -> None:
        silent = tmp_path / "silent.wav"
        sf.write(str(silent), np.zeros((SR, 2), dtype=np.float32), SR)
        assert stem_loudness(silent) is None
        short = _tone(tmp_path / "short.wav", seconds=0.1)
        assert stem_loudness(short) is None

    def test_sample_rate_mismatch_skipped(self, tmp_path: Path) -> None:
        path = _tone(tmp_path / "a.wav", sr=22050)
        assert stem_loudness(path, sample_rate=44100) is None

    def test_align_brings_stems_together(self, tmp_path: Path) -> None:
        loud = _tone(tmp_path / "loud.wav", amp=0.6)
        quiet = _tone(tmp_path / "quiet.wav", amp=0.06)
        measured, applied = align_loudness(
            {"loud": loud, "quiet": quiet}, target_lufs=-18.0, max_db=40.0,
        )
        assert set(measured) == {"loud", "quiet"}
        # 应用修正后两者都应接近目标
        for name, path in (("loud", loud), ("quiet", quiet)):
            data, sr = sf.read(str(path), dtype="float32", always_2d=True)
            gain = 10 ** (applied[name] / 20.0)
            from sunoauxtool.dsp.loudness import integrated_lufs

            assert integrated_lufs(data * gain, sr) == pytest.approx(-18.0, abs=0.5)

    def test_align_banks_equalizes_levels(self) -> None:
        measured = {"a": -18.0, "b": -18.5, "c": -28.0}
        banks = {"a": "/ref.sf2", "b": "/ref.sf2", "c": "/quiet.sf2"}
        result = align_banks(measured, banks, reference_bank="/ref.sf2")
        assert result["a"] == pytest.approx(0.0)
        assert result["b"] == pytest.approx(0.0)
        assert result["c"] == pytest.approx(9.75)   # 补到基准库中位数

    def test_align_banks_single_bank_is_noop(self) -> None:
        measured = {"a": -30.0, "b": -12.0}
        banks = {"a": "/x.sf2", "b": "/x.sf2"}
        assert set(align_banks(measured, banks).values()) == {0.0}

    def test_align_banks_cap(self) -> None:
        measured = {"a": -18.0, "c": -50.0}
        banks = {"a": "/ref.sf2", "c": "/very-quiet.sf2"}
        result = align_banks(measured, banks, reference_bank="/ref.sf2", max_db=6.0)
        assert result["c"] == pytest.approx(6.0)

    def test_align_banks_falls_back_to_largest_group(self) -> None:
        measured = {"a": -20.0, "b": -20.0, "c": -26.0}
        banks = {"a": "/one.sf2", "b": "/two.sf2", "c": "/two.sf2"}
        result = align_banks(measured, banks)  # 无 reference → 以轨道多的库为基准
        # /two 两条轨的中位数 = -23（基准）；/one 的 -20 相对基准要降 3 dB
        assert result["c"] == pytest.approx(0.0)
        assert result["a"] == pytest.approx(-3.0)

    def test_align_respects_cap(self, tmp_path: Path) -> None:
        quiet = _tone(tmp_path / "quiet.wav", amp=0.0005)
        _, applied = align_loudness({"quiet": quiet}, target_lufs=-18.0, max_db=6.0)
        assert applied["quiet"] == pytest.approx(6.0)

    def test_align_skips_silent(self, tmp_path: Path) -> None:
        silent = tmp_path / "silent.wav"
        sf.write(str(silent), np.zeros((SR, 2), dtype=np.float32), SR)
        measured, applied = align_loudness({"s": silent})
        assert measured == {} and applied == {}

    def test_single_bank_needs_no_alignment(self, tmp_path: Path) -> None:
        """单库工程无需对齐（没有可比较的对象），保持既有混音意图。"""
        result = render_song(_song(), out_dir=tmp_path, backend=FakeBackend(),
                             soundfont="/s.sf2")
        assert result.loudness == {} and result.align_db == {}

    def test_two_banks_are_aligned(self, tmp_path: Path) -> None:
        class DualBackend(FakeBackend):
            """第二个库渲染得更小声（模拟 10 dB 电平差）。"""

            def render(self, request: RenderRequest) -> Path:
                self.requests.append(request)
                amp = 0.2 if request.soundfont == "/s.sf2" else 0.05
                out = request.out_path.with_suffix(".wav")
                _tone(out, amp=amp, seconds=1.0)
                return out

        result = render_song(
            _song({"piano": "gm", "bass": "alt"}),
            out_dir=tmp_path, backend=DualBackend(), soundfont="/s.sf2",
            bank_paths={"piano": "/s.sf2", "bass": "/alt.sf2"},
        )
        assert set(result.loudness) <= set(result.stems)
        assert result.align_db
        # 小声那个库应被往上补（正 dB），基准库补 0
        for name, value in result.align_db.items():
            if result.banks[name] == "/alt.sf2":
                assert value > 0
            else:
                assert value == pytest.approx(0.0)

    def test_render_can_disable_alignment(self, tmp_path: Path) -> None:
        result = render_song(
            _song({"piano": "x"}), out_dir=tmp_path, backend=FakeBackend(),
            soundfont="/s.sf2", bank_paths={"piano": "/x.sf2"},
            options=RenderOptions(align_loudness=False),
        )
        assert result.loudness == {} and result.align_db == {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCli:
    def _toml(self, tmp_path: Path, bank: str | None = None) -> Path:
        bank_line = f'\nbank = "{bank}"' if bank else ""
        path = tmp_path / "song.toml"
        path.write_text(f"""\
schema = 1

[song]
title = "bank"
key = "D minor"
tempo = 120
seed = 3

[[section]]
id = "a"
bars = 1
energy = 0.9
progression = ["Dm"]

[[track]]
id = "piano"
role = "harmony"{bank_line}
""", encoding="utf-8")
        return path

    def test_dry_run_unknown_bank_is_41(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["render", str(self._toml(tmp_path, "ghost-bank")),
                                     "-o", str(tmp_path / "o"), "--dry-run"])
        assert result.exit_code == 41, result.output

    def test_dry_run_with_bank_override_path(self, tmp_path: Path) -> None:
        sf2 = _fake_sf2(tmp_path / "custom.sf2")
        result = runner.invoke(app, [
            "render", str(self._toml(tmp_path)), "-o", str(tmp_path / "o"),
            "--bank", str(sf2), "--dry-run",
        ])
        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output

    def test_help_mentions_bank_and_align(self) -> None:
        plain = _ANSI_RE.sub("", runner.invoke(app, ["render", "--help"]).output)
        assert "--bank" in plain
        assert "--no-align-loudness" in plain
