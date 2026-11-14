retrieval_data  --  portable 3GPP search index
==============================================
Built by tgpp-sqlite-retrieval 1.1.0 on 2026-10-11T18:01:21Z.

Files
  search_index.sqlite  The complete index (SQLite + FTS5). The only file needed at query time.
  manifest.json        What is inside, counts, source fingerprint, content types.
  schema_version.json  Index format id + version (check before use).
  build_report.json    Parsing errors, skipped/partial/incomplete records, warnings.
  tgpp_retrieval.py    Optional stdlib-only Python client (same API as documented in the project).

Use it from another application (no rebuild, no original dataset, no server):
  import sys; sys.path.insert(0, r"<this folder>")
  import tgpp_retrieval as tr
  idx = tr.open_index(r"<this folder>")
  hits = tr.search(idx, "registration procedure", {"spec_id": "23.501"}, limit=5)

Command line (from this folder):
  python tgpp_retrieval.py --index . search "registration procedure" --limit 5
  python tgpp_retrieval.py --index . stats

Copy the whole folder; never edit files inside it by hand. Rebuilding is done by the builder project.
