from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from . import tags

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime REAL NOT NULL,
    duration REAL NOT NULL DEFAULT 0,
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    fps REAL NOT NULL DEFAULT 0,
    codec TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    material_type TEXT NOT NULL DEFAULT '普通素材',
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    duplicate_of INTEGER REFERENCES sources(id),
    analyzed_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_sha ON sources(sha256);

CREATE TABLE IF NOT EXISTS shots (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    shot_index INTEGER NOT NULL,
    start REAL NOT NULL,
    end REAL NOT NULL,
    UNIQUE(source_id, shot_index)
);

CREATE TABLE IF NOT EXISTS clips (
    id TEXT PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    shot_id INTEGER REFERENCES shots(id) ON DELETE SET NULL,
    start REAL NOT NULL,
    end REAL NOT NULL,
    duration REAL NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    transcript TEXT NOT NULL DEFAULT '',
    search_text TEXT NOT NULL DEFAULT '',
    action_score REAL NOT NULL DEFAULT 0,
    quality_score REAL NOT NULL DEFAULT 0,
    motion_start REAL,
    motion_end REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    origin TEXT NOT NULL DEFAULT 'auto',
    reviewed INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clips_source ON clips(source_id);
CREATE INDEX IF NOT EXISTS idx_clips_status ON clips(status);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    dimension TEXT NOT NULL,
    name TEXT NOT NULL,
    canonical TEXT NOT NULL,
    UNIQUE(dimension, canonical)
);
CREATE INDEX IF NOT EXISTS idx_tags_canonical ON tags(canonical);

CREATE TABLE IF NOT EXISTS clip_tags (
    clip_id TEXT NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    confidence REAL NOT NULL DEFAULT 1.0,
    origin TEXT NOT NULL DEFAULT 'auto',
    PRIMARY KEY(clip_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_clip_tags_tag ON clip_tags(tag_id);

CREATE TABLE IF NOT EXISTS thumbnails (
    clip_id TEXT NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    position REAL NOT NULL,
    path TEXT NOT NULL,
    PRIMARY KEY(clip_id, position)
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    total INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

"""


class Database:
    """Thread-safe SQLite wrapper using one connection per thread."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        with self.connect() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库 schema 版本 {version} 高于当前程序支持的 "
                    f"{SCHEMA_VERSION}，请使用更新的版本打开: {self.path}"
                )
            conn.executescript(SCHEMA)
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(sources)")
            }
            if "category" not in columns:
                conn.execute(
                    "ALTER TABLE sources ADD COLUMN category TEXT NOT NULL DEFAULT ''"
                )
            if "material_type" not in columns:
                conn.execute(
                    "ALTER TABLE sources ADD COLUMN material_type TEXT NOT NULL DEFAULT '普通素材'"
                )
            clip_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(clips)")
            }
            if "transcript" not in clip_columns:
                conn.execute(
                    "ALTER TABLE clips ADD COLUMN transcript TEXT NOT NULL DEFAULT ''"
                )
            self._migrate_legacy_labels(conn)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()

    @staticmethod
    def _pain_to_ache(value: str) -> str:
        return str(value).replace("痛点", "疼点").replace("痛", "疼")

    @staticmethod
    def _normalize_category_label(value: str) -> str:
        value = Database._pain_to_ache(value)
        if value == "疼点呈现":
            return "疼点 / 疼点呈现"
        return value

    def _migrate_legacy_labels(self, conn: sqlite3.Connection) -> None:
        """Move the former 剧情用途 dimension into the unified 分类 dimension."""
        old_rows = conn.execute(
            "SELECT id, dimension, name, canonical FROM tags "
            "WHERE dimension='剧情用途' OR dimension='分类'"
        ).fetchall()
        for row in old_rows:
            dimension = "分类" if row["dimension"] == "剧情用途" else row["dimension"]
            canonical = self._normalize_category_label(row["canonical"])
            name = self._normalize_category_label(row["name"])
            target = conn.execute(
                "SELECT id FROM tags WHERE dimension=? AND canonical=?",
                (dimension, canonical),
            ).fetchone()
            if target is None:
                conn.execute(
                    "UPDATE tags SET dimension=?, name=?, canonical=? WHERE id=?",
                    (dimension, name, canonical, row["id"]),
                )
                target_id = int(row["id"])
            else:
                target_id = int(target["id"])
                if target_id != int(row["id"]):
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO clip_tags(clip_id, tag_id, confidence, origin)
                        SELECT clip_id, ?, confidence, origin FROM clip_tags WHERE tag_id=?
                        """,
                        (target_id, row["id"]),
                    )
                    conn.execute("DELETE FROM clip_tags WHERE tag_id=?", (row["id"],))
                    conn.execute("DELETE FROM tags WHERE id=?", (row["id"],))
            # Folder and old AI labels may both contain the old spelling.
            if dimension == "分类":
                conn.execute(
                    "UPDATE tags SET name=?, canonical=? WHERE id=?",
                    (name, canonical, target_id),
                )
        category_rows = conn.execute(
            "SELECT id, category FROM sources WHERE category<>''"
        ).fetchall()
        for row in category_rows:
            conn.execute(
                "UPDATE sources SET category=? WHERE id=?",
                (tags.clean_category_path(str(row["category"])), int(row["id"])),
            )
        conn.execute(
            "UPDATE clips SET transcript='' WHERE transcript IS NULL"
        )
        # The old broad label is no longer an AI action/category. Remove only
        # automatic guesses; manual and folder labels remain untouched.
        for dimension, canonical in (("动作", "使用产品"), ("分类", "使用产品")):
            conn.execute(
                "DELETE FROM clip_tags WHERE origin='auto' AND tag_id IN "
                "(SELECT id FROM tags WHERE dimension=? AND canonical=?)",
                (dimension, canonical),
            )
    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


def utc_now() -> float:
    return time.time()


def row_to_clip(row: sqlite3.Row) -> dict[str, Any]:
    tags: dict[str, list[str]] = {}
    raw = row["tags_json"] if "tags_json" in row.keys() else None
    if raw:
        for item in json.loads(raw):
            tags.setdefault(item["dimension"], []).append(item["name"])
    keys = row.keys()
    return {
        "id": row["id"],
        "source_id": row["source_id"],
        "shot_id": row["shot_id"],
        "path": row["path"],
        "start": round(row["start"], 3),
        "end": round(row["end"], 3),
        "duration": round(row["duration"], 3),
        "description": row["description"],
        "action_score": round(row["action_score"], 4),
        "quality_score": round(row["quality_score"], 4),
        "status": row["status"],
        "origin": row["origin"],
        "reviewed": bool(row["reviewed"]),
        "tags": tags,
        "thumbnails": [],
        "category": row["category"] if "category" in keys else "",
        "material_type": row["material_type"] if "material_type" in keys else "普通素材",
        "transcript": row["transcript"] if "transcript" in keys else "",
        "duration_source": round(row["source_duration"], 3) if "source_duration" in keys else None,
        "width": row["width"] if "width" in keys else None,
        "height": row["height"] if "height" in keys else None,
    }


def placeholders(count: int) -> str:
    return ",".join("?" for _ in range(max(1, count)))
