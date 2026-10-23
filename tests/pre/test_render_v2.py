"""渲染 v2（2.0 PR-E）：后端协议 + 分轨混音 + compose.render 编排。

三层各测一遍：

1. ``render/backend.py``  —— 能力自述、可用性探测、不可用时抛 42；
2. ``render/mixer.py``    —— 通道条（增益/声像/静音/独奏）、峰值归一、空测 ≤ −60 dBFS；
3. ``compose/render.py``  —— 假后端驱动的端到端编排（单轨 MIDI → stem → mix）。

真实 fluidsynth 渲染不在单测里跑（CI 有 fluidsynth 但慢且依赖 LFS 音色库），
用 ``FakeBackend`` 写静音/单音 WAV 来验证流程与产物结构。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from sunoauxtool.compose.render import (
    RenderOptions,
    backend_summary,
    describe_result,
    render_song,
    slice_document,
)
from sunoauxtool.compose.spec import SongSpec
from sunoauxtool.exceptions import SmartNoteGenError
from sunoauxtool.render.backend import (
    FluidSynthBackend,
    RenderCapabilities,
    RenderRequest,
    capabilities_of,
)
from sunoauxtool.render.exceptions import BackendUnavailableError, MixdownError
from sunoauxtool.render.mixer import (
    StemStrip,
    db_to_gain,
    mixdown,
    pan_gains,
    peak_of,
    strips_from_song,
)

SR = 44100


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _tone(path: Path, *, freq: float = 440.0, amp: float = 0.5, seconds: float = 0.2,
          channels: int = 2, sr: int = SR) -> Path:
    t = np.arange(int(sr * seconds)) / sr
    wave = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    data = np.column_stack([wave] * channels) if channels > 1 else wave
    sf.write(str(path), data, sr)
    return path


def _silence(path: Path, *, seconds: float = 0.2, sr: int = SR) -> Path:
    sf.write(str(path), np.zeros((int(sr * seconds), 2), dtype=np.float32), sr)
    return path


def _read(path: Path) -> np.ndarray:
    data, _ = sf.read(str(path), always_2d=True, dtype="float32")
    return data


class FakeBackend:
    """测试替身：把请求写成一个标记音高的正弦（证明 MIDI 被按轨切片）。"""

    capabilities = RenderCapabilities(name="fake", supports_stems=True)

    def __init__(self, *, freq: float = 440.0, fail: bool = False) -> None:
        self.freq = freq
        self.fail = fail
        self.requests: list[RenderRequest] = []

    def available(self) -> bool:
        return not self.fail

    def render(self, request: RenderRequest) -> Path:
        self.requests.append(request)
        if self.fail:
            raise BackendUnavailableError("假后端不可用", code=42)
        out = request.out_path.with_suffix(".wav")
        out.parent.mkdir(parents=True, exist_ok=True)
        return _tone(out, freq=self.freq, amp=0.2, seconds=0.2)


def _song(seed: int = 7):
    spec = SongSpec.from_dict({
        "schema": 1,
        "song": {"title": "渲染测试", "key": "D minor", "tempo": 120, "seed": seed},
        "section": [{"id": "a", "bars": 1, "energy": 0.9, "progression": ["Dm"]}],
        "track": [
            {"id": "piano", "role": "harmony", "mix": {"gain_db": -6.0, "pan": -0.3}},
            {"id": "bass", "role": "bass", "mix": {"gain_db": -3.0, "pan": 0.1}},
            {"id": "drums", "role": "drums"},
        ],
    })
    from sunoauxtool.compose.arranger import arrange

    return arrange(spec)


# ---------------------------------------------------------------------------
# 1. 后端协议
# ---------------------------------------------------------------------------

class TestCapabilities:
    def test_describe(self) -> None:
        text = RenderCapabilities(name="fluidsynth", supports_multi_bank=True).describe()
        assert "fluidsynth" in text and "多音源库" in text and "分轨" in text

    def test_describe_single_bank(self) -> None:
        assert "单音源库" in RenderCapabilities().describe()

    def test_capabilities_of_fallback(self) -> None:
        assert capabilities_of(object()).name == "fluidsynth"

    def test_capabilities_of_backend(self) -> None:
        assert capabilities_of(FluidSynthBackend(fluidsynth_path="x")).name == "fluidsynth"

    def test_backend_summary(self) -> None:
        assert "分轨" in backend_summary(FakeBackend())


class TestFluidSynthBackend:
    def test_available_false_when_missing(self, tmp_path: Path) -> None:
        backend = FluidSynthBackend(str(tmp_path / "nope.exe"))
        assert backend.available() is False

    def test_require_raises_42(self, tmp_path: Path) -> None:
        backend = FluidSynthBackend(str(tmp_path / "nope.exe"))
        with pytest.raises(BackendUnavailableError) as excinfo:
            backend.require()
        assert excinfo.value.code == 42
        assert isinstance(excinfo.value, SmartNoteGenError)

    def test_available_true_on_executable(self, tmp_path: Path) -> None:
        exe = tmp_path / ("fake.bat" if __import__("os").name == "nt" else "fake")
        exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        exe.chmod(0o755)
        assert FluidSynthBackend(str(exe)).available() is True


# ---------------------------------------------------------------------------
# 2. 混音
# ---------------------------------------------------------------------------

class TestGains:
    def test_db_to_gain(self) -> None:
        assert db_to_gain(0) == pytest.approx(1.0)
        assert db_to_gain(-6) == pytest.approx(0.5012, abs=1e-3)
        assert db_to_gain(6) == pytest.approx(1.9953, abs=1e-3)

    def test_pan_center_passes_through(self) -> None:
        """立体声 balance：中置不衰减（不是等功率的 −3 dB）。"""
        assert pan_gains(0.0) == (1.0, 1.0)

    def test_pan_fades_one_side(self) -> None:
        assert pan_gains(-0.5) == (1.0, 0.5)
        assert pan_gains(0.25) == (0.75, 1.0)

    def test_pan_extremes(self) -> None:
        assert pan_gains(-1.0)[0] == pytest.approx(1.0)
        assert pan_gains(-1.0)[1] == pytest.approx(0.0, abs=1e-6)
        assert pan_gains(1.0)[0] == pytest.approx(0.0, abs=1e-6)

    def test_pan_clamped(self) -> None:
        assert pan_gains(-5.0) == pan_gains(-1.0)
        assert pan_gains(5.0) == pan_gains(1.0)


class TestMixdown:
    def test_sum_of_stems(self, tmp_path: Path) -> None:
        a = _tone(tmp_path / "a.wav", freq=440.0, amp=0.3)
        b = _tone(tmp_path / "b.wav", freq=220.0, amp=0.2)
        result = mixdown(
            [StemStrip("a", a), StemStrip("b", b)],
            tmp_path / "mix.wav", sample_rate=SR, blocksize=2048,
            normalize=False, subtype="FLOAT",
        )
        mixed = _read(result.path)
        expected = _read(a) + _read(b)
        assert mixed.shape == expected.shape
        assert np.max(np.abs(mixed - expected)) < 1e-6
        assert result.frames == expected.shape[0]
        assert result.stems == ["a", "b"]

    def test_gain_and_pan_applied(self, tmp_path: Path) -> None:
        stem = _tone(tmp_path / "a.wav", amp=0.5)
        result = mixdown(
            [StemStrip("a", stem, gain_db=-6.0, pan=-1.0)],
            tmp_path / "mix.wav", sample_rate=SR, blocksize=4096,
            normalize=False, subtype="FLOAT",
        )
        mixed = _read(result.path)
        assert np.max(np.abs(mixed[:, 0])) == pytest.approx(0.5 * db_to_gain(-6), rel=1e-3)
        assert np.max(np.abs(mixed[:, 1])) < 1e-6

    def test_mute_and_solo(self, tmp_path: Path) -> None:
        a = _tone(tmp_path / "a.wav", freq=440.0, amp=0.4)
        b = _tone(tmp_path / "b.wav", freq=220.0, amp=0.4)
        muted = mixdown(
            [StemStrip("a", a, mute=True), StemStrip("b", b)],
            tmp_path / "m1.wav", sample_rate=SR, blocksize=4096,
            normalize=False, subtype="FLOAT",
        )
        assert np.max(np.abs(_read(muted.path) - _read(b))) < 1e-6

        soloed = mixdown(
            [StemStrip("a", a), StemStrip("b", b, solo=True)],
            tmp_path / "m2.wav", sample_rate=SR, blocksize=4096,
            normalize=False, subtype="FLOAT",
        )
        assert np.max(np.abs(_read(soloed.path) - _read(b))) < 1e-6
        assert soloed.stems == ["b"]

    def test_all_muted_writes_silence(self, tmp_path: Path) -> None:
        a = _tone(tmp_path / "a.wav", amp=0.4)
        result = mixdown([StemStrip("a", a, mute=True)], tmp_path / "mix.wav",
                         sample_rate=SR, blocksize=4096)
        assert result.peak == 0.0
        assert np.max(np.abs(_read(result.path))) == 0.0
        assert result.frames > 0

    def test_silent_input_stays_silent(self, tmp_path: Path) -> None:
        """空测：静音分轨混音后必须精确为 0（≤ −60 dBFS 的硬指标）。"""
        silent = _silence(tmp_path / "s.wav")
        result = mixdown([StemStrip("s", silent)], tmp_path / "mix.wav",
                         sample_rate=SR, blocksize=4096, subtype="FLOAT")
        peak = float(np.max(np.abs(_read(result.path))))
        assert peak == 0.0
        assert 20 * np.log10(max(peak, 1e-12)) <= -60

    def test_normalize_hits_target_peak(self, tmp_path: Path) -> None:
        stem = _tone(tmp_path / "a.wav", amp=0.1)
        result = mixdown([StemStrip("a", stem)], tmp_path / "mix.wav",
                         sample_rate=SR, blocksize=4096, target_peak=0.9,
                         subtype="FLOAT")
        assert result.peak == pytest.approx(0.9, rel=1e-3)
        assert result.gain == pytest.approx(9.0, rel=1e-2)
        assert pytest.approx(float(np.max(np.abs(_read(result.path)))), rel=1e-3) == 0.9

    def test_normalize_cap(self, tmp_path: Path) -> None:
        """极安静的分轨不被放大成爆音（增益上限 64×）。"""
        stem = _tone(tmp_path / "a.wav", amp=1e-4)
        result = mixdown([StemStrip("a", stem)], tmp_path / "mix.wav",
                         sample_rate=SR, blocksize=4096)
        assert result.gain == pytest.approx(64.0)

    def test_mismatched_sample_rate(self, tmp_path: Path) -> None:
        stem = _tone(tmp_path / "a.wav", sr=22050)
        with pytest.raises(MixdownError) as excinfo:
            mixdown([StemStrip("a", stem)], tmp_path / "mix.wav", sample_rate=SR)
        assert excinfo.value.code == 43

    def test_missing_stem(self, tmp_path: Path) -> None:
        with pytest.raises(MixdownError, match="无法打开分轨"):
            mixdown([StemStrip("a", tmp_path / "nope.wav")], tmp_path / "mix.wav")

    def test_peak_of(self, tmp_path: Path) -> None:
        stem = _tone(tmp_path / "a.wav", amp=0.37)
        assert peak_of(stem, blocksize=1024) == pytest.approx(0.37, rel=1e-3)

    def test_blocksize_independence(self, tmp_path: Path) -> None:
        """分块大小不影响结果（流式实现的正确性）。"""
        a = _tone(tmp_path / "a.wav", amp=0.3, seconds=0.3)
        outputs = []
        for index, size in enumerate((1024, 8192, 100000)):
            result = mixdown([StemStrip("a", a)], tmp_path / f"mix{index}.wav",
                             sample_rate=SR, blocksize=size,
                             normalize=False, subtype="FLOAT")
            outputs.append(_read(result.path))
        assert np.max(np.abs(outputs[0] - outputs[1])) < 1e-6
        assert np.max(np.abs(outputs[0] - outputs[2])) < 1e-6

    def test_mono_stem_is_duplicated(self, tmp_path: Path) -> None:
        mono = _tone(tmp_path / "m.wav", amp=0.25, channels=1)
        result = mixdown([StemStrip("m", mono)], tmp_path / "mix.wav",
                         sample_rate=SR, blocksize=4096,
                         normalize=False, subtype="FLOAT")
        mixed = _read(result.path)
        assert mixed.shape[1] == 2
        assert np.max(np.abs(mixed[:, 0] - mixed[:, 1])) < 1e-6

    def test_empty_strip_list_writes_silence(self, tmp_path: Path) -> None:
        silent = _silence(tmp_path / "s.wav")
        result = mixdown([StemStrip("s", silent, mute=True)], tmp_path / "mix.wav",
                         sample_rate=SR)
        assert result.frames > 0 and result.peak == 0.0

    def test_flac_output(self, tmp_path: Path) -> None:
        stem = _tone(tmp_path / "a.wav", amp=0.2)
        result = mixdown([StemStrip("a", stem)], tmp_path / "mix.flac",
                         sample_rate=SR, blocksize=4096, subtype="PCM_16")
        assert result.path.suffix == ".flac"
        assert result.path.exists()


# ---------------------------------------------------------------------------
# 3. compose.render 编排
# ---------------------------------------------------------------------------

class TestSliceDocument:
    def test_slice_keeps_conductor(self) -> None:
        document = _song().to_midi()
        sliced = slice_document(document, 1)
        assert len(sliced.tracks) == 1
        assert sliced.tracks[0].name == document.tracks[1].name
        assert sliced.tempo_map == document.tempo_map
        assert sliced.markers == document.markers
        assert sliced.ppq == document.ppq
        assert sliced.end_beat == document.end_beat

    def test_slice_out_of_range(self) -> None:
        document = _song().to_midi()
        with pytest.raises(MixdownError) as excinfo:
            slice_document(document, 99)
        assert excinfo.value.code == 43


class TestRenderSong:
    def test_full_render(self, tmp_path: Path) -> None:
        song = _song()
        backend = FakeBackend()
        result = render_song(song, out_dir=tmp_path, backend=backend,
                             soundfont="dummy.sf2")
        assert result.mix is not None and result.mix.exists()
        assert set(result.stems) == {"piano", "bass", "drums"}
        assert set(result.midi) == {"piano", "bass", "drums"}
        assert len(backend.requests) == 3
        assert all((tmp_path / "midi" / f"{name}.mid").exists() for name in result.stems)
        assert result.mix_info is not None and result.mix_info.peak > 0

    def test_each_stem_is_single_track(self, tmp_path: Path) -> None:
        from sunoauxtool.midi.io import read_smf

        song = _song()
        render_song(song, out_dir=tmp_path, backend=FakeBackend(), soundfont="x")
        for name in ("piano", "bass", "drums"):
            document = read_smf(tmp_path / "midi" / f"{name}.mid")
            assert len(document.tracks) == 1
            assert document.markers  # 指挥轨信息保留

    def test_no_stems_skips_mix(self, tmp_path: Path) -> None:
        result = render_song(_song(), out_dir=tmp_path, backend=FakeBackend(),
                             soundfont="x", options=RenderOptions(stems=False))
        assert result.mix is None
        assert result.stems == {}
        assert len(result.midi) == 3

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        backend = FakeBackend()
        result = render_song(_song(), out_dir=tmp_path, backend=backend,
                             soundfont="x", options=RenderOptions(dry_run=True))
        assert result.dry_run is True
        assert backend.requests == []
        assert not (tmp_path / "midi").exists()
        assert not (tmp_path / "stems").exists()
        assert result.mix is not None and not result.mix.exists()

    def test_backend_failure_surfaces_42(self, tmp_path: Path) -> None:
        with pytest.raises(BackendUnavailableError) as excinfo:
            render_song(_song(), out_dir=tmp_path, backend=FakeBackend(fail=True),
                        soundfont="x")
        assert excinfo.value.code == 42

    def test_bad_format(self, tmp_path: Path) -> None:
        with pytest.raises(MixdownError, match="不支持的音频格式"):
            render_song(_song(), out_dir=tmp_path, backend=FakeBackend(), soundfont="x",
                        options=RenderOptions(fmt="mp3"))

    def test_flac_mix_with_wav_stems(self, tmp_path: Path) -> None:
        """`--format` 只作用于混音成品；分轨恒为 WAV（fluidsynth 输出限制）。"""
        result = render_song(_song(), out_dir=tmp_path, backend=FakeBackend(),
                             soundfont="x", options=RenderOptions(fmt="flac"))
        assert all(path.suffix == ".wav" for path in result.stems.values())
        assert result.mix is not None and result.mix.suffix == ".flac"

    def test_deterministic_render_bytes(self, tmp_path: Path) -> None:
        song = _song()
        first = render_song(song, out_dir=tmp_path / "a", backend=FakeBackend(),
                            soundfont="x")
        second = render_song(song, out_dir=tmp_path / "b", backend=FakeBackend(),
                             soundfont="x")
        assert first.mix.read_bytes() == second.mix.read_bytes()

    def test_describe_result(self, tmp_path: Path) -> None:
        result = render_song(_song(), out_dir=tmp_path, backend=FakeBackend(),
                             soundfont="x")
        text = "\n".join(describe_result(result))
        assert "混音" in text and "分轨 3 条" in text and "输出目录" in text

    def test_strips_from_song(self, tmp_path: Path) -> None:
        song = _song()
        stems = {track.id: _tone(tmp_path / f"{track.id}.wav") for track in song.tracks}
        strips = strips_from_song(song, stems)
        assert [strip.name for strip in strips] == ["piano", "bass", "drums"]
        assert strips[0].gain_db == -6.0
        assert strips[0].pan == pytest.approx(-0.3)

    def test_strips_skip_missing_stem(self) -> None:
        song = _song()
        assert strips_from_song(song, {}) == []


class TestMixerExtras:
    def test_strip_to_dict(self) -> None:
        strip = StemStrip("piano", Path("x.wav"), gain_db=-6.0, pan=-0.3,
                          mute=True, solo=False, reverb_send=0.2)
        payload = strip.to_dict()
        assert payload["name"] == "piano" and payload["gain_db"] == -6.0
        assert payload["reverb_send"] == 0.2

    def test_mix_result_duration(self, tmp_path: Path) -> None:
        stem = _tone(tmp_path / "a.wav", seconds=0.25)
        result = mixdown([StemStrip("a", stem)], tmp_path / "mix.wav",
                         sample_rate=SR, blocksize=4096)
        assert result.duration == pytest.approx(0.25, abs=1e-3)

    def test_stems_of_different_length_are_padded(self, tmp_path: Path) -> None:
        """尾块长度不一致时按最长补零，不报错也不截断。"""
        long = _tone(tmp_path / "long.wav", amp=0.2, seconds=0.30)
        short = _tone(tmp_path / "short.wav", amp=0.2, seconds=0.21)
        result = mixdown([StemStrip("long", long), StemStrip("short", short)],
                         tmp_path / "mix.wav", sample_rate=SR, blocksize=4096,
                         normalize=False, subtype="FLOAT")
        mixed = _read(result.path)
        assert len(mixed) == len(_read(long))
        # 短轨结束后仍有长轨的信号（不被截断）
        assert np.max(np.abs(mixed[-1000:])) > 0

    def test_write_failure_reports_43(self, tmp_path: Path) -> None:
        """输出目录创建失败（父级是普通文件）→ 43。"""
        stem = _tone(tmp_path / "a.wav")
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        with pytest.raises(MixdownError) as excinfo:
            mixdown([StemStrip("a", stem)], blocker / "mix.wav", sample_rate=SR)
        assert excinfo.value.code == 43

    def test_describe_mix(self, tmp_path: Path) -> None:
        from sunoauxtool.render.mixer import describe_mix

        stem = _tone(tmp_path / "a.wav")
        result = mixdown([StemStrip("a", stem)], tmp_path / "mix.wav",
                         sample_rate=SR, blocksize=4096)
        text = "\n".join(describe_mix(result))
        assert "混音 mix.wav" in text and "1 轨" in text
