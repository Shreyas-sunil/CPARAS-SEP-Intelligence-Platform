#!/usr/bin/env python3
r"""
embed_cparas.py 1.0 -- turn the CPARAS 3GPP dataset into vector embeddings.

Reads the output of build_cparas_dataset.py / extract_3gpp.py exactly as written
(etsi_dataset.zip, etsi_dataset_partNN.zip, or etsi_dataset.jsonl + text/), splits every
spec version into clause-aware chunks, embeds them, and writes a resumable vector store:

    <out>/
      embeddings.npy      float32 (rows, dim), L2-normalised: np.load(..., mmap_mode="r")
      chunks.jsonl        one line per row: text + metadata (spec, version, release, clause, ...)
      docs.jsonl          one line per embedded spec version (commit log, used to resume)
      manifest.json       model / chunking settings (a store never mixes two models)
      failed.jsonl        documents that could not be embedded this run (retried next run)
      embed_report.json   counts for this run
      faiss.index         (optional, --faiss)       chroma/ (optional, --chroma DIR)

Sub-commands
------------
  embed     (default) chunk + embed; re-run the same command to resume or add new records
  query     semantic search against a finished store ("RRC connection re-establishment")
  validate  check a store (row alignment, NaN, norms, duplicate ids, coverage of a dataset)
  export    build a FAISS index and/or a Chroma collection from an existing store

Typical use (PowerShell)
------------------------
  ollama pull nomic-embed-text
  python embed_cparas.py embed --dataset D:\3gpp\build_v2 --out D:\3gpp\vectors --dry-run
  python embed_cparas.py embed --dataset D:\3gpp\build_v2 --out D:\3gpp\vectors
  python embed_cparas.py query --out D:\3gpp\vectors "RRC connection re-establishment failure" -k 5
  python embed_cparas.py validate --out D:\3gpp\vectors --dataset D:\3gpp\build_v2

Never writes into the dataset folder.  Needs numpy; everything else is optional.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import numpy as np
except ImportError:                                    # pragma: no cover
    sys.exit("numpy is required:  pip install numpy")

TOOL_VERSION = "1.0"
CHUNKER_VERSION = "1"
UNKNOWN = "Unknown"
NPY_PREAMBLE = 256                                     # fixed header size so the file can grow in place
STORE_FILES = ("embeddings.npy", "chunks.jsonl", "docs.jsonl", "manifest.json", "failed.jsonl",
               "embed_report.json", "validation_report.json", "dryrun_report.json", "faiss.index")


# ═══════════════════════════════════════════════════════════════════════════
# small helpers
# ═══════════════════════════════════════════════════════════════════════════
def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(v):
    if v is None or isinstance(v, (dict, list)):
        return None
    s = str(v).strip()
    return None if not s or s.lower() in ("unknown", "none", "null", "n/a", "na") else s


def norm_release(r):
    s = _clean(r)
    if not s:
        return None
    m = re.fullmatch(r"(?i)(?:rel(?:ease)?[-_ ]?)?(\d{1,2})", s)
    return str(int(m.group(1))) if m else s


def version_num(v):
    """'15.3.0' -> 15003000 (sortable / range-filterable integer); unknown -> -1."""
    try:
        p = [int(x) for x in str(v).split(".")[:3]]
    except (ValueError, AttributeError):
        return -1
    p += [0] * (3 - len(p))
    return p[0] * 1_000_000 + p[1] * 1_000 + p[2]


def _within(child: Path, parent: Path) -> bool:
    c, p = os.path.normcase(str(child)), os.path.normcase(str(parent))
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def _dump(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except UnicodeEncodeError:                         # lone surrogates from Word text
        return json.dumps(obj, ensure_ascii=True)


def _line(obj) -> bytes:
    s = _dump(obj)
    try:
        return (s + "\n").encode("utf-8")
    except UnicodeEncodeError:
        return (json.dumps(obj, ensure_ascii=True) + "\n").encode("ascii")


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "surrogatepass")).hexdigest()


def _note(msg, quiet=False):
    if not quiet:
        print(msg, file=sys.stderr, flush=True)


# ═══════════════════════════════════════════════════════════════════════════
# Reading the dataset
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class SrcDoc:
    rec: dict
    raw: bytes                                         # the JSONL line (for the fingerprint)
    text: str | None
    text_name: str
    source: str


class Source:
    """etsi_dataset.zip / etsi_dataset_partNN.zip / etsi_dataset.jsonl + text/ (builder or extractor layout)."""

    def __init__(self, dataset: str, text_dir: str = ""):
        p = Path(dataset).expanduser().resolve()
        if not p.exists():
            raise SystemExit(f"--dataset not found: {p}")
        self.zips: list[Path] = []
        self.jsonl: Path | None = None
        self.text_dir: Path | None = Path(text_dir).expanduser().resolve() if text_dir else None
        if p.is_file():
            self.root = p.parent
            if p.suffix.lower() == ".zip":
                self.zips = [p]
            elif p.suffix.lower() == ".jsonl":
                self.jsonl = p
                self.text_dir = self.text_dir or p.parent / "text"
                if not self.text_dir.is_dir():
                    raise SystemExit(f"text folder not found: {self.text_dir}  (pass --text-dir)")
            else:
                raise SystemExit(f"--dataset must be a folder, a .zip or a .jsonl: {p}")
        else:
            if (p / "cparas_dataset").is_dir() and not (p / "etsi_dataset.jsonl").exists() \
                    and not (p / "etsi_dataset.zip").exists():
                p = p / "cparas_dataset"                   # extractor output folder given
            self.root = p
            td = self.text_dir or p / "text"
            parts = sorted(p.glob("etsi_dataset_part*.zip"))
            if (p / "etsi_dataset.jsonl").is_file() and td.is_dir():
                self.jsonl, self.text_dir = p / "etsi_dataset.jsonl", td
            elif (p / "etsi_dataset.zip").is_file():
                self.zips = [p / "etsi_dataset.zip"]
            elif parts:
                self.zips = parts
            elif (p / "etsi_dataset.jsonl").is_file():
                raise SystemExit(f"{p / 'etsi_dataset.jsonl'} found but no text folder and no etsi_dataset.zip; pass --text-dir")
            else:
                raise SystemExit(f"No etsi_dataset.zip / etsi_dataset_part*.zip / etsi_dataset.jsonl in {p}")

    @property
    def guarded(self) -> list[Path]:
        g = [self.root]
        if self.text_dir:
            g.append(self.text_dir)
        return g

    def describe(self) -> str:
        if self.jsonl:
            return f"{self.jsonl}  +  {self.text_dir}"
        return ", ".join(str(z) for z in self.zips)

    def count(self) -> int:
        n = 0
        if self.jsonl:
            with open(self.jsonl, "rb") as f:
                n = sum(1 for line in f if line.strip())
        for z in self.zips:
            with zipfile.ZipFile(z) as zf, zf.open("etsi_dataset.jsonl") as f:
                n += sum(1 for line in f if line.strip())
        return n

    @staticmethod
    def _text_name(rec):
        prov = rec.get("provenance") if isinstance(rec.get("provenance"), dict) else {}
        raw = _clean(prov.get("raw_text_path"))
        return Path(raw.replace("\\", "/")).name if raw else ""

    def __iter__(self):
        """Yields SrcDoc, or (None, error string) for an unreadable line."""
        if self.jsonl:
            with open(self.jsonl, "rb") as f:
                for n, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                        assert isinstance(rec, dict)
                    except Exception as e:
                        yield None, f"{self.jsonl.name} line {n}: {type(e).__name__}"
                        continue
                    name = self._text_name(rec)
                    did = str((rec.get("identity") or {}).get("document_id") or "")
                    text = None
                    for cand in (name, did + ".txt" if did else ""):
                        if cand and (self.text_dir / cand).is_file():
                            text, name = (self.text_dir / cand).read_bytes().decode("utf-8", "replace"), cand
                            break
                    yield SrcDoc(rec, line.strip(), text, name, self.jsonl.name), None
        for z in self.zips:
            with zipfile.ZipFile(z) as zf:
                names = set(zf.namelist())
                if "etsi_dataset.jsonl" not in names:
                    yield None, f"{z.name}: no etsi_dataset.jsonl inside"
                    continue
                with zf.open("etsi_dataset.jsonl") as f:
                    for n, line in enumerate(f, 1):
                        if not line.strip():
                            continue
                        try:
                            rec = json.loads(line)
                            assert isinstance(rec, dict)
                        except Exception as e:
                            yield None, f"{z.name} line {n}: {type(e).__name__}"
                            continue
                        name = self._text_name(rec)
                        text = zf.read("text/" + name).decode("utf-8", "replace") if name and "text/" + name in names else None
                        yield SrcDoc(rec, line.strip(), text, name, z.name), None


def doc_meta(rec: dict) -> dict:
    ident = rec.get("identity") if isinstance(rec.get("identity"), dict) else {}
    cls = rec.get("classification") if isinstance(rec.get("classification"), dict) else {}
    spec = _clean(ident.get("document_number")) or UNKNOWN
    version = _clean(ident.get("version")) or UNKNOWN
    series = _clean(ident.get("etsi_series"))
    if not series:
        series = spec[:2] if spec != UNKNOWN and spec[:2].isdigit() else UNKNOWN
    dtype = (_clean(ident.get("document_type")) or "").upper() or UNKNOWN
    return {"doc_id": str(ident.get("document_id") or ""), "pdf_url": str(ident.get("pdf_url") or ""),
            "spec": spec, "version": version, "version_num": version_num(version) if version != UNKNOWN else -1,
            "release": norm_release(ident.get("release")) or UNKNOWN, "series": series, "document_type": dtype,
            "title": _clean(ident.get("title")) or "", "domain": _clean(cls.get("technology_domain")) or ""}


# ═══════════════════════════════════════════════════════════════════════════
# Chunking
# ═══════════════════════════════════════════════════════════════════════════
HEAD_RE = re.compile(r"^[ \t]*((?:\d{1,3}|[A-Z])(?:\.\d{1,3}){0,7}[a-z]?)[ \t]+(\S[^\n]{0,250})$", re.M)
_WS = re.compile(r"\s")
MIN_KEEP = 40                                          # a chunk shorter than this (stripped) is a stray heading


def _tkey(s) -> str:
    return re.sub(r"[^0-9a-z]", "", (s or "").lower())


def _clause_fields(c: dict):
    num = ""
    for k in ("clause_number", "number", "clause", "id", "section"):
        v = _clean(c.get(k))
        if v:
            num = v
            break
    title = ""
    for k in ("clause_title", "title", "heading", "name"):
        v = _clean(c.get(k))
        if v:
            title = v
            break
    return num, title


def find_sections(text: str, clauses) -> tuple[list, int]:
    """Locate the record's clause headings in the full text.  -> ([(start, end, number, title)], n_known).
    A line counts as a heading only if its number is in the record's clause tree and (when the tree
    has a title) the line's title starts like the tree's title -- so '5.1' in a table never matches."""
    known = {}
    for c in clauses or []:
        if isinstance(c, dict):
            num, title = _clause_fields(c)
            if num:
                known.setdefault(num, title)
    if not known:
        return [], 0
    out, used = [], set()
    for m in HEAD_RE.finditer(text):
        num, title = m.group(1), m.group(2).strip()
        if num not in known or num in used:
            continue
        kt = _tkey(known[num])[:8]
        if kt and not _tkey(title).startswith(kt):
            continue
        used.add(num)
        out.append((m.start(), m.end(), num, known[num] or title))
    return out, len(known)


def split_text(s: str, max_chars: int, overlap: int, min_chars: int) -> list[tuple[int, int]]:
    """Character spans of at most ~max_chars, broken at paragraph > line > sentence > word boundaries,
    consecutive spans overlapping by ~overlap characters.  Always terminates."""
    n, spans, start = len(s), [], 0
    while start < n and s[start].isspace():
        start += 1
    while start < n:
        if n - start <= max_chars:
            end = n
        else:
            hi, lo = start + max_chars, start + max(min_chars, max_chars // 3)
            end = -1
            for sep in ("\n\n", "\n", ". ", "; ", ", ", " "):
                i = s.rfind(sep, lo, hi)
                if i >= 0:
                    end = i + len(sep)
                    break
            if end < 0:
                end = hi
            if n - end < min_chars and n - start <= max_chars + max_chars // 4:
                end = n                                # do not leave a tiny tail
        a, b = start, end
        while b > a and s[b - 1].isspace():
            b -= 1
        if b > a:
            spans.append((a, b))
        if end >= n:
            break
        nxt = end - overlap if overlap else end
        if nxt <= start:
            nxt = end
        if nxt < end:
            m = _WS.search(s, nxt, end)
            nxt = m.end() if m else end
        while nxt < n and s[nxt].isspace():
            nxt += 1
        start = nxt
    return spans


def _as_list(v):
    if isinstance(v, str):
        return [v]
    if not isinstance(v, list):
        return []
    out = []
    for x in v:
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            out.append(str(x.get("name") or x.get("title") or x.get("feature") or x.get("term") or ""))
    return [x.strip() for x in out if x and x.strip()]


def summary_text(rec: dict) -> str:
    cls = rec.get("classification") if isinstance(rec.get("classification"), dict) else {}
    ts = rec.get("technical_summary") if isinstance(rec.get("technical_summary"), dict) else {}
    parts = []
    for label, key in (("Scope", "scope"), ("Purpose", "purpose"), ("Summary", "technical_summary"),
                       ("Summary", "executive_summary")):
        v = _clean(ts.get(key))
        if v and not any(v in p for p in parts):
            parts.append(f"{label}: {v}")
    dom = " / ".join(x for x in (_clean(cls.get("technology_domain")), _clean(cls.get("subdomain"))) if x)
    if dom:
        parts.append("Domain: " + dom)
    for label, key in (("Topics", "topics"), ("Protocols", "protocols"), ("Interfaces", "interfaces"),
                       ("Network components", "network_components"), ("Key features", "key_features")):
        vals = _as_list(cls.get(key)) or _as_list(ts.get(key))
        if vals:
            parts.append(f"{label}: " + ", ".join(vals[:25]))
    body = "\n".join(parts)
    return body if len(body) >= 80 else ""


def make_chunks(rec: dict, text: str | None, cfg: dict, stats: Counter | None = None) -> list[dict]:
    """-> list of {kind, clause, clause_title, clauses_covered, char_start, char_end, text, llm_generated}.
    char offsets refer to the text with \\r\\n normalised to \\n."""
    stats = stats if stats is not None else Counter()
    mx, ov, mn = cfg["max_chars"], cfg["overlap"], cfg["min_chars"]
    chunks = []
    if cfg["summary"]:
        s = summary_text(rec)
        for a, b in split_text(s, mx, ov, mn) if s else []:
            chunks.append({"kind": "summary", "clause": "", "clause_title": "", "clauses_covered": [],
                           "char_start": -1, "char_end": -1, "text": s[a:b], "llm_generated": True})
    if not text or not text.strip():
        return chunks
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    secs, n_known = find_sections(text, rec.get("clauses") if isinstance(rec.get("clauses"), list) else [])
    stats["clauses_known"] += n_known
    stats["clauses_located"] += len(secs)
    stats["docs_with_clause_attribution"] += 1 if secs else 0
    raw = []
    if secs:
        if secs[0][0] > 0:
            raw.append([0, secs[0][0], "", "", []])
        for i, (st, _e, num, title) in enumerate(secs):
            en = secs[i + 1][0] if i + 1 < len(secs) else len(text)
            raw.append([st, en, num, title, [num]])
    else:
        raw = [[0, len(text), "", "", []]]
    units, carry = [], None
    for i, u in enumerate(raw):
        if carry:                                      # tiny section (heading only): glue to the next one
            u[0], u[4] = carry[0], carry[1] + u[4]
            carry = None
        if len(text[u[0]:u[1]].strip()) < mn and i + 1 < len(raw):
            carry = (u[0], u[4])
            continue
        units.append(u)
    for st, en, num, title, covered in units:
        for a, b in split_text(text[st:en], mx, ov, mn):
            piece = text[st + a:st + b]
            if len(piece.strip()) < MIN_KEEP:
                stats["tiny_pieces_dropped"] += 1
                continue
            chunks.append({"kind": "text", "clause": num, "clause_title": title, "clauses_covered": covered,
                           "char_start": st + a, "char_end": st + b, "text": piece, "llm_generated": False})
    return chunks


def context_header(dm: dict, ch: dict) -> str:
    """Spec + clause line prepended to the embedded text (NOT the version, so identical clause text in
    different versions embeds identically and is computed once)."""
    head = " ".join(x for x in (dm["document_type"] if dm["document_type"] != UNKNOWN else "",
                                dm["spec"] if dm["spec"] != UNKNOWN else "", dm["title"]) if x)
    if ch["kind"] == "summary":
        head = (head + " - overview").strip()
    elif ch["clause"]:
        head = (head + "\n" if head else "") + f"Clause {ch['clause']} {ch['clause_title']}".rstrip()
    return head + "\n\n" if head else ""


def embed_input(dm, ch, cfg) -> str:
    return cfg["doc_prefix"] + (context_header(dm, ch) if cfg["context_header"] else "") + ch["text"]


# ═══════════════════════════════════════════════════════════════════════════
# Embedding backends
# ═══════════════════════════════════════════════════════════════════════════
class TransientError(Exception):
    """Server unreachable / overloaded: retried, then the run stops (everything committed stays)."""


class ItemError(Exception):
    """This input (or batch) was rejected: the batch is split to isolate it; the document is retried next run."""


class FatalEmbedError(Exception):
    pass


def auto_prefixes(model: str) -> tuple[str, str]:
    """(document prefix, query prefix) the model was trained with."""
    m = (model or "").lower()
    rsq = "Represent this sentence for searching relevant passages: "
    if "nomic-embed" in m:
        return "search_document: ", "search_query: "
    if "embeddinggemma" in m:
        return "title: none | text: ", "task: search result | query: "
    if "qwen3-embedding" in m or "qwen3_embedding" in m:
        return "", ("Instruct: Given a question about telecom standards, retrieve passages of 3GPP "
                    "specifications that answer it\nQuery: ")
    if re.search(r"(?:^|[/:_-])(?:multilingual-)?e5(?:[-_.:]|$)", m):
        return "passage: ", "query: "
    if "bge-m3" in m:
        return "", ""
    if "mxbai-embed" in m or "snowflake-arctic-embed" in m or re.search(r"(?:^|[/:_-])bge-", m):
        return "", rsq
    return "", ""


def _http_json(url, payload=None, timeout=300, headers=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})},
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read()[:500].decode("utf-8", "replace")
        if e.code in (408, 429, 502, 503, 504):
            raise TransientError(f"HTTP {e.code} from {url}: {body}")
        raise ItemError(f"HTTP {e.code} from {url}: {body}") from None
    except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError) as e:
        raise TransientError(f"{url}: {e}")


class OllamaEmbedder:
    name = "ollama"

    def __init__(self, url, model, timeout=300, keep_alive="30m", num_ctx=0):
        self.url, self.model, self.timeout = url.rstrip("/"), model, timeout
        self.keep_alive, self.num_ctx, self.legacy = keep_alive, num_ctx, False

    def check(self):
        try:
            tags = _http_json(self.url + "/api/tags", timeout=20)
        except (TransientError, ItemError) as e:
            raise SystemExit(f"Ollama not reachable at {self.url} ({e}). Start it with 'ollama serve'.")
        names = {m.get("name", "") for m in tags.get("models", [])} | {m.get("model", "") for m in tags.get("models", [])}
        if self.model not in names and self.model + ":latest" not in names:
            raise SystemExit(f"Ollama model '{self.model}' is not pulled. Run:  ollama pull {self.model}")

    def embed(self, texts):
        if not self.legacy:
            payload = {"model": self.model, "input": texts, "truncate": True}
            if self.keep_alive:
                payload["keep_alive"] = self.keep_alive
            if self.num_ctx:
                payload["options"] = {"num_ctx": self.num_ctx}
            try:
                return _http_json(self.url + "/api/embed", payload, self.timeout)["embeddings"]
            except ItemError as e:
                if "HTTP 404" in str(e) and "not found" not in str(e).lower():
                    self.legacy = True                 # Ollama < 0.3.4 has only /api/embeddings
                else:
                    raise
        return [_http_json(self.url + "/api/embeddings", {"model": self.model, "prompt": t}, self.timeout)["embedding"]
                for t in texts]


class OpenAICompatEmbedder:
    """vLLM, Text Embeddings Inference, LM Studio, llama.cpp server, LocalAI, OpenAI ... (POST <url>/embeddings)."""
    name = "openai"

    def __init__(self, url, model, timeout=300, api_key=""):
        self.url, self.model, self.timeout = url.rstrip("/"), model, timeout
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def check(self):
        self.embed(["connection test"])

    def embed(self, texts):
        r = _http_json(self.url + "/embeddings", {"model": self.model, "input": texts}, self.timeout, self.headers)
        data = sorted(r.get("data", []), key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in data]


class STEmbedder:
    """sentence-transformers, fully offline (model name or a local folder)."""
    name = "sentence-transformers"

    def __init__(self, model, device="", batch=32):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise SystemExit("pip install sentence-transformers   (or use --backend ollama)")
        self.m, self.batch = SentenceTransformer(model, device=device or None), batch

    def check(self):
        pass

    def embed(self, texts):
        try:
            return self.m.encode(texts, batch_size=self.batch, convert_to_numpy=True,
                                 normalize_embeddings=False, show_progress_bar=False)
        except Exception as e:
            raise ItemError(f"{type(e).__name__}: {e}")


class HashEmbedder:
    """Deterministic lexical hashing vectors.  For testing the pipeline only -- NOT semantic."""
    name = "hash"

    def __init__(self, dim=256):
        self.dim = dim

    def check(self):
        pass

    def embed(self, texts):
        out = []
        for t in texts:
            v = np.zeros(self.dim, dtype=np.float32)
            toks = re.findall(r"[a-z0-9]+", t.lower())
            for tok in toks + [a + " " + b for a, b in zip(toks, toks[1:])]:
                h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "little")
                v[h % self.dim] += 1.0 if (h >> 32) & 1 else -1.0
            if not v.any():
                v[0] = 1.0
            out.append(v)
        return out


def make_embedder(cfg: dict, args=None):
    g = lambda k, d=None: (getattr(args, k, None) if args is not None and getattr(args, k, None) not in (None, "") else d)
    b = cfg["backend"]
    if b == "ollama":
        return OllamaEmbedder(g("ollama_url", cfg.get("url") or "http://127.0.0.1:11434"), cfg["model"],
                              g("timeout", 300), g("keep_alive", "30m"), g("num_ctx", 0))
    if b == "openai":
        return OpenAICompatEmbedder(g("api_url", cfg.get("url") or ""), cfg["model"], g("timeout", 300),
                                    os.environ.get("EMBED_API_KEY", ""))
    if b == "sentence-transformers":
        return STEmbedder(cfg["model"], g("device", ""), g("batch_size", 32))
    if b == "hash":
        return HashEmbedder(int(cfg.get("hash_dim") or 256))
    raise SystemExit(f"unknown backend {b}")


def _call(emb, texts, stats, retries):
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            stats["embed_calls"] += 1
            vecs = emb.embed(texts)
            if len(vecs) != len(texts):
                raise ItemError(f"backend returned {len(vecs)} vectors for {len(texts)} inputs")
            stats["embed_inputs"] += len(texts)
            return vecs
        except TransientError as e:
            if attempt == retries:
                raise FatalEmbedError(f"embedding backend unavailable after {retries + 1} attempts: {e}")
            time.sleep(delay)
            delay *= 3


def embed_robust(emb, texts, stats, retries=3):
    """-> list aligned with texts: float32 vector, or ItemError.  Splits a rejected batch to isolate bad inputs."""
    try:
        vecs = _call(emb, texts, stats, retries)
    except ItemError as e:
        if len(texts) == 1:
            return [e]
        stats["batch_splits"] += 1
        mid = len(texts) // 2
        return embed_robust(emb, texts[:mid], stats, retries) + embed_robust(emb, texts[mid:], stats, retries)
    out = []
    for v in vecs:
        a = np.asarray(v, dtype=np.float32).reshape(-1)
        out.append(a if a.size and np.isfinite(a).all() else ItemError("empty or non-finite vector"))
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Store
# ═══════════════════════════════════════════════════════════════════════════
class NpyStore:
    """embeddings.npy that grows in place.  The header has a fixed 256-byte size and is rewritten after
    every commit, so the file is always a valid .npy of the committed rows (np.load(path, mmap_mode='r'))."""

    def __init__(self, path: Path):
        self.path, self.dim, self.rows, self.fh = Path(path), None, 0, None

    @staticmethod
    def header(rows, dim) -> bytes:
        d = "{'descr': '<f4', 'fortran_order': False, 'shape': (%d, %d), }" % (rows, dim)
        hl = NPY_PREAMBLE - 10
        return b"\x93NUMPY\x01\x00" + hl.to_bytes(2, "little") + (d.ljust(hl - 1) + "\n").encode("latin1")

    @staticmethod
    def read_shape(path):
        with open(path, "rb") as f:
            pre = f.read(NPY_PREAMBLE)
        if pre[:8] != b"\x93NUMPY\x01\x00" or 10 + int.from_bytes(pre[8:10], "little") != NPY_PREAMBLE:
            raise ValueError(f"{path} was not written by embed_cparas.py")
        m = re.search(r"'shape': \((\d+), (\d+)\)", pre[10:].decode("latin1"))
        if not m or "'<f4'" not in pre[10:].decode("latin1"):
            raise ValueError(f"{path}: unexpected header")
        return int(m.group(1)), int(m.group(2))

    def open(self, committed_rows):
        if not self.path.exists():
            if committed_rows:
                raise SystemExit(f"{self.path} is missing but docs.jsonl commits {committed_rows} rows: store damaged "
                                 f"(use a new --out or --overwrite)")
            return
        _, self.dim = self.read_shape(self.path)
        need = NPY_PREAMBLE + committed_rows * self.dim * 4
        if self.path.stat().st_size < need:
            raise SystemExit(f"{self.path} is shorter than the {committed_rows} committed rows: store damaged")
        self.fh = open(self.path, "r+b")
        self.fh.truncate(need)
        self.rows = committed_rows
        self.sync()

    def append(self, arr):
        if self.fh is None:
            self.dim = arr.shape[1]
            self.fh = open(self.path, "w+b")
            self.fh.write(self.header(0, self.dim))
        self.fh.seek(0, 2)
        self.fh.write(np.ascontiguousarray(arr, dtype="<f4").tobytes())
        self.rows += arr.shape[0]

    def sync(self):
        if self.fh:
            self.fh.flush()
            self.fh.seek(0)
            self.fh.write(self.header(self.rows, self.dim))
            self.fh.flush()
            self.fh.seek(0, 2)

    def read_row(self, row):
        self.fh.flush()
        self.fh.seek(NPY_PREAMBLE + row * self.dim * 4)
        v = np.frombuffer(self.fh.read(self.dim * 4), dtype="<f4").astype(np.float32)
        self.fh.seek(0, 2)
        return v

    def close(self):
        if self.fh:
            self.sync()
            self.fh.close()
            self.fh = None


def _truncate_lines(path: Path, n: int):
    """Keep exactly the first n complete lines of a file (crash repair)."""
    if not path.exists():
        if n:
            raise SystemExit(f"{path.name} is missing but docs.jsonl commits {n} rows: store damaged")
        return
    with open(path, "r+b") as f:
        if n == 0:
            f.truncate(0)
            return
        count, off = 0, 0
        while True:
            block = f.read(1 << 20)
            if not block:
                break
            i = -1
            while True:
                i = block.find(b"\n", i + 1)
                if i < 0:
                    break
                count += 1
                if count == n:
                    f.truncate(off + i + 1)
                    return
            off += len(block)
    raise SystemExit(f"{path.name} has only {count} complete lines but docs.jsonl commits {n} rows: store damaged")


CONFIG_KEYS = ("backend", "model", "doc_prefix", "query_prefix", "normalize", "max_chars", "overlap",
               "min_chars", "context_header", "summary", "chunker_version", "hash_dim")


def config_hash(cfg):
    return _sha(json.dumps({k: cfg.get(k) for k in CONFIG_KEYS}, sort_keys=True))[:16]


class Store:
    def __init__(self, out: Path, cfg: dict, overwrite=False, quiet=False):
        self.out, self.quiet = out, quiet
        out.mkdir(parents=True, exist_ok=True)
        if overwrite:
            for n in STORE_FILES:
                (out / n).unlink(missing_ok=True)
        mp = out / "manifest.json"
        if mp.exists():
            man = json.loads(mp.read_text(encoding="utf-8"))
            if man.get("config_hash") != config_hash(cfg):
                old = man.get("config", {})
                diff = {k: (old.get(k), cfg.get(k)) for k in CONFIG_KEYS if old.get(k) != cfg.get(k)}
                raise SystemExit(f"{out} holds embeddings made with different settings {diff}. Vectors from two models "
                                 f"or chunkings must not be mixed: use a new --out, or --overwrite to start again.")
            self.manifest = man
        else:
            self.manifest = {"tool_version": TOOL_VERSION, "created_at": _now(), "config": cfg,
                             "config_hash": config_hash(cfg), "dim": None}
        self.done: dict[str, str] = {}
        self.key_row: dict[str, int] = {}
        self.rows = 0
        dp = out / "docs.jsonl"
        if dp.exists():
            good = 0
            data = dp.read_bytes()
            for line in data.splitlines(keepends=True):
                if not line.endswith(b"\n"):
                    break                                  # half-written tail
                try:
                    d = json.loads(line)
                except ValueError:
                    break
                if d["row_start"] != self.rows:
                    raise SystemExit(f"{dp} is inconsistent (row_start {d['row_start']} != {self.rows}): store damaged")
                for i, k in enumerate(d["keys"]):
                    self.key_row.setdefault(k, d["row_start"] + i)
                self.rows = d["row_end"]
                self.done[d["doc_id"]] = d["fingerprint"]
                good += len(line)
            if good != len(data):
                _note(f"repairing {dp.name}: dropping an unfinished last entry", quiet)
                with open(dp, "r+b") as f:
                    f.truncate(good)
        _truncate_lines(out / "chunks.jsonl", self.rows)
        self.npy = NpyStore(out / "embeddings.npy")
        self.npy.open(self.rows)
        if self.npy.dim:
            self.manifest["dim"] = self.npy.dim
        self.chunks_fh = open(out / "chunks.jsonl", "ab")
        self.docs_fh = open(dp, "ab")
        self.failed_fh = open(out / "failed.jsonl", "wb")
        self._write_manifest()

    def _write_manifest(self):
        self.manifest["updated_at"] = _now()
        self.manifest["rows"] = self.rows
        tmp = self.out / "manifest.json.partial"
        tmp.write_text(json.dumps(self.manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.out / "manifest.json")

    def commit(self, job):
        """vectors -> chunk lines -> docs line (the commit marker) -> header.  A crash anywhere is repaired
        on the next start by truncating back to the last docs line."""
        n = len(job.metas)
        r0 = self.rows
        if n:
            arr = np.vstack(job.vecs).astype(np.float32)
            if self.npy.dim and arr.shape[1] != self.npy.dim:
                raise FatalEmbedError(f"vector size changed from {self.npy.dim} to {arr.shape[1]}")
            self.npy.append(arr)
            buf = []
            for i, m in enumerate(job.metas):
                m["row"] = r0 + i
                buf.append(_line(m))
            self.chunks_fh.write(b"".join(buf))
            self.chunks_fh.flush()
        self.docs_fh.write(_line({"doc_id": job.doc_id, "fingerprint": job.fp, "row_start": r0, "row_end": r0 + n,
                                  "n_chunks": n, "spec": job.dm["spec"], "version": job.dm["version"],
                                  "source": job.source, "keys": job.keys, "committed_at": _now()}))
        self.docs_fh.flush()
        self.npy.sync()
        self.rows = r0 + n
        for i, k in enumerate(job.keys):
            self.key_row.setdefault(k, r0 + i)
        self.done[job.doc_id] = job.fp
        if self.npy.dim and not self.manifest.get("dim"):
            self.manifest["dim"] = self.npy.dim
            self._write_manifest()

    def fail(self, job, err):
        self.failed_fh.write(_line({"doc_id": job.doc_id, "spec": job.dm["spec"], "version": job.dm["version"],
                                    "source": job.source, "error": err}))
        self.failed_fh.flush()

    def read_row(self, row):
        return self.npy.read_row(row)

    def close(self):
        self.npy.close()
        for f in (self.chunks_fh, self.docs_fh, self.failed_fh):
            f.close()
        self._write_manifest()


# ═══════════════════════════════════════════════════════════════════════════
# embed
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class Job:
    doc_id: str
    fp: str
    dm: dict
    source: str
    metas: list = field(default_factory=list)
    keys: list = field(default_factory=list)
    vecs: list = field(default_factory=list)
    remaining: int = 0
    failed: bool = False
    error: str = ""


def build_cfg(a) -> dict:
    dp, qp = auto_prefixes(a.model)
    if a.overlap * 2 >= a.max_chars:
        raise SystemExit("--overlap must be less than half of --max-chars")
    url = a.ollama_url if a.backend == "ollama" else (a.api_url if a.backend == "openai" else "")
    if a.backend == "openai" and not url:
        raise SystemExit("--backend openai needs --api-url (e.g. http://localhost:8000/v1)")
    return {"backend": a.backend, "model": a.model, "url": url,
            "doc_prefix": dp if a.doc_prefix is None else a.doc_prefix,
            "query_prefix": qp if a.query_prefix is None else a.query_prefix,
            "normalize": not a.no_normalize, "max_chars": a.max_chars, "overlap": a.overlap,
            "min_chars": a.min_chars, "context_header": not a.no_context_header, "summary": not a.no_summary,
            "chunker_version": CHUNKER_VERSION, "hash_dim": a.hash_dim if a.backend == "hash" else None}


def _selected(dm, series_f, specs_f):
    if series_f and dm["series"] not in series_f:
        return False
    if specs_f and dm["spec"].casefold() not in specs_f:
        return False
    return True


def cmd_embed(a, embedder=None) -> int:
    quiet = a.quiet
    if a.backend in ("ollama", "openai") and not a.model:
        a.model = "nomic-embed-text"
    cfg = build_cfg(a)
    src = Source(a.dataset, a.text_dir)
    out = Path(a.out).expanduser().resolve()
    for g in src.guarded:
        if _within(out, g) or _within(g, out):
            raise SystemExit(f"--out must be a separate folder: {out} overlaps the dataset folder {g} "
                             f"(the dataset is never written to).")
    series_f = {s.strip() for s in a.series.split(",") if s.strip()}
    specs_f = {s.strip().casefold() for s in a.specs.split(",") if s.strip()}
    _note(f"dataset : {src.describe()}\noutput  : {out}\nmodel   : {cfg['backend']} / {cfg['model']}   "
          f"chunks <= {cfg['max_chars']} chars, overlap {cfg['overlap']}", quiet)
    total = src.count()
    stats, cstats = Counter(), Counter()
    for k in ("docs_seen", "docs_queued", "docs_embedded", "docs_failed", "chunks_written", "embed_inputs",
              "embed_calls", "reused_vectors", "already_embedded", "changed_since_embedded", "filtered",
              "docs_without_text", "unreadable_lines", "docs_without_id", "inputs_failed"):
        stats[k] = 0
    started = time.time()

    if a.dry_run:
        keys, kinds, chars, per_doc = set(), Counter(), 0, []
        for item, err in src:
            if item is None:
                stats["unreadable_lines"] += 1
                continue
            dm = doc_meta(item.rec)
            if not _selected(dm, series_f, specs_f):
                stats["filtered"] += 1
                continue
            stats["docs"] += 1
            if item.text is None:
                stats["docs_without_text"] += 1
            chs = make_chunks(item.rec, item.text, cfg, cstats)
            per_doc.append(len(chs))
            for ch in chs:
                inp = embed_input(dm, ch, cfg)
                keys.add(_sha(inp)[:32])
                kinds[ch["kind"]] += 1
                chars += len(inp)
            if a.limit and stats["docs"] >= a.limit:
                break
        n = sum(per_doc)
        rep = {"tool_version": TOOL_VERSION, "dry_run": True, "dataset": src.describe(), "config": cfg,
               "documents_in_dataset": total, **stats, "chunks": n, "chunks_by_kind": dict(kinds),
               "unique_embedding_inputs": len(keys), "saved_by_dedup": n - len(keys),
               "characters_to_embed": chars, "avg_chunks_per_doc": round(n / max(len(per_doc), 1), 1),
               "max_chunks_in_one_doc": max(per_doc or [0]), "chunking": dict(cstats),
               "vector_store_mb_at_768_dims": round(n * 768 * 4 / 1e6, 1)}
        out.mkdir(parents=True, exist_ok=True)
        (out / "dryrun_report.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
        _note(f"dry run: {stats['docs']} documents -> {n} chunks ({len(keys)} unique inputs to embed, "
              f"{chars / 1e6:.1f} M characters).  Clause headings located: {cstats['clauses_located']} of "
              f"{cstats['clauses_known']}.  Report: {out / 'dryrun_report.json'}", quiet)
        return 0

    store = Store(out, cfg, overwrite=a.overwrite, quiet=quiet)
    emb = embedder or make_embedder(cfg, a)
    if embedder is None:
        emb.check()
    _note(f"store   : {store.rows} rows from {len(store.done)} documents already embedded", quiet)

    pending: deque[Job] = deque()
    batch: list[tuple[str, str]] = []
    waiters: dict[str, list] = defaultdict(list)
    recent: dict[str, np.ndarray] = {}                 # resolved, not yet committed
    seen_ids: set[str] = set()
    changed: list[str] = []
    consecutive_fail = [0]
    norm = cfg["normalize"]

    def finish(vec):
        if norm:
            nv = float(np.linalg.norm(vec))
            vec = vec / nv if nv > 0 else vec
        return vec.astype(np.float32)

    def commit_ready():
        while pending and pending[0].remaining == 0:
            job = pending.popleft()
            for k in job.keys:
                recent.pop(k, None)
            if job.failed:
                store.fail(job, job.error)
                stats["docs_failed"] += 1
            else:
                store.commit(job)
                stats["docs_embedded"] += 1
                stats["chunks_written"] += len(job.metas)

    def flush():
        if not batch:
            return
        keys = [k for k, _ in batch]
        texts = [t for _, t in batch]
        batch.clear()
        res = embed_robust(emb, texts, stats)
        for k, r in zip(keys, res):
            ws = waiters.pop(k, [])
            if isinstance(r, Exception):
                consecutive_fail[0] += 1
                stats["inputs_failed"] += 1
                for job, _i in ws:
                    job.failed, job.error = True, str(r)[:300]
                    job.remaining -= 1
            else:
                consecutive_fail[0] = 0
                v = finish(r)
                if store.npy.dim and v.shape[0] != store.npy.dim:
                    raise FatalEmbedError(f"backend returned {v.shape[0]}-dim vectors; this store holds {store.npy.dim}")
                recent[k] = v
                for job, i in ws:
                    job.vecs[i] = v
                    job.remaining -= 1
            if consecutive_fail[0] >= 25:
                raise FatalEmbedError(f"the backend rejected 25 inputs in a row; last error: {r}")
        commit_ready()

    last_note = time.time()
    rc = 0
    try:
        for item, err in src:
            if item is None:
                stats["unreadable_lines"] += 1
                _note(f"  skipped unreadable line: {err}", quiet)
                continue
            stats["docs_seen"] += 1
            dm = doc_meta(item.rec)
            if not dm["doc_id"]:
                stats["docs_without_id"] += 1
                continue
            if not _selected(dm, series_f, specs_f):
                stats["filtered"] += 1
                continue
            if dm["doc_id"] in seen_ids:
                stats["duplicate_doc_id_in_dataset"] += 1
                continue
            seen_ids.add(dm["doc_id"])
            fp = _sha(item.raw.decode("utf-8", "replace") + "\x00" + (item.text or ""))[:32]
            if dm["doc_id"] in store.done:
                if store.done[dm["doc_id"]] == fp:
                    stats["already_embedded"] += 1
                else:
                    stats["changed_since_embedded"] += 1
                    changed.append(dm["doc_id"])
                continue
            if a.limit and stats["docs_queued"] >= a.limit:
                break
            stats["docs_queued"] += 1
            if item.text is None:
                stats["docs_without_text"] += 1
            job = Job(dm["doc_id"], fp, dm, item.source)
            chs = make_chunks(item.rec, item.text, cfg, cstats)
            job.vecs = [None] * len(chs)
            for i, ch in enumerate(chs):
                inp = embed_input(dm, ch, cfg)
                key = _sha(inp)[:32]
                job.keys.append(key)
                job.metas.append({"chunk_id": f"{dm['doc_id']}::{i:04d}", "row": -1, **dm, **ch,
                                  "chunk_index": i, "n_chunks": len(chs), "embed_key": key, "source": item.source})
                if key in recent:
                    job.vecs[i] = recent[key]
                    stats["reused_vectors"] += 1
                elif key in store.key_row:
                    job.vecs[i] = store.read_row(store.key_row[key])
                    stats["reused_vectors"] += 1
                elif key in waiters:
                    waiters[key].append((job, i))
                    job.remaining += 1
                    stats["reused_vectors"] += 1
                else:
                    waiters[key].append((job, i))
                    job.remaining += 1
                    batch.append((key, inp))
                    if len(batch) >= a.batch_size:
                        flush()
            pending.append(job)
            commit_ready()
            if time.time() - last_note > 30:
                el = time.time() - started
                _note(f"  {stats['docs_seen']}/{total} documents read, {stats['docs_embedded']} embedded, "
                      f"{stats['chunks_written']} chunks, {stats['embed_inputs'] / max(el, 1e-9):.1f} inputs/s", quiet)
                last_note = time.time()
        flush()
        commit_ready()
    except KeyboardInterrupt:
        _note("interrupted: finished documents are kept; run the same command again to continue", quiet)
        rc = 130
    except FatalEmbedError as e:
        print(f"STOPPED: {e}\nEverything embedded so far is kept; run the same command again to continue.", file=sys.stderr)
        rc = 3
    stats["docs_unfinished"] = len(pending)
    store.close()
    el = time.time() - started
    rep = {"tool_version": TOOL_VERSION, "finished_at": _now(), "status": "completed" if rc == 0 else "stopped",
           "dataset": src.describe(), "config": cfg, "documents_in_dataset": total, **stats,
           "chunking": dict(cstats), "store_rows": store.rows, "store_documents": len(store.done),
           "dim": store.npy.dim, "seconds": round(el, 1),
           "changed_since_embedded_examples": changed[:100]}
    (out / "embed_report.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
    _note(f"\n  embedded now : {stats['docs_embedded']} documents, {stats['chunks_written']} chunks "
          f"({stats['embed_inputs']} model inputs, {stats['reused_vectors']} identical chunks reused)\n"
          f"  already done : {stats['already_embedded']}    failed: {stats['docs_failed']}"
          + (f" (see failed.jsonl)" if stats["docs_failed"] else "") +
          f"    filtered: {stats['filtered']}\n"
          f"  store        : {store.rows} vectors x {store.npy.dim} dims, {len(store.done)} documents  ->  {out}", quiet)
    if changed:
        _note(f"  NOTE: {len(changed)} document(s) changed in the dataset since they were embedded and were left as they "
              f"are. Embed into a new --out (or --overwrite) to refresh them.", quiet)
    if rc == 0 and stats["docs_failed"]:
        rc = 4
    if rc in (0, 4) and (a.faiss or a.chroma):
        export(out, a.faiss, a.chroma, a.chroma_collection, quiet)
    return rc


# ═══════════════════════════════════════════════════════════════════════════
# export / query / validate
# ═══════════════════════════════════════════════════════════════════════════
def _iter_chunks(out: Path):
    with open(out / "chunks.jsonl", "rb") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def _load_vectors(out: Path):
    p = out / "embeddings.npy"
    if not p.exists():
        raise SystemExit(f"no embeddings.npy in {out}")
    return np.load(p, mmap_mode="r")


def export(out: Path, faiss_kind: str, chroma_dir: str, collection: str, quiet=False):
    X = _load_vectors(out)
    if faiss_kind:
        try:
            import faiss
        except ImportError:
            raise SystemExit("pip install faiss-cpu   (needed for --faiss)")
        d = X.shape[1]
        idx = faiss.IndexFlatIP(d) if faiss_kind == "flat" else faiss.IndexHNSWFlat(d, 32, faiss.METRIC_INNER_PRODUCT)
        for a in range(0, X.shape[0], 50_000):
            idx.add(np.ascontiguousarray(X[a:a + 50_000], dtype=np.float32))
        tmp = out / "faiss.index.partial"
        faiss.write_index(idx, str(tmp))
        os.replace(tmp, out / "faiss.index")
        _note(f"faiss.index written ({faiss_kind}, {idx.ntotal} vectors; row number = id in chunks.jsonl)", quiet)
    if chroma_dir:
        try:
            import chromadb
        except ImportError:
            raise SystemExit("pip install chromadb   (needed for --chroma)")
        client = chromadb.PersistentClient(path=str(Path(chroma_dir).expanduser().resolve()))
        col = client.get_or_create_collection(collection, metadata={"hnsw:space": "cosine"})
        try:
            bs = min(2000, int(client.get_max_batch_size()))
        except Exception:
            bs = 2000
        buf = []

        def put(rows):
            col.upsert(ids=[m["chunk_id"] for m in rows], documents=[m["text"] for m in rows],
                       embeddings=np.asarray(X[rows[0]["row"]:rows[-1]["row"] + 1], dtype=np.float32).tolist(),
                       metadatas=[{k: (", ".join(v) if isinstance(v, list) else v) for k, v in m.items()
                                   if k != "text" and v is not None and not (isinstance(v, list) and not v)} for m in rows])
        n = 0
        for m in _iter_chunks(out):
            buf.append(m)
            if len(buf) >= bs:
                put(buf)
                n += len(buf)
                buf = []
        if buf:
            put(buf)
            n += len(buf)
        _note(f"chroma: {n} chunks upserted into collection '{collection}' at {chroma_dir}", quiet)


def cmd_export(a) -> int:
    out = Path(a.out).expanduser().resolve()
    if not a.faiss and not a.chroma:
        raise SystemExit("nothing to do: pass --faiss flat|hnsw and/or --chroma DIR")
    export(out, a.faiss, a.chroma, a.chroma_collection)
    return 0


def search(out: Path, query: str, k=5, embedder=None, filters=None, args=None):
    man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    cfg = man["config"]
    emb = embedder or make_embedder(cfg, args)
    res = embed_robust(emb, [cfg["query_prefix"] + query], Counter())
    if isinstance(res[0], Exception):
        raise SystemExit(f"could not embed the query: {res[0]}")
    q = res[0]
    if cfg["normalize"]:
        q = q / (np.linalg.norm(q) or 1.0)
    X = _load_vectors(out)
    if q.shape[0] != X.shape[1]:
        raise SystemExit(f"query vector has {q.shape[0]} dims, store has {X.shape[1]}")
    filters = {kk: v for kk, v in (filters or {}).items() if v}
    rows = None
    if filters:
        def ok(m):
            for kk, v in filters.items():
                if kk == "spec" and m["spec"].casefold() not in {s.casefold() for s in v}:
                    return False
                if kk in ("series", "release", "kind") and str(m[kk]) not in v:
                    return False
            return True
        rows = np.array([m["row"] for m in _iter_chunks(out) if ok(m)], dtype=np.int64)
        if rows.size == 0:
            return []
    best = []                                          # (score, row)
    if rows is None:
        for a0 in range(0, X.shape[0], 100_000):
            s = np.asarray(X[a0:a0 + 100_000]) @ q
            top = np.argpartition(-s, min(k, s.size) - 1)[:k]
            best += [(float(s[i]), a0 + int(i)) for i in top]
    else:
        for a0 in range(0, rows.size, 100_000):
            r = rows[a0:a0 + 100_000]
            s = np.asarray(X[r]) @ q
            top = np.argpartition(-s, min(k, s.size) - 1)[:k]
            best += [(float(s[i]), int(r[i])) for i in top]
    best = sorted(best, reverse=True)[:k]
    want = {r: sc for sc, r in best}
    found = {}
    for m in _iter_chunks(out):
        if m["row"] in want:
            found[m["row"]] = m
            if len(found) == len(want):
                break
    return [{"score": round(want[r], 4), **found[r]} for _, r in best if r in found]


def cmd_query(a) -> int:
    out = Path(a.out).expanduser().resolve()
    split = lambda s: [x.strip() for x in s.split(",") if x.strip()] if s else []
    hits = search(out, a.text, a.k, filters={"spec": split(a.spec), "series": split(a.series),
                                              "release": [norm_release(x) for x in split(a.release)],
                                              "kind": split(a.kind)}, args=a)
    if a.json:
        print(json.dumps(hits, indent=2, ensure_ascii=False))
        return 0
    if not hits:
        print("no results")
    for i, h in enumerate(hits, 1):
        where = f"clause {h['clause']} {h['clause_title']}".strip() if h["clause"] else h["kind"]
        print(f"{i}. [{h['score']:.3f}]  {h['document_type']} {h['spec']} V{h['version']} (Rel-{h['release']})  {where}")
        snippet = re.sub(r"\s+", " ", h["text"])[:a.snippet]
        print(f"     {snippet}{'...' if len(h['text']) > a.snippet else ''}")
        print(f"     {h['doc_id']}\n")
    return 0


def validate_store(out: Path, dataset: str = "", text_dir: str = "", quiet=True) -> dict:
    errors, warns = [], []
    err = lambda c, d: errors.append({"code": c, "detail": d})
    warn = lambda c, d: warns.append({"code": c, "detail": d})
    stats = Counter()
    man = {}
    try:
        man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    except Exception as e:
        err("manifest_unreadable", str(e))
    rows = dim = None
    try:
        rows, dim = NpyStore.read_shape(out / "embeddings.npy")
        size = (out / "embeddings.npy").stat().st_size
        if size != NPY_PREAMBLE + rows * dim * 4:
            err("npy_size", f"file has {size} bytes, header says {rows} x {dim}")
    except FileNotFoundError:
        if man.get("rows"):
            err("npy_missing", str(out / "embeddings.npy"))
        else:
            rows, dim = 0, None
            warn("empty_store", "no vectors committed yet")
    except ValueError as e:
        err("npy_header", str(e))
    doc_rows, ids, expect = 0, set(), 0
    try:
        with open(out / "docs.jsonl", "rb") as f:
            for n, line in enumerate(f, 1):
                d = json.loads(line)
                if d["row_start"] != expect:
                    err("docs_gap", f"line {n}: row_start {d['row_start']} expected {expect}")
                if len(d["keys"]) != d["row_end"] - d["row_start"]:
                    err("docs_keys", f"line {n}: {d['doc_id']}")
                if d["doc_id"] in ids:
                    err("duplicate_doc", d["doc_id"])
                ids.add(d["doc_id"])
                expect = d["row_end"]
        doc_rows = expect
        stats["documents"] = len(ids)
    except FileNotFoundError:
        err("docs_missing", "docs.jsonl")
    except (ValueError, KeyError) as e:
        err("docs_invalid", str(e))
    cids, n_chunks = set(), 0
    try:
        for i, m in enumerate(_iter_chunks(out)):
            n_chunks += 1
            if m.get("row") != i:
                err("chunk_row", f"line {i + 1} has row {m.get('row')}")
                break
            if m["chunk_id"] in cids:
                err("duplicate_chunk_id", m["chunk_id"])
            cids.add(m["chunk_id"])
            if not m.get("text", "").strip():
                err("empty_chunk", m["chunk_id"])
    except FileNotFoundError:
        err("chunks_missing", "chunks.jsonl")
    except ValueError as e:
        err("chunks_invalid", str(e))
    stats["chunks"] = n_chunks
    if rows is not None and not (rows == n_chunks == doc_rows):
        err("row_mismatch", f"embeddings.npy {rows}, chunks.jsonl {n_chunks}, docs.jsonl {doc_rows}")
    if rows and dim and not errors:
        X = np.load(out / "embeddings.npy", mmap_mode="r")
        bad = 0
        lo, hi = 9.0, 0.0
        for a0 in range(0, rows, 100_000):
            blk = np.asarray(X[a0:a0 + 100_000], dtype=np.float32)
            fin = np.isfinite(blk).all(axis=1)
            bad += int((~fin).sum())
            nr = np.linalg.norm(blk[fin], axis=1)
            if nr.size:
                lo, hi = min(lo, float(nr.min())), max(hi, float(nr.max()))
        stats["vectors"], stats["dim"] = rows, dim
        if bad:
            err("non_finite_vectors", f"{bad} rows")
        if man.get("config", {}).get("normalize") and rows and (lo < 0.999 or hi > 1.001):
            err("not_normalised", f"vector norms range {lo:.4f} .. {hi:.4f}")
        if man.get("dim") not in (None, dim):
            err("manifest_dim", f"manifest {man.get('dim')} vs npy {dim}")
    if dataset:
        src = Source(dataset, text_dir)
        missing, total = [], 0
        for item, _e in src:
            if item is None:
                continue
            total += 1
            did = doc_meta(item.rec)["doc_id"]
            if did and did not in ids:
                missing.append(did)
        stats["dataset_documents"] = total
        stats["dataset_documents_not_embedded"] = len(missing)
        if missing:
            warn("not_embedded", f"{len(missing)} dataset documents are not in the store (filtered, failed, or "
                                 f"not run yet), e.g. {missing[:3]}")
    res = {"out": str(out), "checked_at": _now(), "ok": not errors, "stats": dict(stats),
           "errors": errors[:50], "warnings": warns}
    try:
        (out / "validation_report.json").write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return res


def cmd_validate(a) -> int:
    res = validate_store(Path(a.out).expanduser().resolve(), a.dataset, a.text_dir)
    print(json.dumps(res["stats"]))
    for e in res["errors"][:20]:
        print(f"  ERROR {e['code']}: {e['detail']}")
    for w in res["warnings"]:
        print(f"  warning {w['code']}: {w['detail']}")
    print("VALID" if res["ok"] else "INVALID")
    return 0 if res["ok"] else 2


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════
def _backend_args(p, query=False):
    p.add_argument("--ollama-url", default=os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434"))
    p.add_argument("--api-url", default=os.environ.get("EMBED_API_URL", ""),
                   help="--backend openai: base URL ending in /v1 (key from $EMBED_API_KEY)")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--device", default="", help="sentence-transformers: cuda / cpu")
    if not query:
        p.add_argument("--keep-alive", default="30m")
        p.add_argument("--num-ctx", type=int, default=0, help="Ollama context size for the embedding model (0 = model default)")


def make_parser():
    ap = argparse.ArgumentParser(prog="embed_cparas.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    e = sub.add_parser("embed", help="chunk and embed the dataset (resumable)")
    e.add_argument("--dataset", "-d", required=True,
                   help="builder --out folder, extractor --output folder, etsi_dataset.zip, or etsi_dataset.jsonl")
    e.add_argument("--text-dir", default="", help="text folder for a .jsonl dataset (default: text/ next to it)")
    e.add_argument("--out", "-o", required=True, help="vector store folder (separate from the dataset)")
    e.add_argument("--backend", choices=["ollama", "sentence-transformers", "openai", "hash"], default="ollama")
    e.add_argument("--model", "-m", default=os.environ.get("EMBED_MODEL", ""),
                   help="default nomic-embed-text (Ollama). e.g. bge-m3, qwen3-embedding:0.6b, BAAI/bge-small-en-v1.5")
    e.add_argument("--doc-prefix", default=None, help="override the model's document prefix (auto by model name)")
    e.add_argument("--query-prefix", default=None, help="override the model's query prefix (auto by model name)")
    e.add_argument("--batch-size", type=int, default=32)
    e.add_argument("--max-chars", type=int, default=1500, help="chunk size in characters (~350 tokens)")
    e.add_argument("--overlap", type=int, default=150)
    e.add_argument("--min-chars", type=int, default=200, help="sections shorter than this are merged with the next")
    e.add_argument("--no-summary", action="store_true", help="skip the per-version overview chunk (LLM-written fields)")
    e.add_argument("--no-context-header", action="store_true", help="embed chunk text without the spec/clause line")
    e.add_argument("--no-normalize", action="store_true")
    e.add_argument("--series", default="", help="e.g. 36,38")
    e.add_argument("--specs", default="", help="e.g. 38.331,55.241")
    e.add_argument("--limit", type=int, default=0, help="embed at most N new documents (trial)")
    e.add_argument("--dry-run", action="store_true", help="chunk only, report counts; no model needed")
    e.add_argument("--overwrite", action="store_true", help="delete an existing store in --out and start again")
    e.add_argument("--faiss", choices=["flat", "hnsw"], default="", help="also write faiss.index (pip install faiss-cpu)")
    e.add_argument("--chroma", default="", help="also upsert into a Chroma DB at this folder (pip install chromadb)")
    e.add_argument("--chroma-collection", default="cparas_3gpp")
    e.add_argument("--hash-dim", type=int, default=256, help=argparse.SUPPRESS)
    e.add_argument("--quiet", action="store_true")
    _backend_args(e)
    e.set_defaults(func=cmd_embed)
    q = sub.add_parser("query", help="semantic search in a store")
    q.add_argument("text")
    q.add_argument("--out", "-o", required=True)
    q.add_argument("-k", type=int, default=5)
    q.add_argument("--spec", default="", help="only these specs, e.g. 38.331")
    q.add_argument("--series", default="")
    q.add_argument("--release", default="", help="e.g. 15,16 or Rel-17")
    q.add_argument("--kind", default="", help="text or summary")
    q.add_argument("--snippet", type=int, default=300)
    q.add_argument("--json", action="store_true")
    _backend_args(q, query=True)
    q.set_defaults(func=cmd_query)
    v = sub.add_parser("validate", help="check a store")
    v.add_argument("--out", "-o", required=True)
    v.add_argument("--dataset", "-d", default="", help="also report dataset documents missing from the store")
    v.add_argument("--text-dir", default="")
    v.set_defaults(func=cmd_validate)
    x = sub.add_parser("export", help="build faiss.index / a Chroma collection from a store")
    x.add_argument("--out", "-o", required=True)
    x.add_argument("--faiss", choices=["flat", "hnsw"], default="")
    x.add_argument("--chroma", default="")
    x.add_argument("--chroma-collection", default="cparas_3gpp")
    x.set_defaults(func=cmd_export)
    return ap


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in ("embed", "query", "validate", "export", "-h", "--help"):
        argv.insert(0, "embed")
    ap = make_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 1
    try:
        return args.func(args)
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
