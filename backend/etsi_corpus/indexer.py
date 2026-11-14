"""
Semantic chunking + embedding + vector-store indexing for ETSI documents.

Writes into the SAME shared vector store cPARAS's Text Matching and
C-SARAS's document search already query (rag.vector_store.get_vector_store())
-- source_type="etsi_standard" distinguishes these chunks from
source_type="3gpp_spec" (the older crawler-based corpus) and
source_type="csaras_document", so existing retrieval callers can keep
filtering by source_type while new ones (etsi_corpus/../services/retrieval.py)
can span both.

v0.3 fix (C-SARAS audit item 2 -- clause-aware indexing): the structured
extraction (etsi_corpus/models.py's EtsiDocumentExtraction.clauses) already
identifies clause_number/clause_title/summary/page for a document, and one
chunk is indexed per clause, tagged with real clause/page metadata. The
full-text chunks are ALSO indexed (broader recall), tagged clause="Unknown"
-- never a guessed clause number for content whose actual clause is unknown.

v0.4.4.1 -- memory-bounded, resumable, idempotent indexing
--------------------------------------------------------
Everything below keeps the v0.4.4 chunk ids (``etsi_<id>_clause_<n>`` / ``etsi_<id>_text_<n>``),
content and metadata for ordinary chunks, so existing indexes and searches are unaffected.

* STREAMING. Chunks are produced lazily and embedded + written ``ETSI_INDEX_GROUP_CHUNKS`` at a
  time.  The previous code built every chunk of a document, embedded them all in one call and
  upserted them in one call: peak memory grew with document size.  Now only one group of vectors
  is ever in RAM.
* NO OVERSIZED SEQUENCES.  A clause chunk's content comes from the dataset (it can be an entire
  clause) and had no size limit at all.  Any chunk longer than ``EMBEDDING_MAX_SEQ_LENGTH`` tokens
  is split into overlapping, numbered PARTS (``<id>_p0``, ``<id>_p1`` ...) that carry
  ``parent_chunk_id`` / ``part_index`` / ``part_count`` / ``parent_char_start`` / ``parent_char_end``
  plus all of the parent's metadata.  Nothing is truncated or dropped.
* ALL-OR-NOTHING PER DOCUMENT.  New chunks are written first; stale chunks of the previous version
  are removed only after EVERY group succeeded.  If anything fails, chunks written by THIS attempt
  are removed again, so a failed document never leaves a half-indexed footprint.  Ids are
  deterministic, so a retry overwrites instead of duplicating.
* MEMORY ERRORS are retried (smaller groups, smaller embedding budget) and, if they persist,
  reported with their type and details - the indexer never raises into the pipeline and never
  pretends success.
"""

from __future__ import annotations

import asyncio
import contextvars
import gc
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Iterable, Iterator, Optional

from core.config import settings
from rag import embedding_budget as eb

logger = logging.getLogger(__name__)

SOURCE_TYPE = "etsi_standard"

# Full metadata schema attached to every chunk. NOTE the key is "technology_domain" (matching the
# EtsiDocument column and what pipeline.py sends) - see the v0.3-continuation fix history.
# v0.4.4.1 adds dataset_document_id (the builder's identity.document_id) so every chunk traces back
# to its dataset record, not only to this app's internal row id.
_METADATA_FIELDS = (
    "standard_name", "specification_number", "version", "release",
    "series", "technology_domain", "topic", "source_url", "dataset_document_id",
)

# Error types (stable strings - they appear in reports and in EtsiDocument.last_error_type).
ERR_OUT_OF_MEMORY = "out_of_memory"            # retryable
ERR_LOW_MEMORY = "low_memory"                  # retryable: the machine, not the document, is short of RAM
ERR_SEQUENCE_TOO_LARGE = "sequence_too_large"  # retryable after raising EMBEDDING_MEMORY_BUDGET_MB
ERR_TOO_LARGE = "document_too_large"           # not retryable until ETSI_MAX_DOC_CHARS is raised
ERR_EMBEDDING_UNAVAILABLE = "embedding_unavailable"
ERR_STORE = "vector_store_error"
ERR_NO_CONTENT = "no_indexable_content"
ERR_UNEXPECTED = "unexpected_error"

RETRYABLE_AFTER_SMALLER_BATCHES = (ERR_OUT_OF_MEMORY, ERR_LOW_MEMORY)
INFRASTRUCTURE_ERRORS = (ERR_EMBEDDING_UNAVAILABLE, ERR_STORE)   # not a property of one document


def _normalize_metadata(metadata: dict) -> dict:
    """Fills every expected metadata key with 'Unknown' rather than leaving
    it absent -- absent keys are easy to silently mis-filter on downstream;
    an explicit 'Unknown' is not."""
    out = {}
    for field_name in _METADATA_FIELDS:
        val = metadata.get(field_name)
        out[field_name] = val if val else "Unknown"
    return out


@dataclass
class IndexOutcome:
    """Result of indexing one document: success, how many chunks were planned/stored, whether it was
    split into parts, and the error type/message/retryability on failure. Recorded in the import
    report.
    """
    ok: bool
    vector_id: Optional[str] = None
    chunks_indexed: int = 0                 # vectors now stored for this document (parts counted individually)
    chunks_planned: int = 0                 # chunk records produced before part-splitting
    chunks_split: int = 0                   # records that had to be split into parts
    parts_created: int = 0
    stale_removed: int = 0
    rolled_back: int = 0
    text_chars: int = 0
    large_document: bool = False
    attempts: int = 1
    oom_retries: int = 0                    # batch splits inside the embedder
    truncated_texts: int = 0                # texts the model had to cut (should be 0)
    max_tokens_seen: int = 0
    group_size_used: int = 0
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    retryable: bool = True
    warnings: list = field(default_factory=list)
    seconds: float = 0.0

    def as_dict(self) -> dict:
        """JSON form for reports."""
        return asdict(self)


# index_document() keeps its (vector_id, n) return value for existing callers; the full outcome of the
# call is available to the caller (same task context) through pop_last_outcome().
_last_outcome: "contextvars.ContextVar[Optional[IndexOutcome]]" = contextvars.ContextVar("etsi_last_index_outcome", default=None)


def pop_last_outcome() -> Optional["IndexOutcome"]:
    """Outcome of the last index call in this context (and clear it) - lets the pipeline report details
    without changing the indexer's return type.
    """
    out = _last_outcome.get()
    _last_outcome.set(None)
    return out


class _IndexError(Exception):
    """Classified indexing failure (`error_type` is one of the ERR_* codes; `retryable` tells the
    pipeline whether to try again with smaller pieces).
    """
    def __init__(self, error_type: str, message: str, retryable: bool = True):
        """Store the error type, message and whether a retry can help."""
        super().__init__(message)
        self.error_type, self.message, self.retryable = error_type, message, retryable


def _batched(it: Iterable, n: int) -> Iterator[list]:
    """Yield lists of up to `n` items from an iterable."""
    n = max(1, int(n))
    buf: list = []
    for x in it:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


def build_chunk_records(etsi_document_id: int, text: str, base_meta: dict,
                        clauses: Optional[list[dict]] = None) -> Iterator[dict]:
    """Yield chunk records lazily: clause chunks first, then full-text chunks.  Ids, content and
    metadata are exactly those v0.4.4 produced."""
    from utils.text_chunker import semantic_chunk
    vector_id = f"etsi_{etsi_document_id}"
    idx = 0
    for clause in (clauses or []):
        summary = (clause.get("summary") or "").strip()
        clause_number = clause.get("clause_number") or "Unknown"
        clause_title = clause.get("clause_title") or "Unknown"
        page = clause.get("page")
        if clause_number == "Unknown" and not summary:
            continue  # nothing real to index for this entry
        # The clause heading is part of the indexed content (not just metadata) so a plain-text
        # similarity match against "clause 5.2" style queries works without metadata filtering.
        content = f"{clause_number} {clause_title}\n{summary}".strip() if summary else f"{clause_number} {clause_title}"
        if len(content) < 10:
            continue
        chunk_meta = {
            "source_type": SOURCE_TYPE, "etsi_document_id": etsi_document_id,
            **base_meta, "clause": clause_number, "clause_title": clause_title,
        }
        # ChromaDB metadata values must be str/int/float/bool - None is rejected outright, so the
        # key is omitted when the page is unknown.
        if isinstance(page, int):
            chunk_meta["page"] = page
        yield {"id": f"{vector_id}_clause_{idx}", "content": content, "metadata": chunk_meta}
        idx += 1

    for chunk in semantic_chunk(text):
        yield {
            "id": f"{vector_id}_text_{idx}",
            "content": chunk,
            "metadata": {
                "source_type": SOURCE_TYPE, "etsi_document_id": etsi_document_id,
                **base_meta, "clause": "Unknown", "clause_title": "Unknown",
            },
        }
        idx += 1


def _expand_parts(record: dict, parts: list[tuple[str, int, int]]) -> list[dict]:
    """One record -> itself (fits) or its numbered parts (too long).  Parts keep ALL parent metadata."""
    if len(parts) <= 1:
        return [record]
    out = []
    for k, (ptext, a, b) in enumerate(parts):
        meta = {**record["metadata"], "parent_chunk_id": record["id"], "part_index": k,
                "part_count": len(parts), "parent_char_start": a, "parent_char_end": b}
        out.append({"id": f"{record['id']}_p{k}", "content": ptext, "metadata": meta})
    return out


async def _check_memory(label: str) -> None:
    """Between groups: make sure the machine still has room.  Frees what it can and waits briefly;
    raises a retryable low_memory error rather than letting the OS kill the process."""
    min_free = int(settings.ETSI_MIN_FREE_RAM_MB or 0)
    rss_cap = int(settings.ETSI_PROCESS_RSS_LIMIT_MB or 0)
    if not min_free and not rss_cap:
        return
    for attempt in range(4):
        free, rss = eb.free_ram_mb(), eb.process_rss_mb()
        low_free = bool(min_free and free is not None and free < min_free)
        high_rss = bool(rss_cap and rss is not None and rss > rss_cap)
        if not (low_free or high_rss):
            return
        gc.collect()
        if attempt == 3:
            what = (f"free RAM {free:.0f} MB < ETSI_MIN_FREE_RAM_MB={min_free}" if low_free
                    else f"process memory {rss:.0f} MB > ETSI_PROCESS_RSS_LIMIT_MB={rss_cap}")
            raise _IndexError(ERR_LOW_MEMORY, f"{what} before {label}", retryable=True)
        await asyncio.sleep(0.5 * (attempt + 1))


def _classify(exc: BaseException) -> _IndexError:
    """Map any exception from embedding/upserting onto an _IndexError (sequence too large, out of
    memory, model unavailable, vector store error...).
    """
    from rag.vector_store import EmbeddingMemoryError, EmbeddingModelUnavailable
    if isinstance(exc, _IndexError):
        return exc
    if isinstance(exc, eb.SequenceTooLarge):
        return _IndexError(ERR_SEQUENCE_TOO_LARGE, str(exc), retryable=True)
    if isinstance(exc, EmbeddingMemoryError) or eb.is_memory_error(exc):
        return _IndexError(ERR_OUT_OF_MEMORY, f"{type(exc).__name__}: {exc}", retryable=True)
    if isinstance(exc, EmbeddingModelUnavailable):
        return _IndexError(ERR_EMBEDDING_UNAVAILABLE, str(exc), retryable=True)
    return _IndexError(ERR_UNEXPECTED, f"{type(exc).__name__}: {exc}", retryable=True)


async def _index_once(vs, etsi_document_id: int, text: str, base_meta: dict, clauses, *,
                      group_chunks: int, budget_scale: float, out: IndexOutcome) -> None:
    """One attempt.  Raises _IndexError after rolling back what THIS attempt wrote."""
    from rag.vector_store import _embedder, embedding_limits
    max_tokens = int(settings.EMBEDDING_MAX_SEQ_LENGTH)
    try:
        prior = await vs.list_chunk_ids_for_document(etsi_document_id)
    except NotImplementedError:
        prior = set()
    except Exception as e:  # noqa: BLE001
        raise _IndexError(ERR_STORE, f"could not list existing chunks: {type(e).__name__}: {e}")
    written: set[str] = set()
    attempted: set[str] = set()      # ids sent to the store, even if that call then failed
    out.chunks_planned = out.chunks_split = out.parts_created = 0
    out.oom_retries = out.truncated_texts = out.max_tokens_seen = 0
    out.group_size_used = group_chunks

    try:
        with embedding_limits(max_tokens, budget_scale):
            for rec_group in _batched(build_chunk_records(etsi_document_id, text, base_meta, clauses), group_chunks):
                await _check_memory("the next group")
                out.chunks_planned += len(rec_group)
                # Cheap pre-filter: a text with fewer characters than the token limit cannot exceed it.
                need = [i for i, r in enumerate(rec_group) if len(r["content"]) > max_tokens]
                parts_by_idx: dict[int, list] = {}
                if need:
                    fitted = await _embedder.fit_to_limit([rec_group[i]["content"] for i in need], max_tokens)
                    parts_by_idx = dict(zip(need, fitted))
                units: list[dict] = []
                for i, rec in enumerate(rec_group):
                    expanded = _expand_parts(rec, parts_by_idx[i]) if i in parts_by_idx else [rec]
                    if len(expanded) > 1:
                        out.chunks_split += 1
                        out.parts_created += len(expanded)
                    units.extend(expanded)
                for unit_group in _batched(units, group_chunks):
                    attempted.update(u["id"] for u in unit_group)
                    try:
                        await vs.upsert(unit_group)
                    except Exception as up_err:  # noqa: BLE001
                        e2 = _classify(up_err)
                        # embedding problems keep their own type; anything else came from the store itself
                        raise (e2 if e2.error_type != ERR_UNEXPECTED
                               else _IndexError(ERR_STORE, f"{type(up_err).__name__}: {up_err}")) from up_err
                    written.update(u["id"] for u in unit_group)
                    st = getattr(_embedder, "last_stats", None)
                    if st is not None:
                        out.oom_retries += st.oom_retries
                        out.truncated_texts += st.truncated
                        out.max_tokens_seen = max(out.max_tokens_seen, st.max_tokens)
                del units, rec_group
    except BaseException as e:  # noqa: BLE001 - includes cancellation: still roll back, then re-raise
        err = _classify(e) if isinstance(e, Exception) else None
        # Remove what THIS attempt added (ids that did not exist before); earlier content stays.
        new_ids = attempted - prior
        if new_ids:
            try:
                await vs.delete_chunk_ids(new_ids)
                out.rolled_back = len(new_ids)
            except Exception as re_err:  # noqa: BLE001
                logger.error(f"Rollback of {len(new_ids)} chunk(s) for ETSI document {etsi_document_id} failed: {re_err}")
                out.warnings.append(f"rollback_failed: {len(new_ids)} chunk(s) of this attempt may remain; "
                                    f"a retry overwrites them by id")
        if err is None:
            raise
        raise err from e

    # Success: only now drop chunks of the PREVIOUS version that this version no longer produces.
    stale = prior - written
    if stale:
        try:
            await vs.delete_chunk_ids(stale)
            out.stale_removed = len(stale)
        except Exception as e:  # noqa: BLE001 - new chunks are in; stale ones are harmless duplicates of an older version
            out.warnings.append(f"stale_chunks_not_removed: {len(stale)} ({type(e).__name__}: {e})")
    out.chunks_indexed = len(written)


async def index_document_detailed(
    etsi_document_id: int,
    text: str,
    metadata: dict,
    clauses: Optional[list[dict]] = None,
) -> IndexOutcome:
    """Chunk, embed and index one document.  NEVER raises (except cancellation) and never reports
    success unless every chunk is stored: the returned IndexOutcome says exactly what happened."""
    t0 = time.monotonic()
    out = IndexOutcome(ok=False, text_chars=len(text or ""))
    out.large_document = out.text_chars > int(settings.ETSI_LARGE_DOC_CHARS)
    if not (text or "").strip() and not clauses:
        out.error_type, out.error_message, out.retryable = ERR_NO_CONTENT, "no text and no clauses to index", False
        return out
    ceiling = int(settings.ETSI_MAX_DOC_CHARS or 0)
    if ceiling and out.text_chars > ceiling:
        out.error_type, out.retryable = ERR_TOO_LARGE, False
        out.error_message = (f"document has {out.text_chars:,} characters, above ETSI_MAX_DOC_CHARS={ceiling:,}; "
                             "raise the setting to index it")
        return out
    try:
        from rag.vector_store import get_vector_store
        vs = get_vector_store()
        await vs.ensure_ready()
    except Exception as e:  # noqa: BLE001
        out.error_type, out.error_message = ERR_STORE, f"vector store unavailable: {type(e).__name__}: {e}"
        out.seconds = round(time.monotonic() - t0, 2)
        return out

    base_meta = _normalize_metadata(metadata)
    group = max(1, int(settings.ETSI_INDEX_GROUP_CHUNKS))
    retries = max(0, int(settings.ETSI_INDEX_DOC_RETRIES))
    last: Optional[_IndexError] = None
    for attempt in range(retries + 1):
        out.attempts = attempt + 1
        out.error_type = out.error_message = None
        try:
            await _index_once(vs, etsi_document_id, text, base_meta, clauses,
                              group_chunks=max(1, group >> attempt), budget_scale=0.5 ** attempt, out=out)
            out.ok, out.retryable = True, False
            out.vector_id = f"etsi_{etsi_document_id}"
            if out.chunks_indexed == 0:
                out.warnings.append("no_indexable_chunks: the text produced no chunk long enough to index")
            break
        except _IndexError as e:
            last = e
            out.error_type, out.error_message, out.retryable = e.error_type, e.message, e.retryable
            if e.error_type in RETRYABLE_AFTER_SMALLER_BATCHES and attempt < retries:
                logger.warning(f"ETSI document {etsi_document_id}: {e.error_type} ({e.message[:160]}); "
                               f"retry {attempt + 1}/{retries} with smaller groups and a smaller memory budget.")
                gc.collect()
                continue
            break
    if not out.ok and last is not None:
        logger.warning(f"ETSI document {etsi_document_id} NOT indexed [{out.error_type}] after {out.attempts} attempt(s): "
                       f"{out.error_message}")
    out.seconds = round(time.monotonic() - t0, 2)
    return out


async def index_document(
    etsi_document_id: int,
    text: str,
    metadata: dict,
    clauses: Optional[list[dict]] = None,
) -> tuple[Optional[str], int]:
    """Back-compatible wrapper: (vector_id_prefix, chunks_indexed), or (None, 0) on failure.
    Prefer index_document_detailed(), which says WHY it failed."""
    res = await index_document_detailed(etsi_document_id, text, metadata, clauses)
    _last_outcome.set(res)
    return (res.vector_id, res.chunks_indexed) if res.ok else (None, 0)


async def delete_document_index(etsi_document_id: int) -> None:
    """Removes a document's chunks (e.g. before a forced re-index) so a re-ingested document never
    leaves stale duplicate chunks behind.  The importer itself no longer calls this before
    indexing: replacing in place (write new, then drop stale) cannot lose a document if the new
    write fails."""
    try:
        from rag.vector_store import get_vector_store
        vs = get_vector_store()
        await vs.ensure_ready()
        ids = await vs.list_chunk_ids_for_document(etsi_document_id)
        if ids:
            await vs.delete_chunk_ids(ids)
    except Exception as e:
        logger.warning(f"Could not clear existing index for ETSI document {etsi_document_id}: {e}")
