"""
3GPP Spec Corpus Ingestion
============================
Turns the local mirror produced by `3gpp_archive_crawler.py` into indexed,
retrievable chunks in this platform's vector store, so `research/engine.py`
and the `/api/text-match` endpoint can compare patent/disclosure text
against REAL 3GPP specification text — instead of the small illustrative
corpus in `research/standards_corpus.py` or the one-line placeholder
descriptions in `ingestion/pipeline.py`'s `BUILTIN_3GPP_KNOWLEDGE`.

WHAT "TRAINING" MEANS HERE
---------------------------
This platform does not fine-tune a neural network on your corpus anywhere
in this codebase. Every "similarity"/"matching" feature here (this module,
research/engine.py, api/routes/search.py, api/routes/text_match.py) is
retrieval-augmented: text gets embedded with a pretrained sentence-
transformers model (see core.config.settings.EMBEDDING_MODEL, default
BAAI/bge-m3) and compared by cosine similarity against other embedded text.
"Training" in that sense = building the index: embedding every chunk of
real spec text once and storing the vectors for fast nearest-neighbour
lookup. That's what this module does. There is no gradient descent, no
loss function, and nothing to overfit — re-running this script just adds
or skips chunks in the vector store.

If you actually want a trained *classifier* (e.g. a supervised SEP/no-SEP
predictor, closer to what research/engine.py's docstring says Paper 1 did
with Doc2Vec + Random Forest), that would be a separate effort: you'd need
labeled examples (patent text -> essential/not-essential) to fit a model
against, which this repo does not currently have or attempt.

WHAT GETS PARSED
-----------------
3GPP ships each spec version as a .zip containing ONE Word document. Two
eras exist:
  - Modern specs: .docx (Office Open XML) -- parsed here via python-docx.
  - Legacy specs: .doc (binary OLE2 format, pre-dates docx) -- NOT parsed
    by python-docx. This module detects and SKIPS these with a warning
    (counted in stats as `skipped_legacy_doc`) rather than silently
    producing garbage or crashing the run. If you need legacy .doc text,
    convert it to .docx/.txt first with an external tool (e.g. LibreOffice
    `soffice --headless --convert-to docx`) and re-point --input at the
    converted copies.

RESUMABILITY
------------
Each chunk gets a stable `source_id` derived from the zip's path relative
to the crawl root (which encodes series/spec/version) plus a chunk index.
Before embedding a zip's contents, this module checks whether chunks with
that file's source_id prefix already exist in the `knowledge_chunks` table
and skips re-embedding it if so -- embedding is the expensive step, and the
archive is large, so an interrupted indexing run can just be re-run.

EXTRACTED ARCHIVES / JSON EXPORTS (v0.4.2)
------------------------------------------
Besides the crawler's zips, the same --input folder may hold (in any
sub-folder layout, mixed freely):
  - extracted Word files named like the zips:  21111-d00.docx
  - per-version JSON exports named  3gpp_<spec>_v<x.y.z>.json
    (e.g. 3gpp_21.111_v13.0.0.json). The JSON schema is read tolerantly:
    clause/section lists (sections, clauses, chapters, content, children,
    paragraphs, chunks ...) with heading keys (clause, number, title,
    heading ...) and text keys (text, content, body, paragraphs ...). If
    no known structure is found, every long string value is indexed.
By default the CLI indexes only the LATEST version of each spec
(--all-versions to index every version), so search results aren't the
same clause repeated once per historical version.

USAGE
-----
    cd backend
    python -m ingestion.spec_corpus --input D:\\3gpp_full_archive
    python -m ingestion.spec_corpus --input D:\\3gpp_full_archive --include "38_series/38.211"
    python -m ingestion.spec_corpus --input D:\\3gpp_full_archive --limit-files 5   # smoke test

or from Python:
    from ingestion.spec_corpus import build_corpus_from_crawl
    stats = await build_corpus_from_crawl(Path("D:/3gpp_full_archive"))
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.database import KnowledgeChunk, init_db, AsyncSessionLocal
from utils.text_chunker import semantic_chunk

logger = logging.getLogger(__name__)

SOURCE_TYPE = "3gpp_spec_text"

# 3GPP version-code first character -> release, per the numbering scheme
# 3GPP has used since Release 10 (single digits 0-9 for early releases,
# then letters a, b, c, ... continuing the sequence). This is a best-effort
# convenience mapping for readability/filtering, not authoritative --
# always verify against the actual spec history page for anything that
# matters. (Same spirit as the CORPUS_DISCLAIMER in standards_corpus.py.)
_RELEASE_LETTERS = "0123456789abcdefghijklmnopqrstuvwxyz"


def version_code_to_release_guess(version_code: str) -> Optional[str]:
    """3GPP file version code -> release guess from its first character ('j40' -> 'Rel-19'), None when
    unknown.
    """
    if not version_code:
        return None
    first = version_code[0].lower()
    idx = _RELEASE_LETTERS.find(first)
    if idx < 0:
        return None
    return f"Rel-{idx}" if idx < 10 else f"Rel-{idx}"


_SPEC_ZIP_NAME_RE = re.compile(r"^(\d{2})(\d{3})-([0-9a-zA-Z]{3})\.zip$", re.IGNORECASE)


@dataclass
class SpecFileMeta:
    """What a 3GPP archive file name says about it: path, series, spec number, version code and (when
    derivable) dotted version and release.
    """
    zip_path: Path
    series: str            # e.g. "38_series"
    spec: str               # e.g. "38.211"
    version_code: str       # e.g. "j40"
    release_guess: Optional[str]
    rel_key: str             # stable relative-path key used for source_id/resume


def parse_spec_zip_meta(zip_path: Path, root: Path) -> Optional[SpecFileMeta]:
    """Derive spec/series/version metadata from a crawled zip's path.

    Expects the crawler's own hierarchy, e.g.:
        <root>/38_series/38.211/38211-j40.zip
    Returns None if the filename doesn't match the expected 3GPP spec-zip
    pattern (e.g. it's some other file the crawl happened to pick up).
    """
    m = _SPEC_ZIP_NAME_RE.match(zip_path.name)
    if not m:
        return None
    spec = f"{m.group(1)}.{m.group(2)}"
    version_code = m.group(3)
    try:
        rel_key = str(zip_path.relative_to(root))
    except ValueError:
        rel_key = zip_path.name
    series = zip_path.parent.parent.name if zip_path.parent.parent != root else zip_path.parent.name
    return SpecFileMeta(
        zip_path=zip_path,
        series=series,
        spec=spec,
        version_code=version_code,
        release_guess=version_code_to_release_guess(version_code),
        rel_key=rel_key,
    )


_SPEC_DOCX_NAME_RE = re.compile(r"^(\d{2})(\d{3})-([0-9a-zA-Z]{3})\.docx$", re.IGNORECASE)
_SPEC_JSON_NAME_RE = re.compile(
    r"^3gpp[_\- ]?(\d{2})\.?(\d{3}(?:-\d+)?)[_\- ]v?(\d+)\.(\d+)\.(\d+)\.json$", re.IGNORECASE)


def _version_code_from_dotted(major: int, minor: int, patch: int) -> str:
    """13.0.0 -> 'd00' (3GPP's own file-name version code)."""
    def c(n: int) -> str:
        """One version digit as 3GPP's base-36-style letter ('z' when out of range)."""
        return _RELEASE_LETTERS[n] if 0 <= n < len(_RELEASE_LETTERS) else "z"
    return f"{c(major)}{c(minor)}{c(patch)}"


def _version_sort_key(version_code: str) -> tuple:
    """Sort key for 3GPP version codes (so 'latest version per spec' can be picked)."""
    return tuple(_RELEASE_LETTERS.find(ch) for ch in (version_code or "").lower())


def _rel_key(path: Path, root: Path) -> str:
    """Path relative to the archive root (file name when outside it) - the stable id used to skip
    already-indexed files.
    """
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def parse_spec_json_meta(path: Path, root: Path) -> Optional[SpecFileMeta]:
    """3gpp_21.111_v13.0.0.json -> spec 21.111, version d00 (Rel-13)."""
    m = _SPEC_JSON_NAME_RE.match(path.name)
    if not m:
        return None
    spec = f"{m.group(1)}.{m.group(2)}"
    code = _version_code_from_dotted(int(m.group(3)), int(m.group(4)), int(m.group(5)))
    return SpecFileMeta(zip_path=path, series=f"{m.group(1)}_series", spec=spec, version_code=code,
                        release_guess=version_code_to_release_guess(code), rel_key=_rel_key(path, root))


def parse_spec_docx_meta(path: Path, root: Path) -> Optional[SpecFileMeta]:
    """SpecFileMeta from an extracted Word file name like '21111-d00.docx', or None."""
    m = _SPEC_DOCX_NAME_RE.match(path.name)
    if not m:
        return None
    code = m.group(3).lower()
    return SpecFileMeta(zip_path=path, series=f"{m.group(1)}_series", spec=f"{m.group(1)}.{m.group(2)}",
                        version_code=code, release_guess=version_code_to_release_guess(code),
                        rel_key=_rel_key(path, root))


_JSON_CHILD_KEYS = ("sections", "clauses", "subclauses", "subsections", "chapters", "children",
                    "content", "contents", "body", "paragraphs", "chunks", "items", "nodes", "blocks",
                    "annexes", "data", "document", "spec")
_JSON_TEXT_KEYS = ("text", "content", "body", "paragraph", "paragraphs", "value", "chunk",
                   "clause_text", "section_text", "full_text")
_JSON_NUMBER_KEYS = ("clause", "clause_number", "clause_id", "number", "section", "section_number",
                     "section_id", "id")
_JSON_TITLE_KEYS = ("title", "heading", "name", "clause_title", "section_title", "header")
_JSON_SKIP_KEYS = {"url", "uri", "path", "file", "filename", "source", "hash", "sha256", "md5", "date",
                   "created", "updated", "timestamp", "version", "spec", "spec_number", "release",
                   "id", "type", "lang", "language", "metadata", "meta"}


def _heading_of(node: dict) -> Optional[str]:
    """Heading text ('5.2.2 Title') of a JSON clause node from its number/title keys."""
    num = next((str(node[k]).strip() for k in _JSON_NUMBER_KEYS
                if isinstance(node.get(k), (str, int, float)) and str(node[k]).strip()), "")
    title = next((node[k].strip() for k in _JSON_TITLE_KEYS
                  if isinstance(node.get(k), str) and node[k].strip()), "")
    h = f"{num} {title}".strip() if title and num and num not in title else (title or num)
    return h or None


def extract_json_paragraphs(data) -> list[tuple[Optional[str], str]]:
    """[(clause heading or None, text), ...] from a 3GPP JSON export of
    unknown exact schema (see module docstring)."""
    out: list[tuple[Optional[str], str]] = []

    def walk(node, heading: Optional[str], depth: int) -> None:
        """Depth-first walk of a 3gpp_*.json document collecting (heading, paragraph) pairs; depth-
        limited against pathological files.
        """
        if depth > 40:
            return
        if isinstance(node, str):
            t = node.strip()
            if t:
                out.append((heading, t))
            return
        if isinstance(node, list):
            for item in node:
                walk(item, heading, depth + 1)
            return
        if not isinstance(node, dict):
            return
        h = _heading_of(node) or heading
        for k in _JSON_TEXT_KEYS:
            v = node.get(k)
            if isinstance(v, str) and v.strip():
                out.append((h, v.strip()))
            elif isinstance(v, list) and v and all(isinstance(x, str) for x in v):
                for x in v:
                    if x.strip():
                        out.append((h, x.strip()))
        for k in _JSON_CHILD_KEYS:
            v = node.get(k)
            if isinstance(v, (list, dict)) and not (isinstance(v, list) and v and all(isinstance(x, str) for x in v)):
                walk(v, h, depth + 1)
        # tables: list of rows
        for k in ("tables", "table"):
            v = node.get(k)
            if isinstance(v, list):
                for tbl in v:
                    rows = tbl.get("rows") if isinstance(tbl, dict) else tbl
                    for row in rows or []:
                        if isinstance(row, list):
                            cells = [str(c).strip() for c in row if str(c).strip()]
                            if cells:
                                out.append((h, " | ".join(cells)))

    walk(data, None, 0)
    if out:
        # de-duplicate exact repeats (a text key AND a child list may both carry it)
        seen = set()
        uniq = []
        for item in out:
            if item not in seen:
                seen.add(item)
                uniq.append(item)
        return uniq

    # Unknown schema: index every long-ish string that isn't obvious metadata.
    def harvest(node, key: str = "") -> None:
        """Fallback for unknown JSON layouts: collect every string value except metadata keys."""
        if isinstance(node, dict):
            for k, v in node.items():
                if str(k).lower() not in _JSON_SKIP_KEYS:
                    harvest(v, str(k))
        elif isinstance(node, list):
            for v in node:
                harvest(v, key)
        elif isinstance(node, str) and len(node.strip()) >= 40:
            out.append((None, node.strip()))
    harvest(data)
    return out


def _source_meta(path: Path, root: Path) -> tuple[Optional[str], Optional[SpecFileMeta]]:
    """(kind, SpecFileMeta) for a file: 'zip', 'docx' or 'json' (None for anything else)."""
    name = path.name.lower()
    if name.endswith(".zip"):
        return "zip", parse_spec_zip_meta(path, root)
    if name.endswith(".docx"):
        return "docx", parse_spec_docx_meta(path, root)
    if name.endswith(".json"):
        return "json", parse_spec_json_meta(path, root)
    return None, None


def iter_spec_sources(root: Path, include_re=None, exclude_re=None, latest_only: bool = False):
    """Yield (path, kind, meta) for every indexable spec file under root —
    crawler zips, extracted .docx and 3gpp_*.json — sorted for reproducible
    runs. latest_only keeps the newest version of each spec (per kind
    preference json > docx > zip when the same version exists twice).
    Files whose name doesn't look like a spec yield meta=None."""
    found = []
    for pattern in ("*.zip", "*.docx", "*.json"):
        for path in root.rglob(pattern):
            if path.name.startswith("~$"):  # Word lock files
                continue
            rel = _rel_key(path, root)
            if exclude_re and exclude_re.search(rel):
                continue
            if include_re and not include_re.search(rel):
                continue
            kind, meta = _source_meta(path, root)
            if kind == "json" and meta is None:
                continue  # unrelated JSON (manifests, configs) — not a spec
            found.append((path, kind, meta))
    found.sort(key=lambda t: str(t[0]))
    if latest_only:
        pref = {"json": 2, "docx": 1, "zip": 0}
        best: dict[str, tuple] = {}
        for path, kind, meta in found:
            if meta is None:
                continue
            key = (_version_sort_key(meta.version_code), pref.get(kind, 0))
            if meta.spec not in best or key > best[meta.spec][0]:
                best[meta.spec] = (key, path)
        keep = {v[1] for v in best.values()}
        found = [t for t in found if t[2] is None or t[0] in keep]
    yield from found


def iter_spec_zip_files(root: Path, include_re=None, exclude_re=None):
    """Yield every .zip under root whose path (relative to root) survives
    the include/exclude filters, in sorted order for reproducible runs."""
    for path in sorted(root.rglob("*.zip")):
        rel = str(path.relative_to(root))
        if exclude_re and exclude_re.search(rel):
            continue
        if include_re and not include_re.search(rel):
            continue
        yield path


def extract_docx_paragraphs(docx_bytes: bytes) -> list[tuple[Optional[str], str]]:
    """Return [(current_heading_or_None, paragraph_text), ...] for a .docx's
    body paragraphs and table cells, using python-docx.

    Heading tracking is best-effort: 3GPP documents use Word's built-in
    "Heading N" styles for clause numbers/titles (e.g. "4.2.1 Random
    Access"), which is what's tracked here as clause context for each
    following paragraph.
    """
    from docx import Document  # python-docx; imported lazily so importing
                                 # this module doesn't hard-require it for
                                 # callers who only need the metadata helpers

    doc = Document(io.BytesIO(docx_bytes))
    out: list[tuple[Optional[str], str]] = []
    current_heading: Optional[str] = None

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style_name = (para.style.name if para.style else "") or ""
        if style_name.lower().startswith("heading"):
            current_heading = text
            continue
        out.append((current_heading, text))

    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                out.append((current_heading, " | ".join(cells)))

    return out


def chunk_spec_paragraphs(
    paragraphs: list[tuple[Optional[str], str]], meta: SpecFileMeta
) -> list[dict]:
    """Group paragraphs by clause heading, then run each group through the
    platform's shared semantic_chunk() so chunk sizes stay consistent with
    everything else indexed into the vector store."""
    sections: dict[Optional[str], list[str]] = {}
    for heading, text in paragraphs:
        sections.setdefault(heading, []).append(text)

    chunks: list[dict] = []
    chunk_idx = 0
    for heading, texts in sections.items():
        section_text = "\n\n".join(texts)
        for piece in semantic_chunk(section_text):
            prefix = f"{meta.spec} {heading}: " if heading else f"{meta.spec}: "
            content = prefix + piece
            chunks.append({
                "id": f"{SOURCE_TYPE}_{meta.rel_key}_{chunk_idx}",
                "content": content,
                "metadata": {
                    "source_type": SOURCE_TYPE,
                    "spec": meta.spec,
                    "series": meta.series,
                    "version_code": meta.version_code,
                    "release_guess": meta.release_guess,
                    "clause": heading,
                    "source_file": meta.rel_key,
                },
            })
            chunk_idx += 1
    return chunks


def find_docx_in_zip(zip_path: Path) -> tuple[Optional[str], Optional[bytes]]:
    """Return (member_name, bytes) for the first .docx in the zip, or
    (member_name, None) if the zip only contains a legacy .doc, or
    (None, None) if it contains neither."""
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        docx_names = [n for n in names if n.lower().endswith(".docx")]
        doc_names = [n for n in names if n.lower().endswith(".doc")]
        if docx_names:
            return docx_names[0], zf.read(docx_names[0])
        if doc_names:
            return doc_names[0], None
        return None, None


@dataclass
class IngestStats:
    """Counters of one archive indexing run (files seen/indexed/skipped by reason, chunks, errors)."""
    files_seen: int = 0
    files_indexed: int = 0
    files_skipped_already_indexed: int = 0
    files_skipped_legacy_doc: int = 0
    files_skipped_no_document: int = 0
    files_skipped_error: int = 0
    chunks_indexed: int = 0
    errors: list = field(default_factory=list)


async def _already_indexed(session, rel_key: str) -> bool:
    """Whether chunks of this archive file are already in the Standards Corpus (re-runs skip it)."""
    from sqlalchemy import select, exists
    stmt = select(
        exists().where(
            KnowledgeChunk.source_type == SOURCE_TYPE,
            KnowledgeChunk.source_id.like(f"{SOURCE_TYPE}_{rel_key}_%"),
        )
    )
    result = await session.execute(stmt)
    return bool(result.scalar())


async def build_corpus_from_crawl(
    root: Path,
    include: Optional[str] = None,
    exclude: Optional[str] = None,
    limit_files: Optional[int] = None,
    batch_size: int = 32,
    vector_store=None,
    dry_run: bool = False,
    latest_only: bool = False,
    progress=None,
) -> IngestStats:
    """Walk a crawler output directory, extract/chunk real spec text, and
    index it into the vector store (+ record provenance rows in SQLite).

    Pass `vector_store` explicitly to inject a fake/mock in tests; defaults
    to the app's real `get_vector_store()` singleton otherwise.
    """
    if vector_store is None:
        from rag.vector_store import get_vector_store
        vector_store = get_vector_store()

    include_re = re.compile(include, re.IGNORECASE) if include else None
    exclude_re = re.compile(exclude, re.IGNORECASE) if exclude else None

    stats = IngestStats()
    await init_db()

    async with AsyncSessionLocal() as session:
        for zip_path, kind, meta in iter_spec_sources(root, include_re, exclude_re, latest_only=latest_only):
            if limit_files is not None and stats.files_indexed >= limit_files:
                break
            stats.files_seen += 1
            if meta is None:
                stats.files_skipped_no_document += 1
                continue

            if await _already_indexed(session, meta.rel_key):
                stats.files_skipped_already_indexed += 1
                continue

            try:
                if kind == "json":
                    with open(zip_path, "r", encoding="utf-8-sig") as f:
                        paragraphs = extract_json_paragraphs(json.load(f))
                    member_name, docx_bytes = zip_path.name, None
                elif kind == "docx":
                    member_name, docx_bytes = zip_path.name, zip_path.read_bytes()
                else:
                    member_name, docx_bytes = find_docx_in_zip(zip_path)
            except (zipfile.BadZipFile, OSError, ValueError) as e:
                logger.warning(f"Could not open {zip_path}: {e}")
                stats.files_skipped_error += 1
                stats.errors.append(f"{zip_path}: {e}")
                continue

            if member_name is None:
                stats.files_skipped_no_document += 1
                continue
            if kind != "json" and docx_bytes is None:
                logger.info(f"Skipping legacy .doc (not parseable here): {zip_path}")
                stats.files_skipped_legacy_doc += 1
                continue

            try:
                if kind != "json":
                    paragraphs = extract_docx_paragraphs(docx_bytes)
                chunks = chunk_spec_paragraphs(paragraphs, meta)
            except Exception as e:
                logger.warning(f"Failed to parse {zip_path}: {e}", exc_info=True)
                stats.files_skipped_error += 1
                stats.errors.append(f"{zip_path}: {e}")
                continue

            if not chunks:
                stats.files_skipped_no_document += 1
                continue

            if dry_run:
                stats.files_indexed += 1
                stats.chunks_indexed += len(chunks)
                logger.info(f"[dry-run] would index {len(chunks)} chunks from {zip_path}")
                continue

            for i in range(0, len(chunks), batch_size):
                batch = chunks[i:i + batch_size]
                await vector_store.upsert(batch)
                for c in batch:
                    session.add(KnowledgeChunk(
                        source_type=SOURCE_TYPE,
                        source_id=c["id"],
                        source_url=None,
                        title=meta.spec,
                        content=c["content"],
                        extra_metadata=json.dumps(c["metadata"]),
                    ))
                await session.flush()

            stats.files_indexed += 1
            stats.chunks_indexed += len(chunks)
            logger.info(f"Indexed {len(chunks)} chunks from {zip_path}")
            # Commit per file so an interrupted run keeps what it indexed
            # (the resume check above reads committed rows).
            await session.commit()
            if progress:
                progress(stats, zip_path)

        if not dry_run:
            await session.commit()

    return stats


def _print_stats(stats: IngestStats, elapsed: float):
    """Print the end-of-run summary for the CLI."""
    print("\n--- Corpus Ingestion Summary ---")
    print(f"Spec files seen:             {stats.files_seen}")
    print(f"Files indexed:                {stats.files_indexed}")
    print(f"Chunks indexed:               {stats.chunks_indexed}")
    print(f"Skipped (already indexed):   {stats.files_skipped_already_indexed}")
    print(f"Skipped (legacy .doc):       {stats.files_skipped_legacy_doc}")
    print(f"Skipped (no document found): {stats.files_skipped_no_document}")
    print(f"Skipped (error):             {stats.files_skipped_error}")
    if stats.errors:
        print("Errors (first 10):")
        for e in stats.errors[:10]:
            print(f"  - {e}")
    print(f"Elapsed:                      {elapsed:.1f}s")


def main():
    """CLI: `python -m ingestion.spec_corpus --input <folder>` - index a crawled/extracted 3GPP archive
    into the Standards Corpus (what index_3gpp.cmd runs when the app is not running).
    """
    import time

    ap = argparse.ArgumentParser(description="Index a crawled 3GPP archive into the vector store.")
    ap.add_argument("--input", default=None,
                    help="Folder with 3GPP spec files: crawler zips, extracted .docx and/or "
                         "3gpp_<spec>_v<x.y.z>.json (default: THREEGPP_ARCHIVE_DIR, i.e. backend/data/3gpp_archive).")
    ap.add_argument("--all-versions", action="store_true",
                    help="Index every version of each spec (default: only the latest version).")
    ap.add_argument("--include", default=None, help="Regex: only index zip files whose relative path matches this.")
    ap.add_argument("--exclude", default=None, help="Regex: skip zip files whose relative path matches this.")
    ap.add_argument("--limit-files", type=int, default=None, help="Stop after indexing this many files (smoke test).")
    ap.add_argument("--batch-size", type=int, default=32, help="Chunks per vector-store upsert call.")
    ap.add_argument("--dry-run", action="store_true", help="Parse and chunk but don't embed/index anything.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    from core.config import settings
    root = Path(args.input or settings.THREEGPP_ARCHIVE_DIR)
    if not root.is_dir():
        print(f"Input folder not found: {root}")
        raise SystemExit(2)
    print(f"Indexing 3GPP specs from {root} ({'all versions' if args.all_versions else 'latest version of each spec'})")
    t0 = time.time()

    def _progress(st, path):
        """Print one progress line per indexed file."""
        print(f"  [{st.files_indexed}] {path.name}: total {st.chunks_indexed} chunks", flush=True)

    stats = asyncio.run(build_corpus_from_crawl(
        root, include=args.include, exclude=args.exclude,
        limit_files=args.limit_files, batch_size=args.batch_size, dry_run=args.dry_run,
        latest_only=not args.all_versions, progress=_progress,
    ))
    _print_stats(stats, time.time() - t0)


if __name__ == "__main__":
    main()
