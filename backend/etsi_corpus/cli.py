"""
CLI entry point for the ETSI dataset import pipeline (v0.3-continuation,
dataset-first architecture).

Run from backend/, with the venv active:

    python -m etsi_corpus.cli import --jsonl path/to/etsi_dataset.jsonl --text-dir path/to/text --limit 25
    python -m etsi_corpus.cli export --output data/etsi_corpus/output
    python -m etsi_corpus.cli stats

This module never downloads a PDF or calls an LLM to extract structured
fields -- that all happens in the standalone `etsi-scraper-pipeline`
project, which produces the JSONL + text/ this command imports.

`import` requires either --limit N or the explicit --all-rows flag -- there
is no silent unbounded default, given a real dataset can have 50,000+
records.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from core.config import settings
from core.database import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("etsi_corpus.cli")


async def _cmd_import(args: argparse.Namespace) -> None:
    """`import --jsonl`: import + index a prepared dataset (limited to ETSI_DEFAULT_IMPORT_LIMIT unless
    --all).
    """
    from etsi_corpus.pipeline import import_dataset, job_status

    await init_db()
    limit = None if args.all_rows else (args.limit or settings.ETSI_DEFAULT_IMPORT_LIMIT)
    jsonl_path = Path(args.jsonl)
    if not jsonl_path.exists():
        raise SystemExit(f"Dataset JSONL not found: {jsonl_path}")
    text_dir = Path(args.text_dir) if args.text_dir else jsonl_path.parent / "text"
    if not text_dir.exists():
        logger.warning(f"Text directory not found at {text_dir} -- records without a matching text file will fail import.")
        text_dir = None
    logger.info(f"Starting dataset import: jsonl={jsonl_path} text_dir={text_dir} limit={limit or 'ALL (unbounded)'}")
    job_id = await import_dataset(jsonl_path=jsonl_path, text_dir=text_dir, limit=limit, job_id=args.resume_job)
    status = await job_status(job_id)
    logger.info(f"Import job {job_id} finished: {status}")


async def _cmd_status_local(_: argparse.Namespace) -> None:
    """`status-local`: what the configured local dataset folder contains and what has been imported."""
    import json
    from etsi_corpus import dataset_local
    await init_db()
    res = await dataset_local.dataset_status()
    print(json.dumps(res, indent=2, default=str))
    if not res.get("ok"):
        raise SystemExit(2)


async def _cmd_validate_local(args: argparse.Namespace) -> None:
    """`validate-local`: full streaming validation of the local dataset (optionally CRC and history
    checks); non-zero exit on problems.
    """
    import json
    from etsi_corpus import dataset_local
    res = await dataset_local.validate_dataset(crc=args.crc, check_history=not args.no_history)
    print(json.dumps(res, indent=2, default=str))
    if not res.get("ok"):
        raise SystemExit(2)


async def _cmd_import_local(args: argparse.Namespace) -> None:
    """`import-local`: import from CPARAS_DATASET_ROOT in place (resumable with --resume-job)."""
    from etsi_corpus import dataset_local
    from etsi_corpus.pipeline import job_status, read_import_report
    await init_db()
    limit = None if args.all_rows else (args.limit or settings.ETSI_DEFAULT_IMPORT_LIMIT)
    job_id = args.resume_job or await dataset_local.create_local_job(limit)
    logger.info(f"Local dataset import job {job_id}: limit={limit or 'ALL'} versions={args.versions}")
    await dataset_local.run_local_import(
        job_id, limit=limit, versions=args.versions, series=[x for x in args.series.split(",") if x.strip()],
        specs=[x for x in args.specs.split(",") if x.strip()], copy_text=args.copy_text, only_failed=args.only_failed)
    final = await job_status(job_id)
    logger.info(f"Import job {job_id} finished: {final}")
    rep = read_import_report(job_id)
    code = _print_import_summary(job_id, final or {}, rep)
    if code:
        raise SystemExit(code)


def _print_import_summary(job_id: int, final: dict, rep: dict | None) -> int:
    """Plain-language outcome. Returns the process exit code: 0 ONLY when every selected record is
    indexed and nothing was stopped early; 3 otherwise (so scripts and setup.ps1 cannot mistake a
    partial import for a finished one)."""
    from etsi_corpus.pipeline import failures_path
    print("")
    print("=" * 72)
    if not rep:
        print(f"Import job {job_id}: no report was written (status: {final.get('status')}).")
        return 3
    c = rep["counts"]
    failed = c["import_failed"] + c["index_failed"] + c["errors"]
    indexed = c["completed"] + c["already_completed"]
    print(f"Import job {job_id}: {rep['state'].upper().replace('_', ' ')}")
    print(f"  selected records     : {c['selected']}")
    print(f"  indexed              : {indexed}  (newly {c['completed']}, already done {c['already_completed']})")
    print(f"  NOT indexed          : {c['unindexed']}  (failed {failed}, not attempted {c['not_attempted']})")
    print(f"  chunks in this run   : {c['chunks_indexed']}")
    if c["large_documents"] or c["split_chunks"] or c["embedding_batch_splits"] or c["retried_documents"]:
        print(f"  memory safeguards    : large docs {c['large_documents']}, oversized chunks split {c['split_chunks']} "
              f"(into {c['parts_created']} parts), embedding batches halved {c['embedding_batch_splits']}, docs retried {c['retried_documents']}")
    if c["truncated_texts"]:
        print(f"  WARNING truncated    : {c['truncated_texts']} text(s) were cut by the embedding model")
    if c["zero_chunk_documents"]:
        print(f"  WARNING zero chunks  : {c['zero_chunk_documents']} document(s) completed with nothing searchable")
    for w in rep.get("warnings") or []:
        print(f"  ! {w}")
    if failed:
        print("  Failures by type     : " + ", ".join(f"{k}={v}" for k, v in sorted(rep.get("failures_by_error_type", {}).items())))
        for f in (rep.get("failures") or [])[:10]:
            print(f"    - {f['specification']} v{f['version']} (dataset id {f['dataset_document_id']}, row {f['etsi_document_id']}): "
                  f"[{f['error_type']}] {f['error_message'][:140]}")
        if failed > 10:
            print(f"    ... and {failed - 10} more")
        print(f"  Full list            : {rep.get('failures_file') or failures_path(job_id)}")
        print("  To retry only those  : re-run the same command (completed documents are skipped), or add --only-failed")
    print(f"  Report               : {Path(settings.ETSI_DATASET_OUTPUT) / 'reports' / f'import_job_{job_id}.json'}")
    print("=" * 72)
    if rep["state"] in ("completed", "completed_with_warnings") and rep.get("all_selected_indexed"):
        return 0
    return 3


async def _cmd_verify_index(args: argparse.Namespace) -> None:
    """`verify-index`: check that imported documents really have vectors (optionally re-index the
    missing ones).
    """
    import json
    from etsi_corpus.verify import verify_index
    await init_db()
    res = await verify_index(limit=args.limit, fix=args.fix, query=args.query)
    print(json.dumps(res, indent=2, default=str))
    if not res["ok"]:
        print("\nVERIFICATION FAILED: " + (f"{res['documents_not_indexed']} document(s) are not indexed. " if res["documents_not_indexed"] else "")
              + (f"{res['chunk_count_mismatches']} completed document(s) have a chunk-count mismatch. " if res["chunk_count_mismatches"] else "")
              + "Re-run the import command to finish them.")
        raise SystemExit(3)
    if not res["checked_all_completed"]:
        print("\nNote: only a subset of completed documents was checked (--limit).")


async def _cmd_failures(args: argparse.Namespace) -> None:
    """`failures`: list documents that failed to import/index."""
    import json
    from etsi_corpus.pipeline import failed_documents
    await init_db()
    rows = await failed_documents(args.job)
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
    else:
        for r in rows:
            print(f"{r['id']:>7}  {r['failure_stage']:<14} {r['standard_name'] or r['pdf_url']}: {(r['error_message'] or '')[:160]}")
        print(f"{len(rows)} document(s) not indexed")
    if rows:
        raise SystemExit(3)


async def _cmd_recover(_: argparse.Namespace) -> None:
    """`recover`: reset documents left mid-processing by a crash so they are picked up again."""
    from etsi_corpus.pipeline import recover_interrupted_documents
    await init_db()
    print(f"recovered: {await recover_interrupted_documents()}")


async def _cmd_export(args: argparse.Namespace) -> None:
    """`export`: write the enriched dataset (XLSX + JSONL) to a folder."""
    from etsi_corpus.export import build_enriched_export

    await init_db()
    xlsx_bytes, jsonl_bytes, n = await build_enriched_export(status=args.status)
    out_dir = Path(args.output or settings.ETSI_DATASET_OUTPUT)
    out_dir.mkdir(parents=True, exist_ok=True)
    xlsx_path = out_dir / "ETSI_Series_Enriched_v0.3.xlsx"
    jsonl_path = out_dir / "ETSI_Series_Enriched_v0.3.jsonl"
    xlsx_path.write_bytes(xlsx_bytes)
    jsonl_path.write_bytes(jsonl_bytes)
    logger.info(f"Exported {n} document record(s) -> {xlsx_path}, {jsonl_path}")


async def _cmd_stats(_: argparse.Namespace) -> None:
    """`stats`: corpus counters."""
    from etsi_corpus.export import corpus_stats

    await init_db()
    stats = await corpus_stats()
    for k, v in stats.items():
        print(f"{k}: {v}")


async def _cmd_package_build(args: argparse.Namespace) -> None:
    """`package build`: snapshot the imported corpus into a portable package."""
    from etsi_corpus.package import build_package

    await init_db()
    manifest = await build_package()
    print(manifest.to_json())
    logger.info(f"Package built at {settings.ETSI_DATASET_DIR}")


async def _cmd_package_status(_: argparse.Namespace) -> None:
    """`package status`: manifest of the installed package."""
    from etsi_corpus.package import load_package_manifest, package_dir

    manifest = load_package_manifest()
    if manifest is None:
        print(f"No Standards Corpus package installed at {package_dir()}")
        return
    print(manifest.to_json())


async def _cmd_package_install(args: argparse.Namespace) -> None:
    """`package install --from`: install a package built on another machine."""
    from etsi_corpus.package import install_package

    manifest = await install_package(args.from_dir)
    print(manifest.to_json())


def main() -> None:
    """Command-line entry point: `python -m etsi_corpus.cli <command>` (see --help for the full list)."""
    parser = argparse.ArgumentParser(prog="python -m etsi_corpus.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="Import an already-prepared ETSI dataset (JSONL + text/) and index it")
    p_import.add_argument("--jsonl", required=True, help="Path to etsi_dataset.jsonl (produced by etsi-scraper-pipeline)")
    p_import.add_argument("--text-dir", default=None, help="Path to the text/ directory (default: alongside the JSONL)")
    p_import.add_argument("--limit", type=int, default=None, help="Max number of records to import")
    p_import.add_argument("--all-rows", action="store_true", help="Import the ENTIRE dataset (no limit) -- explicit opt-in only")
    p_import.add_argument("--resume-job", type=int, default=None, help="Resume an existing EtsiIngestionJob by id")
    p_import.set_defaults(func=_cmd_import)

    # ── v0.4.4: canonical dataset (CPARAS_DATASET_ROOT) ─────────────────────────────
    p_ls = sub.add_parser("status-local", help="Show the configured canonical dataset (CPARAS_DATASET_ROOT) and what is imported")
    p_ls.set_defaults(func=_cmd_status_local)

    p_lv = sub.add_parser("validate-local", help="Stream-validate the configured dataset (schema, text pairing, history checksums)")
    p_lv.add_argument("--crc", action="store_true", help="Also CRC-test every ZIP member (slow on multi-GB archives)")
    p_lv.add_argument("--no-history", action="store_true", help="Skip the spec_history sidecar checksum pass")
    p_lv.set_defaults(func=_cmd_validate_local)

    p_li = sub.add_parser("import-local", help="Import + index the configured canonical dataset (reads etsi_dataset.zip in place)")
    p_li.add_argument("--limit", type=int, default=None, help="Max records this run (default ETSI_DEFAULT_IMPORT_LIMIT)")
    p_li.add_argument("--all-rows", action="store_true", help="Import EVERY selected record - explicit opt-in")
    p_li.add_argument("--versions", choices=["all", "latest", "latest-per-release"], default="all",
                      help="all (default) | latest | latest-per-release; unknown versions are always kept")
    p_li.add_argument("--series", default="", help="Comma-separated, e.g. 36,38")
    p_li.add_argument("--specs", default="", help="Comma-separated, e.g. 38.331,38.211")
    p_li.add_argument("--copy-text", action="store_true", help="Copy text into the app's own store instead of reading the ZIP in place")
    p_li.add_argument("--resume-job", type=int, default=None)
    p_li.add_argument("--only-failed", action="store_true",
                      help="Retry only documents currently in a failed state (completed ones are skipped either way)")
    p_li.set_defaults(func=_cmd_import_local)

    p_vi = sub.add_parser("verify-index", help="Compare the database with the vector store: every completed document must have its chunks")
    p_vi.add_argument("--limit", type=int, default=None, help="Check only the first N completed documents")
    p_vi.add_argument("--fix", action="store_true", help="Mark mismatched documents index_failed so the next import re-indexes them")
    p_vi.add_argument("--query", default=None, help="Also run one real search and show the top hits")
    p_vi.set_defaults(func=_cmd_verify_index)

    p_fl = sub.add_parser("failures", help="List documents that are not indexed, with the reason")
    p_fl.add_argument("--job", type=int, default=None)
    p_fl.add_argument("--json", action="store_true")
    p_fl.set_defaults(func=_cmd_failures)

    p_lr = sub.add_parser("recover", help="Reset documents left half-processed by a crash so the next import retries them")
    p_lr.set_defaults(func=_cmd_recover)

    p_export = sub.add_parser("export", help="Build ETSI_Series_Enriched_v0.3.xlsx + .jsonl from what's imported so far")
    p_export.add_argument("--output", default=None)
    p_export.add_argument("--status", default=None, help="Only export documents with this status (default: all)")
    p_export.set_defaults(func=_cmd_export)

    p_stats = sub.add_parser("stats", help="Print corpus import stats")
    p_stats.set_defaults(func=_cmd_stats)

    p_package = sub.add_parser("package", help="Build/inspect/install the versioned, portable Standards Corpus package")
    package_sub = p_package.add_subparsers(dest="package_command", required=True)

    p_pkg_build = package_sub.add_parser("build", help="Build ETSI_DATASET_DIR from what's imported so far (one-time step)")
    p_pkg_build.set_defaults(func=_cmd_package_build)

    p_pkg_status = package_sub.add_parser("status", help="Show the installed package's manifest")
    p_pkg_status.set_defaults(func=_cmd_package_status)

    p_pkg_install = package_sub.add_parser("install", help="Install a package built on another machine")
    p_pkg_install.add_argument("--from-dir", required=True, help="Path to a package directory (containing package_manifest.json)")
    p_pkg_install.set_defaults(func=_cmd_package_install)

    args = parser.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
