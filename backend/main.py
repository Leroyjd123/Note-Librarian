"""FastAPI application: upload, process, poll, download + static frontend."""
from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import engine
from .config import configured_providers, settings
from .jobs import create_job, get_job
from .schemas import JobStatus, ProcessRequest, ReviewRow, UploadInfo
from .xlsx_io import WorkbookEditor

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / settings.data_dir
UPLOAD_DIR = DATA_DIR / "uploads"
JOBS_DIR = DATA_DIR / "jobs"
FRONTEND_DIR = BASE_DIR / "frontend"
for d in (UPLOAD_DIR, JOBS_DIR):
    d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Note Librarian")

# file_id -> {path, filename}
_UPLOADS: dict[str, dict] = {}


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
    }


@app.post("/api/upload", response_model=UploadInfo)
async def upload(file: UploadFile = File(...)) -> UploadInfo:
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Please upload an .xlsx workbook.")
    file_id = uuid.uuid4().hex[:12]
    dest = UPLOAD_DIR / f"{file_id}.xlsx"
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        wb = WorkbookEditor(str(dest))
    except Exception as exc:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"Could not read workbook: {exc}") from exc

    headers = wb.header_map()
    hr = wb.header_row()
    row_count = sum(1 for r in wb.rows if r != hr)
    missing = [h for h in engine.TARGET_HEADERS if h not in headers]
    _UPLOADS[file_id] = {"path": str(dest), "filename": file.filename}
    return UploadInfo(
        file_id=file_id,
        filename=file.filename,
        sheet_name=wb.sheet_name,
        sheets=wb.list_sheets(),
        row_count=row_count,
        headers=headers,
        missing_targets=missing,
    )


@app.post("/api/process")
async def process(req: ProcessRequest) -> dict:
    up = _UPLOADS.get(req.file_id)
    if not up:
        raise HTTPException(404, "Unknown file_id - please re-upload.")
    providers = configured_providers()
    has_env_key = providers.get(req.provider, {}).get("configured")
    has_request_key = bool(req.api_key and req.api_key.strip())
    if not (has_env_key or has_request_key):
        raise HTTPException(
            400,
            f"No API key for '{req.provider}'. Paste one in the form, or add it to .env and restart.",
        )

    job_dir = JOBS_DIR / uuid.uuid4().hex[:8]
    job_dir.mkdir(parents=True, exist_ok=True)
    base = Path(up["filename"]).stem
    output_path = job_dir / "output.xlsx"
    shutil.copy2(up["path"], output_path)

    job = create_job(
        mode=req.mode,
        provider=req.provider,
        model=req.model,
        output_path=str(output_path),
        download_name=f"{base}.v8.xlsx",
    )
    asyncio.create_task(engine.run_job(job, str(output_path), req))
    return {"job_id": job.id}


@app.get("/api/jobs/{job_id}", response_model=JobStatus)
def job_status(job_id: str) -> JobStatus:
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Unknown job.")
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
def download(job_id: str) -> FileResponse:
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Unknown job.")
    if not os.path.exists(job.output_path):
        raise HTTPException(404, "Output not found.")
    return FileResponse(
        job.output_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=job.download_name,
    )


# Static frontend (registered last so /api/* routes take precedence).
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
