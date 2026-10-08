"""灵感库（P3-C1）：SQLite 存储 + 检索。

管理灵感（inspiration）的增删查改，所有数据存入项目根 sunoauxtool.db。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


DB_FILENAME = "sunoauxtool.db"

_DDL = """
CREATE TABLE IF NOT EXISTS inspirations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    kind TEXT DEFAULT 'suno',
    style TEXT,
    bpm INTEGER,
    seed INTEGER,
    chords TEXT,
    duration_s REAL,
    sample_rate INTEGER,
    rms_db REAL,
    peak_db REAL,
    spectral_centroid REAL,
    tags TEXT,
    rating INTEGER,
    created_at TEXT,
    params_json TEXT
)
"""


def _default_db_path() -> str:
    """默认数据库路径：项目根 sunoauxtool.db。"""
    return str(Path.cwd() / DB_FILENAME)


class InspirationDB:
    """灵感库（SQLite 存储）。"""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or _default_db_path()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # 懒初始化：任何操作（add/list/get/delete/export）都幂等建表，
        # 全新数据库不再依赖显式 inspire init（全流程验证发现的回归，1.5.6）。
        conn.execute(_DDL)
        return conn

    def init_db(self) -> str:
        """初始化数据库（建表）。"""
        with self._conn() as conn:
            conn.execute(_DDL)
        return self.db_path

    def add(
        self,
        wav_path: str,
        metadata: Optional[dict[str, Any]] = None,
        tags: str = "",
        rating: Optional[int] = None,
    ) -> int:
        """添加灵感。

        Args:
            wav_path: WAV 文件路径。
            metadata: 元数据字典（从 metadata.json 或同目录元数据提取）。
            tags: 用户自定义标签（逗号分隔）。
            rating: 评分 1-5。

        Returns:
            新灵感的 id。
        """
        wav = Path(wav_path).resolve()
        if not wav.is_file():
            raise FileNotFoundError(f"文件不存在: {wav}")

        # 尝试从 metadata 提取参数
        params = metadata or {}
        # 尝试从同目录 metadata.json 读取
        if not params:
            params = self._load_metadata(wav.parent)

        # 尝试从文件名提取特征（如已有预览页生成的 metadata）
        features = params.get("features", {})
        if isinstance(features, dict):
            rms_db = features.get("rms_db")
            peak_db = features.get("peak_db")
            spectral_centroid = features.get("spectral_centroid")
        else:
            rms_db = peak_db = spectral_centroid = None

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO inspirations
                   (path, kind, style, bpm, seed, chords, duration_s, sample_rate,
                    rms_db, peak_db, spectral_centroid, tags, rating, created_at, params_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(wav),
                    params.get("kind", "suno"),
                    params.get("style"),
                    params.get("bpm"),
                    params.get("seed"),
                    params.get("chords"),
                    params.get("duration_s"),
                    params.get("sample_rate"),
                    rms_db,
                    peak_db,
                    spectral_centroid,
                    tags,
                    rating,
                    now,
                    json.dumps(params, ensure_ascii=False),
                ),
            )
            return int(cur.lastrowid)

    def list(
        self,
        style: Optional[str] = None,
        tag: Optional[str] = None,
        seed: Optional[int] = None,
        date: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """列出灵感，支持筛选。

        Args:
            style: 按风格筛选。
            tag: 按标签筛选（包含该标签即匹配）。
            seed: 按种子筛选。
            date: 按日期筛选（YYYYMMDD 前缀匹配）。
            limit: 最大返回条数。
        """
        conditions: list[str] = []
        params: list[Any] = []

        if style:
            conditions.append("style = ?")
            params.append(style)
        if tag:
            conditions.append("tags LIKE ?")
            params.append(f"%{tag}%")
        if seed is not None:
            conditions.append("seed = ?")
            params.append(seed)
        if date:
            conditions.append("created_at LIKE ?")
            params.append(f"{date}%")

        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        sql = f"SELECT * FROM inspirations{where} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get(self, id: int) -> Optional[dict[str, Any]]:
        """按 id 获取灵感详情。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM inspirations WHERE id = ?", (id,)
            ).fetchone()
            return dict(row) if row else None

    def delete(self, id: int) -> bool:
        """删除灵感（仅从库中移除，不删文件）。"""
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM inspirations WHERE id = ?", (id,))
            return cur.rowcount > 0

    def export_files(self, id: int, output_dir: str | Path) -> list[str]:
        """复制灵感文件到指定目录。

        Returns:
            复制后的文件路径列表。
        """
        insp = self.get(id)
        if not insp:
            raise ValueError(f"灵感不存在: id={id}")

        src = Path(insp["path"])
        if not src.is_file():
            raise FileNotFoundError(f"灵感文件已不存在: {src}")

        out = Path(output_dir).expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)

        copied: list[str] = []
        # 复制 WAV
        dest = out / src.name
        import shutil
        shutil.copy2(str(src), str(dest))
        copied.append(str(dest))

        # 尝试复制同目录的 metadata.json
        meta_file = src.parent / "metadata.json"
        if meta_file.is_file():
            shutil.copy2(str(meta_file), str(out / "metadata.json"))
            copied.append(str(out / "metadata.json"))

        # 尝试复制同目录的 preview.html
        preview = src.parent / "preview.html"
        if preview.is_file():
            shutil.copy2(str(preview), str(out / "preview.html"))
            copied.append(str(out / "preview.html"))

        return copied

    # -- 内部 ---------------------------------------------------------------

    def _load_metadata(self, wav_dir: Path) -> dict[str, Any]:
        """从目录读取 metadata.json，提取匹配 WAV 的元数据。

        优先取 kind=midi 的产物（含完整 chords/bars），再叠加 wav/suno 的时长与特征。
        """
        meta_file = wav_dir / "metadata.json"
        if not meta_file.is_file():
            return {}
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
            artifacts = data.get("artifacts", [])
            params: dict[str, Any] = {}
            # 优先：midi 产物的完整参数（chords/bpm/bars/style）
            for art in artifacts:
                if art.get("kind") == "midi":
                    params.update(art.get("params", {}))
                    params["seed"] = art.get("seed")
                    break
            # 再叠加：wav/suno 产物的参数 + 时长/采样率/特征
            for art in artifacts:
                if art.get("kind") in ("wav", "suno"):
                    params.update(art.get("params", {}))  # 合并 style/bpm 等
                    params["duration_s"] = art.get("duration_s", 0)
                    params["sample_rate"] = art.get("sample_rate", 0)
                    if params.get("seed") is None:
                        params["seed"] = art.get("seed")
                    params["features"] = art.get("features", {})
                    break
            return params
        except (json.JSONDecodeError, OSError):
            pass
        return {}