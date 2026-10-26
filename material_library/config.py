from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.example.json"
LOCAL_CONFIG = ROOT / "config.local.json"

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm",
    ".flv", ".wmv", ".ts", ".mts", ".m2ts", ".mpg", ".mpeg",
}

def _merged(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merged(result[key], value)
        else:
            result[key] = value
    return result


@dataclass(frozen=True)
class AIConfig:
    enabled: bool = False
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    timeout: float = 120.0
    max_frames: int = 4
    concurrency: int = 2
    retries: int = 2

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.base_url and self.model and self.api_key)


@dataclass(frozen=True)
class SegmentConfig:
    target_duration: float = 3.0
    min_duration: float = 1.0
    max_duration: float = 8.0
    sample_fps: int = 8
    analyze_width: int = 160


@dataclass(frozen=True)
class ShotConfig:
    detector: str = "adaptive"
    threshold: float = 3.0
    min_scene_duration: float = 0.4


@dataclass(frozen=True)
class SearchWeights:
    tag: float = 0.40
    text: float = 0.25
    action: float = 0.15
    duration: float = 0.12
    quality: float = 0.08
    default_limit: int = 40


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 5288


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    library_root: Path = ROOT / "data"
    ai: AIConfig = field(default_factory=AIConfig)
    segment: SegmentConfig = field(default_factory=SegmentConfig)
    shots: ShotConfig = field(default_factory=ShotConfig)
    search: SearchWeights = field(default_factory=SearchWeights)
    server: ServerConfig = field(default_factory=ServerConfig)
    # Folder-name normalization applied when deriving ``分类`` from paths.
    # Empty by default so the library never silently renames a client folder.
    category_aliases: dict[str, str] = field(default_factory=dict)
    category_labels: list[str] = field(default_factory=list)
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    source: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.library_root / "library.db"

    @property
    def thumb_dir(self) -> Path:
        return self.library_root / "cache" / "thumbs"

    @property
    def clip_cache_dir(self) -> Path:
        return self.library_root / "cache" / "clips"

    def ensure_dirs(self) -> None:
        for path in (self.library_root, self.thumb_dir, self.clip_cache_dir):
            path.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_settings(path: Path | None = None) -> Settings:
    base = _load_json(DEFAULT_CONFIG)
    local_path = path or Path(os.environ.get("MATERIAL_LIBRARY_CONFIG", LOCAL_CONFIG))
    merged = _merged(base, _load_json(local_path))
    library_root = Path(merged.get("library_root", "data"))
    if not library_root.is_absolute():
        library_root = ROOT / library_root
    library_root = library_root.resolve()

    ai_raw = merged.get("ai", {})
    api_key = (
        os.environ.get("MATERIAL_LIBRARY_API_KEY")
        or str(ai_raw.get("api_key") or "")
    )
    base_url = os.environ.get("MATERIAL_LIBRARY_BASE_URL") or str(ai_raw.get("base_url") or "")
    model = os.environ.get("MATERIAL_LIBRARY_MODEL") or str(ai_raw.get("model") or "")
    enabled = bool(ai_raw.get("enabled", False))
    if os.environ.get("MATERIAL_LIBRARY_AI") == "1":
        enabled = True

    seg = merged.get("segment", {})
    shots = merged.get("shots", {})
    weights = merged.get("search", {})
    server = merged.get("server", {})
    aliases = {
        str(key).strip(): str(value).strip()
        for key, value in (merged.get("category_aliases") or {}).items()
        if str(key).strip() and str(value).strip()
    }
    category_labels = [
        str(item).strip()
        for item in (merged.get("category_labels") or [])
        if str(item).strip()
    ]
    return Settings(
        library_root=library_root,
        ai=AIConfig(
            enabled=enabled,
            base_url=base_url.rstrip("/"),
            model=model,
            api_key=api_key,
            timeout=float(ai_raw.get("timeout", 120)),
            max_frames=int(ai_raw.get("max_frames", 4)),
            concurrency=max(1, int(ai_raw.get("concurrency", 2))),
            retries=max(0, int(ai_raw.get("retries", 2))),
        ),
        segment=SegmentConfig(
            target_duration=float(seg.get("target_duration", 3.0)),
            min_duration=float(seg.get("min_duration", 1.0)),
            max_duration=float(seg.get("max_duration", 8.0)),
            sample_fps=max(2, int(seg.get("sample_fps", 8))),
            analyze_width=max(80, int(seg.get("analyze_width", 160))),
        ),
        shots=ShotConfig(
            detector=str(shots.get("detector", "adaptive")),
            threshold=float(shots.get("threshold", 3.0)),
            min_scene_duration=float(shots.get("min_scene_duration", 0.4)),
        ),
        search=SearchWeights(
            tag=float(weights.get("weight_tag", 0.40)),
            text=float(weights.get("weight_text", 0.25)),
            action=float(weights.get("weight_action", 0.15)),
            duration=float(weights.get("weight_duration", 0.12)),
            quality=float(weights.get("weight_quality", 0.08)),
            default_limit=int(weights.get("default_limit", 40)),
        ),
        server=ServerConfig(
            host=str(server.get("host", "127.0.0.1")),
            port=int(server.get("port", 5288)),
        ),
        category_aliases=aliases,
        category_labels=category_labels,
        ffmpeg=str(merged.get("ffmpeg", "ffmpeg")),
        ffprobe=str(merged.get("ffprobe", "ffprobe")),
        source=local_path,
    )
