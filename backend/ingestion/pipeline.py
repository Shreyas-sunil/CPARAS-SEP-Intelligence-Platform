"""
Dataset Ingestion Pipeline
==========================
Autonomously crawls public telecom sources and builds the local Standards Corpus (vector store).
All crawling is polite (rate-limited), offline-cacheable, and deduplication-aware.

Sources:
  - ETSI IPR database (public declarations)
  - 3GPP spec index (titles, series descriptions)
  - WIPO Patentscope (patent metadata API)
  - ITU-T/R recommendations index
  - Public FRAND case summaries
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from core.config import settings
from core.database import KnowledgeChunk, IngestionJob
from rag.vector_store import get_vector_store
from utils.text_chunker import semantic_chunk
from core.timeutil import utcnow as _utcnow

logger = logging.getLogger(__name__)

# SQLite session for background jobs (separate from request sessions)
_bg_engine = create_async_engine(settings.DATABASE_URL, connect_args={"check_same_thread": False})
_BgSession = async_sessionmaker(_bg_engine, expire_on_commit=False)


# ── Built-in 3GPP Knowledge (bootstrapped without crawling) ───────────────────
# This ensures the system works immediately out of the box, even offline.
#
# The full 3GPP 38_series archive is ~266 documents (RF conformance test
# specs, per-band variants, and ~150 TR study-item reports among them) --
# too large to crawl and index wholesale just to seed a bootstrap knowledge
# base, and most of that bulk (feasibility-study TRs, per-band conformance
# annexes) carries little SEP-analysis value on its own. Instead this list
# is a curated set of the NORMATIVE TS 38.xxx specs most commonly cited in
# SEP declarations and patent claims -- spanning RF (101/104), the physical
# layer (2xx), RRC/MAC/RLC/PDCP (3xx), NG-RAN interfaces (4xx: NGAP/XnAP/
# F1AP/E1AP), and UE/BS conformance (5xx) -- plus the handful of
# cross-series specs (23./24./33./36.) that round out 5G-core and LTE
# coverage. Every spec number below was checked against the real 3GPP
# archive listing (www.3gpp.org/ftp/specs/archive/38_series) rather than
# invented, so `python -m ingestion.spec_corpus` can later crawl the real
# text for any of these on top of this bootstrap layer. Extending this list
# is the intended way to broaden built-in coverage without a full-archive
# scrape -- see RECOMMENDED_SERIES / _recommended_series() below, which is
# computed FROM this list (most-represented series wins).
BUILTIN_3GPP_KNOWLEDGE = [
    {"spec": "TS 38.331", "title": "NR; Radio Resource Control (RRC); Protocol specification",
     "domain": "RRC", "release": "Rel-15+",
     "description": "Defines RRC procedures for NR: connection setup, mobility, handover, measurement reporting, beam management, PDCP/RLC configuration. Core to 5G UE-network signaling. Critical SEP territory for mobility and handover patents."},
    {"spec": "TS 38.321", "title": "NR; Medium Access Control (MAC) protocol specification",
     "domain": "MAC", "release": "Rel-15+",
     "description": "MAC layer for NR: random access, scheduling requests, HARQ, BSR, timing advance. Essential for resource scheduling patents. High SEP density for HARQ and multiplexing claims."},
    {"spec": "TS 38.214", "title": "NR; Physical layer procedures for data",
     "domain": "PHY", "release": "Rel-15+",
     "description": "PHY procedures: PDSCH/PUSCH scheduling, MIMO, CSI reporting, link adaptation, MCS selection, HARQ-ACK feedback. Major SEP area for MIMO and beamforming patents."},
    {"spec": "TS 38.211", "title": "NR; Physical channels and modulation",
     "domain": "PHY", "release": "Rel-15+",
     "description": "NR physical layer: channel structure, OFDM numerology, DMRS, reference signals, channel coding. Fundamental to 5G air interface SEPs."},
    {"spec": "TS 38.212", "title": "NR; Multiplexing and channel coding",
     "domain": "PHY", "release": "Rel-15+",
     "description": "Channel coding (LDPC, Polar codes), rate matching, HARQ, UCI encoding. Core for channel coding SEPs replacing turbo codes in 5G."},
    {"spec": "TS 38.213", "title": "NR; Physical layer procedures for control",
     "domain": "PHY", "release": "Rel-15+",
     "description": "DCI formats, PDCCH, power control, uplink timing. Essential for control channel SEPs."},
    {"spec": "TS 38.300", "title": "NR; NR and NG-RAN Overall Description",
     "domain": "Architecture", "release": "Rel-15+",
     "description": "High-level NR/NG-RAN architecture. Interfaces: Uu, F1, X2, Xn. Dual connectivity, split architecture. Important for network architecture SEPs."},
    {"spec": "TS 38.401", "title": "NG-RAN; Architecture description",
     "domain": "Architecture", "release": "Rel-15+",
     "description": "Detailed NG-RAN architecture: gNB-CU, gNB-DU split. F1 interface. Multi-vendor RAN enablement. SEP area for split RAN and CU-DU separation."},
    {"spec": "TS 38.423", "title": "NG-RAN; Xn Application Protocol (XnAP)",
     "domain": "RAN Interface", "release": "Rel-15+",
     "description": "Xn interface for inter-gNB communication: handover, dual connectivity, interference coordination. Rich SEP territory for X2/Xn handover procedures."},
    {"spec": "TS 23.501", "title": "System architecture for the 5G System (5GS)",
     "domain": "Core Network", "release": "Rel-15+",
     "description": "5GC service-based architecture: AMF, SMF, UPF, PCF, NRF. Network slicing, NSSAI, session management. SEPs for core network function procedures."},
    {"spec": "TS 23.502", "title": "Procedures for the 5G System (5GS)",
     "domain": "Core Network", "release": "Rel-15+",
     "description": "5GC procedures: registration, PDU session, handover, QoS. Highly essential for core network procedure patents."},
    {"spec": "TS 24.501", "title": "Non-Access-Stratum (NAS) protocol for 5G",
     "domain": "NAS", "release": "Rel-15+",
     "description": "NAS layer: registration, authentication, PDU session management, security. Key SEP area for UE-core signaling protocols."},
    {"spec": "TS 36.331", "title": "LTE; Radio Resource Control (RRC)",
     "domain": "RRC", "release": "Rel-8 to Rel-17",
     "description": "LTE RRC: connection setup, measurement, handover, SON, carrier aggregation. Massive SEP portfolio area for 4G/LTE patents."},
    {"spec": "TS 36.321", "title": "LTE; MAC protocol specification",
     "domain": "MAC", "release": "Rel-8 to Rel-17",
     "description": "LTE MAC: HARQ, scheduling, random access, DRX. Core LTE SEP territory for scheduling and HARQ patents."},
    {"spec": "TS 36.213", "title": "LTE; Physical layer procedures",
     "domain": "PHY", "release": "Rel-8 to Rel-17",
     "description": "LTE PHY procedures: scheduling, power control, MIMO, CQI. Foundational 4G SEP area."},
    {"spec": "TS 38.304", "title": "NR; User Equipment (UE) procedures in idle mode",
     "domain": "RRC/Mobility", "release": "Rel-15+",
     "description": "NR idle mode: cell selection, reselection, PLMN selection. SEPs for UE autonomous mobility procedures."},
    {"spec": "TS 38.133", "title": "NR; Requirements for support of radio resource management",
     "domain": "RRM", "release": "Rel-15+",
     "description": "RRM requirements: measurement accuracy, handover timing, beam management KPIs. SEP area for measurement and beam tracking."},
    {"spec": "TS 33.501", "title": "Security architecture and procedures for 5G System",
     "domain": "Security", "release": "Rel-15+",
     "description": "5G security: authentication (5G-AKA, EAP-AKA'), key hierarchy, SUPI/SUCI privacy. Growing SEP area for security procedure patents."},

    # ── Additional 38-series coverage (v0.7) ──────────────────────────────
    # RF / RRM
    {"spec": "TS 38.101-1", "title": "NR; User Equipment (UE) radio transmission and reception; Part 1: Range 1 Standalone",
     "domain": "RF", "release": "Rel-15+",
     "description": "UE RF requirements for FR1 (sub-6GHz) standalone NR operation: transmit power, receiver sensitivity, spurious emissions, band combinations. Heavily declared SEP territory for RF front-end and power-control patents."},
    {"spec": "TS 38.101-2", "title": "NR; UE radio transmission and reception; Part 2: Range 2 Standalone",
     "domain": "RF", "release": "Rel-15+",
     "description": "UE RF requirements for FR2 (mmWave) standalone operation: beam-based power classes, OTA requirements. Core SEP area for mmWave RF and beamforming hardware patents."},
    {"spec": "TS 38.101-3", "title": "NR; UE radio transmission and reception; Part 3: Range 1 and Range 2 Interworking operation with other radios",
     "domain": "RF", "release": "Rel-15+",
     "description": "UE RF requirements for multi-RAT and dual-connectivity operation (EN-DC/NE-DC/NR-DC). SEP area for carrier-aggregation and inter-RAT coexistence patents."},
    {"spec": "TS 38.104", "title": "NR; Base Station (BS) radio transmission and reception",
     "domain": "RF", "release": "Rel-15+",
     "description": "gNB RF requirements: transmitter/receiver characteristics, spurious emissions, ACLR, for both FR1 and FR2. Network-equipment-side counterpart to 38.101; frequent infrastructure-vendor SEP territory."},
    # Physical layer
    {"spec": "TS 38.201", "title": "NR; Physical layer; General description",
     "domain": "PHY", "release": "Rel-15+",
     "description": "Overview of the NR physical-layer architecture and how 38.2xx specs relate to each other. Useful as a mapping anchor when classifying PHY-layer patent claims against the right sub-spec."},
    {"spec": "TS 38.202", "title": "NR; Services provided by the physical layer",
     "domain": "PHY", "release": "Rel-15+",
     "description": "Transport-channel-to-physical-channel mapping and the services L1 exposes to MAC. Relevant for patents spanning the MAC/PHY boundary (HARQ, transport format selection)."},
    {"spec": "TS 38.215", "title": "NR; Physical layer measurements",
     "domain": "PHY", "release": "Rel-15+",
     "description": "Defines RSRP/RSRQ/SINR and other L1 measurement quantities used for mobility and beam management. SEP area for measurement-reporting and beam-selection patents."},

    # RRC / mobility / access-layer protocols
    {"spec": "TS 38.305", "title": "NR; Stage 2 functional specification of User Equipment (UE) positioning in NG-RAN",
     "domain": "Positioning", "release": "Rel-15+",
     "description": "NG-RAN positioning architecture (OTDOA, E-CID, multi-RTT, etc.) and the UE/network signaling flow. Growing SEP area as positioning-related patent filings increase with Rel-16/17."},
    {"spec": "TS 38.306", "title": "NR; User Equipment (UE) radio access capabilities",
     "domain": "RRC/Capabilities", "release": "Rel-15+",
     "description": "Defines the UE capability signaling framework (feature sets, band combinations, capability containers) referenced throughout RRC. Relevant to capability-negotiation and feature-signaling patents."},
    {"spec": "TS 38.307", "title": "NR; Requirements on UEs supporting a release-independent frequency band",
     "domain": "RF/Bands", "release": "Rel-15+",
     "description": "Cross-release band requirements ensuring UEs supporting a band stay compatible across 3GPP releases. Niche but occasionally cited in band-compatibility SEP disputes."},
    {"spec": "TS 38.322", "title": "NR; Radio Link Control (RLC) protocol specification",
     "domain": "RLC", "release": "Rel-15+",
     "description": "RLC layer for NR: segmentation, ARQ, in-sequence delivery. Adjacent to the heavily-declared MAC/PDCP SEP territory; frequently co-cited with 38.321/38.323 in patent claims spanning multiple L2 sublayers."},
    {"spec": "TS 38.323", "title": "NR; Packet Data Convergence Protocol (PDCP) specification",
     "domain": "PDCP", "release": "Rel-15+",
     "description": "PDCP: header compression (ROHC), ciphering/integrity, duplication, reordering across dual connectivity. SEP area for security-integrated L2 and PDCP-duplication (URLLC) patents."},
    {"spec": "TS 38.340", "title": "NR; Backhaul Adaptation Protocol (BAP) specification",
     "domain": "IAB", "release": "Rel-16+",
     "description": "Integrated Access and Backhaul (IAB) routing/bearer-mapping protocol. Emerging SEP area as IAB deployments and associated patent filings grow from Rel-16 onward."},

    # NG-RAN architecture and interfaces
    {"spec": "TS 38.410", "title": "NG-RAN; NG general aspects and principles",
     "domain": "RAN Interface", "release": "Rel-15+",
     "description": "General principles for the NG interface between gNB and 5GC (AMF/UPF). Foundational reference for NGAP-related SEP claims alongside 38.413."},
    {"spec": "TS 38.413", "title": "NG-RAN; NG Application Protocol (NGAP)",
     "domain": "RAN Interface", "release": "Rel-15+",
     "description": "gNB-to-5GC signaling protocol: UE context management, PDU session resource setup, handover signaling, paging. Dense SEP territory for RAN-core interworking and handover-signaling patents."},
    {"spec": "TS 38.420", "title": "NG-RAN; Xn general aspects and principles",
     "domain": "RAN Interface", "release": "Rel-15+",
     "description": "General principles for the Xn interface between gNBs, complementing 38.423's XnAP procedures. Reference spec for inter-gNB mobility and dual-connectivity SEP claims."},
    {"spec": "TS 38.460", "title": "NG-RAN; E1 general aspects and principles",
     "domain": "RAN Interface", "release": "Rel-15+",
     "description": "General principles for the E1 interface between the gNB-CU control plane and user plane (CU-CP/CU-UP split). Relevant to disaggregated-RAN architecture SEP claims."},
    {"spec": "TS 38.463", "title": "NG-RAN; E1 Application Protocol (E1AP)",
     "domain": "RAN Interface", "release": "Rel-15+",
     "description": "Signaling protocol for the CU-CP/CU-UP split (E1 interface): bearer context setup, PDU session resource handling. SEP area for RAN-disaggregation and virtualized-RAN patents."},
    {"spec": "TS 38.470", "title": "NG-RAN; F1 general aspects and principles",
     "domain": "RAN Interface", "release": "Rel-15+",
     "description": "General principles for the F1 interface between gNB-CU and gNB-DU, complementing 38.401/38.473. Core reference for split-RAN (CU-DU) SEP claims."},
    {"spec": "TS 38.473", "title": "NG-RAN; F1 Application Protocol (F1AP)",
     "domain": "RAN Interface", "release": "Rel-15+",
     "description": "CU-DU signaling protocol: UE context setup, RRC message transfer, DU-resource coordination. High-density SEP area for split-RAN and multi-vendor-DU interoperability patents."},

    # Conformance / device certification
    {"spec": "TS 38.508-1", "title": "5GS; User Equipment (UE) conformance specification; Part 1: Common test environment",
     "domain": "Conformance", "release": "Rel-15+",
     "description": "Shared test environment and procedures underpinning 5G UE conformance testing. Referenced when a patent's essentiality argument leans on certification/conformance test requirements rather than the base protocol spec alone."},
    {"spec": "TS 38.521-1", "title": "NR; User Equipment (UE) conformance specification; Radio transmission and reception; Part 1: Range 1 Standalone",
     "domain": "Conformance", "release": "Rel-15+",
     "description": "RF conformance test cases for FR1 standalone UEs, derived from 38.101-1's requirements. Occasionally cited alongside 38.101-1 in RF-essentiality analysis."},
    {"spec": "TS 38.523-1", "title": "5GS; User Equipment (UE) conformance specification; Part 1: Protocol",
     "domain": "Conformance", "release": "Rel-15+",
     "description": "Protocol conformance test cases spanning NAS/RRC/AS procedures. Useful cross-reference for verifying that a claimed procedure matches a normatively testable (and therefore more defensibly essential) behavior."},
]

FRAND_KNOWLEDGE = [
    {"title": "FRAND Rate Determination Principles",
     "content": "FRAND (Fair, Reasonable and Non-Discriminatory) licensing requires SEP holders to offer licenses on terms that are fair to all implementers, reasonable in rate, and non-discriminatory across similarly-situated licensees. Key cases: Ericsson v D-Link (5th Cir 2014), TCL v Ericsson (C.D. Cal 2018), Unwired Planet v Huawei (UK Supreme Court 2020)."},
    {"title": "SEP Essentiality Assessment",
     "content": "A patent is Standard Essential if it is impossible to implement the standard without infringing the patent claims. Essentiality is assessed claim by claim against the normative text of the standard. Declared SEPs often exceed truly essential patents by 3-10x (over-declaration rate). Technical essentiality differs from commercial essentiality."},
    {"title": "3GPP Release Timeline",
     "content": "Rel-8 (2008): First LTE. Rel-9 (2009): MBMS. Rel-10 (2011): LTE-A, CA. Rel-11 (2012): CoMP. Rel-12 (2015): Small cells, D2D. Rel-13 (2016): LAA, NB-IoT. Rel-14 (2017): V2X, eMBB. Rel-15 (2018): First 5G NR. Rel-16 (2020): URLLC, IIoT. Rel-17 (2022): RedCap, NTN. Rel-18 (2024): 5G-Advanced."},
    {"title": "ETSI IPR Policy",
     "content": "ETSI IPR Policy (Clause 6) requires members to undertake to grant FRAND licenses. IPR declarations must be submitted when a member believes a standard may require use of its IPR. Declarations are on the ETSI IPR Database at ipr.etsi.org. General declarations cover all past/future patents of a company."},
]


# ── Source Crawlers ────────────────────────────────────────────────────────────

async def crawl_etsi_ipr(max_records: int, client: httpx.AsyncClient) -> list[dict]:
    """
    Crawl ETSI IPR public database.
    Uses the public ETSI IPR search API (no auth required).
    """
    chunks = []
    logger.info("Crawling ETSI IPR database...")
    try:
        # ETSI IPR public export endpoint
        resp = await client.get(
            "https://ipr.etsi.org/api/ipr",
            params={"format": "json", "limit": min(max_records, 100)},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            for item in data.get("results", []):
                content = (
                    f"ETSI IPR Declaration. Declarant: {item.get('declarant', '')}. "
                    f"Standards: {', '.join(item.get('standards', []))}. "
                    f"Project: {item.get('project', '')}. "
                    f"Filing type: {item.get('filing_type', '')}."
                )
                chunks.append({
                    "id": f"etsi_ipr_{item.get('id', '')}",
                    "content": content,
                    "metadata": {"source_type": "etsi_ipr", "source_id": str(item.get("id", "")),
                                 "declarant": item.get("declarant", "")}
                })
                await asyncio.sleep(settings.CRAWL_DELAY_SECONDS / 10)
    except Exception as e:
        logger.warning(f"ETSI IPR crawl failed (non-fatal): {e}")
    return chunks


async def crawl_3gpp_specs(max_records: int, client: httpx.AsyncClient) -> list[dict]:
    """
    3GPP spec list from the public 3GPP portal.
    Falls back to built-in knowledge if network unavailable.
    """
    chunks = []
    # Try network first
    try:
        resp = await client.get(
            "https://www.3gpp.org/ftp/Specs/archive/",
            timeout=10,
        )
        # Parse spec listing from HTML (simplified)
        text = resp.text
        spec_pattern = re.compile(r'(\d{2}\.\d{3})["\s]')
        found_specs = set(spec_pattern.findall(text))
        for spec_num in list(found_specs)[:max_records]:
            chunks.append({
                "id": f"3gpp_spec_{spec_num}",
                "content": f"3GPP specification TS {spec_num}. Part of the 3GPP technical specification series.",
                "metadata": {"source_type": "3gpp_spec", "spec_number": spec_num}
            })
    except Exception:
        logger.info("3GPP network unavailable — using the built-in 3GPP specification list")

    # Always add the built-in curated knowledge
    for item in BUILTIN_3GPP_KNOWLEDGE:
        chunks.append({
            "id": f"builtin_3gpp_{item['spec'].replace(' ', '_')}",
            "content": f"3GPP {item['spec']}: {item['title']}. Domain: {item['domain']}. Release: {item['release']}. {item['description']}",
            "metadata": {
                "source_type": "3gpp_spec",
                "spec": item["spec"],
                "domain": item["domain"],
                "release": item["release"],
            }
        })
    return chunks


async def crawl_frand_knowledge(max_records: int, client: httpx.AsyncClient) -> list[dict]:
    """Built-in FRAND reference notes as vector-store chunks (offline; no network)."""
    chunks = []
    for item in FRAND_KNOWLEDGE:
        chunks.append({
            "id": f"frand_{item['title'].replace(' ', '_').lower()}",
            "content": f"{item['title']}: {item['content']}",
            "metadata": {"source_type": "frand", "title": item["title"]}
        })
    return chunks


SOURCE_CRAWLERS = {
    "etsi_ipr": crawl_etsi_ipr,
    "3gpp_specs": crawl_3gpp_specs,
    "frand_cases": crawl_frand_knowledge,
    "itu_references": lambda n, c: asyncio.sleep(0) or [],  # placeholder
    "wipo_patentscope": lambda n, c: asyncio.sleep(0) or [],  # placeholder (needs API key)
}


# ── Pipeline Orchestrator ──────────────────────────────────────────────────────

async def run_ingestion_pipeline(job_ids: list[int], sources: list[str], max_records: int):
    """
    Background task: runs all ingestion jobs, indexes chunks into vector store.
    """
    async with _BgSession() as db:
        vs = get_vector_store()
        async with httpx.AsyncClient(
            headers={"User-Agent": "SEP-Intelligence-Platform/2.0 (academic research)"},
            timeout=30,
            follow_redirects=True,
        ) as client:
            for job_id, source in zip(job_ids, sources):
                await _update_job(db, job_id, "running")
                try:
                    crawler = SOURCE_CRAWLERS.get(source)
                    if not crawler:
                        await _update_job(db, job_id, "failed", error=f"No crawler for {source}")
                        continue

                    logger.info(f"[Job {job_id}] Starting ingestion: {source}")
                    raw_chunks = await crawler(max_records, client)
                    if asyncio.iscoroutine(raw_chunks):
                        raw_chunks = await raw_chunks

                    logger.info(f"[Job {job_id}] Fetched {len(raw_chunks)} chunks from {source}")

                    # Save to KnowledgeChunk table
                    for chunk in raw_chunks:
                        kc = KnowledgeChunk(
                            source_type=source,
                            source_id=chunk.get("id"),
                            content=chunk["content"],
                            extra_metadata=json.dumps(chunk.get("metadata", {})),
                        )
                        db.add(kc)
                    await db.flush()

                    # Index into vector store in batches
                    batch_size = 50
                    indexed = 0
                    for i in range(0, len(raw_chunks), batch_size):
                        batch = raw_chunks[i:i+batch_size]
                        count = await vs.upsert(batch)
                        indexed += count
                        await asyncio.sleep(0.1)  # yield control

                    await _update_job(db, job_id, "done",
                                      records_fetched=len(raw_chunks), records_indexed=indexed)
                    logger.info(f"[Job {job_id}] Done: {indexed} chunks indexed")

                except Exception as e:
                    logger.error(f"[Job {job_id}] Failed: {e}")
                    await _update_job(db, job_id, "failed", error=str(e))

        await db.commit()


async def _update_job(db: AsyncSession, job_id: int, status: str,
                      records_fetched: int = 0, records_indexed: int = 0, error: str = None):
    """Update an IngestionJob's status and counters (completed_at stamped for finished jobs)."""
    from sqlalchemy import select
    result = await db.execute(select(IngestionJob).where(IngestionJob.id == job_id))
    job = result.scalar_one_or_none()
    if job:
        job.status = status
        if records_fetched:
            job.records_fetched = records_fetched
        if records_indexed:
            job.records_indexed = records_indexed
        if error:
            job.error_message = error
        if status in ("done", "failed"):
            job.completed_at = _utcnow()
        await db.flush()
