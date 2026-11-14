"""
Tests for api/routes/text_match.py's extract_matchable_text() -- the
format-detect + extraction step that lets cPARAS's Text Matching upload
accept CSV/XLSX/XLS/DOCX/TXT, not just PDF (v0.3).

Deliberately does not import FastAPI/httpx or spin up the vector store:
extract_matchable_text() is a pure, synchronous function so these tests
run fast and in isolation, matching the convention of
tests/test_csv_parser.py and tests/test_xlsx_parser.py.

Run: python -m pytest backend/tests/test_text_match_extraction.py -q
 or: python3 backend/tests/_run_all.py
"""
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.routes.cparas.text_match import extract_matchable_text, SUPPORTED_FORMATS  # noqa: E402


# A real-shaped ISLD annex, matching the column names actually seen in the
# platform's sample ISLD annex CSVs (Disclosure_Number, Specific_Part as
# the clause/illustrative-part reference, Patent_Application_Title, etc.)
ISLD_CSV = (
    "Disclosure_Number,Project_or_Standard_Name,Work_Item_or_Standard_No,"
    "Specific_Part,Version,Applicant_Holder,Application_No,Publication_No,"
    "Patent_Application_Title,Country_of_Registration\n"
    "1,3GPP 5G NR,TS 22.261,,18.0.0,\"Example Corp\",IN202141005326,,"
    "Method and system for providing access to localized services,INDIA\n"
    "2,3GPP 5G NR,\"TS 38.331; TS 38.212\",,,\"Example Corp\",US202163150391,,"
    "Method and Apparatus of Communications Parameters Measurement,UNITED STATES\n"
)


def test_csv_extracts_per_record_title_standard_clause():
    result = extract_matchable_text("Example_ISLD-202103-010_annex.csv", ISLD_CSV.encode())
    assert result["ok"] is True
    assert result["format"] == "csv"
    assert result["records_extracted"] == 2
    assert "Method and system for providing access" in result["text"]
    assert "TS 22.261" in result["text"]
    assert "Record 1" in result["text"] and "Record 2" in result["text"]


def test_xlsx_extracts_same_as_csv():
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    import csv as _csv
    reader = _csv.reader(io.StringIO(ISLD_CSV))
    for row in reader:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)

    result = extract_matchable_text("Example_ISLD-202103-010_annex.xlsx", buf.getvalue())
    assert result["ok"] is True
    assert result["format"] == "xlsx"
    assert result["records_extracted"] == 2
    assert "Method and system for providing access" in result["text"]


def test_docx_extracts_body_text():
    from docx import Document
    doc = Document()
    doc.add_paragraph("ISLD Declaration Annex")
    doc.add_paragraph("Patent Application Title: Adaptive TDD slot format configuration")
    buf = io.BytesIO()
    doc.save(buf)

    result = extract_matchable_text("declaration.docx", buf.getvalue())
    assert result["ok"] is True
    assert result["format"] == "docx"
    assert result["extraction_method"] == "docx"
    assert "Adaptive TDD slot format configuration" in result["text"]
    # DOCX is a single document, not per-row records.
    assert result["records_extracted"] == 0


def test_txt_with_unstructured_content_falls_back_to_raw_text():
    text = b"Plain text ISLD disclosure: adaptive slot format configuration for TDD sidelink."
    result = extract_matchable_text("disclosure.txt", text)
    assert result["ok"] is True
    assert result["format"] == "txt"
    assert result["records_extracted"] == 0
    assert "Whole file" in result["text_source"]
    assert result["text"] == text.decode()
    # Falling back to raw text is noted, not silent.
    assert any("identify" in w for w in result["warnings"])


def test_gitas_sample_dataset_style_xlsx_is_not_rejected_but_has_no_clause_text():
    """The GITAS/C-SARAS sample dataset (title + standard columns, no
    clause/claim text) is a valid cPARAS Text Matching input too -- cPARAS
    is a generic ISLD/declaration matcher, not restricted to one file's
    intended role. It should extract real title/standard text (never
    fabricate a "claim" column that doesn't exist)."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["DIPG_ID", "COMP_LEGAL_NAME", "Standard", "Patent/Application Title"])
    ws.append(["1001", "Example Corp", "3GPP TS 48.008 version 7.5.0", "Indication of Content of Message Element"])
    buf = io.BytesIO()
    wb.save(buf)

    result = extract_matchable_text("Sample_Data_for_UIUX_Database.xlsx", buf.getvalue())
    assert result["ok"] is True
    assert result["records_extracted"] == 1
    assert "Indication of Content of Message Element" in result["text"]
    # No clause/illustrative-part column existed in this shape -- never
    # fabricated one.
    assert "Clause:" not in result["text"]


def test_unsupported_binary_format_gives_actionable_error_not_a_crash():
    result = extract_matchable_text("weird.bin", b"\x00\x01binarygarbage\xff\xfe")
    assert result["ok"] is False
    assert result["format"] == "unsupported"
    assert "not a supported file type" in result["error"]
    for fmt in ("pdf", "csv", "xlsx", "docx", "txt"):
        assert fmt in SUPPORTED_FORMATS


def test_empty_csv_gives_actionable_error_not_a_stack_trace():
    result = extract_matchable_text("empty.csv", b"")
    assert result["ok"] is False
    assert result["error"]
    assert "Traceback" not in result["error"]


def test_csv_with_no_recognisable_columns_falls_back_to_raw_text():
    junk = "just,a,line\nwith,no,real,header,at,all\n".encode()
    result = extract_matchable_text("mystery.csv", junk)
    # csv_parser's header scan won't find title/app/pub -- either falls
    # back to raw text (ok=True) or reports no usable text (ok=False),
    # but never silently fabricates records.
    if result["ok"]:
        assert result["records_extracted"] == 0
    else:
        assert result["error"]


def test_docx_with_no_text_reports_clear_error():
    from docx import Document
    doc = Document()
    buf = io.BytesIO()
    doc.save(buf)
    result = extract_matchable_text("blank.docx", buf.getvalue())
    assert result["ok"] is False
    assert "empty" in result["error"].lower() or "No extractable" in result["error"]
