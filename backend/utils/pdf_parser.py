"""
PDF Text Extraction
====================
Multi-tier fully offline extraction strategy for ETSI IPR declaration PDFs.

Tier 1 — PyMuPDF direct text extraction (instant)
          Works for: text-based PDFs (Nokia, Qualcomm, Ericsson etc.)

Tier 2 — pdfplumber table extraction (instant)
          Works for: PDFs with embedded text in table cells

Tier 3 — Tesseract OCR, rendered at 300 DPI (local, ~2-5s per page)
          Works for: scanned/photocopied PDFs with NO text layer at all
          (e.g. Samsung declarations scanned on a Konica Minolta copier —
          `pdffonts` shows zero embedded fonts, confirming pure raster).
          Verified: extracts 40/40 application numbers from the Samsung
          ISLD-202103-010 sample at 300 DPI with psm=6.
          Requires: tesseract-ocr installed on the system
          (Windows: choco install tesseract / download from
          https://github.com/UB-Mannheim/tesseract/wiki — then either add
          it to PATH or set TESSERACT_CMD in .env to the .exe path)

Tier 4 — Ollama Vision OCR (local, ~10-30s per page) — fallback only if
          Tesseract is not installed. Uses moondream (default) or llava:7b.
          Generally lower accuracy than Tesseract for dense printed tables,
          so Tesseract is tried first whenever available.

Why DPI matters: the Samsung scan embeds a 150 DPI JPEG background with the
real table content as separate 300 DPI CCITT bitmaps layered on top. PyMuPDF
renders the COMPOSITE page (background + overlays together), so we must
render at a high zoom factor (3x = ~216 DPI minimum, 300 DPI recommended) to
get characters sharp enough for Tesseract to read. Anything below ~200 DPI
on this kind of document tends to produce empty or garbled output.

OCR confidence gating: Tier 3 also computes a per-page mean Tesseract
word-confidence (via image_to_data, not just image_to_string) and flags any
page scoring below TESSERACT_MIN_CONFIDENCE (default 55) as low-confidence.
This doesn't block extraction -- partial signal still beats none -- but it
surfaces *which* pages' numbers/standard codes are likely OCR noise. That
matters because a mangled code (e.g. "TS 38.331" OCR'd as "TS 3B.33l") is
exactly the kind of input an LLM will silently "correct" into a confident-
looking but wrong value instead of reporting extraction failure. Use
extract_text_from_pdf_with_confidence() to get this signal; plain
extract_text_from_pdf() remains string-only for backward compatibility.
"""

from __future__ import annotations

import asyncio
import logging
import os
from io import BytesIO

logger = logging.getLogger(__name__)


# ── Tier 1: PyMuPDF ──────────────────────────────────────────────────────────

def _extract_pymupdf_pages(pdf_bytes: bytes) -> list[str]:
    """
    Per-page variant of Tier 1 extraction. Returning one string per page
    (rather than a single pre-joined blob) is what lets _extract_sync detect
    a page that yielded almost no text — e.g. one scanned/raster page
    embedded in an otherwise text-based declaration — and patch just that
    page via OCR instead of either (a) missing it silently because the
    document's AGGREGATE character count still clears the 200-char "did
    extraction work?" bar, or (b) OCR'ing every page even when only one
    needed it.
    """
    # doc.close() must run under `finally`. PyMuPDF documents hold native
    # (non-Python-heap) memory that the garbage collector will not reclaim
    # promptly, and get_text() on a malformed or truncated page raises — the
    # previous straight-line close() was skipped on exactly those PDFs, so a
    # server processing a batch of damaged scans leaked a document per file.
    doc = None
    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages = []
        for page in doc:
            blocks = page.get_text("blocks") or []
            blocks.sort(key=lambda b: (round(b[1] / 20) * 20, b[0]))
            text = "\n".join(b[4].strip() for b in blocks if b[4].strip())
            pages.append(text)
        return pages
    except Exception as e:
        logger.error(f"PyMuPDF extraction failed: {e}")
        return []
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass


def _extract_pymupdf(pdf_bytes: bytes) -> str:
    """All pages' native text layer (PyMuPDF), pages separated by blank lines."""
    return "\n\n".join(_extract_pymupdf_pages(pdf_bytes))


# ── Tier 2: pdfplumber ────────────────────────────────────────────────────────

def _strip_repeated_boilerplate_lines(pages: list[str]) -> list[str]:
    """
    Multi-page ETSI IPR annex tables repeat their column header row (e.g.
    "Disclosure | Standard, Technical Specification or ETSI Work Item |
    Application No. | ...") on every single page, since pdfplumber's table
    extraction pulls the header row fresh per page. Left in, that header
    text sits right next to real row data in the flattened text a small
    model reads, and it has been observed splicing straight into an
    extracted title (e.g. a title ending in "...or ETSI Work Item").
    Any exact-text line repeated across most pages is boilerplate — no
    genuine disclosure row is identical to another page's row verbatim —
    so this only removes duplicates, never unique row content.
    """
    if len(pages) < 2:
        return pages
    from collections import Counter
    line_counts: Counter = Counter()
    for page in pages:
        # Count each distinct line once per page (not per occurrence within
        # a page) so a header appearing on every page scores len(pages),
        # while a coincidentally repeated data line within one page doesn't
        # inflate its count.
        for line in set(l.strip() for l in page.split("\n") if l.strip()):
            line_counts[line] += 1
    threshold = max(2, len(pages) // 2 + 1)
    boilerplate = {
        line for line, count in line_counts.items()
        if count >= threshold and len(line) < 300
    }
    if not boilerplate:
        return pages
    # Keep the FIRST occurrence of each boilerplate line and drop every
    # occurrence after that, rather than deleting all of them. A repeated
    # header row (e.g. "Application No. | Publication No. | ...") is the
    # LLM's ONLY signal for which column a bare number belongs to. Rows
    # that leave one of those columns blank (extremely common — a pending
    # application has no publication number yet) give the model nothing
    # to anchor on once every copy of the header is gone, and it falls
    # back to guessing, which is exactly what produced the app/pub
    # column-swap bug this fix addresses. One surviving header (ahead of
    # all data rows, since it's on the first page a table appears on)
    # preserves that signal, while every later repeat — the actual
    # title-splicing hazard this function exists to prevent — is still
    # stripped.
    cleaned = []
    seen_once: set = set()
    for page in pages:
        kept = []
        for l in page.split("\n"):
            stripped = l.strip()
            if stripped in boilerplate:
                if stripped not in seen_once:
                    kept.append(l)
                    seen_once.add(stripped)
                # else: drop this repeat entirely
            else:
                kept.append(l)
        cleaned.append("\n".join(kept))
    return cleaned


def _extract_pdfplumber(pdf_bytes: bytes) -> tuple[str, bool, list[str]]:
    """
    Returns (text, has_tables, pages). has_tables is True if pdfplumber
    detected an actual table structure on any page — used by the
    tier-selection logic in _extract_sync to decide whether to trust
    pdfplumber's table-aware output over PyMuPDF's plain block-sorted text.
    PyMuPDF's block sort is NOT table-aware and can silently scramble which
    application number/standard/country belongs to which disclosure row on
    a dense multi-column table — confirmed on a real ETSI IPR declaration
    annex, where PyMuPDF returned ~6000 characters (comfortably over the old
    200-char "did this work?" threshold) with row 5's application number
    sitting next to row 6 and 7's data instead of its own. A length check
    alone can't catch that, since the character count still looks
    completely fine.

    `pages` (list[str], one entry per PDF page, pre-boilerplate-stripping
    order preserved) lets _extract_sync spot an individual page that came
    back nearly empty — e.g. a scanned annex page mixed into an otherwise
    text-based declaration — even though the document's total character
    count still clears the 200-char bar overall.
    """
    try:
        import pdfplumber
        pages = []
        has_tables = False
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                parts = []
                tables = page.extract_tables() or []
                table_rows = []
                for table in tables:
                    for row in (table or []):
                        clean = " | ".join(
                            str(cell or "").strip().replace("\n", " ") for cell in row
                        )
                        if clean.strip(" |"):
                            table_rows.append(clean)

                if table_rows:
                    # This page has real table structure - use ONLY the
                    # pipe-delimited table rows for it, not also
                    # page.extract_text(). Previously both were appended,
                    # which put every disclosure row into the page text
                    # TWICE (once as jumbled flowing prose, once as a clean
                    # pipe-delimited row) - confirmed doubling application/
                    # publication numbers verbatim in the extracted text on
                    # real ETSI annex pages. That duplication roughly
                    # doubles the characters sent to the LLM per page with
                    # no new information, which (a) pushes documents over
                    # CHUNK_SIZE and into multi-chunk splitting far more
                    # often than the real content requires - the actual
                    # cause of under-extraction on larger declarations
                    # (e.g. Qualcomm's ~159-row annex) - and (b) directly
                    # conflicts with the extraction prompt's explicit
                    # "never repeat the same application number twice /
                    # count the distinct numbers you found" instructions,
                    # which a small model can follow literally by treating
                    # a row's second (prose) appearance as an already-used
                    # number and leaving its app/pub field blank on that
                    # entry. The table rows alone are more reliable than
                    # the prose anyway (that's the entire reason has_tables
                    # is preferred over PyMuPDF above), so the flowing-text
                    # duplicate adds noise, not signal.
                    has_tables = True
                    parts.extend(table_rows)
                else:
                    # No table on this page (e.g. the cover/signature page
                    # of an ETSI declaration) - plain text is all we have.
                    txt = page.extract_text() or ""
                    if txt.strip():
                        parts.append(txt)

                pages.append("\n".join(parts))
        pages = _strip_repeated_boilerplate_lines(pages)
        return "\n\n--- PAGE ---\n\n".join(pages), has_tables, pages
    except Exception as e:
        logger.error(f"pdfplumber extraction failed: {e}")
        return "", False, []


# ── Tier 3: Tesseract OCR ──────────────────────────────────────────────────────

# DPI as a zoom multiplier on PyMuPDF's 72-dpi base: 4.17 ≈ 300 DPI.
# 300 DPI is what was verified to extract all 40/40 app numbers on the
# Samsung sample; going lower (e.g. 2x ≈ 144 DPI) reliably fails on the
# small table font this declarant uses.
def _tesseract_settings():
    """Read Tesseract config from app settings (falls back to env vars)."""
    try:
        from core.config import settings
        return settings.TESSERACT_DPI_SCALE, settings.TESSERACT_PSM, settings.TESSERACT_CMD
    except Exception:
        return (
            float(os.environ.get("TESSERACT_DPI_SCALE", "4.17")),
            os.environ.get("TESSERACT_PSM", "6"),
            os.environ.get("TESSERACT_CMD", ""),
        )


# ISSUE 8: CJK applicant names becoming "???????". Every previous call into
# Tesseract omitted the `lang=` argument entirely, which makes pytesseract
# default to English-only recognition (`lang="eng"`). Tesseract's English
# model has no Han/Hangul/Kana glyphs in its dictionary at all — it isn't
# that it OCR's them incorrectly, it structurally cannot produce those
# characters, so a Chinese/Japanese/Korean applicant name on a scanned page
# comes back as blank, Latin noise, or (once that noise round-trips through
# a small LLM that "corrects" unrecognizable input toward the nearest
# plausible token) a literal string of question marks.
#
# Fix: ask Tesseract which language packs are ACTUALLY installed on this
# system and build a `+`-joined multi-language string that includes every
# CJK pack found, falling back gracefully (with a clear one-time warning
# telling the operator how to install them) when none are present — rather
# than silently OCR'ing CJK content as if it were English forever.
_CJK_LANG_CANDIDATES = ["chi_sim", "chi_tra", "jpn", "kor"]
_tesseract_lang_cache: "str | None" = None
_warned_missing_cjk = False


def _resolve_tesseract_lang() -> str:
    """
    Build the `lang=` string passed to every Tesseract call: always
    includes English (every declaration is at least partly in English —
    section headers, standard codes, boilerplate), plus every installed CJK
    language pack. Cached for the process lifetime since the installed
    language set never changes at runtime and `get_languages()` shells out.
    """
    global _tesseract_lang_cache, _warned_missing_cjk
    if _tesseract_lang_cache is not None:
        return _tesseract_lang_cache

    langs = ["eng"]
    try:
        import pytesseract
        _configure_tesseract_cmd()
        available = set(pytesseract.get_languages(config=""))
        found_cjk = [l for l in _CJK_LANG_CANDIDATES if l in available]
        langs.extend(found_cjk)
        missing_cjk = [l for l in _CJK_LANG_CANDIDATES if l not in available]
        if missing_cjk and not _warned_missing_cjk:
            logger.warning(
                "Tesseract CJK language pack(s) not installed: "
                f"{missing_cjk}. Applicant/inventor names in Chinese, "
                "Japanese, or Korean on scanned pages will not be readable "
                "by OCR until these are installed (Linux: apt install "
                f"{' '.join('tesseract-ocr-' + l for l in missing_cjk)}; "
                "Windows/Mac: select the corresponding language packs in "
                "the Tesseract installer / `brew install tesseract-lang`)."
            )
            _warned_missing_cjk = True
    except Exception as e:
        logger.warning(
            f"Could not query installed Tesseract languages ({e}); "
            "falling back to English-only OCR. CJK text will not be "
            "recognized until Tesseract's language packs are reachable."
        )

    _tesseract_lang_cache = "+".join(langs)
    return _tesseract_lang_cache


def _tesseract_min_confidence() -> float:
    """
    Minimum mean word-confidence (Tesseract's 0-100 scale) for a page's OCR
    output to be trusted at face value. Pages scoring below this are still
    returned -- some signal beats none -- but are flagged in the page header
    and in the page-report list from extract_text_from_pdf_with_confidence(),
    so callers can treat any application number / standard code sourced from
    them as unverified rather than feeding silently-garbled text to the LLM
    as if it were clean.
    """
    try:
        from core.config import settings
        return float(settings.TESSERACT_MIN_CONFIDENCE)
    except Exception:
        return float(os.environ.get("TESSERACT_MIN_CONFIDENCE", "55"))


_WINDOWS_TESSERACT_CANDIDATES = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Tesseract-OCR", "tesseract.exe"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Tesseract-OCR", "tesseract.exe"),
)


def find_tesseract_binary() -> tuple[str | None, str]:
    """Locate tesseract. Returns (path_or_None, how_it_was_found).

    Order: TESSERACT_CMD (explicit) -> PATH -> standard Windows install
    locations (the UB-Mannheim installer does not add itself to PATH by
    default, which is the most common reason OCR "isn't available" on
    Windows even though Tesseract is installed)."""
    import shutil
    _, _, custom_path = _tesseract_settings()
    custom_path = (custom_path or "").strip().strip('"')
    if custom_path:
        if os.path.isfile(custom_path):
            return custom_path, "TESSERACT_CMD"
        return None, f"TESSERACT_CMD points to a file that does not exist: {custom_path}"
    on_path = shutil.which("tesseract")
    if on_path:
        return on_path, "PATH"
    if os.name == "nt":
        for cand in _WINDOWS_TESSERACT_CANDIDATES:
            if cand and os.path.isfile(cand):
                return cand, "standard install location"
    return None, "not found on PATH" + (" or in C:\\Program Files\\Tesseract-OCR" if os.name == "nt" else "")


def _configure_tesseract_cmd():
    """Point pytesseract at the binary found by find_tesseract_binary()."""
    import pytesseract
    path, _how = find_tesseract_binary()
    if path:
        pytesseract.pytesseract.tesseract_cmd = path


_tesseract_status: "dict | None" = None


def tesseract_status(refresh: bool = False) -> dict:
    """{"available": bool, "path", "found_via", "version", "error"} — cached
    for the process (probing shells out to `tesseract --version`)."""
    global _tesseract_status
    if _tesseract_status is not None and not refresh:
        return _tesseract_status
    st = {"available": False, "path": None, "found_via": None, "version": None, "error": None}
    try:
        import pytesseract
    except ImportError as e:
        st["error"] = f"pytesseract is not installed ({e}); run: pip install -r requirements.txt"
        _tesseract_status = st
        return st
    path, how = find_tesseract_binary()
    st["path"], st["found_via"] = path, how
    if not path:
        st["error"] = (f"Tesseract OCR binary {how}. Install it (Windows: "
                       "https://github.com/UB-Mannheim/tesseract/wiki; macOS: brew install tesseract; "
                       "Linux: apt install tesseract-ocr) or set TESSERACT_CMD in backend/.env.")
        _tesseract_status = st
        return st
    try:
        pytesseract.pytesseract.tesseract_cmd = path
        st["version"] = str(pytesseract.get_tesseract_version())
        st["available"] = True
    except Exception as e:
        st["error"] = f"Tesseract at {path} could not be run: {type(e).__name__}: {e}"
    _tesseract_status = st
    return st


def _tesseract_available() -> bool:
    """Whether Tesseract OCR can be used (warned/logged once per process)."""
    st = tesseract_status()
    if not st["available"] and not getattr(_tesseract_available, "_warned", False):
        logger.warning(f"Tesseract OCR unavailable — {st['error']}")
        _tesseract_available._warned = True  # type: ignore[attr-defined]
    elif st["available"] and not getattr(_tesseract_available, "_logged", False):
        logger.info(f"Tesseract OCR available: {st['version']} ({st['path']}, via {st['found_via']})")
        _tesseract_available._logged = True  # type: ignore[attr-defined]
    return st["available"]


def _tesseract_timeout() -> int:
    """Per-page Tesseract timeout in seconds (TESSERACT_PAGE_TIMEOUT, default 120; 0 = none)."""
    try:
        from core.config import settings
        return int(getattr(settings, "TESSERACT_PAGE_TIMEOUT", 120) or 0)
    except Exception:
        return 120


def _ocr_page_data(pytesseract_module, img, psm: str) -> tuple[str, "float | None"]:
    """ONE Tesseract pass per page returning (text, mean word confidence).

    Previously every page was OCR'd twice — image_to_string() for the text
    and image_to_data() for the confidence — doubling the most expensive
    step of scanned-PDF processing. The text is rebuilt from image_to_data's
    word boxes in reading order (block -> paragraph -> line -> word), which
    is how Tesseract's own text renderer lays it out."""
    data = pytesseract_module.image_to_data(
        img, lang=_resolve_tesseract_lang(), config=f"--psm {psm}",
        output_type=pytesseract_module.Output.DICT, timeout=_tesseract_timeout(),
    )
    lines: dict[tuple, list[str]] = {}
    order: list[tuple] = []
    scores: list[float] = []
    n = len(data.get("text", []))
    for i in range(n):
        txt = str(data["text"][i] or "")
        if not txt.strip():
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if key not in lines:
            lines[key] = []
            order.append(key)
        lines[key].append(txt.strip())
        try:
            c = float(data["conf"][i])
        except (TypeError, ValueError):
            continue
        if c >= 0:  # -1 marks layout elements, not words
            scores.append(c)
    out_lines: list[str] = []
    prev_par = None
    for key in order:
        par = key[:2]
        if prev_par is not None and par != prev_par:
            out_lines.append("")
        out_lines.append(" ".join(lines[key]))
        prev_par = par
    confidence = (sum(scores) / len(scores)) if scores else None
    return "\n".join(out_lines), confidence


def _ocr_one_page_tesseract(doc, page_index: int, dpi_scale: float, psm: str,
                             min_confidence: float, filename: str) -> dict:
    """
    OCR a single already-open fitz page with Tesseract. Shared by the
    full-document Tier 3 pass (_extract_tesseract) and by _patch_thin_pages.
    Returns {"text": str, "confidence": float|None, "low_confidence": bool|None,
    "chars": int, "error": str (optional)}.
    """
    import fitz
    import pytesseract
    from PIL import Image

    try:
        page = doc[page_index]
        mat = fitz.Matrix(dpi_scale, dpi_scale)
        pix = page.get_pixmap(matrix=mat)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        del pix
        page_text, confidence = _ocr_page_data(pytesseract, img, psm)
        is_low = confidence is not None and confidence < min_confidence
        return {
            "text": page_text.strip(),
            "confidence": confidence,
            "low_confidence": is_low,
            "chars": len(page_text),
        }
    except RuntimeError as e:
        # pytesseract raises RuntimeError("Tesseract process timeout")
        logger.warning(f"[{filename}] Tesseract page {page_index + 1} failed: {e}")
        return {"text": "", "confidence": None, "low_confidence": None, "chars": 0, "error": str(e)}
    except Exception as e:
        logger.warning(f"[{filename}] Tesseract page {page_index + 1} failed: {type(e).__name__}: {e}")
        return {"text": "", "confidence": None, "low_confidence": None, "chars": 0, "error": str(e)}


def _extract_tesseract(pdf_bytes: bytes, filename: str) -> tuple[str, list[dict]]:
    """
    Render each page at high DPI and OCR with Tesseract.
    This is Tier 3 — tried before Ollama vision because it is faster,
    requires no separate model download, and was verified to correctly
    read dense small-font declaration tables (40/40 app numbers on the
    Samsung sample) where a tiny vision LLM like moondream struggled.

    Returns (full_text, page_reports) where page_reports is a list of
    {"page": int, "confidence": float | None, "low_confidence": bool,
    "chars": int} dicts, one per page. This lets callers flag which pages'
    numbers/codes are less trustworthy instead of treating all OCR output as
    equally reliable — the root cause behind a mangled standard code like
    "TS 38.331" -> "TS 3B.33l" getting silently "corrected" by the LLM into
    a confident-looking but wrong value.
    """
    import fitz

    _configure_tesseract_cmd()
    dpi_scale, psm, _ = _tesseract_settings()
    min_confidence = _tesseract_min_confidence()

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        n_pages = len(doc)
    except Exception as e:
        logger.error(f"[{filename}] Cannot open PDF for Tesseract OCR: {e}")
        return "", []

    logger.info(
        f"[{filename}] Tier 3 Tesseract OCR: {n_pages} page(s) "
        f"@ ~{int(dpi_scale * 72)} DPI (min confidence: {min_confidence:.0f}%)"
    )

    page_reports = []
    raw_page_texts = []
    page_tags = []
    for i in range(n_pages):
        r = _ocr_one_page_tesseract(doc, i, dpi_scale, psm, min_confidence, filename)
        confidence = r["confidence"]
        is_low = r["low_confidence"]
        tag = ""
        if confidence is not None:
            tag = f" (OCR confidence: {confidence:.0f}%)"
            if is_low:
                tag += " [LOW CONFIDENCE — numbers/codes on this page are unverified]"

        raw_page_texts.append(r["text"])
        page_tags.append(tag)
        page_reports.append({
            "page": i + 1,
            "confidence": confidence,
            "low_confidence": is_low,
            "chars": r["chars"],
            **({"error": r["error"]} if "error" in r else {}),
        })

        if is_low:
            logger.warning(
                f"[{filename}] Tesseract page {i + 1}/{n_pages}: LOW OCR "
                f"confidence ({confidence:.0f}% avg, threshold "
                f"{min_confidence:.0f}%) — application/publication numbers "
                "and standard codes read from this page are likely "
                "corrupted and should not be trusted without verification "
                "against the raw extracted text."
            )
        else:
            logger.info(
                f"[{filename}] Tesseract page {i + 1}/{n_pages}: "
                f"{r['chars']} chars"
                + (f", {confidence:.0f}% avg confidence" if confidence is not None else "")
            )

    doc.close()
    # Strip lines repeated verbatim across most pages (column headers,
    # repeated form boilerplate) before assembling the final text — same
    # rationale as the pdfplumber path above.
    stripped_page_texts = _strip_repeated_boilerplate_lines(raw_page_texts)
    pages_text = [
        f"--- PAGE {i + 1}{page_tags[i]} ---\n{stripped_page_texts[i]}"
        for i in range(len(stripped_page_texts))
    ]
    result = "\n\n".join(pages_text)
    if result.strip():
        logger.info(f"[{filename}] Tesseract OCR complete — {len(result)} total chars")
    return result, page_reports


# ── Tier 4: Ollama Vision OCR (fallback only) ──────────────────────────────────

VISION_PROMPT_READ = (
    "Read every word and number visible in this image from top to bottom, "
    "left to right. Include all text in tables. Do not describe the image. "
    "Just output all the text you can read."
)


def _page_to_base64(pdf_bytes: bytes, page_num: int, dpi_scale: float = 3.0) -> str:
    """Render one page as a PNG (base64) for the vision-model OCR tier; the document is always closed,
    even when rendering runs out of memory.
    """
    import base64
    import fitz
    # Same leak class as _extract_pymupdf_pages, and more dangerous here:
    # get_pixmap() at dpi_scale=3.0 allocates a full-page RGB bitmap and is
    # the single most likely call in this module to raise (out of memory on
    # an oversized page, or an IndexError from a bad page_num).
    doc = None
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page = doc[page_num]
        mat = fitz.Matrix(dpi_scale, dpi_scale)
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("png")
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
    return base64.standard_b64encode(img_bytes).decode("ascii")


def _vision_ollama_read(b64_img: str, ollama_url: str, vision_model: str) -> str:
    """Ask the configured Ollama vision model to transcribe one page image (local server only; proxies
    bypassed for loopback).
    """
    import httpx
    from core.ollama_client import _is_loopback_url
    payload = {
        "model": vision_model,
        "prompt": VISION_PROMPT_READ,
        "images": [b64_img],
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 4096},
    }
    with httpx.Client(timeout=600.0, trust_env=not _is_loopback_url(ollama_url)) as client:
        r = client.post(f"{ollama_url}/api/generate", json=payload)
    if r.status_code >= 400:
        try:
            detail = r.json().get("error") or r.text
        except Exception:
            detail = r.text
        raise RuntimeError(f"Ollama vision model '{vision_model}' failed (HTTP {r.status_code}): {detail[:200]}")
    return r.json().get("response", "").strip()


def _get_vision_config():
    """(vision_model, ollama_base_url, max_pages) from the shared Settings."""
    from core.config import settings
    return settings.VISION_MODEL, settings.OLLAMA_BASE_URL, int(getattr(settings, "VISION_OCR_MAX_PAGES", 60))


def _resolve_vision_model(filename: str) -> "tuple[str | None, str]":
    """Check (synchronously, cheaply) that the configured vision model is
    actually installed before rendering and posting every page to it.
    Returns (installed_tag_or_None, base_url). Never substitutes another
    model — a missing one is logged with the exact pull command."""
    import httpx
    from core.ollama_client import _is_loopback_url, resolve_installed, classify_model
    vision_model, ollama_url, _ = _get_vision_config()
    if not vision_model:
        logger.warning(f"[{filename}] VISION_MODEL is empty — vision OCR fallback disabled.")
        return None, ollama_url
    try:
        with httpx.Client(timeout=5.0, trust_env=not _is_loopback_url(ollama_url)) as client:
            tags = client.get(f"{ollama_url}/api/tags").json().get("models", [])
    except Exception as e:
        logger.warning(f"[{filename}] Vision OCR fallback skipped: Ollama not reachable at {ollama_url} ({e}).")
        return None, ollama_url
    resolved = resolve_installed(vision_model, [t.get("name", "") for t in tags])
    if not resolved:
        logger.warning(f"[{filename}] Vision OCR fallback skipped: VISION_MODEL '{vision_model}' is not "
                       f"installed. Install it with: ollama pull {vision_model}")
        return None, ollama_url
    entry = next((t for t in tags if t.get("name") == resolved), {})
    if classify_model(entry) != "vision":
        logger.warning(f"[{filename}] VISION_MODEL '{resolved}' does not look like a vision model; "
                       "OCR results may be empty.")
    return resolved, ollama_url


def _extract_vision_ollama(pdf_bytes: bytes, filename: str) -> str:
    """Tier 4: fallback OCR via local Ollama vision model, only used if
    Tesseract is unavailable on the system."""
    vision_model, ollama_url = _resolve_vision_model(filename)
    if not vision_model:
        return ""
    _, _, max_pages = _get_vision_config()

    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        n_pages = len(doc)
        doc.close()
    except Exception as e:
        logger.error(f"[{filename}] Cannot open PDF for vision OCR: {e}")
        return ""

    if n_pages > max_pages:
        logger.warning(
            f"[{filename}] Vision OCR limited to the first {max_pages} of {n_pages} pages "
            "(VISION_OCR_MAX_PAGES). Install Tesseract for full-document OCR."
        )
    logger.info(f"[{filename}] Tier 4 Ollama Vision OCR: model={vision_model}, pages={min(n_pages, max_pages)}")

    pages_text = []
    consecutive_failures = 0
    for page_num in range(min(n_pages, max_pages)):
        try:
            b64 = _page_to_base64(pdf_bytes, page_num, dpi_scale=3.0)
            raw_text = _vision_ollama_read(b64, ollama_url, vision_model)
            consecutive_failures = 0
            if raw_text:
                pages_text.append(f"--- PAGE {page_num + 1} ---\n{raw_text}")
                logger.info(f"[{filename}] Vision page {page_num + 1}/{n_pages}: {len(raw_text)} chars")
            else:
                logger.warning(f"[{filename}] Vision page {page_num + 1}/{n_pages}: empty response")
        except Exception as e:
            consecutive_failures += 1
            logger.warning(f"[{filename}] Vision page {page_num + 1}/{n_pages} failed: {e}")
            if consecutive_failures >= 3:
                logger.error(f"[{filename}] Vision OCR aborted after 3 consecutive page failures.")
                break

    return "\n\n".join(pages_text)


# ── Per-page coverage patch (fixes mixed text+scanned documents) ──────────────

# A genuine text-layer page of an ETSI declaration (even a near-blank cover
# or signature page) reliably clears a few dozen characters. Below this, a
# page is almost certainly one of: a scanned/raster page with no text layer
# at all, a failed render, or a truly blank page — all three are worth a
# targeted OCR attempt rather than silently shipping near-nothing for that
# page while the rest of the document analyzes normally.
MIN_PAGE_CHARS = 40


def _find_thin_pages(pages: list[str], min_chars: int = MIN_PAGE_CHARS) -> list[int]:
    """Indices of pages whose text layer is shorter than `min_chars` - candidates for OCR."""
    return [i for i, p in enumerate(pages) if len((p or "").strip()) < min_chars]


def _patch_thin_pages(pdf_bytes: bytes, pages: list[str], filename: str, method_label: str) -> tuple[list[str], list[int]]:
    """
    Detect pages that came back almost empty from Tier 1/2 (PyMuPDF /
    pdfplumber) and OCR just those pages, splicing the result back into
    the right position.

    This is the fix for the class of bug where a document that is mostly a
    native text layer has one or two scanned/raster pages mixed in (a
    signature page photographed and re-inserted, a fax'd annex page, etc.).
    Previously, _extract_sync only checked the AGGREGATE character count of
    the whole document against a 200-char bar — if the other pages had
    plenty of text, that bar was cleared and OCR was never attempted, so
    the raster page's content (which can include disclosure rows) was
    simply missing from what the LLM ever saw. Checking coverage per page,
    not just for the document as a whole, is what catches this.

    Returns (patched_pages, patched_page_numbers) where patched_page_numbers
    is 1-indexed, for surfacing in the extraction confidence report.
    """
    thin = _find_thin_pages(pages)
    if not thin or len(pages) <= 1:
        return pages, []

    logger.warning(
        f"[{filename}] {method_label}: {len(thin)}/{len(pages)} page(s) yielded "
        f"under {MIN_PAGE_CHARS} chars each (pages: {[i + 1 for i in thin]}) despite "
        "the document as a whole clearing the extraction bar — these are likely "
        "scanned/raster pages embedded in an otherwise text-based declaration. "
        "Patching just these pages via OCR so the full document is covered."
    )

    patched = list(pages)
    patched_numbers: list[int] = []

    use_tesseract = _tesseract_available()
    if use_tesseract:
        import fitz
        _configure_tesseract_cmd()
        dpi_scale, psm, _ = _tesseract_settings()
        min_confidence = _tesseract_min_confidence()
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        except Exception as e:
            logger.error(f"[{filename}] Cannot reopen PDF to patch thin pages: {e}")
            doc = None
        if doc is not None:
            for i in thin:
                if i >= len(doc):
                    continue
                r = _ocr_one_page_tesseract(doc, i, dpi_scale, psm, min_confidence, filename)
                if r["text"].strip():
                    tag = ""
                    if r["confidence"] is not None:
                        tag = f" (OCR confidence: {r['confidence']:.0f}%)"
                        if r["low_confidence"]:
                            tag += " [LOW CONFIDENCE]"
                    patched[i] = f"--- PAGE {i + 1}{tag} ---\n{r['text']}"
                    patched_numbers.append(i + 1)
                    logger.info(f"[{filename}] Patched page {i + 1} via Tesseract: {r['chars']} chars")
                else:
                    logger.warning(f"[{filename}] Tesseract patch for page {i + 1} yielded nothing")
            doc.close()
    else:
        # Tier 4 fallback for patching: only reached if Tesseract itself
        # isn't installed on this system.
        vision_model, ollama_url = _resolve_vision_model(filename)
        if vision_model:
            for i in thin:
                try:
                    b64 = _page_to_base64(pdf_bytes, i, dpi_scale=3.0)
                    text = _vision_ollama_read(b64, ollama_url, vision_model)
                    if text.strip():
                        patched[i] = f"--- PAGE {i + 1} ---\n{text}"
                        patched_numbers.append(i + 1)
                        logger.info(f"[{filename}] Patched page {i + 1} via Vision OCR: {len(text)} chars")
                except Exception as e:
                    logger.warning(f"[{filename}] Vision OCR patch for page {i + 1} failed: {e}")
        else:
            logger.warning(
                f"[{filename}] {len(thin)} thin page(s) detected but neither Tesseract "
                "nor an installed VISION_MODEL is available to patch them — those pages' "
                "content will be missing from analysis. Install Tesseract (recommended) or "
                "`ollama pull moondream`."
            )

    return patched, patched_numbers


class PDFExtractionError(ValueError):
    """The PDF itself can't be processed (encrypted, corrupt, empty). The
    message is safe to show to the user and names the file."""


def _assert_pdf_readable(pdf_bytes: bytes, filename: str) -> int:
    """Fail fast, with a specific reason, on PDFs no tier could ever read —
    instead of silently running four extraction tiers (including slow OCR)
    and then returning empty text."""
    if not pdf_bytes:
        raise PDFExtractionError(f"'{filename}' is empty (0 bytes).")
    try:
        import fitz
    except ImportError as e:  # pragma: no cover
        raise PDFExtractionError(f"PyMuPDF is not installed ({e}); run: pip install -r requirements.txt")
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        raise PDFExtractionError(
            f"'{filename}' is not a readable PDF (it may be corrupted or truncated): {e}")
    try:
        if doc.needs_pass and not doc.authenticate(""):
            raise PDFExtractionError(
                f"'{filename}' is password-protected. Remove the password (e.g. print to PDF) and upload it again.")
        if doc.page_count == 0:
            raise PDFExtractionError(f"'{filename}' contains no pages.")
        return doc.page_count
    finally:
        doc.close()


# ── Orchestrator ──────────────────────────────────────────────────────────────

def _extract_sync(pdf_bytes: bytes, filename: str) -> dict:
    """Validate the PDF, run the extraction tiers, and add `page_count`
    (the real number of pages, whichever tier produced the text)."""
    page_count = _assert_pdf_readable(pdf_bytes, filename)
    result = _extract_tiers(pdf_bytes, filename)
    result["page_count"] = page_count
    return result


def _extract_tiers(pdf_bytes: bytes, filename: str) -> dict:
    """
    Returns {"text": str, "method": str, "page_confidences": list[dict],
    "low_confidence_pages": list[int]}. `page_confidences` /
    `low_confidence_pages` are populated for the Tesseract tier, and also
    whenever the per-page coverage patch (below) OCRs individual pages on
    top of a Tier 1/2 result.

    IMPORTANT: tier selection below is still based on an aggregate
    character count (a whole document with too little text overall is
    genuinely scanned and should go to full-document OCR), but every tier
    that returns per-page text now also runs _patch_thin_pages before
    returning, so a document that clears the aggregate bar but has one or
    two individual pages with almost no text (a scanned signature page, a
    faxed annex sheet, a photographed page mixed into an otherwise
    text-based declaration) still gets those specific pages OCR'd instead
    of silently shipping without them. This is what was previously causing
    declarations to be under-analyzed even when the overall extraction
    "succeeded" by the old whole-document check.
    """
    # Tier 1: PyMuPDF (per-page, so thin pages can be patched individually)
    pymupdf_pages = _extract_pymupdf_pages(pdf_bytes)
    text = "\n\n".join(pymupdf_pages)

    # Tier 2: pdfplumber — run this even when PyMuPDF already cleared the
    # 200-char bar, specifically to get its table detector. PyMuPDF's block
    # sort is NOT table-aware: on a dense multi-column table it can return
    # plenty of "valid-looking" characters while silently scrambling which
    # application number/standard/country belongs to which disclosure row
    # (confirmed on a real ETSI declaration — see _extract_pdfplumber's
    # docstring). A pure length check can't detect that failure mode at all,
    # so when real tables are present, pdfplumber's table-aware output is
    # trusted over PyMuPDF's even though PyMuPDF also "succeeded".
    text_plumber, has_tables, plumber_pages = _extract_pdfplumber(pdf_bytes)

    if has_tables and len(text_plumber.strip()) >= 200:
        logger.info(
            f"[{filename}] Tier 2 (pdfplumber): {len(text_plumber)} chars — "
            "table structure detected, preferring table-aware extraction "
            "over PyMuPDF's non-table-aware block sort"
        )
        patched_pages, patched_nums = _patch_thin_pages(pdf_bytes, plumber_pages, filename, "Tier 2 (pdfplumber)")
        return {
            "text": "\n\n--- PAGE ---\n\n".join(patched_pages),
            "method": "pdfplumber_tables",
            "page_confidences": [],
            "low_confidence_pages": patched_nums,
        }

    if len(text.strip()) >= 200:
        logger.info(f"[{filename}] Tier 1 (PyMuPDF): {len(text)} chars")
        patched_pages, patched_nums = _patch_thin_pages(pdf_bytes, pymupdf_pages, filename, "Tier 1 (PyMuPDF)")
        return {
            "text": "\n\n".join(patched_pages),
            "method": "pymupdf",
            "page_confidences": [],
            "low_confidence_pages": patched_nums,
        }

    if len(text_plumber.strip()) >= 200:
        logger.info(f"[{filename}] Tier 2 (pdfplumber): {len(text_plumber)} chars")
        patched_pages, patched_nums = _patch_thin_pages(pdf_bytes, plumber_pages, filename, "Tier 2 (pdfplumber)")
        return {
            "text": "\n\n--- PAGE ---\n\n".join(patched_pages),
            "method": "pdfplumber",
            "page_confidences": [],
            "low_confidence_pages": patched_nums,
        }

    # Tier 3: Tesseract OCR (preferred for scanned/photocopied PDFs) — this
    # tier already OCRs every page, so there's no "thin page" gap to patch.
    logger.info(
        f"[{filename}] PyMuPDF: {len(text)} chars, pdfplumber: {len(text_plumber)} chars — "
        "document appears to be scanned/raster, trying Tesseract OCR (Tier 3)"
    )
    if _tesseract_available():
        text_tesseract, page_reports = _extract_tesseract(pdf_bytes, filename)
        low_confidence_pages = [r["page"] for r in page_reports if r.get("low_confidence")]
        if len(text_tesseract.strip()) >= 200:
            if low_confidence_pages:
                logger.warning(
                    f"[{filename}] Tesseract OCR completed but "
                    f"{len(low_confidence_pages)}/{len(page_reports)} page(s) "
                    f"were low-confidence (pages: {low_confidence_pages}) — "
                    "do not trust application numbers or standard codes "
                    "extracted from those pages without checking them against "
                    "the raw text."
                )
            return {
                "text": text_tesseract,
                "method": "tesseract",
                "page_confidences": page_reports,
                "low_confidence_pages": low_confidence_pages,
            }
        logger.warning(f"[{filename}] Tesseract yielded only {len(text_tesseract)} chars")
    else:
        logger.warning(
            f"[{filename}] Tesseract not found on this system. "
            "Install it: Windows → https://github.com/UB-Mannheim/tesseract/wiki "
            "(then set TESSERACT_CMD in .env if not on PATH); "
            "Mac → brew install tesseract; Linux → apt install tesseract-ocr"
        )

    # Tier 4: Ollama Vision OCR (fallback if Tesseract isn't installed/failed)
    logger.info(f"[{filename}] Trying Ollama Vision OCR (Tier 4) as fallback")
    text_vision = _extract_vision_ollama(pdf_bytes, filename)
    if text_vision.strip():
        return {"text": text_vision, "method": "vision_ollama", "page_confidences": [], "low_confidence_pages": []}

    best = max([text, text_plumber], key=len)
    logger.warning(
        f"[{filename}] All extraction tiers exhausted — returning {len(best)} chars. "
        "Install Tesseract OCR for scanned PDF support, or set VISION_MODEL "
        "in .env to use an Ollama vision model instead."
    )
    return {"text": best, "method": "fallback_best_effort", "page_confidences": [], "low_confidence_pages": []}


async def extract_text_from_pdf_with_confidence(
    pdf_bytes: bytes, filename: str = "document.pdf"
) -> dict:
    """
    Full extraction result including OCR quality metadata:
    {"text": str, "method": str, "page_confidences": [...], "low_confidence_pages": [...]}.

    Use this instead of extract_text_from_pdf() wherever the caller can act
    on OCR confidence — e.g. to flag which extracted application numbers or
    standard codes are more likely to be OCR noise (and therefore more
    likely for the LLM to have "corrected" into a plausible-looking but
    wrong value) before those values get treated as ground truth.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _extract_sync, pdf_bytes, filename)


async def extract_text_from_pdf(
    pdf_bytes: bytes, filename: str = "document.pdf"
) -> str:
    """
    Backward-compatible entry point — returns extracted text only, with no
    OCR-confidence metadata. Existing callers that just need the text keep
    working unchanged; use extract_text_from_pdf_with_confidence() for the
    quality signal needed to gate trust in OCR'd numbers/codes.
    """
    result = await extract_text_from_pdf_with_confidence(pdf_bytes, filename)
    return result["text"]
