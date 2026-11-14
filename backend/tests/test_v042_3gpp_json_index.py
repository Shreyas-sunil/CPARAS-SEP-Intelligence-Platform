"""
v0.4.2 — index a folder of extracted 3GPP archives / JSON exports
(3gpp_<spec>_v<x.y.z>.json), latest version of each spec by default.
"""
import asyncio
import json
from pathlib import Path

from ingestion import spec_corpus as sc


class FakeVS:
    def __init__(self):
        self.chunks = []

    async def upsert(self, batch):
        self.chunks.extend(batch)


def _write(p: Path, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


def test_json_name_parsing():
    m = sc.parse_spec_json_meta(Path("x/3gpp_21.111_v13.0.0.json"), Path("x"))
    assert m.spec == "21.111" and m.version_code == "d00" and m.release_guess == "Rel-13"
    assert m.series == "21_series"
    m2 = sc.parse_spec_json_meta(Path("3gpp_03.071_v8.9.0.json"), Path("."))
    assert m2.spec == "03.071" and m2.version_code == "890"
    assert sc.parse_spec_json_meta(Path("manifest.json"), Path(".")) is None


def test_extract_nested_sections():
    data = {"spec": "21.111", "version": "13.0.0", "title": "USIM and IC card requirements",
            "sections": [
                {"clause": "1", "title": "Scope", "text": "The present document defines requirements for the USIM."},
                {"clause": "4", "title": "General", "paragraphs": ["Para one about UICC.", "Para two."],
                 "subsections": [{"number": "4.1", "heading": "Security", "content": "Authentication shall use AKA."}]},
            ]}
    paras = sc.extract_json_paragraphs(data)
    assert ("1 Scope", "The present document defines requirements for the USIM.") in paras
    assert ("4 General", "Para one about UICC.") in paras
    assert ("4.1 Security", "Authentication shall use AKA.") in paras


def test_extract_unknown_schema_falls_back_to_long_strings():
    data = {"url": "https://www.3gpp.org/ftp/x" * 3, "blob": {"whatever": "x" * 10 + " real sentence of spec text long enough to keep"}}
    paras = sc.extract_json_paragraphs(data)
    assert len(paras) == 1 and "real sentence" in paras[0][1]


def test_latest_only_and_indexing(tmp_path):
    root = tmp_path / "3gpp"
    for v in ("3.5.0", "12.0.0", "13.0.0"):
        _write(root / f"3gpp_21.111_v{v}.json",
               {"sections": [{"clause": "1", "title": "Scope", "text": f"USIM requirements version {v}. The UICC shall support the authentication procedures defined herein."}]})
    _write(root / "sub" / "3gpp_03.071_v8.9.0.json", {"content": [{"title": "LCS", "text": "Location services (LCS) functional description; stage 2 of the positioning architecture."}]})
    _write(root / "3gpp_03.071_v7.11.0.json", {"content": [{"title": "LCS", "text": "Old location services (LCS) functional description; stage 2 of the positioning architecture."}]})
    _write(root / "unrelated.json", {"a": 1})

    picked = [(p.name, k) for p, k, m in sc.iter_spec_sources(root, latest_only=True)]
    assert sorted(n for n, _ in picked) == ["3gpp_03.071_v8.9.0.json", "3gpp_21.111_v13.0.0.json"]
    assert len(list(sc.iter_spec_sources(root, latest_only=False))) == 5

    vs = FakeVS()
    stats = asyncio.run(sc.build_corpus_from_crawl(root, vector_store=vs, latest_only=True))
    assert stats.files_indexed == 2 and stats.chunks_indexed == len(vs.chunks) > 0
    assert any("version 13.0.0" in c["content"] for c in vs.chunks)
    assert not any("version 3.5.0" in c["content"] for c in vs.chunks)
    meta = vs.chunks[0]["metadata"]
    assert meta["source_type"] == "3gpp_spec_text" and meta["spec"] in ("21.111", "03.071")

    # Re-running skips what is already indexed (resumable)
    vs2 = FakeVS()
    stats2 = asyncio.run(sc.build_corpus_from_crawl(root, vector_store=vs2, latest_only=True))
    assert stats2.files_skipped_already_indexed == 2 and not vs2.chunks
