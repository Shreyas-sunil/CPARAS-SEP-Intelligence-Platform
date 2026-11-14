"""
Regression tests for the Windows import failures (v0.4.4 -> v0.4.4.1):

  * "memory allocation errors, attempted allocations of ~1.17 GB and 8.59 GB" on ETSI documents 2, 13, 26
  * every document failing at the vector store on the CLI path ('NoneType' object has no attribute 'upsert')
  * failures that were invisible in the report, half-indexed documents, duplicates after a retry

Nothing here needs the real bge-m3 weights.  tests/embedding_fakes.FakeModel simulates the attention-score
allocation (batch x 16 heads x tokens^2 x 4 bytes); one test also runs real PyTorch (tiny model, memory
capped with RLIMIT_AS) to show that the formula and the failure are real, and one runs a real
SentenceTransformer built from random weights to check the bounded encoder against the real encode().
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from embedding_fakes import FakeModel, words, VOCAB_WORDS  # noqa: E402

pytest.importorskip("transformers")
pytest.importorskip("tokenizers")
pytest.importorskip("chromadb")

BACKEND = Path(__file__).resolve().parent.parent


# ──────────────────────────────────────────────────────────────────────────────
# harness: fresh settings / DB / Chroma per test, a fake model in the shared embedder
# ──────────────────────────────────────────────────────────────────────────────
class Env:
    pass


_RELOAD = ("core.config", "core.database", "rag.vector_store", "utils.text_chunker")


def _purge():
    for m in list(sys.modules):
        if m in _RELOAD or m.startswith("etsi_corpus"):
            del sys.modules[m]


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in {
        "DATABASE_URL": f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}",
        "ETSI_DATASET_OUTPUT": str(tmp_path / "out"), "CHROMADB_PATH": str(tmp_path / "chroma"),
        "VECTOR_BACKEND": "chromadb", "ETSI_IMPORT_BATCH_SIZE": "10",
        "ETSI_MIN_FREE_RAM_MB": "0",             # the host's RAM must not influence a unit test
    }.items():
        monkeypatch.setenv(k, v)
    _purge()
    e = Env()
    e.tmp, e.monkeypatch = tmp_path, monkeypatch
    e.config = importlib.import_module("core.config")
    e.db = importlib.import_module("core.database")
    e.vs = importlib.import_module("rag.vector_store")
    e.eb = importlib.import_module("rag.embedding_budget")
    e.indexer = importlib.import_module("etsi_corpus.indexer")
    e.pipeline = importlib.import_module("etsi_corpus.pipeline")
    e.settings = e.config.settings
    asyncio.run(e.db.init_db())

    def install_model(model):
        e.vs.LocalEmbedder._load_error = None
        e.vs._embedder._model = model
        e.vs._embedder._load_lock = None
        return model
    e.install_model = install_model

    def collection():
        store = e.vs.get_vector_store()
        asyncio.run(store.ensure_ready())
        return store._collection
    e.collection = collection
    yield e
    _purge()


# ──────────────────────────────────────────────────────────────────────────────
# 1. the arithmetic behind the observed numbers
# ──────────────────────────────────────────────────────────────────────────────
def test_observed_allocation_sizes_match_the_attention_tensor_formula(env):
    eb = env.eb
    # The two shapes below are exactly 8,589,934,592 bytes: the "8.59 GB" in the Windows log.
    assert eb.attention_scores_bytes(batch=32, seq_len=2048, heads=16) == 8_589_934_592
    assert eb.attention_scores_bytes(batch=2, seq_len=8192, heads=16) == 8_589_934_592
    # one sequence at bge-m3's full 8192-token window is 4 GiB by itself
    assert eb.attention_scores_bytes(batch=1, seq_len=8192, heads=16) == 4 * 2**30
    # ... and quadratic: halving the length quarters it
    assert eb.attention_scores_bytes(32, 1024) * 4 == eb.attention_scores_bytes(32, 2048)


def test_planner_keeps_every_batch_inside_the_budget_and_refuses_the_impossible(env):
    eb = env.eb
    budget = 1536 * 2**20
    lengths = [1024] * 5 + [512] * 40 + [30] * 200 + [1024]
    batches = eb.plan_batches(lengths, max_batch=32, budget_bytes=budget)
    assert sorted(i for b in batches for i in b) == list(range(len(lengths)))          # every text, exactly once
    for b in batches:
        padded = max(lengths[i] for i in b)
        assert eb.estimate_peak_bytes(len(b), padded) <= budget
        assert len(b) <= 32
    # longest texts are not mixed into big batches: the 1024-token ones end up in small batches
    assert max(len(b) for b in batches if max(lengths[i] for i in b) == 1024) <= 8
    with pytest.raises(eb.SequenceTooLarge):
        eb.plan_batches([8192], max_batch=8, budget_bytes=budget)         # 8192 tokens alone needs far more than 1.5 GB


def test_memory_error_classifier_recognises_torch_numpy_and_windows_messages(env):
    eb = env.eb
    from embedding_fakes import torch_alloc_error
    assert eb.is_memory_error(torch_alloc_error(8_589_934_592))
    assert eb.is_memory_error(MemoryError())
    assert eb.is_memory_error(RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB"))
    assert eb.is_memory_error(RuntimeError("DefaultCPUAllocator: not enough memory: you tried to allocate 1170000000 bytes"))
    assert eb.is_memory_error(Exception("The paging file is too small for this operation to complete"))
    assert not eb.is_memory_error(ValueError("bad metadata"))
    assert not eb.is_memory_error(KeyError("x"))


# ──────────────────────────────────────────────────────────────────────────────
# 2. real PyTorch: the failure is reproduced, then avoided, under a memory cap
# ──────────────────────────────────────────────────────────────────────────────
_TORCH_SCRIPT = textwrap.dedent(r"""
    import resource, sys, json
    sys.path.insert(0, sys.argv[1])
    resource.setrlimit(resource.RLIMIT_AS, (5 * 2**30, 5 * 2**30))      # a 5 GiB machine
    import torch
    from transformers import XLMRobertaConfig, XLMRobertaModel
    from rag import embedding_budget as eb
    cfg = XLMRobertaConfig(vocab_size=1000, hidden_size=64, num_hidden_layers=1, num_attention_heads=16,
                           intermediate_size=128, max_position_embeddings=8194)
    m = XLMRobertaModel(cfg, add_pooling_layer=False)
    m.config._attn_implementation = "eager"       # materialises [batch, heads, seq, seq] like bge-m3's eager path
    m.eval()

    def run(B, L):
        ids = torch.randint(5, 900, (B, L))
        with torch.no_grad():
            m(input_ids=ids, attention_mask=torch.ones_like(ids))

    res = {}
    run(2, 128); res["control_ok"] = True
    try:
        run(32, 2048); res["unbounded_failed"] = False
    except RuntimeError as e:
        res["unbounded_failed"] = True; res["unbounded_msg"] = str(e)
    # the planned alternative: 32 texts of 2048 tokens, truncated to the indexer's 1024 limit, planned batches
    lengths = [1024] * 32
    for b in eb.plan_batches(lengths, max_batch=8, budget_bytes=1536 * 2**20):
        run(len(b), 1024)
    res["planned_ok"] = True
    print(json.dumps(res))
""")


@pytest.mark.skipif(sys.platform != "linux", reason="uses RLIMIT_AS (POSIX address-space limit)")
def test_real_torch_reproduces_the_8_59_gb_allocation_and_the_planned_batches_avoid_it():
    pytest.importorskip("torch")
    proc = subprocess.run([sys.executable, "-I", "-c", _TORCH_SCRIPT, str(BACKEND)], capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not proc.stdout.strip():
        pytest.skip(f"could not run real torch under a 5 GiB cap here: {proc.stderr[-300:]}")
    res = json.loads(proc.stdout.strip().splitlines()[-1])
    assert res["unbounded_failed"], "32 x 2048 tokens x 16 heads should not fit in 5 GiB"
    assert "8589934592" in res["unbounded_msg"], res["unbounded_msg"]       # the exact byte count from the Windows log
    assert res["planned_ok"]


# ──────────────────────────────────────────────────────────────────────────────
# 3. chunker
# ──────────────────────────────────────────────────────────────────────────────
def _v044_semantic_chunk(text, chunk_size=512):
    """Verbatim copy of the v0.4.4 Platform chunker, as the reference for 'ordinary text is unchanged'."""
    import re
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, current = [], ""
    for para in paragraphs:
        if len(current) + len(para) < chunk_size * 4:
            current += "\n\n" + para if current else para
        else:
            if current:
                chunks.append(current.strip())
            current = para
    if current:
        chunks.append(current.strip())
    final = []
    for chunk in chunks:
        if len(chunk) <= chunk_size * 5:
            final.append(chunk)
        else:
            sentences = re.split(r"(?<=[.!?])\s+", chunk)
            sub = ""
            for sent in sentences:
                if len(sub) + len(sent) < chunk_size * 4:
                    sub += " " + sent
                else:
                    if sub:
                        final.append(sub.strip())
                    sub = sent
            if sub:
                final.append(sub.strip())
    return [c for c in final if len(c) > 50]


def test_chunker_caps_unbroken_text_that_v044_emitted_whole(env):
    chunker = importlib.import_module("utils.text_chunker")
    hard = env.settings.RAG_CHUNK_SIZE * 5
    asn1 = "value-" + "ENUMERATED{a(0),b(1),c(2),d(3)}," * 3000            # no blank line, no ". " anywhere
    toc = "5.1.2 General " + "." * 40000 + " 123"                          # dotted table-of-contents line
    for text in (asn1, toc, "x" * 50_000):
        old = _v044_semantic_chunk(text)
        assert max(len(c) for c in old) > hard * 10, "precondition: the OLD chunker emitted one enormous chunk"
        new = chunker.semantic_chunk(text)
        assert max(len(c) for c in new) <= hard
    # nothing is lost: all non-space characters survive the split, in order
    new = chunker.semantic_chunk(asn1)
    assert "".join("".join(c.split()) for c in new) == "".join(asn1.split())


def test_chunker_output_is_identical_to_v044_for_ordinary_text(env):
    chunker = importlib.import_module("utils.text_chunker")
    para = "The UE shall transmit the message on the uplink. The network shall acknowledge reception. "
    samples = [
        "\n\n".join(para * (i % 7 + 1) for i in range(60)),
        " ".join(f"Sentence number {i} describes a procedure." for i in range(400)),
        "Short text that is under fifty chars.",
        "A\n\n" + para * 40,
    ]
    for t in samples:
        assert chunker.semantic_chunk(t) == _v044_semantic_chunk(t)


# ──────────────────────────────────────────────────────────────────────────────
# 4. the embedder: bounded batches, OOM handling, ordering, serialization
# ──────────────────────────────────────────────────────────────────────────────
def test_embedder_returns_vectors_in_input_order_whatever_the_batching(env):
    model = env.install_model(FakeModel())
    texts = [words(n, start=n) for n in (3, 400, 7, 900, 5, 60, 2, 250, 11, 120)]
    got = asyncio.run(env.vs._embedder.embed(texts))
    expect = model.encode(texts, batch_size=1)
    assert len(got) == len(texts)
    for g, e in zip(got, expect):
        assert pytest.approx(list(e), abs=1e-6) == g


def test_embedder_halves_the_batch_on_out_of_memory_instead_of_failing(env):
    """A batch of >4 'does not fit'; the embedder must split it and still return every vector."""
    limit = 4 * 16 * 100 * 100 * 4                          # fits 4 sequences of 100 tokens, not 5
    model = env.install_model(FakeModel(alloc_limit_bytes=limit))
    env.settings.EMBEDDING_CPU_BATCH_SIZE = 16
    env.settings.EMBEDDING_MEMORY_BUDGET_MB = 4096
    texts = [words(100, start=i) for i in range(16)]
    got = asyncio.run(env.vs._embedder.embed(texts))
    assert len(got) == 16 and all(len(v) == 16 for v in got)
    st = env.vs._embedder.last_stats
    assert st.oom_retries >= 1 and st.min_batch_used <= 4
    assert model.peak_alloc <= limit or any(c["alloc_bytes"] <= limit for c in model.encode_calls)


def test_embedder_reports_a_single_unembeddable_text_as_a_memory_error_and_changes_nothing(env):
    env.install_model(FakeModel(alloc_limit_bytes=1000))
    with pytest.raises(env.vs.EmbeddingMemoryError) as ei:
        asyncio.run(env.vs._embedder.embed([words(300)]))
    assert ei.value.tokens == 300


def test_default_embed_keeps_the_models_own_limit_for_searches_and_restores_it(env):
    model = env.install_model(FakeModel(max_seq_length=8192))
    asyncio.run(env.vs._embedder.embed([words(5000)]))                     # a search/bulk-scoring style call
    assert model.encode_calls[0]["padded_tokens"] == 5000                  # NOT cut to the indexer's limit
    with env.vs.embedding_limits(256):
        asyncio.run(env.vs._embedder.embed([words(5000)]))
    assert model.encode_calls[-1]["padded_tokens"] == 256                  # indexing path is capped ...
    assert model.max_seq_length == 8192                                    # ... and the model's limit is restored
    assert env.vs._embedder.last_stats.truncated == 1                      # and the cut is reported, not hidden


def test_concurrent_embed_calls_do_not_stack_their_memory(env):
    """v0.4.4 ran ETSI_IMPORT_BATCH_SIZE (10) encode() calls at once, each with its own activations."""
    model = env.install_model(FakeModel(delay=0.05))

    async def go():
        await asyncio.gather(*[env.vs._embedder.embed([words(50, start=i)]) for i in range(8)])
    asyncio.run(go())
    assert model.max_concurrent == 1


def test_fit_to_limit_splits_by_tokens_with_overlap_and_loses_no_word(env):
    env.install_model(FakeModel())
    text = words(2500)
    parts = asyncio.run(env.vs._embedder.fit_to_limit([text], 1024))[0]
    assert len(parts) >= 3
    toks = text.split()
    covered = set()
    for ptext, a, b in parts:
        assert text[a:b] == ptext
        assert len(ptext.split()) <= 1024 - 2
        covered.update(range(len(text[:a].split()), len(text[:a].split()) + len(ptext.split())))
    assert covered == set(range(len(toks)))                                # every word is in at least one part
    # a text that fits is returned whole, untouched
    assert asyncio.run(env.vs._embedder.fit_to_limit([words(40)], 1024))[0] == [(words(40), 0, len(words(40)))]


# ──────────────────────────────────────────────────────────────────────────────
# 5. the indexer
# ──────────────────────────────────────────────────────────────────────────────
META = {"standard_name": "TS 38.331", "specification_number": "38.331", "version": "17.0.0", "release": "Rel-17",
        "series": "38", "technology_domain": "5G NR", "topic": "RRC", "source_url": "https://x/y.pdf",
        "dataset_document_id": "ds-doc-7"}


def _ids(env, doc_id):
    return asyncio.run(env.vs.get_vector_store().list_chunk_ids_for_document(doc_id))


def test_cli_path_initialises_the_vector_store_itself(env):
    """v0.4.4: get_vector_store() returned an UNINITIALISED store on the CLI path, so every upsert died with
    'NoneType' object has no attribute 'upsert'."""
    env.install_model(FakeModel())
    assert env.vs.get_vector_store()._collection is None                   # exactly the CLI situation
    out = asyncio.run(env.indexer.index_document_detailed(1, "A procedure is described here. " * 20, META, None))
    assert out.ok, out.error_message
    assert out.chunks_indexed >= 1 and len(_ids(env, 1)) == out.chunks_indexed


def test_ordinary_chunks_keep_their_v044_ids_content_and_metadata(env):
    env.install_model(FakeModel())
    clauses = [{"clause_number": "5.3.1", "clause_title": "RRC setup", "summary": "The UE shall initiate the procedure.", "page": 12}]
    text = "\n\n".join(f"Paragraph {i}. " + "The UE shall do something specific. " * 30 for i in range(6))
    out = asyncio.run(env.indexer.index_document_detailed(5, text, META, clauses))
    assert out.ok and out.chunks_split == 0 and out.parts_created == 0
    got = env.collection().get(where={"etsi_document_id": 5}, include=["documents", "metadatas"])
    byid = {i: (d, m) for i, d, m in zip(got["ids"], got["documents"], got["metadatas"])}
    assert "etsi_5_clause_0" in byid and "etsi_5_text_1" in byid
    doc, meta = byid["etsi_5_clause_0"]
    assert doc == "5.3.1 RRC setup\nThe UE shall initiate the procedure."
    assert meta["clause"] == "5.3.1" and meta["clause_title"] == "RRC setup" and meta["page"] == 12
    assert meta["specification_number"] == "38.331" and meta["version"] == "17.0.0" and meta["source_type"] == "etsi_standard"
    assert meta["dataset_document_id"] == "ds-doc-7"                       # new: traceable to the dataset record
    assert "part_index" not in meta
    assert sorted(i for i in byid if "_text_" in i) == sorted(
        f"etsi_5_text_{i}" for i in range(1, 1 + sum(1 for i in byid if "_text_" in i)))


def test_oversized_clause_is_split_into_traceable_parts_without_losing_text(env):
    env.install_model(FakeModel())
    big = words(5000)
    clauses = [{"clause_number": "6.1", "clause_title": "Big table", "summary": big, "page": 3}]
    out = asyncio.run(env.indexer.index_document_detailed(9, "short text " * 10, META, clauses))
    assert out.ok and out.chunks_split == 1 and out.parts_created >= 5 and out.truncated_texts == 0
    got = env.collection().get(where={"$and": [{"etsi_document_id": 9}, {"clause": "6.1"}]}, include=["documents", "metadatas"])
    parts = sorted(zip(got["ids"], got["documents"], got["metadatas"]), key=lambda x: x[2]["part_index"])
    assert len(parts) == out.parts_created
    for k, (pid, doc, meta) in enumerate(parts):
        assert pid == f"etsi_9_clause_0_p{k}"
        assert meta["parent_chunk_id"] == "etsi_9_clause_0" and meta["part_index"] == k and meta["part_count"] == len(parts)
        assert meta["clause"] == "6.1" and meta["clause_title"] == "Big table" and meta["page"] == 3
        assert meta["specification_number"] == "38.331" and meta["dataset_document_id"] == "ds-doc-7"
        assert meta["parent_char_end"] > meta["parent_char_start"]
    parent = "6.1 Big table\n" + big
    for _id, doc, meta in parts:
        assert parent[meta["parent_char_start"]:meta["parent_char_end"]] == doc          # offsets point back into the parent
    seen = set()
    for _id, doc, _m in parts:
        seen.update(doc.split())
    assert set(big.split()) <= seen                                                      # no word dropped


def test_the_windows_failure_scenario_old_path_dies_new_path_indexes_everything(env):
    """Documents 2, 13 and 26 carry an enormous unbroken clause.  The v0.4.4 path embedded all of a document's
    chunks in one encode() at the model's 8192 limit; the simulated machine can allocate 512 MB for attention."""
    limit = 512 * 2**20
    model = env.install_model(FakeModel(alloc_limit_bytes=limit))

    # ---- OLD behaviour, reproduced: one encode() over a document's chunks, no length cap, batch 32
    chunker = importlib.import_module("utils.text_chunker")
    ordinary = chunker.semantic_chunk("\n\n".join(["ordinary paragraph about radio. " * 40] * 40))
    old_batch = ([("6.1 Big table\n" + words(2045))] + (ordinary * 4))[:32]      # one 2,048-token clause chunk (3 heading + 2,045 words) + 31 ordinary
    assert len(old_batch) == 32
    with pytest.raises(RuntimeError, match="you tried to allocate 8589934592 bytes"):
        model.encode(old_batch, batch_size=32)                             # the observed crash: 32 x 16 x 2048^2 x 4
    assert model.encode_calls[-1]["alloc_bytes"] == 8_589_934_592

    # ---- NEW behaviour: the same content goes through the indexer
    model.encode_calls.clear()
    model.peak_alloc = 0
    for doc_id in (2, 13, 26):
        clauses = [{"clause_number": "6.1", "clause_title": "Big table", "summary": words(2048), "page": 3}]
        text = "\n\n".join(["ordinary paragraph about radio. " * 40] * 40)
        out = asyncio.run(env.indexer.index_document_detailed(doc_id, text, META, clauses))
        assert out.ok, (doc_id, out.error_type, out.error_message)
        assert out.chunks_split == 1 and out.truncated_texts == 0
        assert len(_ids(env, doc_id)) == out.chunks_indexed > 40
    assert model.peak_alloc <= limit, f"largest simulated allocation {model.peak_alloc} exceeds the machine's {limit}"
    assert max(c["padded_tokens"] for c in model.encode_calls) <= env.settings.EMBEDDING_MAX_SEQ_LENGTH


def test_documents_are_embedded_and_written_in_bounded_groups(env):
    model = env.install_model(FakeModel())
    env.settings.ETSI_INDEX_GROUP_CHUNKS = 16
    store = env.vs.get_vector_store()
    asyncio.run(store.ensure_ready())
    sizes = []
    real_upsert = store.upsert

    async def spy(chunks):
        sizes.append(len(chunks))
        return await real_upsert(chunks)
    store.upsert = spy
    text = "\n\n".join(f"Paragraph {i} " + "text about a mechanism. " * 90 for i in range(300))
    out = asyncio.run(env.indexer.index_document_detailed(3, text, META, None))
    assert out.ok and out.chunks_indexed > 200
    assert max(sizes) <= 16 and len(sizes) >= out.chunks_indexed // 16        # never all chunks at once
    assert max(c["batch"] for c in model.encode_calls) <= env.settings.EMBEDDING_CPU_BATCH_SIZE


def test_out_of_memory_rolls_back_this_attempts_chunks_and_keeps_the_previous_version(env):
    env.install_model(FakeModel())
    text_v1 = "\n\n".join(f"Version one paragraph {i}. " + "alpha beta gamma delta. " * 50 for i in range(20))
    assert asyncio.run(env.indexer.index_document_detailed(4, text_v1, META, None)).ok
    before = _ids(env, 4)
    assert before

    # now a re-index whose embedding runs out of memory half-way (a poisoned paragraph, however small the batch)
    text_v2 = text_v1.replace("Version one", "Version two") + "\n\n" + "POISON " * 400
    env.install_model(FakeModel(oom_if=lambda ts: any("POISON" in t for t in ts)))
    env.settings.ETSI_INDEX_DOC_RETRIES = 1
    out = asyncio.run(env.indexer.index_document_detailed(4, text_v2, META, None))
    assert not out.ok and out.error_type == "out_of_memory" and out.retryable
    assert out.attempts == 2                                               # retried once with smaller groups
    assert out.rolled_back >= 0
    after = _ids(env, 4)
    # nothing NEW was left behind: every id present now existed before
    assert after <= before
    # the failure is not a success
    assert out.vector_id is None and out.chunks_indexed == 0


def test_a_failed_first_index_leaves_no_chunks_at_all(env):
    env.install_model(FakeModel(oom_if=lambda ts: any("POISON" in t for t in ts)))
    text = "\n\n".join(f"Paragraph {i}. " + "alpha beta gamma delta. " * 50 for i in range(30)) + "\n\n" + "POISON " * 500
    out = asyncio.run(env.indexer.index_document_detailed(11, text, META, None))
    assert not out.ok and out.error_type == "out_of_memory"
    assert _ids(env, 11) == set()                                          # no half-indexed document


def test_retry_is_idempotent_and_never_duplicates(env):
    env.install_model(FakeModel())
    text = "\n\n".join(f"Paragraph {i}. " + "alpha beta gamma delta. " * 50 for i in range(25))
    a = asyncio.run(env.indexer.index_document_detailed(6, text, META, None))
    first = _ids(env, 6)
    b = asyncio.run(env.indexer.index_document_detailed(6, text, META, None))             # same document again
    assert a.ok and b.ok and _ids(env, 6) == first and b.chunks_indexed == a.chunks_indexed
    assert env.collection().count() == len(first)
    # a shorter version of the same document: stale chunks are removed only after the new ones are in
    c = asyncio.run(env.indexer.index_document_detailed(6, text[: len(text) // 2], META, None))
    assert c.ok and c.stale_removed > 0 and _ids(env, 6) < first
    assert env.collection().count() == c.chunks_indexed


def test_low_free_memory_pauses_the_document_with_a_retryable_error_and_writes_nothing(env):
    env.install_model(FakeModel())
    env.settings.ETSI_MIN_FREE_RAM_MB = 4096
    env.settings.ETSI_INDEX_DOC_RETRIES = 0
    env.monkeypatch.setattr(env.eb, "free_ram_mb", lambda: 120.0)
    env.monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    out = asyncio.run(env.indexer.index_document_detailed(8, "alpha beta gamma delta. " * 100, META, None))
    assert not out.ok and out.error_type == "low_memory" and out.retryable
    assert _ids(env, 8) == set()


async def _instant_sleep(_s):
    return None


def test_a_document_above_the_hard_ceiling_is_reported_not_skipped_silently(env):
    env.install_model(FakeModel())
    env.settings.ETSI_MAX_DOC_CHARS = 1000
    out = asyncio.run(env.indexer.index_document_detailed(12, "alpha beta gamma delta. " * 200, META, None))
    assert not out.ok and out.error_type == "document_too_large" and not out.retryable
    assert "ETSI_MAX_DOC_CHARS" in out.error_message
    env.settings.ETSI_MAX_DOC_CHARS = 0
    assert asyncio.run(env.indexer.index_document_detailed(12, "alpha beta gamma delta. " * 200, META, None)).ok


def test_large_documents_are_flagged_and_still_processed_in_bounded_memory(env):
    env.install_model(FakeModel())
    env.settings.ETSI_LARGE_DOC_CHARS = 20_000
    text = "\n\n".join(f"Paragraph {i}. " + "alpha beta gamma delta. " * 60 for i in range(60))
    out = asyncio.run(env.indexer.index_document_detailed(13, text, META, None))
    assert out.ok and out.large_document and out.text_chars > 20_000


# ──────────────────────────────────────────────────────────────────────────────
# 6. the real SentenceTransformer encode() (random weights) behind the bounded encoder
# ──────────────────────────────────────────────────────────────────────────────
def _tiny_sentence_transformer(path):
    pytest.importorskip("torch")
    st = pytest.importorskip("sentence_transformers")
    from transformers import XLMRobertaConfig, XLMRobertaModel
    from embedding_fakes import make_tokenizer
    tok = make_tokenizer()
    cfg = XLMRobertaConfig(vocab_size=len(tok), hidden_size=32, num_hidden_layers=2, num_attention_heads=16,
                           intermediate_size=64, max_position_embeddings=2200, pad_token_id=1)
    XLMRobertaModel(cfg).save_pretrained(path)
    tok.save_pretrained(path)
    return st.SentenceTransformer(str(path), device="cpu")


def test_bounded_encoder_matches_the_real_sentence_transformers_encode(env, tmp_path):
    model = _tiny_sentence_transformer(tmp_path / "tiny")
    model.max_seq_length = 512
    env.install_model(model)
    texts = [words(n, start=n) for n in (4, 90, 9, 300, 17, 60)]
    got = asyncio.run(env.vs._embedder.embed(texts))
    ref = model.encode(texts, batch_size=1, normalize_embeddings=True, convert_to_numpy=True)
    import numpy as np
    assert np.allclose(np.asarray(got), ref, atol=1e-4)                    # batching/planning does not change the vectors
    assert env.vs._embedder.last_stats.truncated == 0
    assert model.max_seq_length == 512                                     # restored
    # and an over-long text under the indexer's limit is reported as truncated by the real model path
    with env.vs.embedding_limits(64):
        asyncio.run(env.vs._embedder.embed([words(500)]))
    assert env.vs._embedder.last_stats.truncated == 1


# ──────────────────────────────────────────────────────────────────────────────
# 7. the whole import: the 30-document scenario, failures reported, resume, verify
# ──────────────────────────────────────────────────────────────────────────────
def _record(i, clause_words=0):
    doc_id = f"doc{i}"
    rec = {
        "schema_version": "1.0",
        "identity": {"document_id": doc_id, "pdf_url": f"https://www.etsi.org/deliver/doc_{i}.pdf", "etsi_series": "38",
                     "document_number": f"38.{300 + i}", "version": "17.0.0", "release": "Rel-17", "title": f"Spec {i}",
                     "document_type": "TS"},
        "provenance": {"raw_text_path": f"{doc_id}.txt", "extraction_method": "pymupdf"},
        "classification": {"technology_domain": "5G NR", "topics": ["RRC"]},
        "technical_summary": {"purpose": "p"},
        "clauses": [{"clause_number": "5.1", "clause_title": "General", "summary": "Equipment shall comply with the requirements.", "page": 1}],
        "patent_relevance": {}, "extraction_provenance": {"confidence": 0.9, "validation_status": "valid"},
    }
    if clause_words:
        rec["clauses"].append({"clause_number": "6.1", "clause_title": "Huge table", "summary": words(clause_words), "page": 7})
    return rec


def _dataset(tmp: Path, n: int, big_idx=(), poison_idx=()):
    text_dir = tmp / "text"
    text_dir.mkdir(parents=True, exist_ok=True)
    jsonl = tmp / "etsi_dataset.jsonl"
    with open(jsonl, "w", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps(_record(i, clause_words=2400 if i in big_idx else 0)) + "\n")
            body = "\n\n".join(f"Doc {i} paragraph {k}. " + "The UE shall perform the procedure. " * 25 for k in range(12))
            if i in poison_idx:
                body += "\n\n" + "POISON " * 300
            (text_dir / f"doc{i}.txt").write_text(body, encoding="utf-8")
    return jsonl, text_dir


def _run_import(env, jsonl, text_dir, **kw):
    job_id = asyncio.run(env.pipeline.import_dataset(jsonl_path=jsonl, text_dir=text_dir, limit=None, **kw))
    return job_id, env.pipeline.read_import_report(job_id)


def _rows(env):
    from sqlalchemy import select

    async def go():
        async with env.db.AsyncSessionLocal() as s:
            return (await s.execute(select(env.db.EtsiDocument).order_by(env.db.EtsiDocument.id))).scalars().all()
    return asyncio.run(go())


def test_full_import_of_the_30_document_scenario_with_oversized_clauses_in_docs_2_13_26(env):
    limit = 512 * 2**20
    model = env.install_model(FakeModel(alloc_limit_bytes=limit))
    jsonl, text_dir = _dataset(env.tmp, 30, big_idx=(1, 12, 25))           # DB ids 2, 13, 26
    job_id, rep = _run_import(env, jsonl, text_dir)
    assert rep["state"] == "completed", rep["warnings"]
    c = rep["counts"]
    assert c["completed"] == 30 and c["index_failed"] == 0 and c["errors"] == 0 and c["unindexed"] == 0
    assert rep["all_selected_indexed"] is True and rep["failures"] == []
    assert c["split_chunks"] == 3 and c["parts_created"] >= 6              # exactly the three oversized clauses were split
    assert c["truncated_texts"] == 0
    assert model.peak_alloc <= limit
    assert model.max_concurrent == 1                                       # 10 import workers, one encode at a time
    rows = _rows(env)
    assert [r.status for r in rows] == ["completed"] * 30
    for r in rows:
        assert len(_ids(env, r.id)) == r.chunks_indexed > 0 and r.last_error_type is None
    assert env.collection().count() == sum(r.chunks_indexed for r in rows)


def test_failed_documents_are_recorded_with_type_and_detail_and_the_run_does_not_claim_success(env):
    env.install_model(FakeModel(oom_if=lambda ts: any("POISON" in t for t in ts)))
    env.settings.ETSI_INDEX_DOC_RETRIES = 1
    jsonl, text_dir = _dataset(env.tmp, 12, poison_idx=(2, 7))
    job_id, rep = _run_import(env, jsonl, text_dir)
    c = rep["counts"]
    assert rep["state"] == "completed_with_errors" and rep["all_selected_indexed"] is False
    assert c["completed"] == 10 and c["index_failed"] == 2 and c["unindexed"] == 2
    assert rep["failures_by_error_type"] == {"out_of_memory": 2}
    f = {x["dataset_document_id"]: x for x in rep["failures"]}
    assert set(f) == {"doc2", "doc7"}
    for x in f.values():
        assert x["stage"] == "index" and x["retryable"] is True and "retried automatically" in x["retry_status"]
        assert "allocate" in x["error_message"] and x["attempts_this_run"] == 2 and x["text_chars"] > 0
    lines = [json.loads(l) for l in Path(rep["failures_file"]).read_text(encoding="utf-8").splitlines()]
    assert {l["dataset_document_id"] for l in lines} == {"doc2", "doc7"}
    bad = [r for r in _rows(env) if r.status != "completed"]
    assert len(bad) == 2 and all(r.status == "index_failed" and r.last_error_type == "out_of_memory"
                                 and r.error_message.startswith("[out_of_memory]") and r.index_attempts == 2 for r in bad)
    for r in bad:
        assert _ids(env, r.id) == set()                                    # nothing half-indexed
    # the CLI summary must NOT look like success
    cli = importlib.import_module("etsi_corpus.cli")
    status = asyncio.run(env.pipeline.job_status(job_id))
    assert cli._print_import_summary(job_id, status, rep) == 3


def test_resume_retries_only_the_failed_documents_and_duplicates_nothing(env, capsys):
    model = env.install_model(FakeModel(oom_if=lambda ts: any("POISON" in t for t in ts)))
    env.settings.ETSI_INDEX_DOC_RETRIES = 0
    jsonl, text_dir = _dataset(env.tmp, 12, poison_idx=(2, 7))
    _run_import(env, jsonl, text_dir)
    done_before = {r.id: (r.chunks_indexed, r.updated_at) for r in _rows(env) if r.status == "completed"}
    total_before = env.collection().count()

    # "fix the machine": the cause is gone (e.g. more free RAM / smaller batch)
    model.oom_if = None
    model.encode_calls.clear()
    job2, rep2 = _run_import(env, jsonl, text_dir)
    c2 = rep2["counts"]
    assert rep2["state"] == "completed" and rep2["all_selected_indexed"] is True
    assert c2["completed"] == 2 and c2["already_completed"] == 10 and c2["unindexed"] == 0
    rows = _rows(env)
    assert [r.status for r in rows] == ["completed"] * 12
    assert all(r.last_error_type is None and r.error_message is None for r in rows)
    for rid, (n, _) in done_before.items():
        assert next(r for r in rows if r.id == rid).chunks_indexed == n    # successful records untouched
    assert env.collection().count() == sum(r.chunks_indexed for r in rows) > total_before   # only added the 2 documents
    # the 10 finished documents were not even re-embedded
    texts_encoded = sum(c["batch"] for c in model.encode_calls)
    assert 0 < texts_encoded < env.collection().count() - total_before + 1
    # and the second run's CLI summary is a success
    cli = importlib.import_module("etsi_corpus.cli")
    assert cli._print_import_summary(job2, asyncio.run(env.pipeline.job_status(job2)), rep2) == 0


def test_only_failed_mode_touches_nothing_else(env):
    model = env.install_model(FakeModel(oom_if=lambda ts: any("POISON" in t for t in ts)))
    env.settings.ETSI_INDEX_DOC_RETRIES = 0
    jsonl, text_dir = _dataset(env.tmp, 10, poison_idx=(4,))
    _run_import(env, jsonl, text_dir)
    model.oom_if = None
    _job, rep = _run_import(env, jsonl, text_dir, only_failed=True)
    assert rep["retry_candidates"] == 1
    assert rep["counts"]["completed"] == 1 and rep["counts"]["already_completed"] == 0 and rep["counts"]["filtered_out"] == 9
    assert rep["state"] == "completed"


def test_crash_between_stages_is_recovered_and_retried(env):
    env.install_model(FakeModel())
    jsonl, text_dir = _dataset(env.tmp, 6)
    _run_import(env, jsonl, text_dir)
    from sqlalchemy import update

    async def break_two():
        async with env.db.AsyncSessionLocal() as s:
            await s.execute(update(env.db.EtsiDocument).where(env.db.EtsiDocument.id.in_([2, 5])).values(status="indexing", chunks_indexed=0))
            await s.commit()
    asyncio.run(break_two())
    _job, rep = _run_import(env, jsonl, text_dir)
    assert rep["counts"]["completed"] == 2 and rep["counts"]["already_completed"] == 4 and rep["state"] == "completed"
    rows = _rows(env)
    assert all(r.status == "completed" and r.chunks_indexed == len(_ids(env, r.id)) for r in rows)


def test_unavailable_embedding_model_fails_fast_before_any_document_is_touched(env):
    env.vs.LocalEmbedder._load_error = "sentence-transformers is not installed (test)"
    env.vs._embedder._model = None
    jsonl, text_dir = _dataset(env.tmp, 8)
    with pytest.raises(env.pipeline.ImportPreflightError) as ei:
        asyncio.run(env.pipeline.import_dataset(jsonl_path=jsonl, text_dir=text_dir, limit=None))
    assert "embedding model" in str(ei.value).lower()
    assert _rows(env) == []                                                # not one document was started
    reports = list((env.tmp / "out" / "reports").glob("import_job_*.json"))
    assert reports and json.loads(reports[0].read_text())["state"] == "failed"


def test_verify_index_detects_missing_chunks_and_fix_makes_the_next_import_repair_them(env):
    env.install_model(FakeModel())
    jsonl, text_dir = _dataset(env.tmp, 6)
    _run_import(env, jsonl, text_dir)
    verify = importlib.import_module("etsi_corpus.verify")
    ok = asyncio.run(verify.verify_index(query="the UE shall perform the procedure"))
    assert ok["ok"] and ok["completed_documents_checked"] == 6 and ok["chunk_count_mismatches"] == 0
    assert ok["search_check"] and ok["search_check"][0]["etsi_document_id"] is not None

    some = sorted(_ids(env, 3))[:2]
    env.collection().delete(ids=some)                                      # simulate a damaged/partly lost index
    bad = asyncio.run(verify.verify_index(fix=True))
    assert not bad["ok"] and bad["chunk_count_mismatches"] == 1 and bad["mismatch_sample"][0]["etsi_document_id"] == 3
    assert next(r for r in _rows(env) if r.id == 3).status == "index_failed"
    _job, rep = _run_import(env, jsonl, text_dir)
    assert rep["counts"]["completed"] == 1 and rep["state"] == "completed"
    assert asyncio.run(verify.verify_index())["ok"]


# ──────────────────────────────────────────────────────────────────────────────
# 8. other vector backends touched by the fix
# ──────────────────────────────────────────────────────────────────────────────
def test_qdrant_point_ids_are_stable_across_processes(env):
    here = env.vs._stable_point_id("etsi_5_clause_0")
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from rag.vector_store import _stable_point_id; "
            "print(_stable_point_id('etsi_5_clause_0'))")
    outs = {subprocess.run([sys.executable, "-I", "-c", code, str(BACKEND)], capture_output=True, text=True,
                           env={**os.environ, "PYTHONHASHSEED": seed}).stdout.strip() for seed in ("1", "2", "3")}
    assert outs == {here}                       # abs(hash(str)) % 10**9 (v0.4.4) differs per process => duplicate points on retry


def test_faiss_upsert_is_idempotent_and_delete_does_not_re_embed(env):
    pytest.importorskip("faiss")
    env.monkeypatch.setenv("FAISS_INDEX_PATH", str(env.tmp / "faiss"))
    env.settings.FAISS_INDEX_PATH = str(env.tmp / "faiss")
    env.settings.EMBEDDING_DIM = 16
    model = env.install_model(FakeModel())
    store = env.vs.FAISSVectorStore()
    asyncio.run(store.ensure_ready())
    recs = [{"id": f"etsi_1_text_{i}", "content": words(30, i), "metadata": {"etsi_document_id": 1, "source_type": "etsi_standard"}} for i in range(6)]
    recs += [{"id": "etsi_2_text_0", "content": words(30, 99), "metadata": {"etsi_document_id": 2, "source_type": "etsi_standard"}}]
    asyncio.run(store.upsert(recs))
    asyncio.run(store.upsert(recs))                                        # a retry
    assert store._index.ntotal == 7 and len(store._docs) == 7              # replaced, not appended
    embedded_so_far = sum(c["batch"] for c in model.encode_calls)
    asyncio.run(store.delete_chunk_ids({"etsi_1_text_0", "etsi_1_text_1"}))
    assert store._index.ntotal == 5 and sum(c["batch"] for c in model.encode_calls) == embedded_so_far   # no re-embedding
    assert asyncio.run(store.list_chunk_ids_for_document(2)) == {"etsi_2_text_0"}


def test_record_batches_are_flushed_by_size_not_only_by_count(env):
    """200 records x huge clauses must not pile up in RAM: a batch is flushed once its clause text reaches
    ETSI_STREAM_BATCH_MAX_MB."""
    env.install_model(FakeModel())
    env.settings.ETSI_STREAM_BATCH = 200
    env.settings.ETSI_STREAM_BATCH_MAX_MB = 1
    sizes = []
    real = env.pipeline._create_checkpoints_flag

    async def spy(rows, job_id):
        sizes.append(len(rows))
        return await real(rows, job_id)
    env.monkeypatch.setattr(env.pipeline, "_create_checkpoints_flag", spy)
    text_dir = env.tmp / "text"
    text_dir.mkdir()
    jsonl = env.tmp / "etsi_dataset.jsonl"
    with open(jsonl, "w", encoding="utf-8") as f:
        for i in range(6):
            rec = _record(i)
            rec["clauses"].append({"clause_number": "9.9", "clause_title": "Blob", "summary": "x" * 700_000, "page": 1})
            f.write(json.dumps(rec) + "\n")
            (text_dir / f"doc{i}.txt").write_text("alpha beta gamma delta. " * 100, encoding="utf-8")
    _job, rep = _run_import(env, jsonl, text_dir)
    assert rep["counts"]["selected"] == 6 and rep["counts"]["completed"] == 6
    assert sizes == [2, 2, 2]
