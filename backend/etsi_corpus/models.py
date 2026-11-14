"""
Structured extraction schemas (v0.3 item 7).

Every field the LLM is asked to fill in is declared here with Pydantic, so
a response is either valid structured data or is rejected/repaired/retried
-- never accepted as arbitrary prose. Every field defaults to "Unknown" (or
an empty list) rather than being required, because the spec is explicit:
never guess, unknown information must be represented as Unknown.

These models are intentionally flat-ish JSON-friendly shapes (matching the
{"identity": {}, "classification": {}, ...} sketch in the v0.3 spec) so they
serialize directly into EtsiDocument's *_json columns and into the JSONL
export.
"""

from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field

UNKNOWN = "Unknown"

ClaimRelationship = Literal["explicit", "strong", "partial", "weak", "no_match"]


def relationship_from_score(score: float) -> ClaimRelationship:
    """Deterministic score->label thresholds (documented, not hidden) so
    the same score always yields the same label across the whole platform.
    NOTE: this is a TECHNICAL CORRESPONDENCE label, not a legal essentiality
    opinion -- see PatentStandardMapping's docstring."""
    if score >= 0.90:
        return "explicit"
    if score >= 0.75:
        return "strong"
    if score >= 0.55:
        return "partial"
    if score >= 0.35:
        return "weak"
    return "no_match"


# ── Document (identity) ─────────────────────────────────────────────────────

class DocumentIdentity(BaseModel):
    """Who a standards document is: title, standard name, series, spec number, version, release, dates
    (UNKNOWN when not extracted).
    """
    title: str = UNKNOWN
    standard_name: str = UNKNOWN
    series: str = UNKNOWN
    specification_number: str = UNKNOWN
    version: str = UNKNOWN
    release: str = UNKNOWN
    document_type: str = UNKNOWN   # TS | TR | EN | ES | ...
    publication_date: str = UNKNOWN
    source_url: str = UNKNOWN


class DocumentClassification(BaseModel):
    """Technical classification of a document: domain, sub-domain, topics, protocols, interfaces,
    network functions, generations.
    """
    technology_domain: str = UNKNOWN
    subdomain: str = UNKNOWN
    topics: list[str] = Field(default_factory=list)
    protocols: list[str] = Field(default_factory=list)
    interfaces: list[str] = Field(default_factory=list)
    network_components: list[str] = Field(default_factory=list)
    procedures: list[str] = Field(default_factory=list)
    mechanisms: list[str] = Field(default_factory=list)
    algorithms: list[str] = Field(default_factory=list)
    technical_entities: list[str] = Field(default_factory=list)
    terminology: list[str] = Field(default_factory=list)


class TechnicalSummary(BaseModel):
    """LLM-produced summaries of a document (purpose, scope, executive/technical summary, key features,
    procedures).
    """
    purpose: str = UNKNOWN
    scope: str = UNKNOWN
    executive_summary: str = UNKNOWN
    technical_summary: str = UNKNOWN
    key_features: list[str] = Field(default_factory=list)
    important_procedures: list[str] = Field(default_factory=list)
    important_requirements: list[str] = Field(default_factory=list)
    important_mechanisms: list[str] = Field(default_factory=list)
    important_parameters: list[str] = Field(default_factory=list)


class ClauseInfo(BaseModel):
    """One clause of a document: number, title, summary, page."""
    clause_number: str = UNKNOWN
    clause_title: str = UNKNOWN
    summary: str = UNKNOWN
    page: Optional[int] = None


class PatentRelevance(BaseModel):
    """Technical-correspondence signals only -- never a legal opinion (item
    16 of the v0.3 spec: 'The system identifies technical correspondence.
    It does not provide a legal opinion.')."""
    patent_relevant_concepts: list[str] = Field(default_factory=list)
    potentially_essential_features: list[str] = Field(default_factory=list)
    mandatory_normative_features: list[str] = Field(default_factory=list)
    distinctive_mechanisms: list[str] = Field(default_factory=list)
    claim_like_technical_concepts: list[str] = Field(default_factory=list)
    important_technical_relationships: list[str] = Field(default_factory=list)


class Provenance(BaseModel):
    """Where an extracted value came from: confidence, source pages, evidence text, extraction method."""
    confidence: float = 0.0
    source_pages: list[int] = Field(default_factory=list)
    evidence_text: str = UNKNOWN
    extraction_method: str = UNKNOWN


class EtsiDocumentExtraction(BaseModel):
    """Top-level structured-extraction result for one ETSI PDF (item 7)."""
    identity: DocumentIdentity = Field(default_factory=DocumentIdentity)
    classification: DocumentClassification = Field(default_factory=DocumentClassification)
    technical_summary: TechnicalSummary = Field(default_factory=TechnicalSummary)
    technical_features: list[str] = Field(default_factory=list)
    clauses: list[ClauseInfo] = Field(default_factory=list)
    patent_relevance: PatentRelevance = Field(default_factory=PatentRelevance)
    provenance: Provenance = Field(default_factory=Provenance)
    validation_status: Literal["valid", "repaired", "incomplete"] = "valid"


# ── Patent claim extraction (C-SARAS patent workflow, items 13-16) ─────────

class ExtractedPatent(BaseModel):
    """Patent identity fields extracted from an uploaded document."""
    title: str = UNKNOWN
    patent_number: str = UNKNOWN
    application_number: str = UNKNOWN
    publication_number: str = UNKNOWN
    abstract: str = UNKNOWN


class ExtractedClaim(BaseModel):
    """One claim of a patent with its type, dependency, text and the technical elements extracted from
    it.
    """
    claim_number: str
    claim_type: Literal["independent", "dependent"] = "independent"
    depends_on: Optional[str] = None
    claim_text: str = ""
    technical_elements: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    relationships: list[str] = Field(default_factory=list)
    parameters: list[str] = Field(default_factory=list)
    protocols: list[str] = Field(default_factory=list)
    mechanisms: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    distinctive_concepts: list[str] = Field(default_factory=list)


class StandardCandidate(BaseModel):
    """One ranked ETSI standard result for a patent (item 15)."""
    etsi_document_id: Optional[int] = None
    standard: str = UNKNOWN
    specification_number: str = UNKNOWN
    version: str = UNKNOWN
    release: str = UNKNOWN
    domain: str = UNKNOWN
    topic: str = UNKNOWN
    relevance: float = 0.0
    confidence: float = 0.0
    matching_concepts: list[str] = Field(default_factory=list)
    matching_clauses: list[str] = Field(default_factory=list)
    evidence: str = "No evidence found"


class ClaimStandardMappingResult(BaseModel):
    """First-class claim-to-standard data structure (item 16)."""
    claim_number: str
    standard: str = UNKNOWN
    specification_number: str = UNKNOWN
    release: str = UNKNOWN
    clause: str = UNKNOWN
    clause_title: str = UNKNOWN
    page: Optional[int] = None
    claim_elements: list[str] = Field(default_factory=list)
    standard_elements: list[str] = Field(default_factory=list)
    matching_concepts: list[str] = Field(default_factory=list)
    relationship: ClaimRelationship = "no_match"
    similarity_score: float = 0.0
    confidence: float = 0.0
    evidence: str = "No evidence found"
    source_pages: list[int] = Field(default_factory=list)
    etsi_document_id: Optional[int] = None
