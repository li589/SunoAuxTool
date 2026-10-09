"""Suno 打包/清单测试（P3-B2 + P3-B3）。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from sunoauxtool.sunopack import build_pack, write_upload_manifest


@pytest.fixture
def sample_wavs(tmp_path):
    """生成几个测试用 WAV 文件。"""
    files = []
    for i, freq in enumerate([440, 660, 880], 1):
        t = np.linspace(0, 1, 44100, endpoint=False)
        audio = 0.5 * np.sin(2 * np.pi * freq * t)
        wav = tmp_path / f"sample_{i}.wav"
        sf.write(str(wav), audio, 44100)
        files.append(wav)
    return files


@pytest.fixture
def wavs_with_metadata(tmp_path, sample_wavs):
    """带 metadata.json 的 WAV 文件。"""
    meta = {
        "artifacts": [
            {"kind": "midi", "params": {"chords": "C-G-Am-F", "bpm": 120, "style": "pop"},
             "seed": 42},
            {"kind": "suno", "params": {"style": "pop", "bpm": 120},
             "duration_s": 10.0, "seed": 42},
        ]
    }
    meta_file = tmp_path / "metadata.json"
    meta_file.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return sample_wavs


class TestBuildPack:
    def test_build_pack_basic(self, tmp_path, sample_wavs):
        """打包：复制文件 + 生成 manifest + zip。"""
        result = build_pack(sample_wavs, tmp_path, pack_name="my_pack")
        assert result["pack_dir"].endswith("my_pack")
        assert len(result["files"]) == 3
        # 验证文件被复制
        for f in result["files"]:
            assert Path(f).is_file()
        # 验证 manifest
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        assert len(manifest) == 3
        # 验证 zip
        assert result["zip_path"] is not None
        assert Path(result["zip_path"]).is_file()

    def test_build_pack_no_zip(self, tmp_path, sample_wavs):
        """no_zip=True 时不生成 zip。"""
        result = build_pack(sample_wavs, tmp_path, make_zip=False)
        assert result["zip_path"] is None

    def test_build_pack_empty(self, tmp_path):
        """空文件列表不崩溃。"""
        result = build_pack([], tmp_path)
        assert len(result["files"]) == 0

    def test_build_pack_with_metadata(self, tmp_path, wavs_with_metadata):
        """带 metadata 时 manifest 包含元数据。"""
        result = build_pack(wavs_with_metadata, tmp_path, pack_name="meta_pack")
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        # 至少有一个条目的 meta 不为空
        has_meta = any(m.get("meta") for m in manifest)
        assert has_meta


class TestWriteManifest:
    def test_write_csv(self, tmp_path, sample_wavs):
        """CSV 清单包含表头和数据。"""
        out = tmp_path / "upload.csv"
        write_upload_manifest(sample_wavs, out, format="csv")
        content = out.read_text(encoding="utf-8-sig")
        assert "file" in content
        assert "sample_1.wav" in content

    def test_write_json(self, tmp_path, sample_wavs):
        """JSON 清单包含所有条目。"""
        out = tmp_path / "upload.json"
        write_upload_manifest(sample_wavs, out, format="json")
        data = json.loads(out.read_text(encoding="utf-8"))
        assert len(data) == 3

    def test_write_empty(self, tmp_path):
        """空列表不崩溃。"""
        out = tmp_path / "empty.csv"
        write_upload_manifest([], out)
        assert out.is_file()

    def test_write_with_metadata(self, tmp_path, wavs_with_metadata):
        """带 metadata 时清单含 style/bpm。"""
        out = tmp_path / "upload.csv"
        write_upload_manifest(wavs_with_metadata, out, format="csv")
        content = out.read_text(encoding="utf-8-sig")
        assert "pop" in content or "120" in content


class TestSunopackCLI:
    def test_export_suno_pack_help(self):
        """suno-pack --help 正常。"""
        from typer.testing import CliRunner
        from sunoauxtool.cli import app
        runner = CliRunner()
        result = runner.invoke(app, ["export", "suno-pack", "--help"])
        assert result.exit_code == 0

    def test_export_suno_manifest_help(self):
        """suno-manifest --help 正常。"""
        from typer.testing import CliRunner
        from sunoauxtool.cli import app
        runner = CliRunner()
        result = runner.invoke(app, ["export", "suno-manifest", "--help"])
        assert result.exit_code == 0