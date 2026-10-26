from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


class MediaError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: float
    codec: str
    has_audio: bool


def _creation_flags() -> int:
    import os

    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def run(command: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_creation_flags(),
        )
    except FileNotFoundError as error:
        raise MediaError(f"找不到命令: {command[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise MediaError(f"命令超时: {command[0]}") from error


def probe(path: Path, ffprobe: str = "ffprobe") -> MediaInfo:
    result = run([
        ffprobe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ], timeout=60)
    if result.returncode != 0:
        raise MediaError(f"无法读取视频信息: {result.stderr.strip()[:300]}")
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise MediaError("文件中没有视频流")
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    fps_text = str(video.get("avg_frame_rate") or "0/1")
    try:
        num, den = fps_text.split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    if fps <= 0:
        fps = 25.0
    return MediaInfo(
        duration=max(0.0, duration),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=fps,
        codec=str(video.get("codec_name") or ""),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def open_video(path: Path) -> cv2.VideoCapture:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise MediaError(f"无法打开视频: {path}")
    return capture


def sample_frames(
    path: Path,
    sample_fps: int = 8,
    width: int = 160,
    start: float = 0.0,
    end: float | None = None,
) -> tuple[list[float], list[np.ndarray]]:
    """Decode once and return small grayscale frames with timestamps."""
    capture = open_video(path)
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0) or 25.0
        total = float(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        duration = total / fps if total > 0 else 0.0
        end = duration if end is None else min(end, duration or end)
        # OpenCV reports timestamps relative to the requested seek position
        # on some Windows codecs. Keep an absolute origin so shot-local
        # samples are not shifted to the beginning of the source.
        seek_origin = 0.0
        if start > 0:
            capture.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
            seek_origin = start
        step = max(1, round(fps / max(1, sample_fps)))
        times: list[float] = []
        frames: list[np.ndarray] = []
        index = 0
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            timestamp = seek_origin + index / fps
            if timestamp > end:
                break
            if index % step == 0:
                height = max(1, round(frame.shape[0] * width / max(1, frame.shape[1])))
                small = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                times.append(round(timestamp, 4))
                frames.append(gray)
            index += 1
        return times, frames
    finally:
        capture.release()


def detect_shots(
    path: Path,
    *,
    detector: str = "adaptive",
    threshold: float = 3.0,
    min_scene_duration: float = 0.4,
) -> list[tuple[float, float]]:
    from scenedetect import AdaptiveDetector, ContentDetector, SceneManager, open_video as sd_open

    manager = SceneManager()
    min_len = max(1, round(min_scene_duration * 25))
    if detector == "content":
        manager.add_detector(ContentDetector(threshold=threshold, min_scene_len=min_len))
    else:
        manager.add_detector(
            AdaptiveDetector(adaptive_threshold=threshold, min_scene_len=min_len)
        )
    try:
        video = sd_open(str(path))
        manager.detect_scenes(video=video, show_progress=False)
        scenes = manager.get_scene_list(start_in_scene=True)
    except Exception as error:
        raise MediaError(f"镜头检测失败: {error}") from error
    if not scenes:
        info = probe(path)
        return [(0.0, info.duration)]
    # Scene detectors can emit a tiny tail around a fade or codec boundary.
    # Such a fragment is not a useful standalone shot and would make the
    # action locator return a clip shorter than the configured floor.
    minimum = max(0.0, float(min_scene_duration))
    merged: list[tuple[float, float]] = []
    for start, end in scenes:
        current = (float(start.seconds), float(end.seconds))
        if merged and (current[1] - current[0] < minimum
                       or merged[-1][1] - merged[-1][0] < minimum):
            merged[-1] = (merged[-1][0], current[1])
        else:
            merged.append(current)
    return merged


def extract_frame(
    path: Path,
    timestamp: float,
    output: Path,
    *,
    width: int = 480,
    ffmpeg: str = "ffmpeg",
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    result = run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{max(0.0, timestamp):.3f}", "-i", str(path),
        "-frames:v", "1", "-vf", f"scale={width}:-2",
        "-q:v", "3", str(output),
    ], timeout=60)
    if result.returncode != 0 or not output.is_file():
        raise MediaError(f"抽帧失败: {result.stderr.strip()[:300]}")
    return output


def render_clip(
    path: Path,
    start: float,
    end: float,
    output: Path,
    *,
    ffmpeg: str = "ffmpeg",
    crf: int = 20,
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.1, end - start)
    temp = output.with_suffix(".part.mp4")
    result = run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}", "-i", str(path), "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        str(temp),
    ], timeout=900)
    if result.returncode != 0:
        temp.unlink(missing_ok=True)
        raise MediaError(f"裁剪失败: {result.stderr.strip()[:300]}")
    temp.replace(output)
    return output


def has_tool(name: str) -> bool:
    return shutil.which(name) is not None
