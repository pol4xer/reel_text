from __future__ import annotations

import logging
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from reel_text.config import Settings
from reel_text.pipeline import ProcessingError, download_audio, normalize_urls, transcribe_audio
from reel_text.store import JobPending, QueueFull, Store

logger = logging.getLogger(__name__)
Language = Literal["auto", "en", "ru", "uk", "tr", "es", "de", "fr"]
STATIC = Path(__file__).parent / "static"


class JobRequest(BaseModel):
    urls: str = Field(min_length=1, max_length=30_000)
    language: Language = "auto"


class KeyRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=512, repr=False)


def create_app(project_dir: Path | None = None) -> FastAPI:
    root = project_dir or Path(__file__).resolve().parent.parent
    settings = Settings(root)
    store = Store(root / "data" / "reel_text.sqlite3")

    @asynccontextmanager
    async def lifespan(app):
        store.recover()
        app.state.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reel-text")
        yield
        app.state.executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="reel_text", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.store = store
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if origin != expected:
                return JSONResponse(
                    {"detail": "Requests are only allowed from the local application."}, status_code=403
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        return response

    # Do not echo submitted values, including API keys, in validation errors.
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({"detail": "Check your links, selected language, and settings."}, status_code=422)

    def process(job: dict):
        try:
            key = settings.api_key
            if not key:
                raise ProcessingError("Add your Deepgram API key in settings.")
            store.update(job["id"], "downloading")
            with tempfile.TemporaryDirectory(prefix="reel-text-") as directory:
                audio = download_audio(job["url"], Path(directory), cookies_file=settings.cookies_file)
                store.update(job["id"], "transcribing")
                transcript = transcribe_audio(
                    audio, api_key=key, language=job["language"], model=settings.model
                )
                store.update(job["id"], "done", transcript=transcript)
        except ProcessingError as exc:
            store.update(job["id"], "error", error=str(exc))
        except Exception:
            logger.error("Unexpected processing failure for job %s", job["id"])
            store.update(
                job["id"], "error", error="Could not process this item. Try submitting the link again."
            )

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    def status():
        return settings.status()

    @app.post("/api/settings")
    def save_settings(body: KeyRequest):
        try:
            settings.save_api_key(body.api_key)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return settings.status()

    @app.get("/api/jobs")
    def list_jobs():
        return {"jobs": store.list(), "completed_count": store.completed_count()}

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str):
        try:
            deleted = store.delete(job_id)
        except JobPending as exc:
            raise HTTPException(409, str(exc)) from exc
        if not deleted:
            raise HTTPException(404, "Item not found.")
        return {"deleted": 1}

    @app.delete("/api/transcripts")
    def delete_transcripts():
        return {"deleted": store.delete_transcripts()}

    @app.post("/api/jobs", status_code=202)
    def add_jobs(body: JobRequest):
        ready = settings.status()
        if not ready["configured"]:
            raise HTTPException(400, "Add your Deepgram API key in settings.")
        if not ready["ffmpeg"] or not ready["downloader"]:
            raise HTTPException(503, "Downloading requires ffmpeg and yt-dlp. See the project README.")
        try:
            urls = normalize_urls(body.urls)
            jobs = store.create(urls, body.language)
        except (ProcessingError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except QueueFull as exc:
            raise HTTPException(429, str(exc)) from exc
        for job in jobs:
            app.state.executor.submit(process, job)
        return {"jobs": jobs}

    @app.get("/api/jobs/{job_id}/text")
    def download_text(job_id: str):
        job = store.get(job_id)
        if not job:
            raise HTTPException(404, "Item not found.")
        if job["status"] != "done":
            raise HTTPException(409, "The transcript is not ready yet.")
        return PlainTextResponse(
            job["transcript"],
            headers={"Content-Disposition": f'attachment; filename="reel_{job_id[:8]}.txt"'},
        )

    @app.get("/api/export")
    def export_text():
        jobs = [job for job in reversed(store.list()) if job["status"] == "done"]
        if not jobs:
            raise HTTPException(404, "There are no completed transcripts yet.")
        text = "\n\n---\n\n".join(f"{job['url']}\n\n{job['transcript']}" for job in jobs)
        return PlainTextResponse(
            text, headers={"Content-Disposition": 'attachment; filename="reel_text.txt"'}
        )

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
