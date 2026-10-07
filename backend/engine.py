"""Core processing engine: batch notes, call the LLM, apply edits, build reports."""
from __future__ import annotations

import asyncio
import json
import random
import re

from . import prompts
from .config import resolve_api_key
from .providers import get_provider
from .providers.base import ModelDeclined, is_transient
from .xlsx_io import WorkbookEditor

TARGET_HEADERS = ["TITLE", "NOTE", "TAGS", "COLOR"]
CONTEXT_HEADERS = ["HEADING", "PUB", "BK", "CH", "VS", "Reference"]
_WORKFLOW = {"review", "expand", "stub"}
_CONFIDENCE = {"HIGH", "MEDIUM", "LOW"}
_COLORS = range(1, 7)

# Retries for rate limits / overload / network blips, on top of the SDKs' own.
RETRY_ATTEMPTS = 4
RETRY_BASE_DELAY = 2.0


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


def _text(v) -> str | None:
    """A model-supplied text field as a string, or None if missing/unusable."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return None


def _tags_text(v) -> str | None:
    """Tags as "A | B | C". Accepts a pipe-separated string or a list of strings."""
    if isinstance(v, list):
        parts = [str(t).strip() for t in v if isinstance(t, (str, int)) and str(t).strip()]
        return " | ".join(parts)
    return _text(v)


def _color(v) -> int | None:
    """A colour number 1-6, or None if the value is missing or out of range."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, float):
        if not v.is_integer():
            return None
        v = int(v)
    try:
        n = int(str(v).strip())
    except ValueError:
        return None
    return n if n in _COLORS else None


def _confidence(v) -> str:
    s = (_text(v) or "").strip().upper()
    return s if s in _CONFIDENCE else ""


def _build_user(batch: list[dict]) -> str:
    return (
        "Here are the notes for this batch as a JSON array. Process EVERY element and "
        'return a JSON object {"results": [ ... ]} with one result per note, in the same order.\n\n'
        + json.dumps(batch, ensure_ascii=False)
    )


class ProviderCallError(RuntimeError):
    """The model call itself failed (bad key/model, or still failing after retries) - fatal."""


async def _call_with_retry(provider, system: str, user: str) -> str:
    """Call the model, retrying transient failures with exponential backoff."""
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return await asyncio.to_thread(provider.complete, system, user)
        except ModelDeclined:
            raise  # a content problem, not an outage: the caller splits the batch
        except Exception as exc:  # noqa: BLE001
            if attempt == RETRY_ATTEMPTS - 1 or not is_transient(exc):
                raise ProviderCallError(str(exc)) from exc
            await asyncio.sleep(RETRY_BASE_DELAY * (2**attempt) + random.uniform(0, 1))
    raise AssertionError("unreachable")


async def _process_batch(
    provider, system: str, batch: list[dict], warnings: list[str], failed: set[int] | None = None
) -> list[dict]:
    """Call the model for one batch.

    A failed *API call* is fatal (it usually means a bad key/model and would affect
    every batch). A reply that merely can't be parsed is recovered by splitting the
    batch, and only a single unparseable note is ever left unchanged (with a warning).
    """
    try:
        text = await _call_with_retry(provider, system, _build_user(batch))
        results = parse_results(text)
        if len(batch) > 1 and len(results) < max(1, int(len(batch) * 0.6)):
            raise ValueError(f"only {len(results)}/{len(batch)} results returned")
        return results
    except ProviderCallError:
        raise
    except Exception as exc:  # noqa: BLE001 - content/parse problem -> recover
        if len(batch) > 1:
            mid = len(batch) // 2
            left = await _process_batch(provider, system, batch[:mid], warnings, failed)
            right = await _process_batch(provider, system, batch[mid:], warnings, failed)
            return left + right
        warnings.append(f"Row {batch[0].get('row')}: left unchanged ({exc}).")
        if failed is not None:
            failed.add(batch[0].get("row"))
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


def _apply(
    wb: WorkbookEditor,
    headers: dict,
    mode: str,
    results: list[dict],
    extra: dict | None = None,
    allowed_rows: set[int] | None = None,
) -> dict:
    """Turn model results into cell edits (in memory; nothing is saved here).

    Only results whose row was actually sent in this batch are applied, each row at
    most once, and a field the model left out or sent in the wrong shape is left
    untouched rather than blanked.
    """
    extra = extra or {}
    T, N, G, C = (headers.get(k) for k in ("TITLE", "NOTE", "TAGS", "COLOR"))
    edits: dict[int, dict] = {}
    counts = {"title": 0, "note": 0, "tags": 0, "color": 0}
    review: list[dict] = []
    pending: dict[str, list[int]] = {}
    seen: set[int] = set()
    ignored: list[str] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        try:
            row = int(r["row"])
        except (KeyError, TypeError, ValueError):
            continue
        if (allowed_rows is not None and row not in allowed_rows) or row in seen:
            ignored.append(str(row))
            continue
        seen.add(row)
        cur = wb.rows.get(row, {})
        cell: dict[str, object] = {}
        confidence = _confidence(r.get("confidence"))
        if mode == "full":
            title, note = _text(r.get("title")), _text(r.get("note"))
            if T and r.get("title_changed") is True and title and title.strip() and _norm(title) != _norm(cur.get(T, "")):
                cell[T] = title
                counts["title"] += 1
            if N and r.get("note_changed") is True and note and note.strip() and _norm(note) != _norm(cur.get(N, "")):
                cell[N] = note
                counts["note"] += 1
            new_tags = _tags_text(r.get("tags"))
            if G and new_tags is not None and new_tags.strip() != _norm(cur.get(G, "")).strip():
                cell[G] = new_tags.strip()
                counts["tags"] += 1
            color = _color(r.get("color"))
            if C and color is not None and str(color) != str(cur.get(C, "")).strip():
                cell[C] = color
                counts["color"] += 1
            is_review = r.get("review") is True or confidence in ("MEDIUM", "LOW")
            note_type = (_text(r.get("type")) or "").strip()
            if is_review:
                review.append({"row": row, "type": note_type, "title": title or cur.get(T, ""), "confidence": confidence})
            pend = r.get("pending")
            for pt in (pend if isinstance(pend, list) else []):
                if isinstance(pt, str) and pt.strip():
                    pending.setdefault(pt.strip(), []).append(row)
            # extra columns (created beforehand): always populated
            extra_vals = {"TYPE": note_type, "CONFIDENCE": confidence, "REVIEW": "Yes" if is_review else "No"}
            for name, letter in extra.items():
                val = extra_vals.get(name, "")
                if _norm(val) != _norm(cur.get(letter, "")):
                    cell[letter] = val
        else:  # notes
            action = _text(r.get("action")) or ""
            new_note = _text(r.get("new_note"))
            if action == "cleaned" and N and new_note and new_note.strip() and _norm(new_note) != _norm(cur.get(N, "")):
                cell[N] = new_note
                counts["note"] += 1
            if action == "unreconstructable" and G:
                tags = [t.strip() for t in (cur.get(G, "") or "").split("|") if t.strip()]
                if not any(t.lower() in _WORKFLOW for t in tags):
                    cell[G] = " | ".join(tags + ["Review"])
                    counts["tags"] += 1
            if confidence in ("MEDIUM", "LOW") or action == "unreconstructable":
                review.append({"row": row, "type": action, "title": cur.get(T, ""), "confidence": confidence})
        if cell:
            edits[row] = cell
    numeric = {C} if C else set()
    cells_written = wb.apply_edits(edits, numeric_cols=numeric, save=False)
    return {
        "cells": cells_written,
        **counts,
        "review": review,
        "pending": pending,
        "seen": seen,
        "ignored": ignored,
    }


async def run_job(job, output_path: str, req) -> None:
    """Entry point invoked as a background task. Mutates ``job`` in place.

    Edits are collected in memory and the output file is written once, only if the
    whole job succeeds - a failed job never leaves a half-processed workbook.
    """
    tasks: list[asyncio.Task] = []
    try:
        job.status = "running"
        wb = WorkbookEditor(output_path, sheet_name=req.sheet_name, backup=False)
        headers = wb.header_map()
        missing = [h for h in TARGET_HEADERS if h not in headers]
        if len(missing) == len(TARGET_HEADERS):
            raise ValueError("None of TITLE/NOTE/TAGS/COLOR columns were found in the sheet header.")
        if missing:
            job.warnings.append("Columns not found (skipped): " + ", ".join(missing))
        dups = wb.duplicate_headers()
        if dups:
            job.warnings.append("Repeated column headers (the leftmost is used): " + ", ".join(dups))

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
        abort = asyncio.Event()  # set when any batch fails, so queued batches never start

        async def worker(batch: list[dict]) -> None:
            failed: set[int] = set()
            try:
                async with sem:
                    if abort.is_set():
                        return
                    results = await _process_batch(provider, system, batch, job.warnings, failed)
            except BaseException:
                abort.set()
                raise
            sent = {b["row"] for b in batch}
            async with lock:
                # The sheet XML is rewritten in a thread so polling stays responsive.
                out = await asyncio.to_thread(_apply, wb, headers, req.mode, results, extra_cols, sent)
                job.cells_written += out["cells"]
                job.title_edits += out["title"]
                job.note_edits += out["note"]
                job.tag_edits += out["tags"]
                job.color_edits += out["color"]
                job.review.extend(out["review"])
                for tag, rows in out["pending"].items():
                    job.pending.setdefault(tag, []).extend(rows)
                missing_rows = sorted(sent - out["seen"] - failed)  # failed rows were already warned
                if missing_rows:
                    job.warnings.append(
                        "No result returned for rows " + _row_list(missing_rows) + " - left unchanged."
                    )
                if out["ignored"]:
                    job.warnings.append(
                        "Ignored results for rows not in the batch: " + ", ".join(out["ignored"][:20])
                    )
                job.processed_notes += len(batch)
                job.batches_done += 1

        tasks = [asyncio.create_task(worker(b)) for b in batches]
        await asyncio.gather(*tasks)
        await asyncio.to_thread(wb.save)
        job.review.sort(key=lambda x: x["row"])
        job.status = "done"
    except ProviderCallError as exc:
        job.status = "error"
        job.error = f"AI call failed - check your API key and model id. Details: {exc}"
    except Exception as exc:  # noqa: BLE001
        job.status = "error"
        job.error = str(exc)
    finally:
        # Stop any batches still queued or running so a failed job spends nothing more.
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _row_list(rows: list[int], limit: int = 20) -> str:
    shown = ", ".join(str(r) for r in rows[:limit])
    return shown + (f" (+{len(rows) - limit} more)" if len(rows) > limit else "")
