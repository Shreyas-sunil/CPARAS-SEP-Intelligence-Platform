"""
Tests for utils/html_parser.

Run: python -m pytest backend/tests/test_html_parser.py -q
 or: python3 backend/tests/_run_all.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.html_parser import (  # noqa: E402
    looks_like_html,
    parse_html_declaration,
    _cell_text,
)


def _b(text: str) -> bytes:
    return text.encode("utf-8")


SINGLE_TABLE_HTML = """
<!DOCTYPE html>
<html><head><title>ETSI IPR Annex</title></head>
<body>
<h1>Nokia Technologies Oy — IPR Declaration</h1>
<table border="1">
<tr><th>Disclosure Number</th><th>Applicant/holder</th><th>Application No.</th>
    <th>Publication No.</th><th>Country of registration</th>
    <th>Patent/Application Title</th><th>Work Item or Standard No.</th></tr>
<tr><td>1</td><td>Nokia Technologies Oy</td><td>202441060506</td>
    <td>US7817618 B2</td><td>INDIA</td><td>Method and devices</td><td>TS 38.331</td></tr>
<tr><td>2</td><td>Nokia Technologies Oy</td><td>202441059993</td>
    <td></td><td>INDIA</td><td>Another method</td><td>TS 38.214</td></tr>
</table>
</body></html>
"""

MULTI_TABLE_HTML = """
<html><body>
<table><tr><td>Nav</td><td>Links</td></tr></table>
<table>
<tr><th>Disclosure Number</th><th>Applicant/holder</th><th>Application No.</th>
    <th>Publication No.</th><th>Country of registration</th>
    <th>Patent/Application Title</th><th>Work Item or Standard No.</th></tr>
<tr><td>1</td><td>Ericsson</td><td>US1234567</td><td>US2020000001A1</td>
    <td>US</td><td>A wireless method</td><td>TS 38.213</td></tr>
<tr><td>2</td><td>Ericsson</td><td>US7654321</td><td>US2020000002A1</td>
    <td>US</td><td>Another wireless method</td><td>TS 38.214</td></tr>
</table>
<table><tr><td>Footer</td></tr></table>
</body></html>
"""

NO_TABLE_HTML = "<html><body><p>Just prose, no table here at all.</p></body></html>"


# ── Detection ─────────────────────────────────────────────────────────────────

def test_looks_like_html_by_extension():
    assert looks_like_html("annex.html", b"anything") is True
    assert looks_like_html("annex.htm", b"anything") is True


def test_looks_like_html_by_doctype_sniff():
    assert looks_like_html(None, _b("<!DOCTYPE html><html>")) is True


def test_looks_like_html_by_table_sniff():
    assert looks_like_html(None, _b("<table><tr><td>x</td></tr></table>")) is True


def test_looks_like_html_rejects_pdf_and_csv():
    assert looks_like_html("annex.pdf", b"%PDF-1.4") is False
    assert looks_like_html("annex.csv", b"a,b,c\n1,2,3") is False
    assert looks_like_html(None, b"a,b,c\n1,2,3") is False


# ── Cell text flattening ──────────────────────────────────────────────────────

def test_cell_text_collapses_internal_whitespace():
    from bs4 import BeautifulSoup
    soup = BeautifulSoup("<td>  Nokia   \n Technologies \t Oy </td>", "html.parser")
    assert _cell_text(soup.td) == "Nokia Technologies Oy"


# ── End-to-end parsing ────────────────────────────────────────────────────────

def test_parse_html_maps_single_table():
    result = parse_html_declaration(_b(SINGLE_TABLE_HTML), "annex.html")
    assert result["ok"] is True
    assert result["slim"]["declarant"] == "Nokia Technologies Oy"
    patents = result["slim"]["patents"]
    assert len(patents) == 2
    assert patents[0]["app"] == "202441060506"
    assert patents[0]["std"] == ["TS 38.331"]
    assert result["meta"]["format"] == "html"


def test_parse_html_picks_the_table_that_maps_the_most_rows():
    """A page with nav/footer decoration tables plus the real annex table
    must not accidentally map the wrong (tiny, 2-column) table."""
    result = parse_html_declaration(_b(MULTI_TABLE_HTML), "annex.html")
    assert result["ok"] is True
    assert result["meta"]["tables_found"] == 3
    assert result["slim"]["declarant"] == "Ericsson"
    assert len(result["slim"]["patents"]) == 2
    assert result["slim"]["patents"][0]["app"] == "US1234567"


def test_parse_html_no_table_falls_back_to_visible_text():
    result = parse_html_declaration(_b(NO_TABLE_HTML), "prose.html")
    assert result["ok"] is False
    assert result["slim"] == {"declarant": "", "patents": []}
    assert "Just prose" in result["text"]
    assert result["meta"]["tables_found"] == 0
    assert result["meta"]["warnings"]


def test_parse_html_unmapped_table_falls_back_with_warning():
    html = "<html><body><table><tr><td>Foo</td><td>Bar</td></tr><tr><td>x</td><td>y</td></tr></table></body></html>"
    result = parse_html_declaration(_b(html), "junk.html")
    assert result["ok"] is False
    assert result["meta"]["tables_found"] == 1
    assert result["meta"]["warnings"]


def test_parse_html_strips_script_and_style():
    html = (
        "<html><head><style>.x{color:red}</style></head>"
        "<body><script>var x = 'should not appear';</script>"
        "<p>Real content only.</p></body></html>"
    )
    result = parse_html_declaration(_b(html), "clean.html")
    assert "should not appear" in html  # sanity: it really was in the source
    assert "should not appear" not in result["text"]
    assert "color:red" not in result["text"]
    assert "Real content only" in result["text"]


def test_parse_html_malformed_markup_does_not_raise():
    # Unclosed tags, stray angle brackets — BeautifulSoup's lenient parsers
    # should recover something rather than this raising.
    html = "<html><body><table><tr><td>1<td>2</tr><p>oops no closing tags"
    result = parse_html_declaration(_b(html), "broken.html")
    assert isinstance(result, dict)
    assert "ok" in result
