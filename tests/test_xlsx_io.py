"""Safety tests for the surgical xlsx editor.

These guarantee the core promise: editing only the requested cells leaves the rest
of the workbook (other cells, shared strings, styles, hyperlinks) byte-for-byte intact.
"""
import os
import sys
import zipfile

import openpyxl
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.xlsx_io import WorkbookEditor, col_to_idx, idx_to_col  # noqa: E402


def _make_workbook(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "JWLManager"
    ws.append(["TITLE", "NOTE", "TAGS", "COLOR", "Link"])
    ws.append(["Old title", "Old note", "Faith", 6, "x"])
    ws.append(["Second", "Note two", "", 1, "y"])     # empty TAGS cell
    ws["E2"].hyperlink = "https://example.com/a"
    ws["E3"].hyperlink = "https://example.com/b"
    wb.save(path)


def test_col_helpers():
    assert col_to_idx("A") == 1 and col_to_idx("Z") == 26 and col_to_idx("AA") == 27
    assert idx_to_col(1) == "A" and idx_to_col(27) == "AA"


def test_roundtrip_only_targets_change(tmp_path):
    path = str(tmp_path / "wb.xlsx")
    _make_workbook(path)

    wb = WorkbookEditor(path)
    headers = wb.header_map()
    assert headers["TITLE"] == "A" and headers["COLOR"] == "D"

    # Edit title (string), color (numeric), and insert a TAGS value where empty.
    wb.apply_edits(
        {
            2: {headers["TITLE"]: "New & better <title>", headers["COLOR"]: 4},
            3: {headers["TAGS"]: "Inserted | Tag"},
        },
        numeric_cols={headers["COLOR"]},
    )

    # Re-read with openpyxl (independent reader) to verify values + integrity.
    out = openpyxl.load_workbook(path)["JWLManager"]
    assert out["A2"].value == "New & better <title>"
    assert out["D2"].value == 4
    assert out["C3"].value == "Inserted | Tag"
    # Untouched cells unchanged
    assert out["B2"].value == "Old note"
    assert out["A3"].value == "Second"
    # Hyperlinks preserved
    assert out["E2"].hyperlink is not None
    assert out["E3"].hyperlink is not None


def test_backup_created(tmp_path):
    path = str(tmp_path / "wb.xlsx")
    _make_workbook(path)
    wb = WorkbookEditor(path)
    wb.apply_edits({2: {"A": "changed"}})
    assert os.path.exists(path + ".bak")


def test_shared_strings_and_styles_untouched(tmp_path):
    path = str(tmp_path / "wb.xlsx")
    _make_workbook(path)
    with zipfile.ZipFile(path) as z:
        before_styles = z.read("xl/styles.xml")

    wb = WorkbookEditor(path)
    wb.apply_edits({2: {"A": "changed"}})

    with zipfile.ZipFile(path) as z:
        after_styles = z.read("xl/styles.xml")
    assert before_styles == after_styles


def test_ensure_columns_adds_new_and_keeps_data(tmp_path):
    path = str(tmp_path / "wb.xlsx")
    _make_workbook(path)
    wb = WorkbookEditor(path)
    cols = wb.ensure_columns(["TAGS", "CONFIDENCE", "REVIEW"])
    # existing header reused, new ones appended after the last column (E=5 -> F, G)
    assert cols["TAGS"] == "C"
    assert cols["CONFIDENCE"] == "F" and cols["REVIEW"] == "G"
    wb.apply_edits({2: {cols["CONFIDENCE"]: "HIGH", cols["REVIEW"]: "No"}})

    out = openpyxl.load_workbook(path)["JWLManager"]
    assert out["F1"].value == "CONFIDENCE" and out["G1"].value == "REVIEW"
    assert out["F2"].value == "HIGH" and out["G2"].value == "No"
    # original data + hyperlinks intact
    assert out["A2"].value == "Old title" and out["B2"].value == "Old note"
    assert out["E2"].hyperlink is not None
    assert out.max_column == 7


def _write_raw_sheet(path, sheet_data):
    """A minimal workbook whose sheet XML is exactly ``sheet_data`` (to test shapes
    Excel writes but openpyxl does not, e.g. self-closing rows)."""
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Notes" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{sheet_data}</sheetData></worksheet>"
        ),
    }
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)


def _is(col_row, text):
    return f'<c r="{col_row}" t="inlineStr"><is><t>{text}</t></is></c>'


def test_self_closing_row_does_not_swallow_next_row(tmp_path):
    path = str(tmp_path / "wb.xlsx")
    _write_raw_sheet(
        path,
        f'<row r="1">{_is("A1", "TITLE")}{_is("B1", "NOTE")}</row>'
        '<row r="2" ht="30" customHeight="1"/>'
        f'<row r="3">{_is("A3", "keep")}{_is("B3", "note three")}</row>',
    )
    wb = WorkbookEditor(path)
    assert wb.rows[2] == {}
    assert wb.rows[3] == {"A": "keep", "B": "note three"}

    # Editing the empty self-closing row reopens it and leaves row 3 intact.
    wb.apply_edits({2: {"A": "filled"}, 3: {"B": "edited"}})
    out = openpyxl.load_workbook(path)["Notes"]
    assert out["A2"].value == "filled"
    assert out["A3"].value == "keep" and out["B3"].value == "edited"
    assert out.row_dimensions[2].height == 30


def test_illegal_xml_characters_are_stripped(tmp_path):
    path = str(tmp_path / "wb.xlsx")
    _make_workbook(path)
    wb = WorkbookEditor(path)
    wb.apply_edits({2: {"A": "bad\x00 \x0bchars\x1f here"}})
    out = openpyxl.load_workbook(path)["JWLManager"]
    assert out["A2"].value == "bad chars here"


def test_unknown_sheet_is_an_error(tmp_path):
    path = str(tmp_path / "wb.xlsx")
    _make_workbook(path)
    with pytest.raises(ValueError, match="not found"):
        WorkbookEditor(path, sheet_name="Nope")
    assert WorkbookEditor(path, sheet_name="JWLManager").sheet_name == "JWLManager"


def test_oversized_workbook_rejected(tmp_path):
    path = str(tmp_path / "wb.xlsx")
    _make_workbook(path)
    with pytest.raises(ValueError, match="too large"):
        WorkbookEditor(path, max_uncompressed=100)


def test_duplicate_headers_leftmost_wins(tmp_path):
    path = str(tmp_path / "wb.xlsx")
    wb = openpyxl.Workbook()
    wb.active.append(["TITLE", "NOTE", "TITLE"])
    wb.save(path)
    ed = WorkbookEditor(path)
    assert ed.header_map()["TITLE"] == "A"
    assert ed.duplicate_headers() == ["TITLE"]


def test_unsaved_edits_do_not_touch_file(tmp_path):
    path = str(tmp_path / "wb.xlsx")
    _make_workbook(path)
    with open(path, "rb") as f:
        before = f.read()
    wb = WorkbookEditor(path, backup=False)
    wb.apply_edits({2: {"A": "changed"}}, save=False)
    with open(path, "rb") as f:
        assert f.read() == before
    wb.save()
    assert openpyxl.load_workbook(path)["JWLManager"]["A2"].value == "changed"
    assert not os.path.exists(path + ".bak")


def test_sheet_names_with_special_characters(tmp_path):
    path = str(tmp_path / "wb.xlsx")
    wb = openpyxl.Workbook()
    wb.active.title = "Q&A <1>"
    wb.active.append(["TITLE"])
    wb.create_sheet("Second").append(["NOTE"])
    wb.save(path)
    ed = WorkbookEditor(path, sheet_name="Q&A <1>")
    assert ed.list_sheets() == ["Q&A <1>", "Second"]
    assert ed.sheet_name == "Q&A <1>" and "TITLE" in ed.header_map()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
