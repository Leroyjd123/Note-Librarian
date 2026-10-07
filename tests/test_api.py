"""API tests: uploads, ownership, .xlsm handling, size limits and cleanup."""
import io
import os
import sys
import time

import openpyxl
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend import main  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.jobs import create_job, get_job  # noqa: E402


def _xlsx_bytes(sheets=("Notes",)):
    wb = openpyxl.Workbook()
    wb.active.title = sheets[0]
    wb.active.append(["TITLE", "NOTE", "TAGS", "COLOR"])
    wb.active.append(["t", "n", "Faith", 4])
    for name in sheets[1:]:
        ws = wb.create_sheet(name)
        ws.append(["TITLE", "NOTE"])
        ws.append(["a", "b"])
        ws.append([None, None])  # blank row: not counted
        ws.append(["c", "d"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(main, "JOBS_DIR", tmp_path / "jobs")
    (tmp_path / "uploads").mkdir()
    (tmp_path / "jobs").mkdir()
    main._UPLOADS.clear()
    with TestClient(main.app) as c:
        yield c


def _upload(client, name="notes.xlsx", data=None):
    return client.post("/api/upload", files={"file": (name, data or _xlsx_bytes())})


def test_upload_reports_columns_and_sheets(client):
    r = _upload(client, data=_xlsx_bytes(("Notes", "Other")))
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["sheet_name"] == "Notes" and info["sheets"] == ["Notes", "Other"]
    assert info["missing_targets"] == []

    r = client.get(f"/api/uploads/{info['file_id']}/sheet", params={"name": "Other"})
    assert r.status_code == 200
    assert r.json()["row_count"] == 2 and r.json()["missing_targets"] == ["TAGS", "COLOR"]

    r = client.get(f"/api/uploads/{info['file_id']}/sheet", params={"name": "Missing"})
    assert r.status_code == 400


def test_uploads_belong_to_the_uploading_browser(client):
    fid = _upload(client).json()["file_id"]
    other = TestClient(main.app)  # fresh cookie jar = another browser
    r = other.get(f"/api/uploads/{fid}/sheet", params={"name": "Notes"})
    assert r.status_code == 404
    r = other.post("/api/process", json={"file_id": fid, "provider": "anthropic", "model": "m", "api_key": "k"})
    assert r.status_code == 404


def test_jobs_belong_to_the_creating_browser(client, tmp_path):
    client.get("/api/config")  # obtain a session cookie
    out = tmp_path / "jobs" / "x" / "output.xlsx"
    out.parent.mkdir()
    out.write_bytes(_xlsx_bytes())
    job = create_job(mode="full", provider="anthropic", model="m", output_path=str(out),
                     download_name="n.v8.xlsx", owner=client.cookies.get(main.SESSION_COOKIE))
    assert client.get(f"/api/jobs/{job.id}").status_code == 200
    assert client.get(f"/api/jobs/{job.id}/download").status_code == 404  # not done yet
    job.status = "done"
    assert client.get(f"/api/jobs/{job.id}/download").status_code == 200
    other = TestClient(main.app)
    assert other.get(f"/api/jobs/{job.id}").status_code == 404
    assert other.get(f"/api/jobs/{job.id}/download").status_code == 404


def test_xlsm_keeps_its_extension(client, monkeypatch):
    started = {}

    async def fake_run_job(job, path, req):
        started["path"] = path
        job.status = "done"

    monkeypatch.setattr(main.engine, "run_job", fake_run_job)
    fid = _upload(client, name="macros.xlsm").json()["file_id"]
    r = client.post("/api/process", json={"file_id": fid, "provider": "anthropic", "model": "m", "api_key": "k"})
    assert r.status_code == 200, r.text
    assert started["path"].endswith(".xlsm")
    r = client.get(f"/api/jobs/{r.json()['job_id']}/download")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/vnd.ms-excel.sheet.macroEnabled.12"
    assert 'filename="macros.v8.xlsm"' in r.headers["content-disposition"]


def test_upload_size_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    r = _upload(client, data=b"x" * (1024 * 1024 + 10))
    assert r.status_code == 413
    assert list(main.UPLOAD_DIR.iterdir()) == []


def test_bad_extension_and_bad_file(client):
    assert _upload(client, name="notes.csv").status_code == 400
    assert _upload(client, data=b"not a zip").status_code == 400
    assert list(main.UPLOAD_DIR.iterdir()) == []


def test_cleanup_removes_old_uploads_and_jobs(client, tmp_path):
    fid = _upload(client).json()["file_id"]
    out = tmp_path / "jobs" / "old" / "output.xlsx"
    out.parent.mkdir()
    out.write_bytes(b"x")
    job = create_job(mode="full", provider="anthropic", model="m", output_path=str(out),
                     download_name="n", owner="s", status="done")
    running_out = tmp_path / "jobs" / "busy" / "output.xlsx"
    running_out.parent.mkdir()
    running_out.write_bytes(b"x")
    busy = create_job(mode="full", provider="anthropic", model="m", output_path=str(running_out),
                      download_name="n", owner="s", status="running")

    main.cleanup(now=time.time() + settings.retention_minutes * 60 + 5)
    assert fid not in main._UPLOADS
    assert list(main.UPLOAD_DIR.iterdir()) == []
    assert get_job(job.id) is None and not out.parent.exists()
    assert get_job(busy.id) is not None and running_out.exists()
