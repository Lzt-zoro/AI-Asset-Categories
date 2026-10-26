from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from material_library import tags
from material_library.config import AIConfig, Settings
from material_library.library import (
    Library,
    SearchQuery,
    category_for,
    clip_id_for,
    iter_videos,
)
from material_library.motion import compute_signal, find_candidates


def synthetic_motion(times: list[float], active: tuple[float, float]) -> list[np.ndarray]:
    frames = []
    for index, moment in enumerate(times):
        frame = np.full((48, 32), 90 + index, dtype=np.uint8)
        if active[0] <= moment <= active[1]:
            frame[8:40, 4:28] = 235 if index % 2 else 20
        frames.append(frame)
    return frames


def test_candidate_window_respects_duration_and_covers_motion():
    times = [index / 8 for index in range(40)]
    frames = synthetic_motion(times, (2.0, 4.0))
    signal = compute_signal(times, frames)
    candidates = find_candidates(
        signal, shot_start=0.0, shot_end=5.0,
        target_duration=3.0, min_duration=1.0, max_duration=3.0,
    )
    assert candidates, "motion segment should produce a candidate"
    best = candidates[0]
    assert best.end - best.start <= 3.05
    assert best.start >= -0.01
    # The chosen window should overlap the moving region.
    assert best.start < 4.0 and best.end > 2.0
    assert best.action_score > 0.2


def test_exact_action_keeps_gesture_bounds_instead_of_padding_to_three_seconds():
    times = [index / 8 for index in range(48)]
    frames = synthetic_motion(times, (1.0, 1.9))
    signal = compute_signal(times, frames)
    candidates = find_candidates(
        signal, shot_start=0.0, shot_end=5.0, target_duration=3.0,
        min_duration=1.0, max_duration=8.0, exact_action=True,
    )
    assert candidates
    best = candidates[0]
    assert best.start <= 1.1
    assert best.end >= 1.8
    assert best.end - best.start < 2.2


def test_category_aliases_keep_shared_taxonomy_spellings_explicit():
    assert tags.clean_category_path("中远") == "中远"
    assert tags.clean_category_path("痛点使用 / 痛点涂抹") == "疼点使用 / 疼点涂抹"


def test_sample_frames_keeps_absolute_timestamp_after_seek(tmp_path, monkeypatch):
    from material_library import media

    class Capture:
        def __init__(self):
            self.index = 0
        def isOpened(self): return True
        def get(self, prop):
            if prop == media.cv2.CAP_PROP_FPS: return 10
            if prop == media.cv2.CAP_PROP_FRAME_COUNT: return 100
            return 0
        def set(self, prop, value):
            if prop == media.cv2.CAP_PROP_POS_MSEC:
                self.index = round(value / 100)
        def read(self):
            if self.index >= 100: return False, None
            frame = np.zeros((8, 8, 3), dtype=np.uint8)
            self.index += 1
            return True, frame
        def release(self): pass

    monkeypatch.setattr(media.cv2, "VideoCapture", lambda _: Capture())
    times, _ = media.sample_frames(tmp_path / "a.mp4", sample_fps=10, width=8,
                                   start=5.0, end=5.3)
    assert times and times[0] >= 5.0


def test_static_shot_falls_back_to_single_window():
    times = [index / 8 for index in range(24)]
    frames = [np.full((48, 32), 120, dtype=np.uint8) for _ in times]
    signal = compute_signal(times, frames)
    candidates = find_candidates(
        signal, shot_start=0.0, shot_end=3.0,
        target_duration=2.0, min_duration=1.0, max_duration=4.0,
    )
    assert len(candidates) == 1
    assert 1.5 <= candidates[0].end - candidates[0].start <= 3.0


def test_short_motion_is_padded_to_target_duration():
    """A brief gesture must still yield the requested 3s clip."""
    times = [index / 8 for index in range(33)]
    # Motion only between 0.5s and 1.4s, like a single hand gesture.
    frames = synthetic_motion(times, (0.5, 1.4))
    signal = compute_signal(times, frames)
    candidates = find_candidates(
        signal, shot_start=0.0, shot_end=4.04,
        target_duration=3.0, min_duration=1.0, max_duration=8.0,
    )
    assert len(candidates) == 1
    best = candidates[0]
    assert 2.9 <= best.end - best.start <= 3.2, best
    # The padded window must still contain the original gesture.
    assert best.start <= 0.5 and best.end >= 1.4


def test_long_shot_motion_stays_within_max_duration():
    times = [index / 8 for index in range(160)]
    frames = synthetic_motion(times, (2.0, 16.0))
    signal = compute_signal(times, frames)
    candidates = find_candidates(
        signal, shot_start=0.0, shot_end=20.0,
        target_duration=3.0, min_duration=1.0, max_duration=4.0,
    )
    assert candidates
    assert all(c.end - c.start <= 4.05 for c in candidates)
    assert all(c.end - c.start >= 1.0 for c in candidates)


def test_alias_resolution():
    assert tags.resolve("近景") == ("景别", "近景")
    assert tags.resolve("半身", "景别") == ("景别", "近景")
    assert tags.resolve("小蓝瓶") == ("产品", "祛痘霜")
    assert tags.resolve("口播") == ("动作", "说话")
    assert tags.resolve("完全不存在") is None
    # A dimension mismatch must not silently accept the alias.
    assert tags.resolve("半身", "动作") is None


def test_clip_id_is_stable_and_time_sensitive():
    first = clip_id_for("A" * 64, 1.2345, 4.5)
    second = clip_id_for("a" * 64, 1.2345, 4.5)
    third = clip_id_for("a" * 64, 1.2345, 4.6)
    assert first == second
    assert first != third
    assert first.startswith("clip_")


class FakeMedia:
    """Stand-in for probe/detect/sample so search tests stay fast."""


def make_settings(tmp_path: Path) -> Settings:
    base = Settings()
    return Settings(
        root=base.root, library_root=tmp_path / "data",
        ai=AIConfig(), segment=base.segment, shots=base.shots,
        search=base.search, server=base.server,
    )


def seed(library: Library) -> None:
    conn = library.db.connect()
    now = 1.0
    with library.db.transaction() as tx:
        tx.execute(
            """
            INSERT INTO sources(id, path, sha256, size_bytes, mtime, duration,
                status, created_at, updated_at)
            VALUES (1, ?, ?, 10, 1, 20, 'ready', ?, ?)
            """,
            (str(Path("D:/materials/demo.mp4")), "a" * 64, now, now),
        )
        rows = [
            ("clip_a", 0.0, 3.0, "女生在浴室涂抹祛痘霜，近景手部动作", "涂抹"),
            ("clip_b", 5.0, 8.0, "男生在办公室展示产品包装", "展示产品"),
            ("clip_c", 9.0, 12.0, "空镜产品静物，纯色背景", "展示产品"),
        ]
        for clip_id, start, end, description, action in rows:
            tx.execute(
                """
                INSERT INTO clips(id, source_id, start, end, duration, description,
                    action_score, quality_score, status, origin, search_text,
                    created_at, updated_at)
                VALUES (?, 1, ?, ?, ?, ?, 0.9, 0.8, 'ready', 'auto', ?, ?, ?)
                """,
                (clip_id, start, end, end - start, description,
                 description + " " + action, now, now),
            )
            tags.attach(tx, clip_id, [("动作", action)], origin="auto")
            tags.attach(tx, clip_id, [("景别", "近景" if clip_id == "clip_a" else "全景")])
    for clip_id in ("clip_a", "clip_b", "clip_c"):
        library._refresh_search_text(clip_id)


def test_search_filters_and_ranks(tmp_path):
    library = Library(make_settings(tmp_path))
    seed(library)

    by_tag = library.search(SearchQuery(tags=[("景别", "近景")]))
    assert [item["id"] for item in by_tag["items"]] == ["clip_a"]
    assert by_tag["items"][0]["score"] > 0

    by_keyword = library.search(SearchQuery(keyword="办公室"))
    assert [item["id"] for item in by_keyword["items"]] == ["clip_b"]

    by_both = library.search(SearchQuery(
        keyword="产品", tags=[("动作", "展示产品")],
    ))
    assert {item["id"] for item in by_both["items"]} == {"clip_b", "clip_c"}
    assert by_both["total"] == 2

    duration = library.search(SearchQuery(min_duration=2.5, max_duration=3.5))
    assert duration["total"] == 3
    assert duration["items"][0]["score"] >= duration["items"][-1]["score"]


def test_manual_update_resolves_alias_and_locks(tmp_path):
    library = Library(make_settings(tmp_path))
    seed(library)
    updated = library.update_clip(
        "clip_a", description="改成人工描述", tags_map={"场景": ["浴室"]}, reviewed=True,
    )
    assert updated["description"] == "改成人工描述"
    assert updated["tags"]["场景"] == ["浴室"]
    assert updated["reviewed"] is True
    again = library.search(SearchQuery(keyword="人工描述"))
    assert again["total"] == 1


def test_iter_videos_skips_hidden_and_non_video(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"x")
    (tmp_path / "b.txt").write_text("nope")
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "c.mov").write_bytes(b"x")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "d.MKV").write_bytes(b"x")
    found = {path.name for path in iter_videos(tmp_path)}
    assert found == {"a.mp4", "d.MKV"}


def test_category_for_uses_relative_folders_and_aliases(tmp_path):
    root = tmp_path / "ai"
    nested = root / "疼点" / "额头疙瘩"
    nested.mkdir(parents=True)
    video = nested / "a.mp4"
    video.write_bytes(b"x")

    assert category_for(video, root) == "疼点 / 额头疙瘩"
    assert category_for(video, root, {"痛点": "疼点"}) == "疼点 / 额头疙瘩"
    # Files sitting directly in the scanned root have no category.
    flat = root / "b.mp4"
    flat.write_bytes(b"x")
    assert category_for(flat, root) == ""


def test_category_counts_follow_tree_order(tmp_path):
    library = Library(make_settings(tmp_path))
    now = 1.0
    rows = [
        ("疼点使用 / 疼点涂抹", 3),
        ("疼点使用 / 额头", 1),
        ("美好展示", 2),
    ]
    with library.db.transaction() as tx:
        for index, (category, count) in enumerate(rows, start=1):
            for extra in range(count):
                source_id = index * 100 + extra
                tx.execute(
                    """
                    INSERT INTO sources(id, path, sha256, size_bytes, mtime,
                        duration, status, category, created_at, updated_at)
                    VALUES (?, ?, ?, 10, 1, 5, 'ready', ?, ?, ?)
                    """,
                    (source_id, f"D:/{source_id}.mp4", str(source_id) * 8, category, now, now),
                )
    counts = library.category_counts()
    assert list(counts) == [
        "疼点使用", "疼点使用 / 疼点涂抹", "疼点使用 / 额头", "美好展示",
    ]
    assert counts["疼点使用"] == 4
    assert counts["疼点使用 / 疼点涂抹"] == 3
