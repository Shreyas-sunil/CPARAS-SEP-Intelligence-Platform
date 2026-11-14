"""
v0.4.6 regression: CPARAS Bulk scoring must never block the event loop.

With the 3GPP retrieval index installed, the first get_bulk_corpus() call builds the clause corpus from a
multi-GB SQLite file. It used to run inline in the async scorer, so every other request (including
/api/health) hung and the UI showed "Backend offline" while a Bulk upload was scored.
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import etsi_bulk.scoring as scoring_mod  # noqa: E402
from research.bulk_corpus import _illustrative_corpus  # noqa: E402


def test_slow_corpus_build_does_not_block_other_requests(monkeypatch):
    def slow_corpus():
        time.sleep(1.5)          # stands in for reading clause text out of a 22 GB index
        return _illustrative_corpus()

    monkeypatch.setattr(scoring_mod, "get_bulk_corpus", slow_corpus)
    records = [{"title": f"RRC reconfiguration {i}", "standard_raw": "TS 38.331", "spec_number": "38.331",
                "spec_version": "v16.5.0"} for i in range(5)]

    async def main():
        worst_gap = 0.0
        done = False

        async def heartbeat():            # what /api/health needs: the loop answering every ~50 ms
            nonlocal worst_gap
            last = time.monotonic()
            while not done:
                await asyncio.sleep(0.05)
                now = time.monotonic()
                worst_gap = max(worst_gap, now - last)
                last = now

        hb = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.1)          # let the heartbeat start before scoring begins
        out = []
        async for batch, _meta in scoring_mod.score_records_batched(records):
            out.extend(batch)
        done = True
        await hb
        return worst_gap, out

    worst_gap, out = asyncio.run(main())
    assert len(out) == 5
    # a blocked loop stalls for the whole 1.5 s build; a free one answers every ~50 ms
    assert worst_gap < 1.0, f"event loop was blocked for {worst_gap:.2f} s during the Bulk corpus build"
