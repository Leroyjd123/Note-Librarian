"""Pydantic request/response models."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class UploadInfo(BaseModel):
    file_id: str
    filename: str
    sheet_name: str
    sheets: list[str]
    row_count: int          # data rows (excludes header)
    headers: dict[str, str]  # header name -> column letter
    missing_targets: list[str]  # of TITLE/NOTE/TAGS/COLOR not found


class ProcessRequest(BaseModel):
    file_id: str
    provider: Literal["anthropic", "openai", "gemini"]
    model: str
    # Bring-your-own-key: used for this request only, never stored server-side.
    # If omitted, the server falls back to the matching key in .env.
    api_key: Optional[str] = None
    mode: Literal["full", "notes"] = "full"
    note_level: Literal["spelling", "clarity", "reconstruct"] = "reconstruct"
    batch_size: int = Field(40, ge=1, le=500)
    concurrency: int = Field(4, ge=1, le=16)
    max_tokens: int = Field(16000, ge=1000, le=64000)
    sheet_name: Optional[str] = None
    # Extra columns to add to the output (full pass only). Created if absent.
    write_type: bool = True
    write_confidence: bool = True
    write_review: bool = True


class ReviewRow(BaseModel):
    row: int
    type: str = ""
    title: str = ""
    confidence: str = ""


class JobStatus(BaseModel):
    id: str
    status: Literal["pending", "running", "done", "error"]
    mode: str
    provider: str
    model: str
    total_notes: int = 0
    processed_notes: int = 0
    batches_total: int = 0
    batches_done: int = 0
    cells_written: int = 0
    title_edits: int = 0
    note_edits: int = 0
    tag_edits: int = 0
    color_edits: int = 0
    review: list[ReviewRow] = []
    pending: dict[str, list[int]] = {}
    warnings: list[str] = []
    error: Optional[str] = None
