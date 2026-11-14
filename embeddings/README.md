# embed_cparas.py 1.0 — CPARAS 3GPP dataset → vector embeddings

> Full documentation with architecture diagrams: open **`DOCUMENTATION.html`** in any browser (works offline, prints to PDF).

Turns the output of `build_cparas_dataset.py` (or `extract_3gpp.py`) into a searchable vector store.
It reads the dataset exactly as written — `etsi_dataset.zip`, the split `etsi_dataset_partNN.zip`, or
`etsi_dataset.jsonl` + `text/` — and never writes into the dataset folder.

```
<out>/
  embeddings.npy     float32 (rows x dim), L2-normalised (cosine = dot product). np.load(p, mmap_mode="r")
  chunks.jsonl       line N = row N: chunk text + spec, version, release, series, clause, title, doc_id, pdf_url ...
  docs.jsonl         commit log: one line per embedded spec version (used to resume)
  manifest.json      model + chunking settings
  failed.jsonl       documents that could not be embedded this run (retried automatically next run)
  embed_report.json  what this run did
  faiss.index        optional (--faiss)            <chroma dir>/  optional (--chroma)
```

## 1. Setup

```powershell
pip install -r requirements.txt          # only numpy is required
ollama pull nomic-embed-text             # default model (small, fast, 768 dims)
```

Ollama is the default backend because the extractor already uses it. Other choices:

| `--backend` | `--model` examples | Notes |
|---|---|---|
| `ollama` (default) | `nomic-embed-text`, `bge-m3`, `qwen3-embedding:0.6b`, `mxbai-embed-large` | `--ollama-url`, `$OLLAMA_BASE_URL` |
| `sentence-transformers` | `BAAI/bge-small-en-v1.5`, `D:\models\bge-m3` (local folder = fully offline) | `pip install sentence-transformers`; `--device cuda` |
| `openai` | whatever the server serves | Any OpenAI-compatible `/v1/embeddings` server (vLLM, TEI, LM Studio, llama.cpp). `--api-url http://host:8000/v1`, key in `$EMBED_API_KEY` |

The query/document prefixes each model was trained with (`search_query:` / `search_document:` for nomic,
`query:` / `passage:` for e5, the retrieval instruction for bge/mxbai/qwen3) are applied automatically;
override with `--doc-prefix` / `--query-prefix`.

## 2. Run

```powershell
# 1) dry run: chunks everything, reports how many chunks / characters will be embedded. No model needed.
python embed_cparas.py embed --dataset D:\3gpp\build_v2 --out D:\3gpp\vectors --dry-run

# 2) trial on a few documents
python embed_cparas.py embed --dataset D:\3gpp\build_v2 --out D:\3gpp\vectors_test --series 55 --limit 5

# 3) full run (resumable: Ctrl+C or a crash loses nothing that was committed; run the same command again)
python embed_cparas.py embed --dataset D:\3gpp\build_v2 --out D:\3gpp\vectors

# 4) search it
python embed_cparas.py query --out D:\3gpp\vectors "UE behaviour on radio link failure during re-establishment" -k 5
python embed_cparas.py query --out D:\3gpp\vectors "A5/3 key derivation" --series 55 --release 19 --json

# 5) check it
python embed_cparas.py validate --out D:\3gpp\vectors --dataset D:\3gpp\build_v2
```

`--dataset` accepts the builder's `--out` folder, the extractor's `--output` folder (it finds
`cparas_dataset/` itself), a single `.zip`, or a `.jsonl` (with `--text-dir` if `text/` is elsewhere).

**Adding data later.** Run the same command after the dataset has grown (e.g. another series was
extracted and rebuilt): documents already in the store are skipped, new ones are appended. A document
whose content changed since it was embedded is *not* silently mixed in — it is counted as
`changed_since_embedded` in the report; embed into a new `--out` (or `--overwrite`) to refresh.

**One store = one model.** The model, prefixes and chunk settings are recorded in `manifest.json`;
running with different settings against the same `--out` is refused instead of mixing incompatible vectors.

## 3. How the text is chunked

1. **Clause-aware.** Each record's own clause tree is used to find the clause headings in the full
   extracted text (`5.3.7<TAB>RRC connection re-establishment`). A line only counts as a heading when its
   number is in the clause tree *and* its title starts like the tree's title, so table rows such as
   `5.5  128 ms` are never mistaken for clauses. Every chunk carries `clause`, `clause_title` and
   `char_start`/`char_end` into the text file. Records without a clause tree get plain windows.
2. **Sized windows.** Sections are split at paragraph → line → sentence → word boundaries into chunks of
   at most `--max-chars` (1500 ≈ 350 tokens) with `--overlap` (150). Heading-only sections
   (e.g. `5 Procedures`) are glued to the next section (`clauses_covered` lists both).
3. **Context line.** The embedded input starts with the spec and clause
   (`TS 38.331 Radio Resource Control ... / Clause 5.3.7 RRC connection re-establishment`), which
   noticeably improves retrieval of short clauses. `--no-context-header` turns it off.
   The version is deliberately *not* in that line: unchanged clause text in Rel-15/16/17 produces the
   same input, is embedded **once**, and the vector is copied to each version's row. Versions stay
   separate rows with their own metadata, so you can still filter by release.
4. **Overview chunk.** One `kind: "summary"` chunk per version from scope/purpose/domain/topics/protocols.
   Those fields were written by the LLM during extraction, so these chunks are marked
   `"llm_generated": true`; filter with `--kind text` or skip them with `--no-summary`.

## 4. Using the vectors in your own code

```python
import json, numpy as np
X = np.load(r"D:\3gpp\vectors\embeddings.npy", mmap_mode="r")          # (rows, dim), normalised
meta = [json.loads(l) for l in open(r"D:\3gpp\vectors\chunks.jsonl", encoding="utf-8")]
q = ...                                                                 # embed the query with the SAME model + query prefix
scores = X @ q                                                          # cosine similarity
for i in np.argsort(-scores)[:5]:
    m = meta[i]; print(scores[i], m["spec"], m["version"], m["clause"], m["text"][:120])
```
or programmatically through the tool: `import embed_cparas as ec; ec.search(Path(out), "query", k=5, filters={"spec": ["38.331"]})`.

**FAISS / Chroma.** `--faiss flat|hnsw` writes `faiss.index` (inner product; the id is the row number in
`chunks.jsonl`). `--chroma D:\3gpp\chroma` upserts every chunk with its metadata into collection
`cparas_3gpp` (cosine). Both also work afterwards: `python embed_cparas.py export --out ... --faiss hnsw --chroma ...`.

## 5. Scale and speed

Rough sizes: every 1 GB of extracted text is ~750 000 chunks. At 768 dims that is ~2.3 GB of vectors.
Throughput depends almost entirely on the embedding model and hardware (a GPU is many times faster than CPU),
so measure it: a `--limit 20` trial prints inputs/second and `embed_report.json` has the timing. The dry run reports the exact
chunk count and how many identical chunks the de-duplication saves, so you can estimate before starting.
For a run of many hours, raise `--batch-size` (64–128) on a GPU; set `OLLAMA_NUM_PARALLEL` if you also run
the extractor at the same time.

## 6. Options

| Option | Default | Meaning |
|---|---|---|
| `--dataset` / `--out` | – | input dataset / vector store folder (must not overlap) |
| `--backend` / `--model` | `ollama` / `nomic-embed-text` | see section 1 (`$EMBED_MODEL`) |
| `--batch-size` | 32 | inputs per request |
| `--max-chars` / `--overlap` / `--min-chars` | 1500 / 150 / 200 | chunking |
| `--series` / `--specs` / `--limit` | – | embed a subset |
| `--no-summary` / `--no-context-header` / `--no-normalize` | off | see section 3 |
| `--num-ctx` | model default | Ollama context size for the embedding model |
| `--dry-run` | – | chunk and count only |
| `--overwrite` | – | start the store again |
| `--faiss` / `--chroma` / `--chroma-collection` | – | optional index exports |

Exit codes: 0 ok, 1 bad arguments / refused, 2 validation failed, 3 stopped (backend unreachable or
rejecting everything — committed work is kept), 4 finished but some documents failed (see `failed.jsonl`),
130 interrupted.

## 7. Tests

```powershell
python -m unittest discover -s tests -v      # 32 tests, numpy only; includes a fake Ollama server
```
They cover chunk offsets and heading detection, resume, crash repair of a half-written tail,
de-duplication across versions and batch sizes, isolation of a rejected input to its own document,
refusing mixed settings, never touching the dataset, the zip / split-zip / jsonl / extractor layouts,
search with filters, and the Ollama batch + legacy-endpoint paths.

---

## Documentation

| Document | What it covers |
|----------|----------------|
| [`DOCUMENTATION.html`](DOCUMENTATION.html) / [`DOCUMENTATION.pdf`](DOCUMENTATION.pdf) | Full documentation for `embed_cparas.py` with architecture diagrams |
| [`docs/Benchmarking_Embedding_Models_Patent_Standard.pdf`](docs/Benchmarking_Embedding_Models_Patent_Standard.pdf) | Paper: *Benchmarking Sentence Embedding Models for Patent-Standard Semantic Alignment: A Comparative Study on 3GPP Telecommunication Specifications* |
