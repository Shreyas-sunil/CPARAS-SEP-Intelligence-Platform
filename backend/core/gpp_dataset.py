"""
CPARAS 3GPP dataset contract  (gpp_dataset.py, contract 1.0)
============================================================
One small, standard-library-only module that knows how to READ the validated
dataset produced by ``build_cparas_dataset.py`` (builder 2.x).  The very same
file is vendored into CPARAS Platform, CPARAS ISLD and Initial Categoriser
(``backend/core/gpp_dataset.py``) so the three independent applications share
a *file-format contract*, never a runtime dependency on each other.

Canonical dataset root  (``CPARAS_DATASET_ROOT``, e.g. ``D:\\3gpp\\cparas_dataset\\build_v4``)::

    etsi_dataset.jsonl            one JSON record per line (identity / classification / clauses ...)
    etsi_dataset.zip              etsi_dataset.jsonl + text/<raw_text_path>  (the extracted full text)
    spec_history_blobs.jsonl.gz   sidecar for large ``spec_history`` values (SHA-256 addressed)
    spec_reference_3gpp.json      cparas_spec_reference/1.0   -> Initial Categoriser
    standards_corpus_3gpp.json    cparas_standards_corpus/1.0 -> CPARAS Bulk scoring corpus
    build_report.json / validation_report.json / ledger.csv / warnings.csv / ...

Nothing in this module ever writes into the dataset root.  Everything is
streaming (bounded memory) and never invents data: a missing text file, a
missing history blob or an unknown version is reported, not papered over.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Iterator, Optional

CONTRACT_VERSION = "1.0"
ENV_ROOT = "CPARAS_DATASET_ROOT"
UNKNOWN = "Unknown"

JSONL_NAME = "etsi_dataset.jsonl"
ZIP_NAME = "etsi_dataset.zip"
SIDECAR_NAME = "spec_history_blobs.jsonl.gz"
REFERENCE_NAME = "spec_reference_3gpp.json"
CORPUS_NAME = "standards_corpus_3gpp.json"
BUILD_REPORT_NAME = "build_report.json"
VALIDATION_REPORT_NAME = "validation_report.json"

SUPPORTED_CORPUS_SCHEMA = "cparas_standards_corpus/1."
SUPPORTED_REFERENCE_SCHEMA = "cparas_spec_reference/1."
SUPPORTED_BUILDER_MAJOR = "2"

_SPEC_RE = re.compile(r"\d{2}\.\d{2,3}(?:-\d{1,2})?(?:[A-Za-z][A-Za-z0-9]{0,9})?")


class DatasetError(Exception):
    """A dataset problem the user can act on. ``code`` is machine readable."""

    def __init__(self, code: str, message: str):
        """`code` is a stable machine-readable reason (e.g. 'file_missing', 'zip_corrupt'); `message`
        is shown to the user.
        """
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        """API/JSON form {code, message}."""
        return {"code": self.code, "message": self.message}


class HistoryBlobError(DatasetError):
    """A problem with spec_history_blobs.jsonl.gz (externalised version-history blobs)."""
    pass


# ───────────────────────── paths / layout ─────────────────────────
@dataclass(frozen=True)
class Layout:
    """File names of a validated dataset build folder (etsi_dataset.jsonl, etsi_dataset.zip, history
    sidecar, spec reference, Bulk corpus, build/validation reports, text/). Each property returns
    the full path under `root`; nothing is checked for existence here.
    """
    root: Path

    @property
    def jsonl(self) -> Path: return self.root / JSONL_NAME
    @property
    def zip(self) -> Path: return self.root / ZIP_NAME
    @property
    def sidecar(self) -> Path: return self.root / SIDECAR_NAME
    @property
    def reference(self) -> Path: return self.root / REFERENCE_NAME
    @property
    def corpus(self) -> Path: return self.root / CORPUS_NAME
    @property
    def build_report(self) -> Path: return self.root / BUILD_REPORT_NAME
    @property
    def validation_report(self) -> Path: return self.root / VALIDATION_REPORT_NAME
    @property
    def text_dir(self) -> Path: return self.root / "text"


def clean_path_text(value: Optional[str]) -> str:
    """Tolerate the quotes/whitespace people paste around Windows paths."""
    if value is None:
        return ""
    return str(value).strip().strip('"').strip("'").strip()


def resolve_root(explicit: Optional[str] = None, *, must_exist: bool = True) -> Optional[Path]:
    """Dataset root from an explicit value or the CPARAS_DATASET_ROOT environment
    variable.  Returns None when nothing is configured.  Raises DatasetError when a
    configured path does not exist / is not a folder."""
    raw = clean_path_text(explicit) or clean_path_text(os.environ.get(ENV_ROOT))
    if not raw:
        return None
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if must_exist:
        if not p.exists():
            raise DatasetError("root_missing", f"Dataset folder does not exist: {p}")
        if not p.is_dir():
            raise DatasetError("root_not_directory", f"Dataset root is not a folder: {p}")
    return p


def layout(root: Path | str) -> Layout:
    """`Layout` for a dataset root folder."""
    return Layout(Path(root))


# ───────────────────────── small helpers ─────────────────────────
def is_unknown(value: Any) -> bool:
    """True for values the dataset uses to mean 'not known' (None, '', 'unknown', 'none', 'null',
    'n/a').
    """
    if value is None:
        return True
    s = str(value).strip().lower()
    return s in ("", "unknown", "none", "null", "n/a", "na")


def version_key(v: Any) -> Optional[tuple]:
    """Sortable (major, minor, patch) or None for an unknown/unparseable version."""
    if is_unknown(v):
        return None
    s = str(v).strip().lstrip("vV")
    try:
        parts = [int(x) for x in s.split(".")[:3]]
    except ValueError:
        return None
    return tuple(parts + [0] * (3 - len(parts)))


def norm_version(v: Any) -> str:
    """'v15.3.0' / '15.3.0' -> '15.3.0'; unknown stays 'Unknown' (never invented)."""
    if is_unknown(v):
        return UNKNOWN
    return str(v).strip().lstrip("vV")


def norm_release(r: Any) -> str:
    """'Rel-15' / 'Release 15' / '15' -> '15'; unknown stays 'Unknown'."""
    if is_unknown(r):
        return UNKNOWN
    m = re.fullmatch(r"(?i)\s*(?:rel(?:ease)?[-_ ]?)?(\d{1,2})\s*", str(r))
    return str(int(m.group(1))) if m else str(r).strip()


def norm_spec(s: Any) -> str:
    """'TS 38.331' / '38.331' -> '38.331'; unknown stays 'Unknown'."""
    if is_unknown(s):
        return UNKNOWN
    m = _SPEC_RE.search(str(s))
    return m.group(0) if m else str(s).strip()


_CLAUSE_NUM_KEYS = ("clause_number", "number", "clause", "id", "section")
_CLAUSE_TITLE_KEYS = ("clause_title", "title", "heading", "name")
_CLAUSE_TEXT_KEYS = ("text", "excerpt", "content", "body", "verbatim_text", "clause_text", "summary")


def normalise_clause(c: Any) -> Optional[dict]:
    """Map the builder's tolerated clause-key variants onto
    {clause_number, clause_title, summary, page}.  The text field the importer indexes
    is ``summary``; if the record stores clause text under another key
    (text/excerpt/content/...) it is carried over - verbatim, not rewritten."""
    if not isinstance(c, dict):
        return None
    num = next((str(c[k]).strip() for k in _CLAUSE_NUM_KEYS if c.get(k) not in (None, "")), "")
    title = next((str(c[k]).strip() for k in _CLAUSE_TITLE_KEYS if c.get(k) not in (None, "")), "")
    text = next((str(c[k]).strip() for k in _CLAUSE_TEXT_KEYS if c.get(k) not in (None, "")), "")
    page = c.get("page")
    out = {"clause_number": num or UNKNOWN, "clause_title": title or UNKNOWN, "summary": text}
    if isinstance(page, int):
        out["page"] = page
    return out


def atomic_write_json(path: Path, obj: Any) -> None:
    """Write via a temp file + rename so a crash never leaves half a report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Any:
    """Load a JSON file (BOM tolerated), raising DatasetError with a precise code for a missing,
    unreadable or invalid file.
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        raise DatasetError("file_missing", f"Missing file: {path}")
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise DatasetError("json_invalid", f"{path.name} is not valid JSON: {e}")
    except OSError as e:
        raise DatasetError("file_unreadable", f"Cannot read {path}: {e}")


# ───────────────────────── JSONL streaming ─────────────────────────
@dataclass
class JsonlStats:
    """Counters collected while streaming a JSONL file (lines, records, blank/malformed/non-object
    lines) plus the first few error locations.
    """
    lines: int = 0
    records: int = 0
    blank: int = 0
    malformed: int = 0
    not_object: int = 0
    errors: list = field(default_factory=list)   # first few (line_no, message)

    def note(self, line_no: int, msg: str, limit: int = 50) -> None:
        """Remember an error location (only the first `limit`)."""
        if len(self.errors) < limit:
            self.errors.append({"line": line_no, "error": msg})


def iter_jsonl(path: Path, stats: Optional[JsonlStats] = None) -> Iterator[tuple[int, dict]]:
    """Yield (line_no, record) one line at a time - never loads the file.  Malformed
    lines are counted in ``stats`` and skipped (never silently: the count is reported)."""
    stats = stats if stats is not None else JsonlStats()
    try:
        f = open(path, "rb")
    except OSError as e:
        raise DatasetError("file_unreadable", f"Cannot open {path}: {e}")
    with f:
        for line_no, raw in enumerate(f, start=1):
            stats.lines += 1
            if not raw.strip():
                stats.blank += 1
                continue
            try:
                rec = json.loads(raw.decode("utf-8"))
            except UnicodeDecodeError:
                try:
                    rec = json.loads(raw.decode("utf-8", errors="replace"))
                except Exception as e:  # noqa: BLE001
                    stats.malformed += 1
                    stats.note(line_no, f"undecodable line: {e}")
                    continue
            except json.JSONDecodeError as e:
                stats.malformed += 1
                stats.note(line_no, f"invalid JSON: {e}")
                continue
            if not isinstance(rec, dict):
                stats.not_object += 1
                stats.note(line_no, "line is valid JSON but not an object")
                continue
            stats.records += 1
            yield line_no, rec


# ───────────────────────── text sources ─────────────────────────
class TextSource:
    """Resolves a record's extracted text.  Context manager."""

    kind = "none"

    def get(self, raw_text_name: Optional[str], document_id: Optional[str]) -> Optional[str]:
        """Full text for a record, or None. The base class has no text; see DirTextSource /
        ZipTextSource.
        """
        return None

    def has(self, raw_text_name: Optional[str], document_id: Optional[str]) -> bool:
        """Existence check that does not read the text (cheap)."""
        return bool(self.get(raw_text_name, document_id))

    def close(self) -> None:
        """Release any open file handle."""
        pass

    def __enter__(self):
        """Use as a context manager; closed on exit."""
        return self

    def __exit__(self, *a):
        """Release the underlying file handle."""
        self.close()

    @staticmethod
    def candidates(raw_text_name: Optional[str], document_id: Optional[str]) -> list[str]:
        """File names under which a record's text may be stored: its raw_text_name (base name only) and
        '<document_id>.txt', de-duplicated.
        """
        names = []
        if raw_text_name:
            names.append(PurePosixPath(str(raw_text_name).replace("\\", "/")).name)
        if document_id:
            names.append(f"{document_id}.txt")
        return list(dict.fromkeys(n for n in names if n))


class DirTextSource(TextSource):
    """Record texts stored as individual files in a text/ folder next to the JSONL."""
    kind = "directory"

    def __init__(self, text_dir: Path):
        """`text_dir` is the folder holding '<document_id>.txt' / raw_text_name files."""
        self.dir = Path(text_dir)

    def has(self, raw_text_name, document_id):
        """Whether a text file exists for this record."""
        try:
            return any((self.dir / n).is_file() for n in self.candidates(raw_text_name, document_id))
        except OSError:
            return False

    def get(self, raw_text_name, document_id):
        """The record's text read from its file (undecodable bytes replaced), or None."""
        for name in self.candidates(raw_text_name, document_id):
            p = self.dir / name
            try:
                if p.is_file():
                    return p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        return None


class ZipTextSource(TextSource):
    """Reads ``text/<name>`` straight out of etsi_dataset.zip - no extraction to disk."""
    kind = "zip"

    def __init__(self, zip_path: Path):
        """Open the dataset ZIP once and index its member names; DatasetError for a corrupt or
        unreadable archive.
        """
        self.path = Path(zip_path)
        try:
            self._zf = zipfile.ZipFile(self.path)
        except zipfile.BadZipFile as e:
            raise DatasetError("zip_corrupt", f"{self.path.name} is not a valid ZIP archive: {e}")
        except OSError as e:
            raise DatasetError("file_unreadable", f"Cannot open {self.path}: {e}")
        self._names = {n for n in self._zf.namelist()}

    def get(self, raw_text_name, document_id):
        """The record's text read in place from the ZIP (text/<name> or <name>), or None - no
        extraction to disk.
        """
        for name in self.candidates(raw_text_name, document_id):
            for member in (f"text/{name}", name):
                if member in self._names:
                    try:
                        return self._zf.read(member).decode("utf-8", errors="replace")
                    except (zipfile.BadZipFile, OSError, RuntimeError):
                        return None
        return None

    def has(self, raw_text_name, document_id):
        """Whether the ZIP contains a text member for this record."""
        return any(m in self._names for name in self.candidates(raw_text_name, document_id)
                   for m in (f"text/{name}", name))

    def jsonl_member(self) -> Optional[str]:
        """Name of the dataset JSONL inside the ZIP (path-traversal entries ignored), or None."""
        for n in self._names:
            if n.endswith(".jsonl") and ".." not in PurePosixPath(n).parts:
                return n
        return None

    def open_jsonl(self):
        """Binary file object for the JSONL inside the ZIP; DatasetError when the ZIP has none."""
        m = self.jsonl_member()
        if not m:
            raise DatasetError("zip_no_jsonl", f"{self.path.name} contains no .jsonl dataset file.")
        return self._zf.open(m)

    def close(self):
        """Close the archive (errors ignored)."""
        try:
            self._zf.close()
        except Exception:  # noqa: BLE001
            pass


def check_zip(path: Path, *, crc: bool = False) -> dict:
    """Integrity check *before* trusting a ZIP.  Rejects unsafe member paths
    (zip-slip: absolute, drive-letter or '..' parts).  ``crc=True`` additionally
    reads every member (slow for multi-GB archives, but streaming)."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            for i in infos:
                n = i.filename.replace("\\", "/")
                parts = PurePosixPath(n).parts
                if n.startswith("/") or ".." in parts or re.match(r"^[A-Za-z]:", n):
                    raise DatasetError("zip_unsafe_path", f"Unsafe path inside {path.name}: {i.filename!r}")
            has_jsonl = any(i.filename.endswith(".jsonl") for i in infos)
            n_text = sum(1 for i in infos if i.filename.startswith("text/") and not i.is_dir())
            bad = zf.testzip() if crc else None
            if bad:
                raise DatasetError("zip_crc", f"CRC failure in {path.name}: {bad}")
            return {"members": len(infos), "has_jsonl": has_jsonl, "text_files": n_text, "crc_checked": crc}
    except zipfile.BadZipFile as e:
        raise DatasetError("zip_corrupt", f"{path.name} is not a valid ZIP archive: {e}")
    except FileNotFoundError:
        raise DatasetError("file_missing", f"Missing file: {path}")


def open_text_source(lay: Layout, prefer: str = "auto") -> TextSource:
    """prefer: auto (zip, then text/ folder) | zip | dir.  Raises if neither exists."""
    if prefer in ("auto", "zip") and lay.zip.is_file():
        return ZipTextSource(lay.zip)
    if prefer in ("auto", "dir") and lay.text_dir.is_dir():
        return DirTextSource(lay.text_dir)
    raise DatasetError("text_source_missing",
                       f"No extracted text found: expected {ZIP_NAME} (or a text/ folder) in {lay.root}.")


# ───────────────────────── fingerprint / status ─────────────────────────
def fingerprint(lay: Layout) -> str:
    """Cheap, stable identity of a build (no multi-GB hashing): file names + sizes + mtimes of the
    data files plus build_report.generated_at / records_written.  Changes whenever the build is
    replaced, so re-import and index-rebuild decisions can be made without reading 3 GB."""
    h = hashlib.sha256()
    for p in (lay.jsonl, lay.zip, lay.sidecar, lay.reference, lay.corpus):
        try:
            st = p.stat()
            h.update(f"{p.name}|{st.st_size}|{st.st_mtime_ns}\n".encode())
        except OSError:
            h.update(f"{p.name}|absent\n".encode())
    try:
        br = _read_json(lay.build_report)
        h.update(f"{br.get('generated_at')}|{br.get('records_written')}|{br.get('builder_version')}".encode())
    except DatasetError:
        pass
    return h.hexdigest()[:32]


def file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    """SHA-256 of a file, read in 1 MiB chunks (used for dataset/blob integrity checks)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def inspect_dataset(root: Path | str, *, needs: Iterable[str] = ("jsonl", "text")) -> dict:
    """Cheap status + diagnostics (reads only the small JSON reports).
    ``needs`` selects which artifacts the calling application requires: jsonl, text, reference,
    corpus, sidecar.  ``ok`` is True only if every needed artifact is present and compatible."""
    lay = layout(root)
    needs = set(needs)
    problems: list[dict] = []
    warnings: list[dict] = []
    files = {}
    for key, p in (("jsonl", lay.jsonl), ("zip", lay.zip), ("sidecar", lay.sidecar),
                   ("reference", lay.reference), ("corpus", lay.corpus),
                   ("build_report", lay.build_report), ("validation_report", lay.validation_report)):
        try:
            files[key] = {"path": str(p), "present": p.is_file(), "bytes": p.stat().st_size if p.is_file() else 0}
        except OSError:
            files[key] = {"path": str(p), "present": False, "bytes": 0}
    files["text_dir"] = {"path": str(lay.text_dir), "present": lay.text_dir.is_dir()}

    def need(name, ok, code, msg):
        """Record a problem when a file this inspection mode requires is missing."""
        if name in needs and not ok:
            problems.append({"code": code, "message": msg})

    need("jsonl", files["jsonl"]["present"], "jsonl_missing", f"{JSONL_NAME} not found in {lay.root}")
    need("text", files["zip"]["present"] or files["text_dir"]["present"], "text_missing",
         f"Neither {ZIP_NAME} nor a text/ folder found in {lay.root} (the extracted full text is required)")
    need("reference", files["reference"]["present"], "reference_missing", f"{REFERENCE_NAME} not found in {lay.root}")
    need("corpus", files["corpus"]["present"], "corpus_missing", f"{CORPUS_NAME} not found in {lay.root}")
    need("sidecar", files["sidecar"]["present"], "sidecar_missing", f"{SIDECAR_NAME} not found in {lay.root}")

    build = None
    if files["build_report"]["present"]:
        try:
            br = _read_json(lay.build_report)
            build = {k: br.get(k) for k in ("builder_version", "generated_at", "records_written",
                                           "records_with_unknown_version", "records_with_unknown_spec",
                                           "structured_files_scanned", "text_pairing", "spec_history", "corpus")}
            bv = str(br.get("builder_version") or "")
            if bv and not bv.startswith(SUPPORTED_BUILDER_MAJOR + "."):
                problems.append({"code": "builder_incompatible",
                                 "message": f"Dataset was built by builder {bv}; this integration supports {SUPPORTED_BUILDER_MAJOR}.x"})
            if (br.get("records_with_unknown_version") or 0) > 0:
                warnings.append({"code": "unknown_versions",
                                 "message": f"{br['records_with_unknown_version']} record(s) have an unknown specification version "
                                            "(kept, version shown as 'Unknown')."})
        except DatasetError as e:
            problems.append(e.to_dict())
    else:
        warnings.append({"code": "build_report_missing", "message": f"{BUILD_REPORT_NAME} not found - counts unverified."})

    validation = None
    if files["validation_report"]["present"]:
        try:
            vr = _read_json(lay.validation_report)
            validation = {"ok": bool(vr.get("ok")), "errors": len(vr.get("errors") or []),
                          "warnings": len(vr.get("warnings") or []), "records": (vr.get("stats") or {}).get("records")}
            if not vr.get("ok"):
                problems.append({"code": "validation_failed",
                                 "message": f"{VALIDATION_REPORT_NAME} reports {validation['errors']} error(s); re-run the builder's validate command."})
        except DatasetError as e:
            warnings.append(e.to_dict())
    else:
        warnings.append({"code": "validation_report_missing", "message": f"{VALIDATION_REPORT_NAME} not found - dataset not validated by the builder."})

    return {"contract": CONTRACT_VERSION, "root": str(lay.root), "ok": not problems, "needs": sorted(needs),
            "files": files, "build": build, "validation": validation, "problems": problems, "warnings": warnings,
            "fingerprint": fingerprint(lay)}


# ───────────────────────── version selection ─────────────────────────
VERSION_MODES = ("all", "latest", "latest-per-release")


def scan_identities(jsonl: Path, stats: Optional[JsonlStats] = None) -> Iterator[dict]:
    """Streaming pre-pass reading only identity fields (memory: one record at a time)."""
    for line_no, rec in iter_jsonl(jsonl, stats):
        ident = rec.get("identity") if isinstance(rec.get("identity"), dict) else {}
        yield {"line": line_no, "document_id": ident.get("document_id"), "spec": norm_spec(ident.get("document_number")),
               "version": norm_version(ident.get("version")), "release": norm_release(ident.get("release")),
               "series": str(ident.get("etsi_series") or "")}


def select_document_ids(idents: Iterable[dict], *, mode: str = "all", series: Iterable[str] = (),
                        specs: Iterable[str] = ()) -> tuple[set, dict]:
    """Return (selected document_ids, counters).  A record whose version is UNKNOWN cannot be
    ranked, so it is ALWAYS kept (counted in counters['unknown_version_kept']) - a 'latest' mode
    only ever drops records that have a known version older than another known version."""
    if mode not in VERSION_MODES:
        raise DatasetError("bad_mode", f"versions must be one of {VERSION_MODES}")
    series = {str(s).strip() for s in series if str(s).strip()}
    specs = {norm_spec(s) for s in specs if str(s).strip()}
    rows = [r for r in idents if r["document_id"]]
    counters = {"scanned": len(rows), "filtered_by_series": 0, "filtered_by_spec": 0,
                "superseded_by_newer_version": 0, "unknown_version_kept": 0, "selected": 0}
    pool = []
    for r in rows:
        if series and r["series"] not in series and r["spec"][:2] not in series:
            counters["filtered_by_series"] += 1
        elif specs and r["spec"] not in specs:
            counters["filtered_by_spec"] += 1
        else:
            pool.append(r)
    if mode == "all":
        keep = {r["document_id"] for r in pool}
    else:
        groups: dict = {}
        for r in pool:
            key = (r["spec"], r["release"]) if mode == "latest-per-release" else (r["spec"],)
            groups.setdefault(key, []).append(r)
        keep = set()
        for g in groups.values():
            known = [r for r in g if version_key(r["version"]) is not None]
            if known:
                best = max(known, key=lambda r: version_key(r["version"]))
                keep.add(best["document_id"])
                counters["superseded_by_newer_version"] += len(known) - 1
            unknown = [r for r in g if version_key(r["version"]) is None]
            keep.update(r["document_id"] for r in unknown)
            counters["unknown_version_kept"] += len(unknown)
    counters["selected"] = len(keep)
    return keep, counters


# ───────────────────────── spec_history sidecar ─────────────────────────
def history_refs(value: Any) -> Iterator[dict]:
    """Find externalised-history pointers ({'externalized': True, 'sha256': ..., 'sidecar': ...})."""
    if isinstance(value, dict):
        if value.get("externalized") is True and ("sha256" in value or "sidecar" in value):
            yield value
        else:
            for v in value.values():
                yield from history_refs(v)
    elif isinstance(value, list):
        for v in value:
            yield from history_refs(v)


def _canonical(value: Any) -> str:
    """Canonical compact JSON of a value (stable key order is the caller's), used to hash history
    blobs.
    """
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def iter_sidecar(path: Path) -> Iterator[tuple[int, dict]]:
    """Stream (line number, record) from the gzipped history-blob sidecar; HistoryBlobError for an
    unreadable file or a corrupt line.
    """
    try:
        f = gzip.open(path, "rt", encoding="utf-8")
    except OSError as e:
        raise HistoryBlobError("sidecar_unreadable", f"Cannot open {path}: {e}")
    with f:
        try:
            for n, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield n, json.loads(line)
                except json.JSONDecodeError as e:
                    raise HistoryBlobError("sidecar_line_invalid", f"{path.name} line {n}: {e}")
        except (EOFError, OSError, gzip.BadGzipFile) as e:
            raise HistoryBlobError("sidecar_corrupt", f"{path.name} is truncated or corrupt: {e}")


def verify_sidecar(path: Path, wanted: Optional[set] = None) -> dict:
    """Stream the whole sidecar, checking every blob's SHA-256 and byte count against its
    canonical JSON.  ``wanted`` (set of sha256) additionally reports references that have no blob."""
    seen, bad = set(), []
    n = 0
    for ln, item in iter_sidecar(path):
        n += 1
        sha, val = item.get("sha256"), item.get("value")
        canon = _canonical(val).encode("utf-8")
        if hashlib.sha256(canon).hexdigest() != sha:
            bad.append({"line": ln, "sha256": sha, "problem": "sha256_mismatch"})
        elif item.get("bytes") is not None and int(item["bytes"]) != len(canon):
            bad.append({"line": ln, "sha256": sha, "problem": "byte_count_mismatch"})
        seen.add(sha)
    missing = sorted(wanted - seen) if wanted else []
    return {"blobs": n, "bad": bad, "missing": missing, "ok": not bad and not missing}


def resolve_history_blobs(path: Path, shas: set) -> dict:
    """Return {sha256: value} for the requested blobs (one streaming pass), verifying each.
    Raises HistoryBlobError if one is missing or fails its checksum - never returns a stand-in."""
    out: dict = {}
    remaining = set(shas)
    for _ln, item in iter_sidecar(path):
        sha = item.get("sha256")
        if sha in remaining:
            if hashlib.sha256(_canonical(item.get("value")).encode()).hexdigest() != sha:
                raise HistoryBlobError("history_checksum", f"History blob {sha} fails its SHA-256 check")
            out[sha] = item.get("value")
            remaining.discard(sha)
            if not remaining:
                break
    if remaining:
        raise HistoryBlobError("history_blob_missing", f"{len(remaining)} history blob(s) not found in {path.name}: "
                                                       f"{sorted(remaining)[:3]}")
    return out


def resolve_history(record_history: Any, sidecar: Optional[Path]) -> Any:
    """Replace every pointer in a record's spec_history with its verified value.  Without a
    sidecar (or with a pointer that cannot be resolved) raises - callers decide how to report."""
    refs = list(history_refs(record_history))
    if not refs:
        return record_history
    if sidecar is None or not Path(sidecar).is_file():
        raise HistoryBlobError("sidecar_missing", f"History pointers present but {SIDECAR_NAME} is not available")
    shas = {r.get("sha256") for r in refs}
    if None in shas:
        raise HistoryBlobError("history_pointer_malformed", "A history pointer has no sha256")
    blobs = resolve_history_blobs(Path(sidecar), shas)

    def sub(v):
        """Recursively replace {'externalized': true, 'sha256': ...} placeholders with the stored blob."""
        if isinstance(v, dict):
            if v.get("externalized") is True and "sha256" in v:
                return blobs[v["sha256"]]
            return {k: sub(x) for k, x in v.items()}
        if isinstance(v, list):
            return [sub(x) for x in v]
        return v
    return sub(record_history)


# ───────────────────────── corpus / reference JSON ─────────────────────────
def load_corpus(path: Path) -> dict:
    """Load standards_corpus_3gpp.json (the CPARAS Bulk clause corpus) and check its schema/version and
    'families' list.
    """
    data = _read_json(path)
    if not isinstance(data, dict) or not str(data.get("schema", "")).startswith(SUPPORTED_CORPUS_SCHEMA) \
            or not isinstance(data.get("families"), list):
        raise DatasetError("corpus_schema", f"{Path(path).name}: expected schema '{SUPPORTED_CORPUS_SCHEMA}x' with a "
                                            f"'families' list, found {data.get('schema') if isinstance(data, dict) else type(data).__name__!r}")
    return data


def load_reference(path: Path) -> dict:
    """Load spec_reference_3gpp.json (spec id -> title/series metadata) and check its schema; adds an
    empty 'series' map when absent.
    """
    data = _read_json(path)
    if not isinstance(data, dict) or not str(data.get("schema", "")).startswith(SUPPORTED_REFERENCE_SCHEMA) \
            or not isinstance(data.get("specs"), dict):
        raise DatasetError("reference_schema", f"{Path(path).name}: expected schema '{SUPPORTED_REFERENCE_SCHEMA}x' with a "
                                               f"'specs' object, found {data.get('schema') if isinstance(data, dict) else type(data).__name__!r}")
    data.setdefault("series", {})
    return data
