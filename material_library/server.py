from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import tags
from .config import ROOT, Settings, load_settings
from .library import Library, LibraryError, SearchQuery

WEB_DIR = ROOT / "web"


class ScanRequest(BaseModel):
    path: str
    recursive: bool = True
    material_type: str = "普通素材"


class AnalyzeRequest(BaseModel):
    limit: int | None = None
    source_ids: list[int] | None = None
    # Explicit opt-in: recut existing sources while preserving reviewed/manual
    # clips. Normal analysis remains incremental and never rewrites old clips.
    reanalyze: bool = False


class TagUpdate(BaseModel):
    description: str | None = None
    transcript: str | None = None
    tags: dict[str, list[str]] | None = None
    category: str | None = None
    reviewed: bool | None = None
    status: str | None = None
    start: float | None = None
    end: float | None = None


class Job:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.kind = ""
        self.status = "idle"
        self.current = 0
        self.total = 0
        self.message = ""
        self.error: str | None = None
        self.result: dict[str, Any] | None = None

    def start(self, kind: str, total: int = 0) -> None:
        with self.lock:
            self.kind = kind
            self.status = "running"
            self.current = 0
            self.total = total
            self.message = ""
            self.error = None
            self.result = None

    def start_if_idle(self, kind: str) -> bool:
        with self.lock:
            if self.status == "running":
                return False
            self.kind = kind
            self.status = "running"
            self.current = 0
            self.total = 0
            self.message = ""
            self.error = None
            self.result = None
            return True

    def update(self, stage: str, current: int, total: int, message: str) -> None:
        with self.lock:
            self.current = current
            self.total = total
            self.message = f"{stage}: {message}"

    def finish(self, result: dict[str, Any]) -> None:
        with self.lock:
            self.status = "done"
            self.result = result
            self.message = "完成"

    def fail(self, error: str) -> None:
        with self.lock:
            self.status = "failed"
            self.error = error
            self.message = error

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "kind": self.kind, "status": self.status, "current": self.current,
                "total": self.total, "message": self.message, "error": self.error,
                "result": self.result,
            }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    library = Library(settings)
    job_state = Job()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.ensure_dirs()
        yield

    app = FastAPI(
        title="AI 智能视频素材库",
        version="0.1.0",
        description="本地素材索引与片段检索接口，供剪辑工具引用。",
        lifespan=lifespan,
    )
    app.state.library = library
    app.state.settings = settings
    app.state.job = job_state
    # One router serves both /api/* (current) and /api/v1/* (stable alias for
    # other tools). Adding the alias now means consumers can pin a version
    # before the two projects are merged.
    api = APIRouter()

    def run_job(kind: str, fn) -> None:
        # Mark running before the thread spawns so a follow-up request (or a
        # client polling immediately) never observes the previous job's state.
        if not job_state.start_if_idle(kind):
            raise HTTPException(409, "已有任务在运行")

        def worker() -> None:
            try:
                result = fn()
            except Exception as error:  # surfaced through /api/job
                job_state.fail(str(error))
            else:
                job_state.finish(result)

        thread = threading.Thread(target=worker, daemon=True, name=f"ml-{kind}")
        thread.start()

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))

    @api.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "version": app.version,
            "ai_enabled": settings.ai.configured,
            "library_root": str(settings.library_root),
        }

    @api.post("/scan")
    def scan(body: ScanRequest) -> dict[str, Any]:
        path = Path(body.path).expanduser()
        if not path.is_dir() and not path.is_file():
            raise HTTPException(400, f"文件或目录不存在: {path}")
        run_job(
            "scan",
            lambda: library.scan(
                path,
                recursive=body.recursive,
                material_type=body.material_type,
                progress=job_state.update,
            ),
        )
        return {"started": True}

    @api.post("/analyze")
    def analyze(body: AnalyzeRequest) -> dict[str, Any]:
        if job_state.snapshot()["status"] == "running":
            raise HTTPException(409, "已有任务在运行")
        if body.source_ids is not None:
            ids = (
                library.source_ids_for_reanalyze(body.source_ids)
                if body.reanalyze
                else library.source_ids_for_analyze(body.source_ids)
            )
            if not ids:
                # Nothing new was found by the preceding scan. Treat it as a
                # successful no-op so the UI does not show a scary error.
                return {"started": False, "reason": "没有需要分析的新素材"}

            def run_ids() -> dict[str, Any]:
                ok, failed = 0, 0
                for index, source_id in enumerate(ids, start=1):
                    job_state.update("analyze", index, len(ids), str(source_id))
                    try:
                        library.analyze_source(source_id, reanalyze=body.reanalyze)
                        ok += 1
                    except Exception:
                        failed += 1
                return {"analyzed": ok, "failed": failed}

            run_job("analyze", run_ids)
        else:
            run_job(
                "analyze",
                lambda: (
                    _reanalyze_all(library, body.limit, job_state)
                    if body.reanalyze
                    else library.analyze_pending(limit=body.limit, progress=job_state.update)
                ),
            )
        return {"started": True}

    def _reanalyze_all(library: Library, limit: int | None, job_state: Job) -> dict[str, int]:
        ids = library.source_ids_for_reanalyze()[:limit] if limit else library.source_ids_for_reanalyze()
        stats = {"analyzed": 0, "failed": 0}
        for index, source_id in enumerate(ids, start=1):
            job_state.update("reanalyze", index, len(ids), str(source_id))
            try:
                library.analyze_source(source_id, reanalyze=True)
                stats["analyzed"] += 1
            except Exception as error:
                stats["failed"] += 1
                library._mark_source_failed(source_id, str(error))
        return stats

    @api.get("/job")
    def job() -> dict[str, Any]:
        return job_state.snapshot()

    @api.get("/stats")
    def stats() -> dict[str, Any]:
        return library.stats()

    @api.get("/tags")
    def list_tags() -> dict[str, Any]:
        vocab = tags.dimensions()
        vocab["分类"] = sorted(
            set(vocab.get("分类", []))
            | set(library.categories())
        )
        return {
            "dimensions": list(tags.DIMENSIONS),
            "ai_dimensions": list(tags.AI_DIMENSIONS),
            "vocabulary": vocab,
            "counts": library.tags_by_dimension(),
            "categories": library.categories(),
            "category_counts": library.category_counts(),
        }

    @api.get("/sources")
    def sources(limit: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
        return {"items": library.list_sources(limit)}

    @api.get("/clips")
    def clips(
        q: str = "",
        tags_param: str = Query("", alias="tags"),
        category: str = "",
        min_duration: float | None = None,
        max_duration: float | None = None,
        status: str | None = None,
        material_type: str | None = None,
        source_id: int | None = None,
        limit: int = Query(40, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        parsed: list[tuple[str, str]] = []
        for item in tags_param.split(","):
            item = item.strip()
            if not item:
                continue
            if ":" in item:
                dimension, name = item.split(":", 1)
                resolved = tags.resolve(name, dimension)
                if resolved is None:
                    continue
                dimension, name = resolved
            else:
                resolved = tags.resolve(item)
                if resolved is None:
                    continue
                dimension, name = resolved
            parsed.append((dimension, name))
        return library.search(SearchQuery(
            keyword=q, tags=parsed,
            categories=[item for item in (category or "").split(",") if item.strip()],
            min_duration=min_duration,
            max_duration=max_duration, status=status, source_id=source_id,
            material_type=material_type,
            limit=limit, offset=offset,
        ))

    @api.get("/clips/{clip_id}")
    def clip(clip_id: str) -> dict[str, Any]:
        value = library.get_clip(clip_id)
        if value is None:
            raise HTTPException(404, "片段不存在")
        return value

    @api.patch("/clips/{clip_id}")
    def update_clip(clip_id: str, body: TagUpdate) -> dict[str, Any]:
        try:
            return library.update_clip(
                clip_id,
                description=body.description,
                transcript=body.transcript,
                tags_map=body.tags,
                reviewed=body.reviewed,
                status=body.status,
                start=body.start,
                end=body.end,
                category=body.category,
            )
        except LibraryError as error:
            raise HTTPException(400, str(error)) from None

    @api.post("/clips/{clip_id}/relabel")
    def relabel(clip_id: str) -> dict[str, Any]:
        if not settings.ai.configured:
            raise HTTPException(400, "AI 未配置")
        try:
            changed = library.label_clip(clip_id, force=True)
        except LibraryError as error:
            raise HTTPException(400, str(error)) from None
        return {"updated": changed, "clip": library.get_clip(clip_id)}

    @api.delete("/clips/{clip_id}")
    def delete_clip(clip_id: str) -> JSONResponse:
        try:
            library.delete_clip(clip_id)
        except LibraryError as error:
            raise HTTPException(404, str(error)) from None
        return JSONResponse({"deleted": clip_id})

    @api.get("/clips/{clip_id}/thumbnail")
    def thumbnail(clip_id: str, index: int = 1) -> FileResponse:
        value = library.get_clip(clip_id)
        if value is None or not value["thumbnails"]:
            raise HTTPException(404, "缩略图不存在")
        thumbs = value["thumbnails"]
        path = Path(thumbs[max(0, min(index, len(thumbs) - 1))])
        if not path.is_file():
            raise HTTPException(404, "缩略图文件缺失")
        return FileResponse(path, media_type="image/jpeg")

    @api.get("/clips/{clip_id}/media")
    def media(clip_id: str) -> FileResponse:
        value = library.get_clip(clip_id)
        if value is None:
            raise HTTPException(404, "片段不存在")
        path = Path(value["path"])
        if not path.is_file():
            raise HTTPException(404, "源文件缺失")
        return FileResponse(path, media_type="video/mp4")

    @api.get("/clips/{clip_id}/preview")
    def preview(clip_id: str) -> FileResponse:
        try:
            path = library.render_cached_clip(clip_id)
        except Exception as error:
            raise HTTPException(400, f"预览生成失败: {error}") from None
        return FileResponse(path, media_type="video/mp4")

    app.include_router(api, prefix="/api")
    app.include_router(api, prefix="/api/v1", include_in_schema=False)

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
    return app


def serve(host: str | None = None, port: int | None = None) -> None:
    import uvicorn

    settings = load_settings()
    app = create_app(settings)
    uvicorn.run(
        app,
        host=host or settings.server.host,
        port=port or settings.server.port,
        log_level="info",
    )
