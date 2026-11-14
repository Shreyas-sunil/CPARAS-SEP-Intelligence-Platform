"""Semantic text chunker for RAG indexing."""
import re
from core.config import settings


def hard_split_spans(text: str, limit: int) -> list[tuple[int, int]]:
    """Character spans (start, end) that cover ``text`` in pieces of at most ``limit`` characters,
    breaking at whitespace where there is any near the limit and mid-token only when the text has no
    whitespace at all.  Whitespace trimmed from a piece's edges is excluded from its span, so
    ``text[a:b]`` is exactly the piece; no word is lost."""
    spans: list[tuple[int, int]] = []
    n = len(text)
    pos = 0
    while pos < n:
        while pos < n and text[pos].isspace():
            pos += 1
        if pos >= n:
            break
        if n - pos <= limit:
            end = n
        else:
            cut = text.rfind(" ", pos, pos + limit + 1)
            nl = text.rfind("\n", pos, pos + limit + 1)
            cut = max(cut, nl)
            end = cut if cut > pos + limit // 2 else pos + limit
        e = end
        while e > pos and text[e - 1].isspace():
            e -= 1
        if e > pos:
            spans.append((pos, e))
        pos = end
    return spans


def _hard_split(text: str, limit: int) -> list[str]:
    """Last-resort split of a piece that no paragraph or sentence boundary
    could break up, on whitespace where possible and mid-token only if the
    text contains no whitespace at all. Guarantees every returned piece is
    at most `limit` characters."""
    return [text[a:b] for a, b in hard_split_spans(text, limit)]


def semantic_chunk(text: str, chunk_size: int = None, overlap: int = None) -> list[str]:
    """
    Split text into chunks for vector indexing, preferring paragraph then
    sentence boundaries. Every returned chunk is at most `chunk_size * 5`
    characters.

    NOTE on `overlap`: accepted for call-site compatibility and currently
    NOT applied -- chunks do not overlap. Implementing it would change
    every chunk boundary (a full re-index), so it is deliberately left as
    a known gap rather than changed blind.

    v0.4.4.1: this hard ceiling was missing from the CPARAS Platform copy of
    this module (it existed only in the standalone ISLD copy). Without it a
    piece with no paragraph break and no sentence-ending punctuation - an
    ASN.1 block, a table dump, a dotted table-of-contents - was emitted
    whole, at any length, and went to the embedding model as one enormous
    sequence (see rag/embedding_budget.py for the memory arithmetic).
    """
    chunk_size = chunk_size or settings.RAG_CHUNK_SIZE
    overlap = overlap or settings.RAG_CHUNK_OVERLAP
    accumulate_limit = chunk_size * 4
    hard_limit = chunk_size * 5

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    chunks = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) < accumulate_limit:
            current += "\n\n" + para if current else para
        else:
            if current:
                chunks.append(current.strip())
            current = para

    if current:
        chunks.append(current.strip())

    final_chunks = []
    for chunk in chunks:
        if len(chunk) <= hard_limit:
            final_chunks.append(chunk)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", chunk)
        sub = ""
        for sent in sentences:
            if len(sub) + len(sent) < accumulate_limit:
                sub += " " + sent
            else:
                if sub:
                    final_chunks.append(sub.strip())
                sub = sent
        if sub:
            final_chunks.append(sub.strip())

    # Hard ceiling (see the v0.4.4.1 note above).
    capped: list[str] = []
    for chunk in final_chunks:
        if len(chunk) <= hard_limit:
            capped.append(chunk)
        else:
            capped.extend(_hard_split(chunk, hard_limit))

    return [c for c in capped if len(c) > 50]  # filter trivial chunks
