"""
Standards Reference Corpus
===========================
A small, fully offline, version- and clause-aware reference corpus describing
representative 3GPP technical specification content across several releases.

This corpus plays the role that the "ETSI concordance list" (Paper 1, Table I)
and the "Standard DB / Standard Map" (Paper 2, Fig.1) play in the two reference
papers: it is the thing patent text gets compared against to recommend a
standard, a clause/section, and a version.

IMPORTANT — SCOPE & ACCURACY NOTE
----------------------------------
The clause numbers, section numbers and version strings below are
*illustrative reference data* written to give the offline similarity engine
something realistic and version-aware to match against, in the spirit of the
two papers ("similar TABLE/figure style outputs, not a faithful reproduction
of the underlying methodology"). They are based on general 3GPP numbering
conventions and the public 3GPP release timeline, but they are **not** pulled
live from ETSI/3GPP and must be re-verified against the live specification
text before being used for any FRAND, licensing, or litigation purpose.
Every output that uses this corpus carries a `CORPUS_DISCLAIMER` string so the
UI/report can surface this.

Extending the corpus
---------------------
Each "family" below is one 3GPP TS number. Each family has 1-2 "clause
groups" (a clause/section that is tracked across versions). Each clause group
has 2-3 chronological versions with a short descriptive text. Adding a new
family/clause/version is just adding another entry to `FAMILY_DEFS` —
embeddings are (re)computed automatically and cached.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


CORPUS_DISCLAIMER = (
    "Standard/clause/version references come from an offline illustrative "
    "reference corpus for similarity matching. Verify clause numbers and "
    "version strings against the live ETSI/3GPP specification text before "
    "relying on them for FRAND, licensing, or litigation purposes."
)


# ── Domain keyword lexicon ──────────────────────────────────────────────────
# Used both to enrich the embedding text for each corpus entry (so semantic
# search responds to domain terminology) and to generate human-readable
# "X keyword overlap" explanation bullets (Requirement 5 / Paper 2 Table 3
# style topic-keyword lists).

DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "RRC": [
        "rrc connection", "reconfiguration", "handover", "measurement report",
        "mobility", "re-establishment", "rrc state", "ue capability", "srb",
    ],
    "MAC": [
        "harq", "scheduling request", "random access", "buffer status report",
        "drx", "logical channel", "transport block", "retransmission",
    ],
    "PHY": [
        "pdsch", "pusch", "mimo", "csi report", "modulation", "channel coding",
        "harq-ack", "beamforming", "reference signal",
    ],
    "Architecture": [
        "ng-ran interface", "gnb", "split architecture", "dual connectivity",
        "qos flow", "mobility anchor", "network function", "handover",
    ],
    "Core Network": [
        "amf", "smf", "upf", "pdu session", "network slice", "nssai",
        "session management", "policy control",
    ],
    "NAS": [
        "registration", "authentication", "security context", "tracking area",
        "pdu session establishment", "nas signalling", "mobility management",
    ],
    "RAN Interface": [
        "xn interface", "handover preparation", "inter-gnb", "context transfer",
        "dual connectivity", "interference coordination",
    ],
    "Security": [
        "authentication", "key derivation", "5g-aka", "eap-aka", "ciphering",
        "integrity protection", "key hierarchy", "suci",
    ],
    "RRM": [
        "measurement", "handover threshold", "beam management",
        "cell reselection", "rsrp", "rsrq", "mobility kpi",
    ],
}


@dataclass(frozen=True)
class StandardClauseEntry:
    """One (standard, version, clause) reference point in the corpus."""

    standard: str          # "TS 38.331"
    family: str            # "38.331" — used to group versions of the same spec
    version: str           # "v15.3.0"
    release: str           # "Rel-15"
    year: int              # approximate release year, for timeline plots
    order: int             # chronological order within the clause group (0,1,2..)
    section_type: str      # "Clause" | "Section"
    clause: str            # "5.3.3"
    clause_title: str      # "RRC Connection Establishment"
    domain: str            # "RRC" — key into DOMAIN_KEYWORDS
    text: str              # descriptive text used for embedding
    clause_group: str = field(default="")  # f"{family}-{clause}", set in __post_init__

    def __post_init__(self):
        """Derive `clause_group` ('<family>-<clause>'), which links the versions of one clause."""
        object.__setattr__(self, "clause_group", f"{self.family}-{self.clause}")

    @property
    def id(self) -> str:
        """Stable id '<family>-<clause>-<version>'."""
        return f"{self.family}-{self.clause}-{self.version}"

    @property
    def standard_version(self) -> str:
        """e.g. 'TS 38.331 v15.3.0'"""
        return f"{self.standard} {self.version}"

    @property
    def clause_label(self) -> str:
        """e.g. 'Clause 5.3.3' or 'Section 9'"""
        return f"{self.section_type} {self.clause}"

    @property
    def keywords(self) -> list[str]:
        """Keyword phrases of the entry's technical domain."""
        return DOMAIN_KEYWORDS.get(self.domain, [])

    def embedding_text(self) -> str:
        """Text representation used to compute the entry's embedding."""
        kw = ", ".join(self.keywords[:6])
        return (
            f"{self.standard} {self.version} ({self.release}) "
            f"{self.clause_label} — {self.clause_title}. "
            f"Domain: {self.domain}. Key terms: {kw}. {self.text}"
        )


# ── Corpus definitions ──────────────────────────────────────────────────────
# Each family: standard, family number, section_type, domain, and a list of
# clause groups. Each clause group: clause id, title, and a chronologically
# ordered list of (version, release, year, text).

FAMILY_DEFS = [
    {
        "standard": "TS 38.331", "family": "38.331",
        "section_type": "Clause", "domain": "RRC",
        "clauses": [
            {
                "clause": "5.3.3", "title": "RRC Connection Establishment",
                "versions": [
                    ("v15.3.0", "Rel-15", 2018,
                     "Defines the RRC Setup procedure for establishing an RRC "
                     "connection from RRC_IDLE, including four-step random "
                     "access based initial access, SRB1 establishment and UE "
                     "capability transfer for NR standalone deployments."),
                    ("v16.2.0", "Rel-16", 2020,
                     "Extends RRC Setup to support two-step random access "
                     "(RACH Type-2) for reduced-latency initial access, and "
                     "introduces early measurement reporting and conditional "
                     "configuration storage during connection establishment."),
                    ("v17.4.0", "Rel-17", 2022,
                     "Adds Small Data Transmission (SDT) procedures allowing "
                     "data exchange while remaining in RRC_INACTIVE, and "
                     "introduces capability differentiation for reduced "
                     "capability (RedCap) devices during connection setup."),
                ],
            },
            {
                "clause": "5.3.5", "title": "RRC Reconfiguration and Handover",
                "versions": [
                    ("v15.3.0", "Rel-15", 2018,
                     "Specifies the RRC Reconfiguration procedure used for "
                     "intra-NR handover, SRB/DRB addition and modification, "
                     "and measurement configuration — the baseline mobility "
                     "framework for NR standalone."),
                    ("v16.2.0", "Rel-16", 2020,
                     "Introduces Conditional Handover (CHO) and Conditional "
                     "PSCell Change/Addition, letting the UE evaluate and "
                     "execute a pre-configured reconfiguration once execution "
                     "conditions are met, improving handover robustness."),
                    ("v17.4.0", "Rel-17", 2022,
                     "Adds Dual Active Protocol Stack (DAPS) handover "
                     "refinements, NTN-aware mobility triggers, and "
                     "Layer-1/Layer-2 triggered mobility for inter-cell beam "
                     "management without a full RRC reconfiguration."),
                ],
            },
        ],
    },
    {
        "standard": "TS 38.321", "family": "38.321",
        "section_type": "Clause", "domain": "MAC",
        "clauses": [
            {
                "clause": "5.1", "title": "Random Access Procedure",
                "versions": [
                    ("v15.3.0", "Rel-15", 2018,
                     "Defines the four-step contention-based and "
                     "contention-free random access procedures (Msg1-Msg4) "
                     "for initial access, handover and uplink "
                     "re-synchronization."),
                    ("v16.2.0", "Rel-16", 2020,
                     "Introduces the two-step random access type (Type-2 RA), "
                     "combining preamble and payload (MsgA) with the response "
                     "(MsgB) to reduce latency for small uplink transmissions "
                     "and URLLC use cases."),
                    ("v17.4.0", "Rel-17", 2022,
                     "Adds random access enhancements for RedCap UEs, "
                     "including separate RACH resource configuration and "
                     "coverage-recovery procedures for reduced-bandwidth "
                     "devices."),
                ],
            },
            {
                "clause": "5.4", "title": "HARQ Operation",
                "versions": [
                    ("v15.3.0", "Rel-15", 2018,
                     "Specifies asynchronous adaptive HARQ for downlink and "
                     "uplink with up to 16 parallel HARQ processes, "
                     "soft-buffer management and feedback timing for NR "
                     "scheduling."),
                    ("v16.2.0", "Rel-16", 2020,
                     "Adds HARQ process handling for Configured Grant PUSCH "
                     "retransmissions and supports multiple active HARQ "
                     "feedback codebooks to reduce latency for URLLC."),
                    ("v17.4.0", "Rel-17", 2022,
                     "Introduces extended HARQ round-trip-time handling for "
                     "Non-Terrestrial Networks (NTN), including additional "
                     "HARQ process identifiers for satellite propagation "
                     "delay."),
                ],
            },
        ],
    },
    {
        "standard": "TS 38.214", "family": "38.214",
        "section_type": "Clause", "domain": "PHY",
        "clauses": [
            {
                "clause": "5.1", "title": "PDSCH Reception and CSI Reporting",
                "versions": [
                    ("v15.3.0", "Rel-15", 2018,
                     "Defines UE procedures for receiving the Physical "
                     "Downlink Shared Channel, Type-I CSI codebooks, and "
                     "aperiodic/periodic CSI reporting for single- and "
                     "multi-user MIMO."),
                    ("v16.2.0", "Rel-16", 2020,
                     "Introduces enhanced Type-II CSI codebooks with "
                     "frequency-domain compression for higher-resolution "
                     "multi-user MIMO feedback, improving spectral efficiency "
                     "for massive MIMO."),
                    ("v17.4.0", "Rel-17", 2022,
                     "Adds support for up to 8 receive-branch UEs, a unified "
                     "TCI framework for simplified beam indication, and CSI "
                     "enhancements for high-mobility and FR2 operation."),
                ],
            },
            {
                "clause": "6.1", "title": "PUSCH Transmission and HARQ-ACK Feedback",
                "versions": [
                    ("v15.3.0", "Rel-15", 2018,
                     "Specifies UE procedures for PUSCH transmission, dynamic "
                     "and configured grant scheduling, and HARQ-ACK codebook "
                     "construction for uplink control information."),
                    ("v16.2.0", "Rel-16", 2020,
                     "Adds support for multiple HARQ-ACK codebooks and "
                     "sub-slot based PUCCH repetition to reduce feedback "
                     "latency for URLLC uplink reliability."),
                    ("v17.4.0", "Rel-17", 2022,
                     "Introduces PUSCH repetition across slot boundaries and "
                     "HARQ-ACK feedback adaptations for reduced-capability and "
                     "non-terrestrial network UEs."),
                ],
            },
        ],
    },
    {
        "standard": "TS 38.300", "family": "38.300",
        "section_type": "Section", "domain": "Architecture",
        "clauses": [
            {
                "clause": "9", "title": "Mobility and Handover",
                "versions": [
                    ("v15.3.0", "Rel-15", 2018,
                     "Overall description of NR intra-gNB and Xn-based "
                     "handover, including measurement-triggered handover and "
                     "dual connectivity mobility scenarios."),
                    ("v16.2.0", "Rel-16", 2020,
                     "Describes Conditional Handover and Dual Active Protocol "
                     "Stack handover at the architectural level, reducing "
                     "handover interruption time across gNB-DU boundaries."),
                    ("v17.4.0", "Rel-17", 2022,
                     "Adds architectural support for mobility in "
                     "Non-Terrestrial Networks, including satellite "
                     "ephemeris-assisted handover triggers and extended "
                     "timers for long propagation delay."),
                ],
            },
            {
                "clause": "16", "title": "QoS Framework",
                "versions": [
                    ("v15.3.0", "Rel-15", 2018,
                     "Describes the 5G QoS model based on QoS Flows, 5QI "
                     "values, and reflective QoS for mapping IP flows to "
                     "radio bearers."),
                    ("v16.2.0", "Rel-16", 2020,
                     "Introduces Time Sensitive Networking (TSN) integration, "
                     "including the 5G-TSN translator function and "
                     "deterministic-latency QoS profiles."),
                    ("v17.4.0", "Rel-17", 2022,
                     "Adds QoS handling for RedCap devices and extended "
                     "reality (XR) traffic profiles with jitter-aware "
                     "scheduling guidance."),
                ],
            },
        ],
    },
    {
        "standard": "TS 23.501", "family": "23.501",
        "section_type": "Section", "domain": "Core Network",
        "clauses": [
            {
                "clause": "5.15", "title": "Network Slicing",
                "versions": [
                    ("v15.3.0", "Rel-15", 2018,
                     "Defines Network Slice Selection Assistance Information "
                     "(NSSAI), slice-aware AMF selection, and the baseline "
                     "architecture for supporting multiple network slice "
                     "instances."),
                    ("v16.2.0", "Rel-16", 2020,
                     "Introduces Network Slice Admission Control (NSAC), "
                     "enabling the core network to limit registered UEs or "
                     "PDU sessions per slice based on operator policy."),
                    ("v17.4.0", "Rel-17", 2022,
                     "Adds slice-based enhancements for Time Sensitive "
                     "Communication and RedCap admission, including per-slice "
                     "maximum data burst volume signalling."),
                ],
            },
            {
                "clause": "5.6", "title": "Session and Mobility Management",
                "versions": [
                    ("v15.3.0", "Rel-15", 2018,
                     "Specifies PDU Session establishment, SSC modes, and UE "
                     "mobility registration management across AMF and SMF for "
                     "the baseline 5G core."),
                    ("v16.2.0", "Rel-16", 2020,
                     "Adds redundant PDU sessions over dual-connectivity paths "
                     "and Wireless-Wireline Convergence session handling."),
                    ("v17.4.0", "Rel-17", 2022,
                     "Introduces session-continuity enhancements for "
                     "satellite access and multi-access PDU sessions combining "
                     "3GPP and non-3GPP paths."),
                ],
            },
        ],
    },
    {
        "standard": "TS 24.501", "family": "24.501",
        "section_type": "Clause", "domain": "NAS",
        "clauses": [
            {
                "clause": "5.5", "title": "Registration Procedure",
                "versions": [
                    ("v15.3.0", "Rel-15", 2018,
                     "Specifies the initial, mobility and periodic "
                     "registration update procedures between the UE and AMF, "
                     "including 5G-GUTI allocation and tracking area "
                     "updates."),
                    ("v16.2.0", "Rel-16", 2020,
                     "Adds registration via satellite NG-RAN access and "
                     "enhancements for emergency registration without valid "
                     "subscription credentials."),
                    ("v17.4.0", "Rel-17", 2022,
                     "Introduces registration handling for RedCap UEs, "
                     "including capability signalling for coverage "
                     "enhancement and a small-data-transmission registration "
                     "variant."),
                ],
            },
            {
                "clause": "5.4", "title": "Security Mode Control",
                "versions": [
                    ("v15.3.0", "Rel-15", 2018,
                     "Defines the NAS security mode command procedure for "
                     "negotiating ciphering and integrity algorithms between "
                     "UE and AMF following primary authentication."),
                    ("v16.2.0", "Rel-16", 2020,
                     "Adds additional integrity/ciphering algorithm "
                     "identifiers and security context handling during "
                     "inter-system mobility from EPC to 5GC."),
                    ("v17.4.0", "Rel-17", 2022,
                     "Introduces security context handling for UEs supporting "
                     "Access Traffic Steering, Switching and Splitting "
                     "(ATSSS) across multiple access types."),
                ],
            },
        ],
    },
    {
        "standard": "TS 36.331", "family": "36.331",
        "section_type": "Clause", "domain": "RRC",
        "clauses": [
            {
                "clause": "5.3.3",
                "title": "RRC Connection Establishment and Re-establishment",
                "versions": [
                    ("v14.0.0", "Rel-14", 2017,
                     "Baseline RRC Connection Establishment and "
                     "Re-establishment for LTE, including contention-based "
                     "random access and SRB1 setup, with extensions for "
                     "Cat-M1/NB-IoT coverage enhancement."),
                    ("v15.8.0", "Rel-15", 2019,
                     "Adds Make-Before-Break handover signalling and RRC "
                     "connection re-establishment enhancements to reduce "
                     "interruption time, plus parameters supporting EN-DC "
                     "(E-UTRA NR Dual Connectivity) configuration during "
                     "connection setup."),
                    ("v16.4.0", "Rel-16", 2021,
                     "Introduces further LTE-NR tight-interworking parameters "
                     "for multi-RAT dual connectivity connection establishment "
                     "and re-establishment, including fast MCG recovery "
                     "procedures."),
                ],
            },
            {
                "clause": "5.3.10",
                "title": "Connection Re-establishment and Mobility Enhancements",
                "versions": [
                    ("v14.0.0", "Rel-14", 2017,
                     "Defines RRC connection re-establishment triggers (radio "
                     "link failure, handover failure, reconfiguration "
                     "failure) and the cell-selection criteria for "
                     "identifying a suitable re-establishment cell."),
                    ("v15.8.0", "Rel-15", 2019,
                     "Adds enhanced re-establishment handling for split-bearer "
                     "configurations under dual connectivity, reducing service "
                     "interruption following a secondary-cell-group radio link "
                     "failure."),
                    ("v16.4.0", "Rel-16", 2021,
                     "Introduces fast recovery via secondary cell group (SCG) "
                     "failure reporting without a full RRC re-establishment, "
                     "improving resilience for multi-connectivity "
                     "deployments."),
                ],
            },
        ],
    },
    {
        "standard": "TS 36.321", "family": "36.321",
        "section_type": "Clause", "domain": "MAC",
        "clauses": [
            {
                "clause": "5.4", "title": "HARQ Operation",
                "versions": [
                    ("v14.0.0", "Rel-14", 2017,
                     "Specifies synchronous uplink and asynchronous downlink "
                     "HARQ with up to 8 processes, including HARQ operation "
                     "for Cat-M1/NB-IoT coverage-extension repetitions."),
                    ("v15.8.0", "Rel-15", 2019,
                     "Adds HARQ process handling for short-TTI and shortened "
                     "processing-time configurations to reduce latency for "
                     "latency-critical LTE deployments."),
                    ("v16.4.0", "Rel-16", 2021,
                     "Introduces HARQ feedback adaptations supporting "
                     "multi-RAT dual-connectivity scheduling coordination "
                     "between LTE and NR MAC entities."),
                ],
            },
        ],
    },
    {
        "standard": "TS 38.423", "family": "38.423",
        "section_type": "Section", "domain": "RAN Interface",
        "clauses": [
            {
                "clause": "8.3",
                "title": "Handover and Mobility Procedures over Xn",
                "versions": [
                    ("v15.3.0", "Rel-15", 2018,
                     "Defines Xn-based handover preparation, execution, and UE "
                     "context retrieval procedures between source and target "
                     "gNBs."),
                    ("v16.2.0", "Rel-16", 2020,
                     "Adds Conditional Handover signalling over Xn, letting "
                     "the source gNB provide multiple candidate target "
                     "configurations to the UE in advance."),
                    ("v17.4.0", "Rel-17", 2022,
                     "Introduces enhancements for inter-gNB mobility involving "
                     "Integrated Access and Backhaul (IAB) nodes and "
                     "NTN-related context transfer."),
                ],
            },
        ],
    },
    {
        "standard": "TS 33.501", "family": "33.501",
        "section_type": "Clause", "domain": "Security",
        "clauses": [
            {
                "clause": "6.1",
                "title": "Primary Authentication and Key Agreement",
                "versions": [
                    ("v15.3.0", "Rel-15", 2018,
                     "Specifies 5G-AKA and EAP-AKA' primary authentication, "
                     "SUCI-based subscriber identity concealment, and the 5G "
                     "key hierarchy derivation."),
                    ("v16.2.0", "Rel-16", 2020,
                     "Adds authentication enhancements for non-3GPP access via "
                     "N3IWF and updated key derivation for roaming "
                     "authentication confirmation."),
                    ("v17.4.0", "Rel-17", 2022,
                     "Introduces authentication procedure adaptations for "
                     "satellite-access NG-RAN and credential handling for "
                     "RedCap and IoT-class devices."),
                ],
            },
        ],
    },
    {
        "standard": "TS 38.133", "family": "38.133",
        "section_type": "Clause", "domain": "RRM",
        "clauses": [
            {
                "clause": "9",
                "title": "Measurement and Mobility Requirements",
                "versions": [
                    ("v15.3.0", "Rel-15", 2018,
                     "Defines RSRP/RSRQ/SINR measurement accuracy requirements "
                     "and event-based measurement reporting (A1-A6) used to "
                     "trigger mobility procedures."),
                    ("v16.2.0", "Rel-16", 2020,
                     "Adds beam-level measurement and reporting requirements "
                     "(L1-RSRP) supporting Conditional Handover evaluation and "
                     "beam management."),
                    ("v17.4.0", "Rel-17", 2022,
                     "Introduces relaxed measurement requirements for RedCap "
                     "UEs and additional mobility KPIs for inter-RAT "
                     "measurements involving NTN cells."),
                ],
            },
        ],
    },
]


def _build_corpus() -> list[StandardClauseEntry]:
    """Expand FAMILY_DEFS (families -> clauses -> versions) into flat StandardClauseEntry records in
    chronological order.
    """
    entries: list[StandardClauseEntry] = []
    for fam in FAMILY_DEFS:
        for cg in fam["clauses"]:
            for order, (version, release, year, text) in enumerate(cg["versions"]):
                entries.append(StandardClauseEntry(
                    standard=fam["standard"],
                    family=fam["family"],
                    version=version,
                    release=release,
                    year=year,
                    order=order,
                    section_type=fam["section_type"],
                    clause=cg["clause"],
                    clause_title=cg["title"],
                    domain=fam["domain"],
                    text=text,
                ))
    return entries


STANDARDS_CORPUS: list[StandardClauseEntry] = _build_corpus()


# ── Helper accessors ─────────────────────────────────────────────────────────

_FAMILY_RE = re.compile(r"(\d{2}\.\d{3})")


def family_of(standard_or_text: str) -> Optional[str]:
    """Extract a '38.331'-style family id from a standard string like 'TS 38.331'."""
    m = _FAMILY_RE.search(standard_or_text or "")
    return m.group(1) if m else None


def all_families() -> list[str]:
    """Every specification family in the illustrative corpus."""
    return sorted({e.family for e in STANDARDS_CORPUS})


def all_domains() -> list[str]:
    """Every technical domain in the illustrative corpus."""
    return sorted({e.domain for e in STANDARDS_CORPUS})


def entries_by_family(family: str) -> list[StandardClauseEntry]:
    """Illustrative-corpus entries of one family."""
    return [e for e in STANDARDS_CORPUS if e.family == family]


def clause_groups_by_family(family: str) -> dict[str, list[StandardClauseEntry]]:
    """Return {clause_group: [entries sorted by .order]} for one family."""
    groups: dict[str, list[StandardClauseEntry]] = {}
    for e in entries_by_family(family):
        groups.setdefault(e.clause_group, []).append(e)
    for g in groups.values():
        g.sort(key=lambda e: e.order)
    return groups


def domain_of_family(family: str) -> Optional[str]:
    """Domain of a family in the illustrative corpus, or None."""
    entries = entries_by_family(family)
    return entries[0].domain if entries else None


def representative_entry(family: str) -> Optional[StandardClauseEntry]:
    """A single 'mid release' entry representing a family, for compact plots."""
    entries = entries_by_family(family)
    if not entries:
        return None
    entries = sorted(entries, key=lambda e: e.order)
    return entries[len(entries) // 2]
