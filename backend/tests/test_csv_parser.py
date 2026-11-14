"""
Tests for utils/csv_parser.

Run: python -m pytest backend/tests/test_csv_parser.py -q
 or: python3 backend/tests/_run_all.py

Bias of this file: most of these target failures that would otherwise be
SILENT — a wrong column mapping, a family number overwriting a disclosure
number, a footnote becoming a patent. A CSV that fails loudly is a support
ticket; a CSV that parses into confidently wrong data is a bad analysis
nobody catches.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.csv_parser import (  # noqa: E402
    looks_like_csv,
    parse_csv_declaration,
    parse_rows_declaration,
    _norm_header,
    _match_field,
    _find_header,
    _split_standards,
)


def _b(text: str, encoding: str = "utf-8") -> bytes:
    return text.encode(encoding)


# Canonical ETSI annex shape: banner rows, two-row header, duplicated
# PATENT FAMILY columns, merged-cell continuation rows, trailing footnote.
ETSI_CSV = """ETSI Rules of Procedure 29-30 November 2022,,,,,,,,
IPR Information Statement Annex,,,,,,,,
Legal Name: Nokia Technologies Oy,,,,,,,,
Disclosure Number,Project or Standard name,Work Item or Standard No.,Version (V.X.X.X),Applicant/holder,Application No.,Publication No.,Patent/Application Title,Country of registration,Application No.,Publication No.,Country of registration
1,5G,TS 38.331,,Nokia Technologies Oy,202441060506,,,INDIA,,,
2,5G,TS 38.331,,,202441059993,,,INDIA,,,
,,TS 38.213,,,,,,,,,
,,TS 38.214,,,,,,,,,
27,5G,TS 123 501,15.13.0,Nokia Corporation,US20060440089,US7817618 B2,Method and devices,UNITED STATES,EP20060440089,EP1817618 B1,EUROPEAN PATENT OFFICE
,,,,,,,,,,,
* Information on other members of a PATENT FAMILY is provided voluntarily (Clause 4.3 of the ETSI IPR Policy).,,,,,,,,
"""


# ── Header normalisation and matching ────────────────────────────────────────

def test_norm_header_strips_case_punctuation_and_parentheticals():
    assert _norm_header("Application No.") == "application no"
    assert _norm_header("Version (V.X.X.X)") == "version"
    assert _norm_header("Illustrative Specific part of the standard (e.g. Section)") == \
        "illustrative specific part of the standard"


def test_norm_header_handles_nbsp_and_unicode_dashes():
    """A header copied out of a PDF-derived sheet looks identical on screen
    but contains NBSP/en-dash — it must not fail to match because of it."""
    assert _norm_header("Application\u00a0No.") == "application no"
    assert _norm_header("Patent\u2013Application Title") == "patent application title"


def test_identical_column_names_map_exactly():
    """The stated contract: 'the column names are usually identical'."""
    for header, expected in [
        ("Application No.", "app"),
        ("Publication No.", "pub"),
        ("Patent/Application Title", "title"),
        ("Country of registration", "cc"),
        ("Work Item or Standard No.", "std"),
        ("Applicant/holder", "who"),
        ("Disclosure Number", "n"),
    ]:
        field, score = _match_field(_norm_header(header))
        assert field == expected, f"{header!r} -> {field!r}, expected {expected!r}"
        assert score == 1.0


def test_variant_column_names_still_map():
    assert _match_field(_norm_header("APPLICATION NUMBER"))[0] == "app"
    assert _match_field(_norm_header("application_no"))[0] == "app"
    assert _match_field(_norm_header("Applicant Name"))[0] == "who"
    assert _match_field(_norm_header("Publication Number"))[0] == "pub"


def test_unrelated_headers_do_not_match():
    """A false mapping is worse than no mapping — it writes one field's data
    into another and still looks like a successful parse."""
    for header in ("alpha", "notes", "internal comment", "reviewed by", "cost centre"):
        field, _ = _match_field(_norm_header(header))
        assert field is None, f"{header!r} wrongly matched {field!r}"


# ── Detection ────────────────────────────────────────────────────────────────

def test_pdf_magic_vetoes_csv_extension():
    """A PDF misnamed .csv must not be handed to the CSV reader."""
    assert looks_like_csv("declaration.csv", b"%PDF-1.7\n%\xe2\xe3\xcf\xd3") is False


def test_csv_extension_is_accepted():
    assert looks_like_csv("annex.csv", b"a,b,c\n1,2,3\n") is True
    assert looks_like_csv("annex.tsv", b"a\tb\n1\t2\n") is True


def test_extensionless_delimited_content_is_detected():
    assert looks_like_csv("upload", b"a,b,c\n1,2,3\n4,5,6\n") is True


# ── Structure ────────────────────────────────────────────────────────────────

def test_header_is_found_below_banner_rows():
    r = parse_csv_declaration(_b(ETSI_CSV), "annex.csv")
    assert r["ok"] is True
    assert r["meta"]["header_row"] == 3


def test_duplicate_family_columns_do_not_overwrite_disclosure_numbers():
    """The single most damaging silent failure available here: the annex
    repeats Application/Publication/Country under 'Other members of this
    PATENT FAMILY', and letting the later columns win would replace every
    disclosure's real number with a family member's."""
    r = parse_csv_declaration(_b(ETSI_CSV), "annex.csv")
    row = [p for p in r["slim"]["patents"] if p.get("n") == 27][0]
    assert row["app"] == "US20060440089"
    assert row["pub"] == "US7817618 B2"
    assert row["cc"] == "UNITED STATES"
    # ...and the family values are kept separately rather than discarded.
    assert row["_family"]["family_app"] == "EP20060440089"
    assert row["_family"]["family_cc"] == "EUROPEAN PATENT OFFICE"


def test_mapped_columns_are_reported_for_inspection():
    r = parse_csv_declaration(_b(ETSI_CSV), "annex.csv")
    mapped = set(r["meta"]["mapped_columns"].values())
    assert {"n", "std", "who", "app", "pub", "title", "cc"} <= mapped
    assert "family_app" in mapped


def test_continuation_rows_are_emitted_for_downstream_merging():
    """Rows carrying only an extra standard must come out in the shape
    core.prompts._merge_continuation_patents folds into the row above —
    reusing that merge is what keeps CSV and PDF output identical."""
    r = parse_csv_declaration(_b(ETSI_CSV), "annex.csv")
    conts = [p for p in r["slim"]["patents"] if p.get("n") is None]
    assert len(conts) == 2
    assert all(set(p) == {"n", "std"} for p in conts)
    assert conts[0]["std"] == ["TS 38.213"]


def test_footnote_and_blank_rows_are_not_treated_as_patents():
    r = parse_csv_declaration(_b(ETSI_CSV), "annex.csv")
    titles = " ".join(p.get("title", "") for p in r["slim"]["patents"])
    assert "PATENT FAMILY is provided voluntarily" not in titles
    assert r["meta"]["rows_skipped"] >= 2


def test_declarant_is_read_from_preamble_label():
    r = parse_csv_declaration(_b(ETSI_CSV), "annex.csv")
    assert r["slim"]["declarant"] == "Nokia Technologies Oy"
    assert r["meta"]["declarant_source"] == "preamble label"


# ── Deep/split headers ──────────────────────────────────────────────────────
# Regression coverage for two real-world bugs found in production: a
# full-declaration-form export (the legal IPR statement on "page 1", annex
# starting on "page 2") pushes the real header far past a shallow scan
# limit, and Excel's merged-cell semantics — where only a spanning header
# cell's anchor column has text, every other column it visually covers
# reads as empty — silently mis-mapped a column when rows were naively
# string-concatenated instead of coalesced cell-wise.

def test_header_found_deep_in_a_long_preamble():
    """A 40-row legal-form preamble ahead of the real header (the shape of
    a full IPR Information Statement + Licensing Declaration export, not
    just an annex-only export) must not be missed because it's past an
    unrealistically shallow scan window."""
    preamble = [f"Legal boilerplate line {i}" for i in range(40)]
    header = ["Disclosure Number", "Applicant/holder", "Application No.",
              "Publication No.", "Country of registration", "Patent/Application Title",
              "Work Item or Standard No."]
    data_row = ["1", "Ericsson", "US1234567", "US2020000001A1", "US",
                "A wireless method", "TS 38.213"]
    rows = [[p] for p in preamble] + [header, data_row]
    r = parse_rows_declaration(rows, "deep_header.csv")
    assert r["ok"] is True
    assert r["meta"]["header_row"] == 40
    assert r["slim"]["patents"][0]["app"] == "US1234567"


def test_three_row_split_header_from_merged_excel_cells():
    """Simulates exactly what openpyxl hands back for a merged header cell:
    only the anchor column ('STANDARD, TECHNICAL SPECIFICATION or ETSI
    Work Item', spanning columns 0-4) has text; every other column that
    banner visually covers reads back as None. Column 2's own sub-label
    ('Work Item or Standard No.') must win the 'std' field — not column 0,
    which naive string-concatenation would have handed it to via the
    banner's wording alone."""
    banner_row = [
        "STANDARD, TECHNICAL SPECIFICATION or\nETSI Work Item", None, None, None, None,
        "Applicant/holder", "Application No.", "Publication No.",
        "Patent/Application Title", "Country of\nregistration",
    ]
    sub_row = [
        "Disclosure\nNumber", "Project or\nStandard name", "Work Item or\nStandard No.",
        "Illustrative\nSpecific part", "Version", None, None, None, None, None,
    ]
    data_row = ["1", "5G", "TS 38.331", None, None, "Nokia Technologies Oy",
                "202441060506", None, None, "INDIA"]
    rows = [banner_row, sub_row, data_row]
    r = parse_rows_declaration(rows, "merged_header.xlsx")
    assert r["ok"] is True
    assert r["meta"]["mapped_columns"]["2"] == "std"
    assert r["meta"]["mapped_columns"]["5"] == "who"
    assert r["slim"]["patents"][0]["std"] == ["TS 38.331"]
    assert r["slim"]["patents"][0]["app"] == "202441060506"


def test_none_cells_in_a_merge_window_do_not_become_literal_none_text():
    """A column with no value in either row of a merge window must produce
    an empty string, not the literal text 'None' leaking into the header
    (which would then pollute token-overlap matching for that column)."""
    row_a = ["Application No.", None]
    row_b = [None, None]
    idx, mapping, unmapped, span = _find_header([row_a, row_b, ["X", "Y"]])
    # Column 1 has no real content in either row — it must map to nothing,
    # not silently match a field because "None" text happened to overlap.
    assert 1 not in mapping or mapping[1] is None


def test_narrower_header_wins_ties_over_a_wider_window():
    """When a wide coalesced window scores identically to the true (single-
    row) header — because the extra rows it absorbs contribute nothing new
    — the narrower, more precise candidate must win. Otherwise the header
    is reported as starting too early, and rows that are genuinely part of
    the preamble (like a 'Legal Name:' declarant line) get misclassified
    as part of the header block and excluded from declarant inference."""
    r = parse_csv_declaration(_b(ETSI_CSV), "annex.csv")
    assert r["meta"]["header_row"] == 3
    assert r["meta"]["header_span"] == 1
    # The regression this guards: if header_row had shifted to 1 (swallowing
    # the "Legal Name:" preamble row into the header block), declarant
    # inference would fall back to the filename instead.
    assert r["slim"]["declarant"] == "Nokia Technologies Oy"
    assert r["meta"]["declarant_source"] == "preamble label"


# ── Cell handling ────────────────────────────────────────────────────────────

def test_split_standards_handles_newlines_and_commas():
    assert _split_standards("TS 38.331\nTS 38.213") == ["TS 38.331", "TS 38.213"]
    assert _split_standards("TS 38.321, TS 38.213") == ["TS 38.321", "TS 38.213"]
    assert _split_standards("TS 38.331; TS 38.423") == ["TS 38.331", "TS 38.423"]


def test_split_standards_does_not_chop_a_single_code():
    assert _split_standards("TS 123 501") == ["TS 123 501"]


def test_split_standards_deduplicates():
    assert _split_standards("TS 38.331\nTS 38.331") == ["TS 38.331"]


def test_excel_text_guard_is_stripped():
    """Excel writes ="0012345" to stop a numeric cell losing leading zeros;
    the guard must not survive into the application number."""
    csv_text = 'Application No.,Patent/Application Title\n="202441056276",Some invention\n'
    r = parse_csv_declaration(_b(csv_text), "x.csv")
    assert r["slim"]["patents"][0]["app"] == "202441056276"


def test_placeholder_nulls_do_not_become_literal_values():
    csv_text = "Application No.,Publication No.,Patent/Application Title\n123456789,N/A,Some invention\n"
    r = parse_csv_declaration(_b(csv_text), "x.csv")
    assert r["slim"]["patents"][0]["pub"] == ""


# ── Encodings and delimiters ─────────────────────────────────────────────────

def test_utf8_bom_does_not_break_the_first_header():
    csv_text = "\ufeffApplication No.,Patent/Application Title\n123456789,Invention\n"
    r = parse_csv_declaration(_b(csv_text), "x.csv")
    assert r["ok"] is True
    assert "app" in r["meta"]["mapped_columns"].values()


def test_utf16_export_is_decoded():
    csv_text = "Application No.\tPatent/Application Title\r\n123456789\tInvention\r\n"
    r = parse_csv_declaration(csv_text.encode("utf-16"), "x.tsv")
    assert r["ok"] is True
    assert r["meta"]["encoding"] == "utf-16"
    assert r["slim"]["patents"][0]["app"] == "123456789"


def test_semicolon_delimiter_is_detected():
    csv_text = "Application No.;Publication No.;Patent/Application Title\n123456789;US1234567 B2;Invention\n"
    r = parse_csv_declaration(_b(csv_text), "x.csv")
    assert r["meta"]["delimiter"] == ";"
    assert r["slim"]["patents"][0]["pub"] == "US1234567 B2"


def test_commas_inside_quoted_titles_do_not_split_the_row():
    csv_text = (
        'Application No.,Patent/Application Title,Country of registration\n'
        '123456789,"Method, system and devices for optimizing use",UNITED STATES\n'
    )
    r = parse_csv_declaration(_b(csv_text), "x.csv")
    p = r["slim"]["patents"][0]
    assert p["title"] == "Method, system and devices for optimizing use"
    assert p["cc"] == "UNITED STATES"


# ── Graceful degradation ─────────────────────────────────────────────────────

def test_unrecognised_csv_falls_back_rather_than_failing():
    r = parse_csv_declaration(_b("alpha,beta,gamma\n1,2,3\n"), "junk.csv")
    assert r["ok"] is False
    assert r["text"]  # something for the LLM path to work from
    assert r["meta"]["warnings"]


def test_csv_with_no_identifying_column_is_not_mapped():
    """Standards alone are not enough to call something a disclosure."""
    r = parse_csv_declaration(_b("Work Item or Standard No.,Version\nTS 38.331,15.0.0\n"), "x.csv")
    assert r["ok"] is False


def test_empty_file_does_not_raise():
    r = parse_csv_declaration(b"", "empty.csv")
    assert r["ok"] is False
    assert r["meta"]["warnings"]


def test_binary_garbage_does_not_raise():
    r = parse_csv_declaration(bytes(range(256)) * 4, "weird.csv")
    assert r["ok"] is False


def test_missing_declarant_is_flagged_not_invented():
    r = parse_csv_declaration(
        _b("Application No.,Patent/Application Title\n123456789,Invention\n"), "mystery.csv"
    )
    assert r["meta"]["declarant_source"] == "filename (unverified)"
    assert any("filename" in w for w in r["meta"]["warnings"])


# ── Verification surface ─────────────────────────────────────────────────────

def test_rendered_text_contains_the_values_for_verification():
    """_run_analysis verifies every extracted identifier against the source
    text. If the rendering omitted values, every CSV field would report as
    unverified — indistinguishable from a hallucinating model."""
    r = parse_csv_declaration(_b(ETSI_CSV), "annex.csv")
    for value in ("202441060506", "US7817618 B2", "TS 38.331", "UNITED STATES"):
        assert value in r["text"], f"{value!r} missing from rendered text"


# ── Integration with the shared schema conversion ────────────────────────────

def test_end_to_end_through_slim_to_full():
    from core.prompts import slim_to_full

    r = parse_csv_declaration(_b(ETSI_CSV), "annex.csv")
    full = slim_to_full(r["slim"])
    discs = full["patent_disclosures"]

    # 3 real disclosures; the 2 continuation rows folded into #2.
    assert len(discs) == 3
    by_num = {d["disclosure_number"]: d for d in discs}
    assert by_num[2]["explicit_data"]["standards"] == [
        "TS 38.331", "TS 38.213", "TS 38.214",
    ]
    assert by_num[1]["explicit_data"]["application_number"] == "202441060506"
    assert by_num[27]["explicit_data"]["title"] == "Method and devices"
