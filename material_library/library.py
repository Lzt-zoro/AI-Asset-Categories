"""Ingest, analysis and query orchestration."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from . import tags
from .config import VIDEO_EXTENSIONS, Settings
from .db import Database, row_to_clip, utc_now
from .labeling import ClipContext, LabelProvider, LabelingError, build_provider
from .media import (
    MediaError,
    detect_shots,
    extract_frame,
    probe,
    sample_frames,
)
from .motion import compute_signal, find_candidates

Progress = Callable[[str, int, int, str], None]


class LibraryError(RuntimeError):
    pass


def file_sha256(path: Path, block: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(block), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clip_id_for(source_sha: str, start: float, end: float) -> str:
    key = f"{source_sha.lower()}:{round(start * 1000)}:{round(end * 1000)}"
    return "clip_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


def iter_videos(root: Path, recursive: bool = True) -> Iterable[Path]:
    if not recursive:
        for path in sorted(root.iterdir()):
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
                yield path
        return
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            path = Path(current) / name
            if path.suffix.lower() in VIDEO_EXTENSIONS and not name.startswith("."):
                yield path


def category_for(
    path: Path, root: Path, aliases: dict[str, str] | None = None
) -> str:
    """Folder path relative to the scanned root, e.g. ``痛点使用 / 痛点涂抹``.

    The client already curates its library by folder, so the folder path is a
    free, zero-token category label that does not depend on the model.
    """
    try:
        relative = path.resolve().parent.relative_to(Path(root).resolve())
    except (ValueError, OSError):
        return ""
    parts = [part for part in relative.parts if part not in {".", ""}]
    return tags.clean_category_path(" / ".join(parts), aliases)


def _parse_clock(value: str) -> float:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    try:
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        return float(value)
    except ValueError:
        return 0.0


def sidecar_transcript(path: Path, start: float, end: float) -> str:
    """Read optional SRT/VTT/ASS text overlapping one time interval."""
    for suffix in (".srt", ".vtt", ".ass"):
        subtitle = path.with_suffix(suffix)
        if not subtitle.is_file():
            continue
        try:
            lines = subtitle.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            continue
        items: list[str] = []
        if suffix == ".ass":
            for line in lines:
                if not line.lower().startswith("dialogue:"):
                    continue
                fields = line.split(",", 9)
                if len(fields) < 10:
                    continue
                item_start = _parse_clock(fields[1])
                item_end = _parse_clock(fields[2])
                if item_start < end and item_end > start:
                    text = re.sub(r"\{[^}]*\}", "", fields[9]).replace("\\N", " ")
                    text = text.strip()
                    if text and text not in items:
                        items.append(text)
            if items:
                return " ".join(items)[:1000]
            continue
        current_start = current_end = None
        current_text: list[str] = []
        def flush() -> None:
            if current_start is None or current_end is None:
                return
            if current_start < end and current_end > start:
                text = " ".join(item.strip() for item in current_text if item.strip())
                if text and text not in items:
                    items.append(text)
        for line in lines + [""]:
            text = line.strip()
            if " --> " in text:
                flush()
                left, right = text.split(" --> ", 1)
                current_start = _parse_clock(left.split()[0])
                current_end = _parse_clock(right.split()[0])
                current_text = []
            elif not text:
                flush()
                current_start = current_end = None
                current_text = []
            elif current_start is not None:
                if not text.isdigit() and not text.startswith("WEBVTT"):
                    current_text.append(text)
        if items:
            return " ".join(items)[:1000]
    return ""


def _tokenize(text: str) -> list[str]:
    return [token for token in re.split(r"[\s,，、;；/|\-]+", text.lower()) if token]


def _search_concepts(text: str, category_names: Iterable[str] = ()) -> list[str]:
    """Find canonical tags inside continuous Chinese search text.

    This keeps common combinations such as ``涂抹祛痘霜`` local and
    deterministic. Semantic embeddings can be added later without changing
    the query contract.
    """
    compact = re.sub(r"[\s,，、;；/|\-]+", "", str(text)).lower()
    if not compact:
        return []
    candidates: list[tuple[str, str]] = []
    for dimension, entries in tags.VOCABULARY.items():
        for canonical, aliases in entries.items():
            for value in [canonical, *aliases]:
                needle = re.sub(r"[\s,，、;；/|\-]+", "", value).lower()
                if needle and needle in compact:
                    candidates.append((needle, canonical))
    for category in category_names:
        canonical = tags.clean_category_path(category)
        for value in (canonical, *canonical.split(" / ")):
            needle = re.sub(r"[\s,，、;；/|\-]+", "", value).lower()
            if needle and needle in compact:
                candidates.append((needle, value))
    # Query aliases such as 痛点 should still find the canonical 疼点 labels.
    for alias, canonical in tags.DEFAULT_CATEGORY_ALIASES.items():
        if alias.lower() in compact:
            candidates.append((alias.lower(), canonical))
    matches = sorted(candidates, key=lambda item: (-len(item[0]), compact.find(item[0])))
    result: list[str] = []
    occupied: list[tuple[int, int]] = []
    for needle, canonical in matches:
        start = compact.find(needle)
        end = start + len(needle)
        if any(start < right and end > left for left, right in occupied):
            continue
        occupied.append((start, end))
        if canonical not in result:
            result.append(canonical)
    return result or _tokenize(text)


@dataclass
class SearchQuery:
    keyword: str = ""
    tags: list[tuple[str, str]] | None = None
    categories: list[str] | None = None
    min_duration: float | None = None
    max_duration: float | None = None
    status: str | None = None
    material_type: str | None = None
    source_id: int | None = None
    limit: int = 40
    offset: int = 0


class Library:
    def __init__(self, settings: Settings, provider: LabelProvider | None = None) -> None:
        settings.ensure_dirs()
        self.settings = settings
        self.db = Database(settings.db_path)
        self.provider = provider or build_provider(settings.ai)
        self._write_lock = threading.Lock()
        with self.db.transaction() as conn:
            tags.ensure_vocabulary(conn)
            # A previous process may have died mid-analysis. Those rows would
            # otherwise never be picked up again because we only scan
            # 'pending'/'failed'.
            conn.execute(
                "UPDATE sources SET status='pending' WHERE status='analyzing'"
            )
        # Backfill derived tags after schema/taxonomy migrations. This also
        # clears stale folder labels when a source moved to the root.
        for row in self.db.connect().execute(
            "SELECT DISTINCT s.id, s.category FROM sources s JOIN clips c ON c.source_id=s.id"
        ).fetchall():
            self._sync_source_category(int(row["id"]), str(row["category"] or ""))
        for row in self.db.connect().execute("SELECT id FROM clips").fetchall():
            self._refresh_search_text(str(row["id"]))

    # ---------------------------------------------------------------- ingest
    def scan(
        self,
        root: Path,
        *,
        recursive: bool = True,
        material_type: str = "普通素材",
        progress: Progress | None = None,
    ) -> dict[str, Any]:
        root = Path(root).resolve()
        if root.is_file():
            if root.suffix.lower() not in VIDEO_EXTENSIONS:
                raise LibraryError(f"不是支持的视频文件: {root}")
            files = [root]
        elif root.is_dir():
            files = sorted(iter_videos(root, recursive))
        else:
            raise LibraryError(f"文件或目录不存在: {root}")
        stats = {
            "found": len(files), "added": 0, "updated": 0,
            "unchanged": 0, "duplicate": 0, "failed": 0,
            "source_ids": [],
        }
        for index, path in enumerate(files, start=1):
            if progress:
                progress("scan", index, len(files), path.name)
            try:
                result, source_id = self._register(
                    path,
                    "" if root.is_file() else category_for(
                        path, root, self.settings.category_aliases
                    ),
                    material_type=material_type,
                )
            except (OSError, MediaError, LibraryError):
                stats["failed"] += 1
                continue
            stats[result] += 1
            # Only new/changed files need analysis; the caller passes these ids
            # back so "扫描并分析" never re-analyzes the whole library.
            if source_id and result in {"added", "updated"}:
                stats["source_ids"].append(source_id)
        return stats

    def _register(
        self, path: Path, category: str = "", *, material_type: str = "普通素材"
    ) -> tuple[str, int | None]:
        material_type = material_type if material_type in {"普通素材", "成片"} else "普通素材"
        resolved = str(path.resolve())
        stat = path.stat()
        conn = self.db.connect()
        existing = conn.execute(
            """
            SELECT id, sha256, size_bytes, mtime, category, material_type
            FROM sources WHERE path=?
            """,
            (resolved,),
        ).fetchone()
        if (
            existing
            and existing["size_bytes"] == stat.st_size
            and abs(existing["mtime"] - stat.st_mtime) < 1e-6
        ):
            metadata_changed = (
                existing["category"] != category
                or existing["material_type"] != material_type
            )
            if metadata_changed:
                with self.db.transaction() as tx:
                    tx.execute(
                        "UPDATE sources SET category=?, material_type=?, updated_at=? WHERE id=?",
                        (category, material_type, utc_now(), existing["id"]),
                    )
                self._sync_source_category(int(existing["id"]), category)
            # A folder move/category alias change only updates metadata. It
            # must not trigger another hard-cut or AI pass; changing the
            # material mode does require re-analysis.
            needs_reanalysis = existing["material_type"] != material_type
            return (
                "updated" if needs_reanalysis else "unchanged",
                int(existing["id"]) if needs_reanalysis else None,
            )

        digest = file_sha256(path)
        duplicate = conn.execute(
            "SELECT id FROM sources WHERE sha256=? AND id<>? AND duplicate_of IS NULL LIMIT 1",
            (digest, existing["id"] if existing else -1),
        ).fetchone()
        duplicate_of = int(duplicate["id"]) if duplicate else None
        now = utc_now()
        with self.db.transaction() as tx:
            if existing:
                tx.execute(
                    """
                    UPDATE sources SET sha256=?, size_bytes=?, mtime=?, status='pending',
                        error=NULL, duplicate_of=?, category=?, material_type=?, updated_at=? WHERE id=?
                    """,
                    (
                        digest, stat.st_size, stat.st_mtime, duplicate_of,
                        category, material_type, now, existing["id"],
                    ),
                )
                source_id = int(existing["id"])
            else:
                cursor = tx.execute(
                    """
                    INSERT INTO sources(path, sha256, size_bytes, mtime, status,
                        duplicate_of, category, material_type, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                    """,
                    (
                        resolved, digest, stat.st_size, stat.st_mtime,
                        duplicate_of, category, material_type, now, now,
                    ),
                )
                source_id = int(cursor.lastrowid)
        if duplicate_of:
            return "duplicate", None
        if existing:
            # A moved file keeps its clips; new folders/categories must reach
            # the existing clips as well.
            self._sync_source_category(source_id, category)
        return ("updated" if existing else "added"), source_id

    def _sync_source_category(self, source_id: int, category: str) -> None:
        rows = self.db.connect().execute(
            "SELECT id FROM clips WHERE source_id=?", (source_id,)
        ).fetchall()
        for row in rows:
            self._attach_category(str(row["id"]), category)
        source = self.db.connect().execute(
            "SELECT material_type FROM sources WHERE id=?", (source_id,)
        ).fetchone()
        if source:
            for row in rows:
                self._attach_material_type(
                    str(row["id"]), str(source["material_type"] or "普通素材")
                )

    def pending_sources(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT * FROM sources WHERE status IN ('pending','failed') "
            "AND duplicate_of IS NULL ORDER BY id"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        return list(self.db.connect().execute(sql))

    def source_ids_for_analyze(self, source_ids: list[int] | None = None) -> list[int]:
        """Resolve which sources an analyze request should process.

        ``None`` means every pending source. An explicit list keeps the order the
        caller provided and skips duplicates or already-finished files.
        """
        if source_ids is None:
            return [int(row["id"]) for row in self.pending_sources()]
        wanted = list(dict.fromkeys(int(item) for item in source_ids))
        if not wanted:
            return []
        marks = ",".join("?" for _ in wanted)
        rows = self.db.connect().execute(
            f"""
            SELECT id FROM sources
            WHERE id IN ({marks}) AND duplicate_of IS NULL
            ORDER BY id
            """,
            wanted,
        ).fetchall()
        allowed = {int(row["id"]) for row in rows}
        return [item for item in wanted if item in allowed]

    # --------------------------------------------------------------- analyze
    def analyze_pending(
        self,
        *,
        limit: int | None = None,
        progress: Progress | None = None,
    ) -> dict[str, int]:
        sources = self.pending_sources(limit)
        stats = {"analyzed": 0, "failed": 0}
        for index, source in enumerate(sources, start=1):
            if progress:
                progress("analyze", index, len(sources), Path(source["path"]).name)
            try:
                self.analyze_source(int(source["id"]))
                stats["analyzed"] += 1
            except (MediaError, LibraryError, OSError) as error:
                stats["failed"] += 1
                self._mark_source_failed(int(source["id"]), str(error))
        return stats

    def analyze_source(self, source_id: int, *, relabel: bool = False) -> int:
        conn = self.db.connect()
        source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if source is None:
            raise LibraryError("素材不存在")
        path = Path(source["path"])
        if not path.is_file():
            raise LibraryError(f"源文件缺失: {path}")
        if source["duplicate_of"]:
            raise LibraryError("该文件与已入库素材重复")

        info = probe(path, self.settings.ffprobe)
        self._update_source(
            source_id, duration=info.duration, width=info.width,
            height=info.height, fps=info.fps, codec=info.codec,
            status="analyzing", error=None,
        )
        shots = detect_shots(
            path,
            detector=self.settings.shots.detector,
            threshold=self.settings.shots.threshold,
            min_scene_duration=self.settings.shots.min_scene_duration,
        )
        with self.db.transaction() as tx:
            tx.execute("DELETE FROM shots WHERE source_id=?", (source_id,))
            for index, (start, end) in enumerate(shots):
                tx.execute(
                    "INSERT INTO shots(source_id, shot_index, start, end) VALUES (?, ?, ?, ?)",
                    (source_id, index, start, end),
                )
        shot_rows = conn.execute(
            "SELECT * FROM shots WHERE source_id=? ORDER BY shot_index", (source_id,)
        ).fetchall()

        created = 0
        for shot in shot_rows:
            created += self._analyze_shot(int(source["id"]), source, shot, relabel=relabel)
        self._update_source(source_id, status="ready", error=None, analyzed_at=utc_now())
        return created

    def _analyze_shot(
        self,
        source_id: int,
        source: sqlite3.Row,
        shot: sqlite3.Row,
        *,
        relabel: bool,
    ) -> int:
        path = Path(source["path"])
        start, end = float(shot["start"]), float(shot["end"])
        seg = self.settings.segment
        shot_duration = max(0.0, end - start)
        if shot_duration <= 0.05:
            return 0

        times, frames = sample_frames(
            path,
            sample_fps=seg.sample_fps,
            width=seg.analyze_width,
            start=start,
            end=end,
        )
        signal = compute_signal(times, frames)
        if str(source["material_type"] or "普通素材") == "成片":
            # A finished edit is already cut for rhythm. Preserve each hard
            # cut as-is instead of searching for a shorter action window.
            candidates = [FallbackCandidate(start, end, start, end, 0.5, 0.5)]
        else:
            candidates = find_candidates(
                signal,
                shot_start=start,
                shot_end=end,
                target_duration=seg.target_duration,
                min_duration=seg.min_duration,
                max_duration=seg.max_duration,
                sample_fps=seg.sample_fps,
                exact_action=True,
            )
        if not candidates:
            window = min(
                shot_duration,
                max(seg.min_duration, min(seg.target_duration, seg.max_duration)),
            )
            candidates = [FallbackCandidate(start, min(end, start + window), start, end)]

        created = 0
        for candidate in candidates:
            clip_id = clip_id_for(source["sha256"], candidate.start, candidate.end)
            transcript = sidecar_transcript(path, candidate.start, candidate.end)
            now = utc_now()
            existing = self.db.connect().execute(
                "SELECT reviewed, origin, status FROM clips WHERE id=?", (clip_id,)
            ).fetchone()
            if existing and (existing["reviewed"] or existing["origin"] == "manual"):
                continue
            is_new = existing is None
            # A clip left in 'pending' means a previous run was interrupted
            # before labeling; treat it as unfinished work.
            needs_label = is_new or relabel or (existing["status"] == "pending")
            with self.db.transaction() as tx:
                tx.execute(
                    """
                    INSERT INTO clips(id, source_id, shot_id, start, end, duration,
                        transcript,
                        action_score, quality_score, motion_start, motion_end,
                        status, origin, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'auto', ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        transcript=excluded.transcript,
                        action_score=excluded.action_score,
                        quality_score=excluded.quality_score,
                        motion_start=excluded.motion_start,
                        motion_end=excluded.motion_end,
                        updated_at=excluded.updated_at
                    """,
                    (
                        clip_id, source_id, int(shot["id"]), candidate.start,
                        candidate.end, candidate.end - candidate.start, transcript,
                        candidate.action_score, candidate.quality_score,
                        candidate.motion_start, candidate.motion_end, now, now,
                    ),
                )
            if is_new:
                created += 1
            self._attach_category(clip_id, str(source["category"] or ""))
            self._attach_material_type(
                clip_id, str(source["material_type"] or "普通素材")
            )
            self._make_thumbnails(path, clip_id, candidate.start, candidate.end)
            if needs_label:
                self.label_clip(clip_id, force=relabel)
            self._refresh_search_text(clip_id)
        return created

    def _attach_category(self, clip_id: str, category: str) -> None:
        """Folder-derived category is structural, not inferred from pixels."""
        with self.db.transaction() as tx:
            # Folder membership can change between scans, so replace any
            # previous folder tag for this clip.
            tx.execute(
                """
                DELETE FROM clip_tags
                WHERE clip_id=? AND origin='folder'
                """,
                (clip_id,),
            )
            if category:
                tags.attach(tx, clip_id, [("分类", category)], origin="folder")
        self._refresh_search_text(clip_id)

    def _attach_material_type(self, clip_id: str, material_type: str) -> None:
        value = material_type if material_type in {"普通素材", "成片"} else "普通素材"
        with self.db.transaction() as tx:
            tx.execute(
                "DELETE FROM clip_tags WHERE clip_id=? AND origin='system' "
                "AND tag_id IN (SELECT id FROM tags WHERE dimension='素材类型')",
                (clip_id,),
            )
            tags.attach(tx, clip_id, [("素材类型", value)], origin="system")
        self._refresh_search_text(clip_id)

    def label_clip(self, clip_id: str, *, force: bool = False) -> bool:
        conn = self.db.connect()
        row = conn.execute(
            """
            SELECT c.*, s.path AS source_path, s.duration AS source_duration,
                s.category, s.material_type
            FROM clips c JOIN sources s ON s.id=c.source_id
            WHERE c.id=?
            """,
            (clip_id,),
        ).fetchone()
        if row is None:
            raise LibraryError("片段不存在")
        if row["reviewed"] and not force:
            return False
        if not self.settings.ai.configured:
            self._set_clip_status(clip_id, "needs_review")
            return False

        thumbs = [
            Path(item["path"])
            for item in conn.execute(
                "SELECT path FROM thumbnails WHERE clip_id=? ORDER BY position", (clip_id,)
            )
        ]
        context = ClipContext(
            source_name=Path(row["source_path"]).name,
            source_duration=float(row["source_duration"]),
            start=float(row["start"]),
            end=float(row["end"]),
            frames=thumbs,
            category=str(row["category"] or ""),
            categories=self.settings.category_labels,
            transcript=str(row["transcript"] or ""),
        )
        try:
            result = self.provider.label(context)
        except LabelingError as error:
            self._set_clip_status(clip_id, "needs_review", str(error))
            return False
        with self.db.transaction() as tx:
            tx.execute(
                "DELETE FROM clip_tags WHERE clip_id=? AND origin='auto'", (clip_id,)
            )
            for dimension, values in result.tags.items():
                for name in values:
                    tags.attach(
                        tx, clip_id, [(dimension, name)],
                        confidence=result.confidence, origin="auto",
                    )
            tx.execute(
                "UPDATE clips SET description=?, status=?, updated_at=? WHERE id=?",
                (
                    result.description,
                    "ready" if result.description else "needs_review",
                    utc_now(), clip_id,
                ),
            )
        self._refresh_search_text(clip_id)
        return True

    # ------------------------------------------------------------ maintenance
    def _update_source(self, source_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{name}=?" for name in fields)
        with self.db.transaction() as tx:
            tx.execute(
                f"UPDATE sources SET {assignments} WHERE id=?",
                (*fields.values(), source_id),
            )

    def _mark_source_failed(self, source_id: int, message: str) -> None:
        self._update_source(source_id, status="failed", error=message[:500])

    def _set_clip_status(self, clip_id: str, status: str, error: str | None = None) -> None:
        with self.db.transaction() as tx:
            tx.execute(
                "UPDATE clips SET status=?, updated_at=? WHERE id=?",
                (status, utc_now(), clip_id),
            )
            if error:
                tx.execute(
                    "UPDATE sources SET error=?, updated_at=? "
                    "WHERE id=(SELECT source_id FROM clips WHERE id=?)",
                    (f"labeling: {error}"[:500], utc_now(), clip_id),
                )

    def _make_thumbnails(self, path: Path, clip_id: str, start: float, end: float) -> None:
        folder = self.settings.thumb_dir / clip_id[:2]
        folder.mkdir(parents=True, exist_ok=True)
        span = max(0.0, end - start)
        for index, ratio in enumerate((0.15, 0.5, 0.85)):
            output = folder / f"{clip_id}_{index}.jpg"
            if not output.is_file():
                try:
                    extract_frame(
                        path, start + span * ratio, output, ffmpeg=self.settings.ffmpeg
                    )
                except MediaError:
                    continue
            with self.db.transaction() as tx:
                tx.execute(
                    """
                    INSERT INTO thumbnails(clip_id, position, path) VALUES (?, ?, ?)
                    ON CONFLICT(clip_id, position) DO UPDATE SET path=excluded.path
                    """,
                    (clip_id, index, str(output)),
                )

    def _refresh_search_text(self, clip_id: str) -> None:
        conn = self.db.connect()
        row = conn.execute(
            """
            SELECT c.description, c.transcript, c.start, c.end, s.path
            FROM clips c JOIN sources s ON s.id=c.source_id WHERE c.id=?
            """,
            (clip_id,),
        ).fetchone()
        if row is None:
            return
        label_rows = conn.execute(
            """
            SELECT t.dimension, t.name FROM clip_tags ct
            JOIN tags t ON t.id=ct.tag_id WHERE ct.clip_id=?
            """,
            (clip_id,),
        ).fetchall()
        parts = [
            row["description"],
            row["transcript"],
            Path(row["path"]).stem,
            Path(row["path"]).parent.name,
            f"{row['start']:.1f}s {row['end']:.1f}s",
        ]
        source_row = conn.execute(
            "SELECT category FROM sources WHERE id=(SELECT source_id FROM clips WHERE id=?)",
            (clip_id,),
        ).fetchone()
        if source_row and source_row["category"]:
            parts.append(str(source_row["category"]))
        parts.extend(f"{item['dimension']}:{item['name']}" for item in label_rows)
        parts.extend(item["name"] for item in label_rows)
        for item in label_rows:
            for canonical, aliases in tags.VOCABULARY.get(item["dimension"], {}).items():
                if canonical == item["name"]:
                    parts.extend(aliases)
                    break
        with self.db.transaction() as tx:
            tx.execute(
                "UPDATE clips SET search_text=? WHERE id=?",
                (" ".join(part for part in parts if part), clip_id),
            )

    # --------------------------------------------------------------- queries
    def _clip_select(self) -> str:
        return """
            SELECT c.*, s.path, s.duration AS source_duration, s.width, s.height,
                s.category, s.material_type,
                (
                    SELECT json_group_array(json_object('dimension', t.dimension, 'name', t.name))
                    FROM clip_tags ct JOIN tags t ON t.id=ct.tag_id
                    WHERE ct.clip_id=c.id
                ) AS tags_json
            FROM clips c JOIN sources s ON s.id=c.source_id
        """

    def get_clip(self, clip_id: str) -> dict[str, Any] | None:
        row = self.db.connect().execute(
            self._clip_select() + " WHERE c.id=?", (clip_id,)
        ).fetchone()
        return self._decorate(row) if row is not None else None

    def _decorate(self, row: sqlite3.Row) -> dict[str, Any]:
        clip = row_to_clip(row)
        thumbnails = self.db.connect().execute(
            "SELECT position, path FROM thumbnails WHERE clip_id=? ORDER BY position",
            (clip["id"],),
        ).fetchall()
        clip["thumbnails"] = [str(Path(item["path"])) for item in thumbnails]
        return clip

    def tags_by_dimension(self) -> dict[str, dict[str, int]]:
        rows = self.db.connect().execute(
            """
            SELECT t.dimension, t.canonical, COUNT(DISTINCT ct.clip_id) AS count
            FROM tags t
            LEFT JOIN clip_tags ct ON ct.tag_id=t.id
            GROUP BY t.dimension, t.canonical
            HAVING count > 0
            ORDER BY t.dimension, count DESC
            """
        ).fetchall()
        result: dict[str, dict[str, int]] = {}
        for row in rows:
            result.setdefault(row["dimension"], {})[row["canonical"]] = int(row["count"])
        return result

    def categories(self) -> list[str]:
        """All unified categories, including folder and AI category labels."""
        conn = self.db.connect()
        rows = conn.execute(
            """
            SELECT DISTINCT t.canonical AS category
            FROM tags t JOIN clip_tags ct ON ct.tag_id=t.id
            WHERE t.dimension='分类'
            ORDER BY category
            """
        ).fetchall()
        values = {str(row["category"]) for row in rows}
        values.update(
            str(row["category"])
            for row in conn.execute(
                """
                SELECT DISTINCT category FROM sources
                WHERE category<>'' AND duplicate_of IS NULL
                """
            ).fetchall()
        )
        return sorted(values)

    def category_counts(self) -> dict[str, int]:
        """Source counts per folder category, ordered as a tree.

        Parent folders also count their descendants, so selecting
        ``痛点使用`` returns everything below it. Ordering keeps each parent
        immediately above its children, which the sidebar relies on to indent.
        """
        conn = self.db.connect()
        rows = conn.execute(
            """
            SELECT category, COUNT(*) AS n FROM sources
            WHERE category<>'' AND duplicate_of IS NULL GROUP BY category
            """
        ).fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            parts = str(row["category"]).split(" / ")
            for depth in range(1, len(parts) + 1):
                key = " / ".join(parts[:depth])
                counts[key] = counts.get(key, 0) + int(row["n"])
        # AI/manual categories are clip-level labels. Include them in the
        # same tree so the UI exposes one unified 分类 facet.
        label_rows = conn.execute(
            """
            SELECT t.canonical AS category, COUNT(DISTINCT ct.clip_id) AS n
            FROM tags t JOIN clip_tags ct ON ct.tag_id=t.id
            WHERE t.dimension='分类' AND ct.origin<>'folder'
            GROUP BY t.canonical
            """
        ).fetchall()
        for row in label_rows:
            category = str(row["category"])
            if category in counts:
                counts[category] = max(counts[category], int(row["n"]))
                continue
            parts = category.split(" / ")
            for depth in range(1, len(parts) + 1):
                key = " / ".join(parts[:depth])
                counts[key] = max(counts.get(key, 0), int(row["n"]))
        children: dict[str, list[str]] = {}
        for name in counts:
            parent = name.rsplit(" / ", 1)[0] if " / " in name else ""
            children.setdefault(parent, []).append(name)

        ordered: list[str] = []

        def walk(parent: str) -> None:
            for name in sorted(
                children.get(parent, []), key=lambda item: (-counts[item], item)
            ):
                ordered.append(name)
                walk(name)

        walk("")
        return {name: counts[name] for name in ordered}

    def list_sources(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.connect().execute(
            """
            SELECT s.*, (SELECT COUNT(*) FROM clips c WHERE c.source_id=s.id) AS clip_count
            FROM sources s ORDER BY s.updated_at DESC LIMIT ?
            """,
            (max(1, min(limit, 2000)),),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "path": row["path"],
                "name": Path(row["path"]).name,
                "duration": round(row["duration"], 2),
                "status": row["status"],
                "material_type": row["material_type"],
                "error": row["error"],
                "duplicate_of": row["duplicate_of"],
                "clip_count": int(row["clip_count"]),
            }
            for row in rows
        ]

    def stats(self) -> dict[str, Any]:
        conn = self.db.connect()
        sources = conn.execute(
            "SELECT status, COUNT(*) AS n FROM sources "
            "WHERE duplicate_of IS NULL GROUP BY status"
        ).fetchall()
        clips = conn.execute(
            "SELECT status, COUNT(*) AS n FROM clips GROUP BY status"
        ).fetchall()
        total_seconds = conn.execute(
            "SELECT COALESCE(SUM(duration),0) AS s FROM clips"
        ).fetchone()["s"]
        return {
            "sources": {row["status"]: int(row["n"]) for row in sources},
            "clips": {row["status"]: int(row["n"]) for row in clips},
            "clip_seconds": round(float(total_seconds), 1),
            "ai": {
                "enabled": self.settings.ai.configured,
                "model": self.settings.ai.model,
            },
        }

    def search(self, query: SearchQuery) -> dict[str, Any]:
        conn = self.db.connect()
        clauses = ["1=1"]
        params: list[Any] = []
        if query.status:
            clauses.append("c.status=?")
            params.append(query.status)
        if query.source_id:
            clauses.append("c.source_id=?")
            params.append(query.source_id)
        if query.material_type:
            clauses.append("s.material_type=?")
            params.append(query.material_type)
        if query.categories:
            # Category is hierarchical ("痛点使用 / 痛点涂抹"), so matching a
            # parent must also return its children.
            likes = []
            for category in query.categories:
                canonical = tags.clean_category_path(category)
                likes.append("(s.category = ? OR s.category LIKE ? OR EXISTS ("
                             "SELECT 1 FROM clip_tags ct JOIN tags t ON t.id=ct.tag_id "
                             "WHERE ct.clip_id=c.id AND t.dimension='分类' "
                             "AND (t.canonical=? OR t.canonical LIKE ?)))")
                params.append(canonical)
                params.append(f"{canonical} / %")
                params.append(canonical)
                params.append(f"{canonical} / %")
            clauses.append("(" + " OR ".join(likes) + ")")
        if query.min_duration is not None:
            clauses.append("c.duration>=?")
            params.append(float(query.min_duration))
        if query.max_duration is not None:
            clauses.append("c.duration<=?")
            params.append(float(query.max_duration))

        grouped: dict[str, list[str]] = {}
        for dimension, name in query.tags or []:
            grouped.setdefault(dimension, []).append(name)
        for dimension, names in grouped.items():
            marks = ",".join("?" for _ in names)
            clauses.append(
                f"""EXISTS (
                    SELECT 1 FROM clip_tags ct JOIN tags t ON t.id=ct.tag_id
                    WHERE ct.clip_id=c.id AND t.dimension=?
                        AND t.canonical IN ({marks})
                )"""
            )
            params.append(dimension)
            params.extend(names)

        keyword_tokens = _search_concepts(query.keyword, self.categories())
        if keyword_tokens:
            # Require every extracted concept. This makes a compound search
            # such as "涂抹祛痘霜" mean action AND product, while ordinary free
            # text remains substring-based.
            keyword_clause = " AND ".join("c.search_text LIKE ?" for _ in keyword_tokens)
            clauses.append(f"({keyword_clause})")
            params.extend(f"%{token}%" for token in keyword_tokens)

        sql = self._clip_select() + " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY c.updated_at DESC LIMIT 20000"
        rows = conn.execute(sql, params).fetchall()
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            clip = self._decorate(row)
            clip["score"] = round(self._score(clip, grouped, keyword_tokens), 4)
            scored.append((clip["score"], clip))
        scored.sort(key=lambda item: item[0], reverse=True)
        page = scored[query.offset: query.offset + max(1, min(query.limit, 200))]
        return {
            "total": len(scored),
            "offset": query.offset,
            "limit": query.limit,
            "items": [item[1] for item in page],
        }

    def _score(
        self,
        clip: dict[str, Any],
        grouped: dict[str, list[str]],
        keyword_tokens: list[str],
    ) -> float:
        weights = self.settings.search
        tag_score = 0.0
        if grouped:
            total = sum(len(names) for names in grouped.values())
            matched = 0
            for dimension, names in grouped.items():
                available = {str(name) for name in clip["tags"].get(dimension, [])}
                matched += sum(1 for name in names if name in available)
            tag_score = matched / max(1, total)
        text_score = 0.0
        if keyword_tokens:
            haystack = " ".join(
            [clip["description"], clip.get("transcript", ""), *[
                    name for names in clip["tags"].values() for name in names
                ]]
            ).lower()
            text_score = sum(1 for token in keyword_tokens if token in haystack) / len(
                keyword_tokens
            )
        duration = float(clip["duration"])
        target = self.settings.segment.target_duration
        duration_score = max(0.0, 1.0 - abs(duration - target) / max(target, 1e-6))
        score = (
            weights.tag * tag_score
            + weights.text * text_score
            + weights.action * float(clip["action_score"])
            + weights.duration * duration_score
            + weights.quality * float(clip["quality_score"])
        )
        if clip["reviewed"]:
            score += 0.03
        return max(0.0, min(1.0, score))

    # ---------------------------------------------------------------- editing
    def update_clip(
        self,
        clip_id: str,
        *,
        description: str | None = None,
        transcript: str | None = None,
        tags_map: dict[str, list[str]] | None = None,
        reviewed: bool | None = None,
        status: str | None = None,
        start: float | None = None,
        end: float | None = None,
        category: str | None = None,
    ) -> dict[str, Any]:
        conn = self.db.connect()
        current = conn.execute(
            "SELECT * FROM clips WHERE id=?", (clip_id,)
        ).fetchone()
        if current is None:
            raise LibraryError("片段不存在")
        original_source = conn.execute(
            "SELECT category, material_type FROM sources WHERE id=?",
            (int(current["source_id"]),),
        ).fetchone()
        updates: dict[str, Any] = {"updated_at": utc_now()}
        if description is not None:
            updates["description"] = description.strip()[:1000]
        if transcript is not None:
            updates["transcript"] = transcript.strip()[:2000]
        if reviewed is not None:
            updates["reviewed"] = 1 if reviewed else 0
        if status is not None:
            if status not in {"pending", "ready", "needs_review", "rejected"}:
                raise LibraryError("无效状态")
            updates["status"] = status
        if category is not None and current["source_id"]:
            clean = tags.clean_category_path(category, self.settings.category_aliases)
            with self.db.transaction() as tx:
                tx.execute(
                    "UPDATE sources SET category=?, updated_at=? WHERE id=?",
                    (clean, utc_now(), int(current["source_id"])),
                )
            # Category lives on the source, so every clip cut from that file
            # must follow it instead of keeping a stale folder tag.
            self._sync_source_category(int(current["source_id"]), clean)
        new_start = float(start) if start is not None else float(current["start"])
        new_end = float(end) if end is not None else float(current["end"])
        if new_end - new_start <= 0.05:
            raise LibraryError("片段时间区间无效")
        if start is not None or end is not None:
            updates["start"] = new_start
            updates["end"] = new_end
            updates["duration"] = new_end - new_start
            updates["origin"] = "manual"
        with self.db.transaction() as tx:
            assignments = ", ".join(f"{name}=?" for name in updates)
            tx.execute(
                f"UPDATE clips SET {assignments} WHERE id=?",
                (*updates.values(), clip_id),
            )
            if tags_map is not None:
                tx.execute("DELETE FROM clip_tags WHERE clip_id=?", (clip_id,))
                source_category = tx.execute(
                    "SELECT category, material_type FROM sources WHERE id=?",
                    (int(current["source_id"]),),
                ).fetchone()
                structural_categories = {
                    str(original_source["category"] or "") if original_source else "",
                    str(source_category["category"] or "") if source_category else "",
                }
                for dimension, values in tags_map.items():
                    if dimension not in tags.VOCABULARY:
                        continue
                    if dimension == "素材类型":
                        continue
                    for value in values:
                        resolved = tags.resolve(value, dimension)
                        if resolved is None:
                            continue
                        if (
                            dimension == "分类"
                            and resolved[1] in structural_categories
                        ):
                            continue
                        tags.attach(tx, clip_id, [resolved], origin="manual")
                if source_category and source_category["category"]:
                    tags.attach(
                        tx, clip_id,
                        [("分类", str(source_category["category"]))],
                        origin="folder",
                    )
                tags.attach(
                    tx, clip_id,
                    [("素材类型", str(source_category["material_type"] or "普通素材"))],
                    origin="system",
                )
        self._refresh_search_text(clip_id)
        clip = self.get_clip(clip_id)
        assert clip is not None
        return clip

    def delete_clip(self, clip_id: str) -> None:
        with self.db.transaction() as tx:
            row = tx.execute(
                "SELECT source_id FROM clips WHERE id=?", (clip_id,)
            ).fetchone()
            if row is None:
                raise LibraryError("片段不存在")
            tx.execute("DELETE FROM clips WHERE id=?", (clip_id,))

    def render_cached_clip(self, clip_id: str) -> Path:
        conn = self.db.connect()
        row = conn.execute(
            """
            SELECT c.start, c.end, s.path, s.mtime
            FROM clips c JOIN sources s ON s.id=c.source_id WHERE c.id=?
            """,
            (clip_id,),
        ).fetchone()
        if row is None:
            raise LibraryError("片段不存在")
        output = self.settings.clip_cache_dir / f"{clip_id}.mp4"
        stamp = output.with_suffix(".stamp")
        token = f"{row['path']}|{row['mtime']}|{row['start']}|{row['end']}"
        if output.is_file() and stamp.is_file() and stamp.read_text(encoding="utf-8") == token:
            return output
        from .media import render_clip

        render_clip(
            Path(row["path"]), float(row["start"]), float(row["end"]),
            output, ffmpeg=self.settings.ffmpeg,
        )
        stamp.write_text(token, encoding="utf-8")
        return output


@dataclass(frozen=True)
class FallbackCandidate:
    start: float
    end: float
    motion_start: float
    motion_end: float
    action_score: float = 0.2
    quality_score: float = 0.5
