"""In-memory job registry (sufficient for a single-user local tool)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Job:
    id: str
    mode: str
    provider: str
    model: str
    output_path: str
    download_name: str
    status: str = "pending"
    total_notes: int = 0
    processed_notes: int = 0
    batches_total: int = 0
    batches_done: int = 0
    cells_written: int = 0
    title_edits: int = 0
    note_edits: int = 0
    tag_edits: int = 0
    color_edits: int = 0
    review: list = field(default_factory=list)
    pending: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    error: Optional[str] = None


_JOBS: dict[str, Job] = {}


def create_job(**kwargs) -> Job:
    job = Job(id=uuid.uuid4().hex[:12], **kwargs)
    _JOBS[job.id] = job
    return job


def get_job(job_id: str) -> Optional[Job]:
    return _JOBS.get(job_id)
