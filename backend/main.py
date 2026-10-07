"""FastAPI application: upload, process, poll, download + static frontend."""
from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import engine
from .config import BASE_DIR, configured_providers, settings
from .jobs import create_job, delete_job, get_job, list_jobs
from .schemas import JobStatus, ProcessRequest, ReviewRow, UploadInfo
from .xlsx_io import WorkbookEditor

DATA_DIR = BASE_DIR / settings.data_dir
UPLOAD_DIR = DATA_DIR / "uploads"
JOBS_DIR = DATA_DIR / "jobs"
FRONTEND_DIR = BASE_DIR / "frontend"
for d in (UPLOAD_DIR, JOBS_DIR):
    d.mkdir(parents=True, exist_ok=True)

MEDIA_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
}
SESSION_COOKIE = "nl_session"

@asynccontextmanager
async def lifespan(_app: FastAPI):
    cleanup()
    task = asyncio.create_task(_cleanup_loop())
    yield
    task.cancel()


app = FastAPI(title="Note Librarian", lifespan=lifespan)

# file_id -> {path, filename, ext, owner, created}
_UPLOADS: dict[str, dict] = {}


# ----- browser sessions -----
# Each browser gets a random session cookie; uploads and jobs belong to the session
# that created them, so one person's notes can't be fetched by another.
@app.middleware("http")
async def session_cookie(request: Request, call_next):
    sid = request.cookies.get(SESSION_COOKIE)
    new = not sid
    if new:
        sid = secrets.token_urlsafe(24)
    request.state.session = sid
    response = await call_next(request)
    if new:
        response.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="strict")
    return response


def _owned_upload(file_id: str, request: Request) -> dict:
    up = _UPLOADS.get(file_id)
    if not up or up["owner"] != request.state.session:
        raise HTTPException(404, "Unknown file_id - please re-upload.")
    return up


def _owned_job(job_id: str, request: Request):
    job = get_job(job_id)
    if not job or job.owner != request.state.session:
        raise HTTPException(404, "Unknown job.")
    return job


# ----- retention -----
def cleanup(now: float | None = None) -> None:
    """Delete uploads and finished jobs older than the retention period."""
    cutoff = (now or time.time()) - settings.retention_minutes * 60
    for fid, up in list(_UPLOADS.items()):
        if up["created"] < cutoff:
            Path(up["path"]).unlink(missing_ok=True)
            _UPLOADS.pop(fid, None)
    busy_dirs = set()
    for job in list_jobs():
        job_dir = Path(job.output_path).parent
        if job.status in ("pending", "running"):
            busy_dirs.add(job_dir)
        elif job.created < cutoff:
            shutil.rmtree(job_dir, ignore_errors=True)
            delete_job(job.id)
    # Leftovers from a previous run of the server (not in memory any more).
    known_uploads = {Path(u["path"]) for u in _UPLOADS.values()}
    for p in UPLOAD_DIR.iterdir():
        if p not in known_uploads and p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)
    known_jobs = {Path(j.output_path).parent for j in list_jobs()} | busy_dirs
    for p in JOBS_DIR.iterdir():
        if p not in known_jobs and p.stat().st_mtime < cutoff:
            shutil.rmtree(p, ignore_errors=True)


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(300)
        try:
            cleanup()
        except Exception:  # noqa: BLE001 - housekeeping must never kill the server
            pass


# ----- API -----
@app.get("/api/config")
def get_config() -> dict:
    return {
        "providers": configured_providers(),
        "defaults": {
            "provider": settings.default_provider,
            "batch_size": settings.default_batch_size,
            "concurrency": settings.default_concurrency,
            "max_tokens": settings.default_max_tokens,
        },
        "max_upload_mb": settings.max_upload_mb,
    }


def _sheet_info(file_id: str, up: dict, sheet: str | None) -> UploadInfo:
    try:
        wb = WorkbookEditor(up["path"], sheet_name=sheet, backup=False)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not read workbook: {exc}") from exc
    headers = wb.header_map()
    hr = wb.header_row()
    return UploadInfo(
        file_id=file_id,
        filename=up["filename"],
        sheet_name=wb.sheet_name,
        sheets=wb.list_sheets(),
        row_count=sum(1 for r in wb.rows if r != hr),
        headers=headers,
        missing_targets=[h for h in engine.TARGET_HEADERS if h not in headers],
        duplicate_headers=wb.duplicate_headers(),
    )


@app.post("/api/upload", response_model=UploadInfo)
async def upload(request: Request, file: UploadFile = File(...)) -> UploadInfo:
    cleanup()
    ext = Path(file.filename or "").suffix.lower()
    if ext not in MEDIA_TYPES:
        raise HTTPException(400, "Please upload an .xlsx workbook.")
    file_id = uuid.uuid4().hex
    dest = UPLOAD_DIR / f"{file_id}{ext}"
    limit = settings.max_upload_mb * 1024 * 1024
    size = 0
    with open(dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"File is larger than {settings.max_upload_mb} MB.")
            f.write(chunk)
    up = {
        "path": str(dest),
        "filename": file.filename,
        "ext": ext,
        "owner": request.state.session,
        "created": time.time(),
    }
    try:
        info = _sheet_info(file_id, up, None)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    _UPLOADS[file_id] = up
    return info


@app.get("/api/uploads/{file_id}/sheet", response_model=UploadInfo)
def sheet_info(file_id: str, name: str, request: Request) -> UploadInfo:
    """Column/row details for another sheet of an uploaded workbook."""
    return _sheet_info(file_id, _owned_upload(file_id, request), name)


@app.post("/api/process")
async def process(req: ProcessRequest, request: Request) -> dict:
    cleanup()
    up = _owned_upload(req.file_id, request)
    providers = configured_providers()
    has_env_key = providers.get(req.provider, {}).get("configured")
    has_request_key = bool(req.api_key and req.api_key.strip())
    if not (has_env_key or has_request_key):
        raise HTTPException(
            400,
            f"No API key for '{req.provider}'. Paste one in the form, or add it to .env and restart.",
        )

    job_dir = JOBS_DIR / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=True)
    base = Path(up["filename"]).stem
    output_path = job_dir / f"output{up['ext']}"
    shutil.copy2(up["path"], output_path)

    job = create_job(
        mode=req.mode,
        provider=req.provider,
        model=req.model,
        output_path=str(output_path),
        download_name=f"{base}.v8{up['ext']}",
        owner=request.state.session,
    )
    asyncio.create_task(engine.run_job(job, str(output_path), req))
    return {"job_id": job.id}


@app.get("/api/jobs/{job_id}", response_model=JobStatus)
def job_status(job_id: str, request: Request) -> JobStatus:
    job = _owned_job(job_id, request)
    return JobStatus(
        id=job.id,
        status=job.status,
        mode=job.mode,
        provider=job.provider,
        model=job.model,
        total_notes=job.total_notes,
        processed_notes=job.processed_notes,
        batches_total=job.batches_total,
        batches_done=job.batches_done,
        cells_written=job.cells_written,
        title_edits=job.title_edits,
        note_edits=job.note_edits,
        tag_edits=job.tag_edits,
        color_edits=job.color_edits,
        review=[ReviewRow(**r) for r in job.review],
        pending={k: sorted(set(v)) for k, v in job.pending.items()},
        warnings=job.warnings,
        error=job.error,
    )


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str, request: Request) -> FileResponse:
    job = _owned_job(job_id, request)
    if job.status != "done" or not os.path.exists(job.output_path):
        raise HTTPException(404, "Output not found.")
    ext = Path(job.output_path).suffix.lower()
    return FileResponse(job.output_path, media_type=MEDIA_TYPES[ext], filename=job.download_name)


# Static frontend (registered last so /api/* routes take precedence).
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
