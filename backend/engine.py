"""Core processing engine: batch notes, call the LLM, apply edits, build reports."""
from __future__ import annotations

import asyncio
import json
import re

from . import prompts
from .config import resolve_api_key
from .providers import get_provider
from .xlsx_io import WorkbookEditor

TARGET_HEADERS = ["TITLE", "NOTE", "TAGS", "COLOR"]
CONTEXT_HEADERS = ["HEADING", "PUB", "BK", "CH", "VS", "Reference"]
_WORKFLOW = {"review", "expand", "stub"}


def _norm(s) -> str:
    return ("" if s is None else str(s)).replace("\r\n", "\n").replace("\r", "\n")


def parse_results(text: str) -> list[dict]:
    """Tolerantly extract the results array from a model response."""
    if not text:
        return []
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t).strip()
    obj = None
    try:
        obj = json.loads(t)
    except Exception:
        # Grab the outermost {...} or [...]
        for opener, closer in (("{", "}"), ("[", "]")):
            s, e = t.find(opener), t.rfind(closer)
            if s != -1 and e > s:
                try:
                    obj = json.loads(t[s : e + 1])
                    break
                except Exception:
                    continue
    if obj is None:
        raise ValueError("Could not parse JSON from model response.")
    if isinstance(obj, list):
        return obj
    for key in ("results", "notes", "items", "data"):
        if isinstance(obj.get(key), list):
            return obj[key]
    raise ValueError("JSON response did not contain a results array.")


def _build_user(batch: list[dict]) -> str:
    return (
        "Here are the notes for this batch as a JSON array. Process EVERY element and "
        'return a JSON object {"results": [ ... ]} with one result per note, in the same order.\n\n'
        + json.dumps(batch, ensure_ascii=False)
    )


class ProviderCallError(RuntimeError):
    """The model call itself failed (bad key/model, network, rate limit) - treat as fatal."""


async def _process_batch(provider, system: str, batch: list[dict], warnings: list[str]) -> list[dict]:
    """Call the model for one batch.

    A failed *API call* is fatal (it usually means a bad key/model and would affect
    every batch). A reply that merely can't be parsed is recovered by splitting the
    batch, and only a single unparseable note is ever left unchanged (with a warning).
    """
    try:
        text = await asyncio.to_thread(provider.complete, system, _build_user(batch))
    except Exception as exc:  # noqa: BLE001
        raise ProviderCallError(str(exc)) from exc

    try:
        results = parse_results(text)
        if len(batch) > 1 and len(results) < max(1, int(len(batch) * 0.6)):
            raise ValueError(f"only {len(results)}/{len(batch)} results returned")
        return results
    except ProviderCallError:
        raise
    except Exception as exc:  # noqa: BLE001 - content/parse problem -> recover
        if len(batch) > 1:
            mid = len(batch) // 2
            left = await _process_batch(provider, system, batch[:mid], warnings)
            right = await _process_batch(provider, system, batch[mid:], warnings)
            return left + right
        warnings.append(f"Row {batch[0].get('row')}: left unchanged ({exc}).")
        return []


def _ctx(cells: dict, headers: dict, mode: str) -> dict:
    d = {"row": None}
    if mode == "full":
        for name in CONTEXT_HEADERS:
            if name in headers:
                d[name.lower()] = cells.get(headers[name], "")
        d["cur_tags"] = cells.get(headers.get("TAGS", ""), "")
        d["cur_color_IGNORE"] = cells.get(headers.get("COLOR", ""), "")
        d["cur_title"] = cells.get(headers.get("TITLE", ""), "")
        d["cur_note"] = cells.get(headers.get("NOTE", ""), "")
    else:
        if "HEADING" in headers:
            d["heading"] = cells.get(headers["HEADING"], "")
        d["cur_title"] = cells.get(headers.get("TITLE", ""), "")
        d["cur_note"] = cells.get(headers.get("NOTE", ""), "")
    return d


def _apply(wb: WorkbookEditor, headers: dict, mode: str, results: list[dict], extra: dict | None = None) -> dict:
    extra = extra or {}
    T, N, G, C = (headers.get(k) for k in ("TITLE", "NOTE", "TAGS", "COLOR"))
    edits: dict[int, dict] = {}
    counts = {"title": 0, "note": 0, "tags": 0, "color": 0}
    review: list[dict] = []
    pending: dict[str, list[int]] = {}
    for r in results:
        try:
            row = int(r["row"])
        except (KeyError, TypeError, ValueError):
            continue
        cur = wb.rows.get(row, {})
        cell: dict[str, object] = {}
        if mode == "full":
            if T and r.get("title_changed") and str(r.get("title", "")).strip() and _norm(r.get("title")) != _norm(cur.get(T, "")):
                cell[T] = r["title"]
                counts["title"] += 1
            if N and r.get("note_changed") and str(r.get("note", "")).strip() and _norm(r.get("note")) != _norm(cur.get(N, "")):
                cell[N] = r["note"]
                counts["note"] += 1
            if G:
                new_tags = (r.get("tags") or "").strip()
                if new_tags != _norm(cur.get(G, "")).strip():
                    cell[G] = new_tags
                    counts["tags"] += 1
            if C and r.get("color") is not None:
                try:
                    if str(int(r["color"])) != str(cur.get(C, "")).strip():
                        cell[C] = int(r["color"])
                        counts["color"] += 1
                except (TypeError, ValueError):
                    pass
            if r.get("review") or r.get("confidence") in ("MEDIUM", "LOW"):
                review.append({"row": row, "type": r.get("type", ""), "title": r.get("title") or cur.get(T, ""), "confidence": r.get("confidence", "")})
            for pt in (r.get("pending") or []):
                if isinstance(pt, str) and pt.strip():
                    pending.setdefault(pt.strip(), []).append(row)
            # extra columns (created beforehand): always populated
            is_review = bool(r.get("review")) or r.get("confidence") in ("MEDIUM", "LOW")
            extra_vals = {
                "TYPE": r.get("type", ""),
                "CONFIDENCE": r.get("confidence", ""),
                "REVIEW": "Yes" if is_review else "No",
            }
            for name, letter in extra.items():
                val = extra_vals.get(name, "")
                if _norm(val) != _norm(cur.get(letter, "")):
                    cell[letter] = val
        else:  # notes
            action = r.get("action")
            if action == "cleaned" and N and str(r.get("new_note", "")).strip() and _norm(r.get("new_note")) != _norm(cur.get(N, "")):
                cell[N] = r["new_note"]
                counts["note"] += 1
            if action == "unreconstructable" and G:
                tags = [t.strip() for t in (cur.get(G, "") or "").split("|") if t.strip()]
                if not any(t.lower() in _WORKFLOW for t in tags):
                    cell[G] = " | ".join(tags + ["Review"])
                    counts["tags"] += 1
            if r.get("confidence") in ("MEDIUM", "LOW") or action == "unreconstructable":
                review.append({"row": row, "type": action or "", "title": cur.get(T, ""), "confidence": r.get("confidence", "")})
        if cell:
            edits[row] = cell
    numeric = {C} if C else set()
    cells_written = wb.apply_edits(edits, numeric_cols=numeric)
    return {"cells": cells_written, **counts, "review": review, "pending": pending}


async def run_job(job, output_path: str, req) -> None:
    """Entry point invoked as a background task. Mutates ``job`` in place."""
    try:
        job.status = "running"
        wb = WorkbookEditor(output_path, sheet_name=req.sheet_name)
        headers = wb.header_map()
        missing = [h for h in TARGET_HEADERS if h not in headers]
        if "TITLE" in missing and "NOTE" in missing and "TAGS" in missing and "COLOR" in missing:
            raise ValueError("None of TITLE/NOTE/TAGS/COLOR columns were found in the sheet header.")
        if missing:
            job.warnings.append("Columns not found (skipped): " + ", ".join(missing))

        # Resolve the key and build the provider FIRST, so a bad key never modifies the file.
        api_key = resolve_api_key(req.provider, getattr(req, "api_key", None))
        provider = get_provider(req.provider, req.model, req.max_tokens, api_key)
        system = prompts.FULL_SPEC if req.mode == "full" else prompts.notes_spec(req.note_level)

        # Create extra output columns (full pass only) before processing.
        extra_cols: dict[str, str] = {}
        if req.mode == "full":
            want = []
            if getattr(req, "write_type", False):
                want.append("TYPE")
            if getattr(req, "write_confidence", False):
                want.append("CONFIDENCE")
            if getattr(req, "write_review", False):
                want.append("REVIEW")
            if want:
                extra_cols = wb.ensure_columns(want)
                headers = wb.header_map()

        hr = wb.header_row()
        items = []
        for r in sorted(wb.rows):
            if r == hr:
                continue
            ctx = _ctx(wb.rows[r], headers, req.mode)
            ctx["row"] = r
            if req.mode == "notes" and not (ctx.get("cur_note") or "").strip():
                continue
            items.append(ctx)

        job.total_notes = len(items)
        batches = [items[i : i + req.batch_size] for i in range(0, len(items), req.batch_size)]
        job.batches_total = len(batches)

        lock = asyncio.Lock()
        sem = asyncio.Semaphore(req.concurrency)

        async def worker(batch: list[dict]) -> None:
            async with sem:
                results = await _process_batch(provider, system, batch, job.warnings)
            async with lock:
                out = _apply(wb, headers, req.mode, results, extra_cols)
                job.cells_written += out["cells"]
                job.title_edits += out["title"]
                job.note_edits += out["note"]
                job.tag_edits += out["tags"]
                job.color_edits += out["color"]
                job.review.extend(out["review"])
                for tag, rows in out["pending"].items():
                    job.pending.setdefault(tag, []).extend(rows)
                job.processed_notes += len(batch)
                job.batches_done += 1

        await asyncio.gather(*(worker(b) for b in batches))
        job.review.sort(key=lambda x: x["row"])
        job.status = "done"
    except ProviderCallError as exc:
        job.status = "error"
        job.error = f"AI call failed - check your API key and model id. Details: {exc}"
    except Exception as exc:  # noqa: BLE001
        job.status = "error"
        job.error = str(exc)
