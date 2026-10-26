from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from material_library.config import AIConfig, Settings
from material_library.server import create_app


def make_settings(tmp_path: Path) -> Settings:
    base = Settings()
    return Settings(
        root=base.root, library_root=tmp_path / "data",
        ai=AIConfig(), segment=base.segment, shots=base.shots,
        search=base.search, server=base.server,
    )


def make_video(path: Path) -> bool:
    if shutil.which("ffmpeg") is None:
        return False
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=160x284:d=2:r=25",
        "-f", "lavfi", "-i", "color=c=red:s=160x284:d=2:r=25",
        "-f", "lavfi", "-i", "color=c=green:s=160x284:d=2:r=25",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    return result.returncode == 0 and path.is_file()


def wait_job(test_client: TestClient, attempts: int = 150, wait: float = 0.1) -> dict:
    job: dict = {}
    for _ in range(attempts):
        job = test_client.get("/api/job").json()
        if job["status"] in {"done", "failed"}:
            break
        time.sleep(wait)
    return job


@pytest.fixture()
def client(tmp_path: Path):
    settings = make_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client, settings, tmp_path


def test_health_and_index(client):
    test_client, _, _ = client
    assert test_client.get("/").status_code == 200
    health = test_client.get("/api/health").json()
    assert health["ok"] is True
    assert health["ai_enabled"] is False
    assert "景别" in test_client.get("/api/tags").json()["vocabulary"]
    assert test_client.get("/api/stats").json()["clips"] == {}


def test_scan_analyze_and_search(client):
    test_client, _, tmp_path = client
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    video = media_dir / "demo.mp4"
    if not make_video(video):
        pytest.skip("ffmpeg is required for the integration test")

    started = test_client.post("/api/scan", json={"path": str(media_dir)})
    assert started.status_code == 200
    job = wait_job(test_client)
    assert job["status"] == "done", job
    assert job["result"]["added"] == 1

    started = test_client.post("/api/analyze", json={})
    assert started.status_code == 200
    job = wait_job(test_client, attempts=400)
    assert job["status"] == "done", job

    listing = test_client.get("/api/clips").json()
    assert listing["total"] >= 1
    clip = listing["items"][0]
    assert clip["path"].endswith("demo.mp4")
    assert 0 <= clip["start"] < clip["end"] <= 6.2
    assert clip["status"] == "needs_review"

    detail = test_client.get(f"/api/clips/{clip['id']}").json()
    assert detail["id"] == clip["id"]
    assert len(detail["thumbnails"]) == 3

    thumb = test_client.get(f"/api/clips/{clip['id']}/thumbnail?index=0")
    assert thumb.status_code == 200
    assert thumb.headers["content-type"].startswith("image/jpeg")

    updated = test_client.patch(f"/api/clips/{clip['id']}", json={
        "description": "纯色测试镜头",
        "tags": {"场景": ["纯色背景"], "动作": ["涂抹"]},
        "reviewed": True,
        "status": "ready",
    })
    assert updated.status_code == 200
    body = updated.json()
    assert body["description"] == "纯色测试镜头"
    assert body["reviewed"] is True
    assert body["tags"]["场景"] == ["纯色背景"]

    found = test_client.get("/api/clips", params={"tags": "动作:涂抹"}).json()
    assert [item["id"] for item in found["items"]] == [clip["id"]]

    found = test_client.get("/api/clips", params={"q": "纯色"}).json()
    assert found["total"] == 1

    preview = test_client.get(f"/api/clips/{clip['id']}/preview")
    assert preview.status_code == 200
    assert preview.headers["content-type"].startswith("video/mp4")

    reopened = test_client.get(f"/api/clips/{clip['id']}").json()
    assert reopened["reviewed"] is True
    assert reopened["description"] == "纯色测试镜头"


def test_duplicate_scan_is_idempotent(client):
    test_client, _, tmp_path = client
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    video = media_dir / "demo.mp4"
    if not make_video(video):
        pytest.skip("ffmpeg is required for the integration test")

    test_client.post("/api/scan", json={"path": str(media_dir)})
    assert wait_job(test_client)["status"] == "done"
    test_client.post("/api/scan", json={"path": str(media_dir)})
    job = wait_job(test_client)
    assert job["status"] == "done", job
    assert job["result"]["unchanged"] == 1
    assert job["result"]["duplicate"] == 0


def test_missing_source_returns_error(client):
    test_client, _, _ = client
    response = test_client.get("/api/clips/clip_missing")
    assert response.status_code == 404
    assert test_client.post("/api/scan", json={"path": "Z:/definitely/missing"}).status_code == 400


def test_v1_alias_matches_current_api(client):
    test_client, _, _ = client
    current = test_client.get("/api/stats").json()
    versioned = test_client.get("/api/v1/stats").json()
    assert versioned == current
    assert test_client.get("/api/v1/health").json()["ok"] is True


def test_scan_and_analyze_only_new_files(client):
    test_client, _, tmp_path = client
    media_dir = tmp_path / "media"
    (media_dir / "疼点").mkdir(parents=True)
    video = media_dir / "疼点" / "demo.mp4"
    if not make_video(video):
        pytest.skip("ffmpeg is required for the integration test")

    test_client.post("/api/scan", json={"path": str(media_dir)})
    job = wait_job(test_client)
    assert job["status"] == "done", job
    source_ids = job["result"]["source_ids"]
    assert len(source_ids) == 1

    started = test_client.post("/api/analyze", json={"source_ids": source_ids})
    assert started.json()["started"] is True
    job = wait_job(test_client, attempts=400)
    assert job["status"] == "done", job

    clip = test_client.get("/api/clips").json()["items"][0]
    assert clip["category"] == "疼点"
    assert clip["tags"]["分类"] == ["疼点"]

    # A second scan finds nothing new, so "scan and analyze" must not queue work.
    test_client.post("/api/scan", json={"path": str(media_dir)})
    job = wait_job(test_client)
    assert job["result"]["source_ids"] == []
    response = test_client.post("/api/analyze", json={"source_ids": []})
    assert response.status_code == 200
    assert response.json()["started"] is False


def test_scan_material_type_is_exposed_and_analyzes_hard_cut_shots(client):
    test_client, _, tmp_path = client
    media_dir = tmp_path / "finished"
    media_dir.mkdir()
    video = media_dir / "finished.mp4"
    if not make_video(video):
        pytest.skip("ffmpeg is required for the integration test")

    response = test_client.post(
        "/api/scan", json={"path": str(media_dir), "material_type": "成片"}
    )
    assert response.status_code == 200
    job = wait_job(test_client)
    assert job["status"] == "done"
    test_client.post("/api/analyze", json={"source_ids": job["result"]["source_ids"]})
    job = wait_job(test_client, attempts=400)
    assert job["status"] == "done", job
    clips = test_client.get("/api/clips", params={"material_type": "成片"}).json()
    assert clips["total"] >= 1
    assert all(item["material_type"] == "成片" for item in clips["items"])
    assert all("成片" in item["tags"].get("素材类型", []) for item in clips["items"])


def test_category_edit_applies_to_clip_and_search(client):
    test_client, _, tmp_path = client
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    video = media_dir / "demo.mp4"
    if not make_video(video):
        pytest.skip("ffmpeg is required for the integration test")

    test_client.post("/api/scan", json={"path": str(media_dir)})
    wait_job(test_client)
    test_client.post("/api/analyze", json={})
    wait_job(test_client, attempts=400)
    clip = test_client.get("/api/clips").json()["items"][0]
    assert clip["category"] == ""

    updated = test_client.patch(
        f"/api/clips/{clip['id']}",
        json={"category": "痛点使用 / 痛点涂抹"},
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["category"] == "疼点使用 / 疼点涂抹"
    assert body["tags"]["分类"] == ["疼点使用 / 疼点涂抹"]

    found = test_client.get("/api/clips", params={"category": "疼点使用"}).json()
    # Every clip cut from the source follows the source-level category.
    assert found["total"] >= 1
    assert all(item["category"] == "疼点使用 / 疼点涂抹" for item in found["items"])
    assert clip["id"] in {item["id"] for item in found["items"]}
    tags = test_client.get("/api/tags").json()
    assert tags["category_counts"]["疼点使用"] == tags["category_counts"]["疼点使用 / 疼点涂抹"]
