"""
Exact phrase matching between a patent and its matched standard clause
=======================================================================
v0.4.9 (CPARAS ISLD). The clause/version recommendation in research/engine.py
comes from embedding similarity, which says *how close* a patent is to a
clause but not *what text* the two share. This module answers the second
question: which exact word sequences appear in BOTH the patent's own text
(title, LLM-extracted descriptions, claim scope, use cases ...) and the
matched clause's text (the retrieved ETSI/3GPP excerpt, or the illustrative
corpus entry).

Pure stdlib, deterministic, no embedding and no LLM. Matching is
case-insensitive and ignores punctuation, but is otherwise exact: words have
to appear in the same order, contiguously, in both texts. Phrases are
reported in the wording the STANDARD uses (its original casing), because that
is what a reviewer will search for in the specification.

It never changes a score -- it is evidence shown next to the clause.
"""

from __future__ import annotations

import re
from typing import Optional

# Words that carry no technical meaning on their own. A phrase must contain
# at least one word outside this list, and leading/trailing stopwords are
# trimmed off ("of the handover command" -> "handover command").
STOPWORDS = frozenset("""
a an and are as at be been being by can could do does for from has have having he her his i if in into is it
its may might more most must no nor not of on one or our shall should so such than that the their them then
there these they this those to two upon us was we were what when where whether which while who whom will with
within without would also any each other some via per used using based further first second e g i e etc
said wherein thereof thereby therein herein according least
""".split())

# Field labels that build_patent_text()-style blobs add; never real content.
_LABEL_WORDS = frozenset({"declared", "standards", "standard", "technical", "domains", "protocol", "components"})

_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-/]*[A-Za-z0-9]|[A-Za-z0-9]")

MAX_PHRASE_WORDS = 12
DEFAULT_MAX_PHRASES = 10
DEFAULT_MAX_TERMS = 12


def _words_with_spans(text: str) -> list[tuple[str, int, int]]:
    """(lower-cased word, start, end) for every word in `text`, so matches can be reported in the
    original wording.
    """
    return [(m.group(0).lower(), m.start(), m.end()) for m in _WORD_RE.finditer(text or "")]


def _is_content(word: str) -> bool:
    """True for a word that can carry meaning in a phrase (not a stopword, contains a letter)."""
    if word in STOPWORDS:
        return False
    # bare numbers / version strings ("38", "16.2.0", "5") are not phrases
    return any(ch.isalpha() for ch in word)


def _trim(seq: list[str]) -> tuple[int, int]:
    """(start, end) after stripping leading/trailing stopwords/non-content words."""
    s, e = 0, len(seq)
    while s < e and not _is_content(seq[s]):
        s += 1
    while e > s and not _is_content(seq[e - 1]):
        e -= 1
    return s, e


def find_matched_phrases(
    patent_text: Optional[str],
    clause_text: Optional[str],
    *,
    min_words: int = 2,
    max_phrases: int = DEFAULT_MAX_PHRASES,
    max_terms: int = DEFAULT_MAX_TERMS,
) -> dict:
    """
    Returns {"phrases": [...], "terms": [...]}

      phrases -- maximal multi-word sequences (>= `min_words` words after
                 trimming stopwords) present verbatim in both texts, longest
                 first, as written in the clause text. A phrase contained in
                 a longer reported phrase is not repeated.
      terms   -- single technical words shared by both texts that are not
                 already part of a reported phrase (e.g. "handover", "PDCCH").
    """
    p_words = [w for w, _, _ in _words_with_spans(patent_text or "")]
    c_spans = _words_with_spans(clause_text or "")
    c_words = [w for w, _, _ in c_spans]
    if not p_words or not c_words:
        return {"phrases": [], "terms": []}

    # Index every clause n-gram (n = 1..MAX) -> first position, for exact lookup.
    first_pos: dict[tuple, int] = {}
    for i in range(len(c_words)):
        for n in range(1, min(MAX_PHRASE_WORDS, len(c_words) - i) + 1):
            first_pos.setdefault(tuple(c_words[i:i + n]), i)

    # Greedy longest match from every patent position.
    found: dict[str, tuple[int, int]] = {}   # normalised phrase -> (n_words, clause position)
    i = 0
    while i < len(p_words):
        best = 0
        for n in range(min(MAX_PHRASE_WORDS, len(p_words) - i), 0, -1):
            if tuple(p_words[i:i + n]) in first_pos:
                best = n
                break
        if best == 0:
            i += 1
            continue
        seq = p_words[i:i + best]
        s, e = _trim(seq)
        core = seq[s:e]
        if core and not all(w in _LABEL_WORDS for w in core):
            key = " ".join(core)
            if key not in found:
                found[key] = (len(core), first_pos[tuple(core)])
        i += max(1, best if best >= min_words else 1)

    def surface(key: str) -> str:
        """The phrase exactly as written in the clause text (original casing and punctuation)."""
        n, pos = found[key]
        return (clause_text or "")[c_spans[pos][1]:c_spans[pos + n - 1][2]]

    multi = sorted((k for k, (n, _) in found.items() if n >= min_words), key=lambda k: (-found[k][0], found[k][1]))
    phrases: list[str] = []
    kept_keys: list[str] = []
    for k in multi:
        if any(f" {k} " in f" {longer} " for longer in kept_keys):
            continue
        kept_keys.append(k)
        phrases.append(surface(k))
        if len(phrases) >= max_phrases:
            break

    in_phrase = {w for k in kept_keys for w in k.split()}
    patent_set = set(p_words)
    terms: list[str] = []
    seen: set[str] = set()
    for w, a, b in c_spans:   # clause order, clause casing
        if w in seen or w in in_phrase or w in _LABEL_WORDS or not _is_content(w) or len(w) < 3:
            continue
        if w in patent_set:
            seen.add(w)
            terms.append((clause_text or "")[a:b])
            if len(terms) >= max_terms:
                break
    return {"phrases": phrases, "terms": terms}
