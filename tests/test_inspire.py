"""灵感库测试（P3-C1）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sunoauxtool.inspire import InspirationDB


@pytest.fixture
def db(tmp_path):
    """临时数据库。"""
    path = str(tmp_path / "test.db")
    inst = InspirationDB(path)
    inst.init_db()
    return inst


@pytest.fixture
def sample_wav(tmp_path):
    """生成一个测试用 WAV 文件。"""
    import numpy as np
    import soundfile as sf
    t = np.linspace(0, 1, 44100, endpoint=False)
    audio = 0.5 * np.sin(2 * np.pi * 440 * t)
    wav = tmp_path / "test.wav"
    sf.write(str(wav), audio, 44100)
    return wav


@pytest.fixture
def sample_metadata(tmp_path, sample_wav):
    """生成测试用 metadata.json。"""
    meta = {
        "schema_version": "1.0",
        "run": {"seed": 42, "version": "0.3.0"},
        "artifacts": [
            {
                "path": str(sample_wav),
                "kind": "suno",
                "params": {"style": "pop", "bpm": 120, "chords": "C-G-Am-F"},
                "seed": 42,
                "duration_s": 10.0,
                "sample_rate": 44100,
                "features": {"rms_db": -14.5, "peak_db": -1.0},
            }
        ],
    }
    meta_file = sample_wav.parent / "metadata.json"
    meta_file.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return sample_wav


def test_init_db(db):
    """init_db 创建表结构。"""
    import sqlite3
    conn = sqlite3.connect(db.db_path)
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    conn.close()
    assert ("inspirations",) in tables


def test_add_inspiration(db, sample_wav):
    """添加灵感返回 id。"""
    insp_id = db.add(str(sample_wav), tags="test", rating=4)
    assert insp_id > 0
    # 验证入库
    item = db.get(insp_id)
    assert item is not None
    assert item["tags"] == "test"
    assert item["rating"] == 4


def test_add_inspiration_auto_metadata(db, sample_metadata):
    """从同目录 metadata.json 自动提取元数据。"""
    insp_id = db.add(str(sample_metadata))
    item = db.get(insp_id)
    assert item is not None
    assert item["style"] == "pop"
    assert item["bpm"] == 120
    assert item["seed"] == 42
    assert item["duration_s"] == 10.0
    assert item["sample_rate"] == 44100
    assert item["rms_db"] == -14.5


def test_add_nonexistent_file(db):
    """不存在的文件抛 FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        db.add("/nonexistent/path.wav")


def test_list_all(db, sample_metadata):
    """list 返回所有条目。"""
    db.add(str(sample_metadata), tags="pop")
    db.add(str(sample_metadata), tags="rock")
    items = db.list()
    assert len(items) >= 2


def test_list_filter_style(db, sample_metadata):
    """按风格筛选。"""
    db.add(str(sample_metadata), tags="pop")  # style=pop
    items = db.list(style="pop")
    assert all(i["style"] == "pop" for i in items)


def test_list_filter_tag(db, sample_metadata):
    """按标签筛选。"""
    db.add(str(sample_metadata), tags="upbeat,pop")
    items = db.list(tag="upbeat")
    assert len(items) >= 1


def test_get_nonexistent(db):
    """不存在的 id 返回 None。"""
    assert db.get(9999) is None


def test_delete(db, sample_metadata):
    """删除灵感。"""
    insp_id = db.add(str(sample_metadata))
    assert db.delete(insp_id) is True
    assert db.get(insp_id) is None


def test_delete_nonexistent(db):
    """删除不存在的 id 返回 False。"""
    assert db.delete(9999) is False


def test_export_files(db, sample_metadata, tmp_path):
    """导出灵感文件。"""
    insp_id = db.add(str(sample_metadata))
    out_dir = tmp_path / "export"
    files = db.export_files(insp_id, str(out_dir))
    assert len(files) >= 1
    assert Path(files[0]).is_file()


def test_export_nonexistent(db):
    """导出不存在的灵感抛 ValueError。"""
    with pytest.raises(ValueError):
        db.export_files(9999, "/tmp")


def test_add_features_non_dict(db, sample_wav):
    """features 非 dict 时特征字段为 None（不崩溃）。"""
    insp_id = db.add(str(sample_wav), metadata={"features": "not-a-dict"})
    item = db.get(insp_id)
    assert item is not None
    assert item["rms_db"] is None
    assert item["peak_db"] is None


def test_add_chords_extracted(db, tmp_path):
    """从 metadata.json 提取 chords（midi 产物）。"""
    import numpy as np
    import soundfile as sf
    t = np.linspace(0, 1, 44100, endpoint=False)
    audio = 0.5 * np.sin(2 * np.pi * 440 * t)
    wav = tmp_path / "song.wav"
    sf.write(str(wav), audio, 44100)

    meta = {
        "artifacts": [
            {"kind": "midi", "params": {"chords": "C-G-Am-F", "bpm": 120, "style": "pop"},
             "seed": 7},
            {"kind": "suno", "params": {"style": "pop", "bpm": 120},
             "duration_s": 10.0, "sample_rate": 44100},
        ]
    }
    (tmp_path / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    insp_id = db.add(str(wav))
    item = db.get(insp_id)
    assert item["chords"] == "C-G-Am-F"
    assert item["style"] == "pop"
    assert item["bpm"] == 120
    assert item["seed"] == 7
    assert item["duration_s"] == 10.0


def test_list_filter_combined(db, sample_metadata):
    """组合筛选（style + tag + seed）。"""
    db.add(str(sample_metadata), tags="upbeat,pop")  # style=pop, seed=42
    items = db.list(style="pop", tag="upbeat", seed=42)
    assert len(items) >= 1
    # 不匹配的筛选
    items2 = db.list(style="rock", tag="upbeat")
    assert all(i["style"] == "rock" for i in items2)


def test_export_files_with_meta(db, sample_metadata, tmp_path):
    """导出时复制 metadata.json 和 preview.html（若存在）。"""
    insp_id = db.add(str(sample_metadata))
    # 创建 preview.html
    (sample_metadata.parent / "preview.html").write_text("<html>preview</html>", encoding="utf-8")
    out_dir = tmp_path / "export"
    files = db.export_files(insp_id, str(out_dir))
    names = [Path(f).name for f in files]
    assert "metadata.json" in names
    assert "preview.html" in names

def test_add_without_init_lazy_schema(tmp_path, sample_wav):
    """全流程验证回归（1.5.6）：全新库未显式 init 时 add/list 不再报 no such table。"""
    inst = InspirationDB(str(tmp_path / "fresh.db"))
    insp_id = inst.add(str(sample_wav), tags="x", rating=3)
    assert insp_id >= 1
    items = inst.list()
    assert len(items) == 1 and items[0]["tags"] == "x"


def test_list_without_init_lazy_schema(tmp_path):
    """全新库直接 list 返回空而不抛错。"""
    inst = InspirationDB(str(tmp_path / "fresh2.db"))
    assert inst.list() == []
