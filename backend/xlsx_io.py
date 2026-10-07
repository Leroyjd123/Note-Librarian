"""Surgical .xlsx reader/writer.

Edits only specific cells (by column letter) in one worksheet, rewriting just the
affected ``<row>`` blocks in the sheet XML. String cells become inline strings;
chosen columns can be written as numbers. Everything else in the workbook
(sharedStrings, styles, hyperlinks, other cells/rows/sheets) is preserved
byte-for-byte. Edits are held in memory until ``save()``; with ``backup=True`` a
``.bak`` of the original file is created on the first save.

This is the safety-critical core of the app and is covered by tests/test_xlsx_io.py.
"""
from __future__ import annotations

import html
import os
import re
import shutil
import zipfile
from typing import Optional

# Full cell token (handles both ``<c .../>`` and ``<c ...>...</c>``).
_CELL_RE = re.compile(r'<c\b[^>]*?\br="([A-Z]+)(\d+)"[^>]*?(?:/>|>.*?</c>)', re.S)
# Rows may be self-closing (``<row r="5" ht="30" customHeight="1"/>``) when they
# hold no cells; group 2 is None for those.
_ROW_RE = re.compile(r'<row\b[^>]*?\br="(\d+)"[^>]*?(?:/>|>(.*?)</row>)', re.S)
# Characters that are not allowed anywhere in an XML 1.0 document.
_XML_ILLEGAL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")

# Refuse workbooks that would inflate to more than this in memory (zip bombs).
MAX_UNCOMPRESSED_BYTES = 300 * 1024 * 1024


def col_to_idx(col: str) -> int:
    """'A' -> 1, 'B' -> 2, 'AA' -> 27 ..."""
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - 64)
    return n


def idx_to_col(idx: int) -> str:
    s = ""
    while idx > 0:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def _xml_escape(s: str) -> str:
    s = _XML_ILLEGAL_RE.sub("", s)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class WorkbookEditor:
    """Loads a workbook into memory and applies surgical cell edits."""

    def __init__(
        self,
        path: str,
        sheet_name: Optional[str] = None,
        backup: bool = True,
        max_uncompressed: int = MAX_UNCOMPRESSED_BYTES,
    ):
        self.path = path
        self.backup = backup
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if sum(i.file_size for i in infos) > max_uncompressed:
                raise ValueError("Workbook is too large once decompressed.")
            self.names = [i.filename for i in infos]
            self.parts = {n: z.read(n) for n in self.names}
        self.sheet_name, self.sheet_path = self._resolve_sheet(sheet_name)
        self.sheet_xml = self.parts[self.sheet_path].decode("utf-8")
        self.shared = self._load_shared()
        self.rows = self._parse_rows()
        self._bak_done = False

    # ----- workbook structure -----
    def list_sheets(self) -> list[str]:
        wb = self.parts.get("xl/workbook.xml", b"").decode("utf-8")
        return [html.unescape(m.group(1)) for m in re.finditer(r'<sheet\b[^>]*?\bname="([^"]*)"', wb)]

    def _resolve_sheet(self, want: Optional[str]):
        wb = self.parts.get("xl/workbook.xml", b"").decode("utf-8")
        rels = self.parts.get("xl/_rels/workbook.xml.rels", b"").decode("utf-8")
        relmap = {}
        for m in re.finditer(r"<Relationship\b[^>]*?/>", rels):
            tag = m.group(0)
            rid = re.search(r'Id="([^"]*)"', tag)
            tgt = re.search(r'Target="([^"]*)"', tag)
            if rid and tgt:
                relmap[rid.group(1)] = tgt.group(1)
        ordered = []
        for m in re.finditer(r"<sheet\b[^>]*?/>", wb):
            tag = m.group(0)
            nm = re.search(r'name="([^"]*)"', tag)
            rid = re.search(r'r:id="([^"]*)"', tag) or re.search(r'\bid="([^"]*)"', tag)
            if not (nm and rid):
                continue
            tgt = relmap.get(rid.group(1))
            if tgt:
                tgt = tgt.lstrip("/")
                if not tgt.startswith("xl/"):
                    tgt = "xl/" + tgt
            ordered.append((html.unescape(nm.group(1)), tgt))

        candidates = sorted(n for n in self.names if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        if want:
            for name, tgt in ordered:
                if name == want and tgt in self.parts:
                    return name, tgt
            raise ValueError(f"Sheet '{want}' not found in workbook.")
        for name, tgt in ordered:
            if tgt and tgt in self.parts:
                return name, tgt
        if not candidates:
            raise ValueError("No worksheet found in workbook.")
        first_name = ordered[0][0] if ordered else "Sheet1"
        return first_name, candidates[0]

    # ----- parsing -----
    def _load_shared(self) -> list[str]:
        raw = self.parts.get("xl/sharedStrings.xml")
        if not raw:
            return []
        xml = raw.decode("utf-8")
        out = []
        for si in re.findall(r"<si>(.*?)</si>", xml, re.S):
            ts = re.findall(r"<t[^>]*>(.*?)</t>", si, re.S)
            out.append(html.unescape("".join(ts)))
        return out

    def _cell_value(self, token: str) -> str:
        if token.endswith("/>"):
            return ""
        m = re.search(r">(.*)</c>$", token, re.S)
        inner = m.group(1) if m else ""
        if 't="s"' in token:
            v = re.search(r"<v>(.*?)</v>", inner, re.S)
            if v:
                idx = int(v.group(1))
                return self.shared[idx] if 0 <= idx < len(self.shared) else ""
            return ""
        if 't="inlineStr"' in token:
            ts = re.findall(r"<t[^>]*>(.*?)</t>", inner, re.S)
            return html.unescape("".join(ts))
        if 't="str"' in token:
            v = re.search(r"<v>(.*?)</v>", inner, re.S)
            return html.unescape(v.group(1)) if v else ""
        v = re.search(r"<v>(.*?)</v>", inner, re.S)
        return v.group(1) if v else ""

    def _parse_rows(self) -> dict[int, dict[str, str]]:
        rows: dict[int, dict[str, str]] = {}
        for rm in _ROW_RE.finditer(self.sheet_xml):
            rn = int(rm.group(1))
            cells = {}
            for cm in _CELL_RE.finditer(rm.group(2) or ""):
                cells[cm.group(1)] = self._cell_value(cm.group(0))
            rows[rn] = cells
        return rows

    # ----- public helpers -----
    def header_row(self) -> int:
        return min(self.rows) if self.rows else 1

    def _header_cells(self) -> list[tuple[str, str]]:
        """(column letter, header name) pairs from the header row, left to right."""
        if not self.rows:
            return []
        cells = self.rows[self.header_row()]
        return [(c, cells[c].strip()) for c in sorted(cells, key=col_to_idx) if cells[c].strip()]

    def header_map(self) -> dict[str, str]:
        """Header name -> column letter. If a name repeats, the leftmost column wins."""
        out: dict[str, str] = {}
        for c, name in self._header_cells():
            out.setdefault(name, c)
        return out

    def duplicate_headers(self) -> list[str]:
        """Header names that appear in more than one column."""
        seen: dict[str, int] = {}
        for _, name in self._header_cells():
            seen[name] = seen.get(name, 0) + 1
        return [n for n, k in seen.items() if k > 1]

    def _max_col_index(self) -> int:
        mx = 0
        for cells in self.rows.values():
            for c in cells:
                mx = max(mx, col_to_idx(c))
        m = re.search(r'<dimension ref="[A-Z]+\d+:([A-Z]+)\d+"', self.sheet_xml)
        if m:
            mx = max(mx, col_to_idx(m.group(1)))
        return mx

    def _extend_dimension(self, last_col: str) -> None:
        self.sheet_xml = re.sub(
            r'<dimension ref="([A-Z]+\d+):[A-Z]+(\d+)"\s*/>',
            lambda m: f'<dimension ref="{m.group(1)}:{last_col}{m.group(2)}"/>',
            self.sheet_xml,
            count=1,
        )

    def ensure_columns(self, names: list[str]) -> dict[str, str]:
        """Make sure each header name exists; create missing ones as new columns
        appended after the last used column. Returns {name: column_letter}."""
        hr = self.header_row()
        existing = self.header_map()
        result: dict[str, str] = {}
        new_headers: dict[str, object] = {}
        next_idx = self._max_col_index()
        for name in names:
            if name in existing:
                result[name] = existing[name]
                continue
            next_idx += 1
            letter = idx_to_col(next_idx)
            result[name] = letter
            new_headers[letter] = name
        if new_headers:
            self.apply_edits({hr: new_headers}, save=False)   # also updates self.rows
            self._extend_dimension(idx_to_col(next_idx))
        return result

    # ----- writing -----
    def _build_cell(self, col: str, rn: int, val, style: Optional[str], numeric: bool) -> str:
        s_attr = f' s="{style}"' if style is not None else ""
        if numeric:
            return f'<c r="{col}{rn}"{s_attr}><v>{int(val)}</v></c>'
        txt = _xml_escape("" if val is None else str(val))
        return f'<c r="{col}{rn}"{s_attr} t="inlineStr"><is><t xml:space="preserve">{txt}</t></is></c>'

    def apply_edits(
        self, edits: dict[int, dict[str, object]], numeric_cols=frozenset(), save: bool = True
    ) -> int:
        """edits: {row_number: {col_letter: value}}. Returns number of cells written.

        Only rows that already exist in the sheet are edited. With ``save=False``
        the change stays in memory until ``save()`` is called."""
        if not edits:
            return 0
        changed = 0

        def repl(m: re.Match) -> str:
            nonlocal changed
            rn = int(m.group(1))
            if rn not in edits:
                return m.group(0)
            whole = m.group(0)
            if m.group(2) is None:  # self-closing <row .../>: reopen it
                head = whole[:-2].rstrip() + ">"
                body = ""
            else:
                head = whole[: whole.index(">") + 1]
                body = m.group(2)
            tokens: dict[str, str] = {}
            styles: dict[str, Optional[str]] = {}
            for cm in _CELL_RE.finditer(body):
                col = cm.group(1)
                tokens[col] = cm.group(0)
                sm = re.search(r'\bs="(\d+)"', cm.group(0))
                styles[col] = sm.group(1) if sm else None
            default_style = next((styles[c] for c in sorted(tokens, key=col_to_idx) if styles.get(c)), None)
            for col, val in edits[rn].items():
                if val is None:
                    continue
                numeric = col in numeric_cols
                tokens[col] = self._build_cell(col, rn, val, styles.get(col, default_style), numeric)
                self.rows.setdefault(rn, {})[col] = str(int(val)) if numeric else str(val)
                changed += 1
            ordered = sorted(tokens, key=col_to_idx)
            return head + "".join(tokens[c] for c in ordered) + "</row>"

        self.sheet_xml = _ROW_RE.sub(repl, self.sheet_xml)
        if save:
            self.save()
        return changed

    def save(self) -> None:
        """Write the workbook back to ``self.path`` (atomically, via a temp file)."""
        if self.backup and not self._bak_done:
            bak = self.path + ".bak"
            if not os.path.exists(bak):
                shutil.copy2(self.path, bak)
            self._bak_done = True
        self.parts[self.sheet_path] = self.sheet_xml.encode("utf-8")
        tmp = self.path + ".tmp"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zo:
            for n in self.names:
                zo.writestr(n, self.parts[n])
        os.replace(tmp, self.path)
