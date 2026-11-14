// ── Core Analysis Types ────────────────────────────────────────────────────────

export interface ReleaseMapping {
  release: string;
  confidence: number;
  reasoning?: string;
}

export interface TechnicalDomain {
  domain: string;
  confidence: number;
}

export interface NetworkLayer {
  layer: string;
  confidence: number;
}

export interface SpecDescription {
  specification: string;
  description: string;
  functional_role: string;
  technology_area: string;
}

export interface StandardsAnalysis {
  '3gpp_release_mapping': ReleaseMapping[];
  technical_domains: TechnicalDomain[];
  network_layer_mapping: NetworkLayer[];
  likely_protocol_components: string[];
  standard_descriptions: SpecDescription[];
}

export interface SepAnalysis {
  likely_sep_strength: string;
  probable_implementation_scope: string;
  implementation_dependency: string;
  probable_device_impact: string[];
  probable_network_impact: string[];
  estimated_licensing_relevance: string;
  confidence: number;
}

export interface GeographicAnalysis {
  jurisdiction_strength: string;
  enforcement_significance: string;
  regional_strategy_inference: string;
}

export interface InferredFields {
  probable_title: string;
  probable_technology_focus: string;
  probable_use_cases: string[];
  probable_claim_scope: string;
}

export interface ExplicitData {
  project: string;
  standards: string[];
  applicant: string;
  application_number: string;
  publication_number: string;
  country: string;
  title: string;
}

export interface PatentDisclosure {
  disclosure_number: number;
  explicit_data: ExplicitData;
  standards_analysis: StandardsAnalysis;
  sep_analysis: SepAnalysis;
  geographic_analysis: GeographicAnalysis;
  inferred_fields: InferredFields;
}

export interface DeclarationMetadata {
  declarant: string;
  filing_type: string;
  project: string;
  overall_technology_domains: string[];
  portfolio_characterization: string;
  likely_standard_generation: string;
  confidence: number;
}

export interface PortfolioAnalysis {
  dominant_technology_domains: string[];
  release_focus: string[];
  most_targeted_specs: string[];
  cross_specification_patterns: string[];
  geographic_filing_strategy: string;
  portfolio_strength_assessment: string;
  frand_implications: string;
  implementation_risk_summary: string;
}

// Extraction methods that mean the source PDF had no reliable text layer and
// had to be read via OCR (Tesseract) or a vision model, rather than pulled
// directly from embedded text. These are inherently lower-fidelity than
// pymupdf/pdfplumber, so the UI flags them for manual verification.
export const OCR_EXTRACTION_METHODS = ['tesseract', 'vision_ollama', 'fallback_best_effort'] as const;

export interface OcrQuality {
  method?: string | null;
  low_confidence_pages?: number[];
  page_confidences?: Array<{ page: number; confidence?: number; low_confidence?: boolean }>;
}

export interface CountDiagnostic {
  extracted_patents: number;
  detected_source_numbers: number;
  significant_gap: boolean;
  message?: string | null;
  note?: string | null;
}

// Provenance for how the rows in a declaration were obtained. A CSV/XLSX/
// HTML table whose columns were recognised is mapped deterministically
// with no extraction model involved (`used_extraction_model: false`),
// which is a materially different trust level from rows an LLM read off an
// OCR'd scan — so the UI distinguishes them rather than presenting both as
// simply "extracted". `encoding`/`delimiter` are CSV-only; `format`/
// `sheet`/`table_index`/`tables_found` are XLSX/HTML-only — everything
// else is shared across all three tabular formats.
export interface CsvSourceMeta {
  encoding?: string;
  delimiter?: string;
  format?: string;
  sheet?: string;
  table_index?: number;
  tables_found?: number;
  header_row?: number | null;
  merged_header?: boolean;
  header_span?: number;
  mapped_columns?: Record<string, string>;
  unmapped_columns?: string[];
  rows_total?: number;
  rows_parsed?: number;
  rows_skipped?: number;
  declarant_source?: string;
  truncated?: boolean;
  warnings?: string[];
}

export interface SourceInfo {
  kind: 'pdf' | 'csv' | 'xlsx' | 'html';
  extraction_method?: string | null;
  used_extraction_model: boolean;
  csv?: CsvSourceMeta | null;
}

export interface Declaration {
  filename: string;
  declaration_metadata: DeclarationMetadata;
  patent_disclosures: PatentDisclosure[];
  portfolio_level_analysis: PortfolioAnalysis;
  research_outputs?: ResearchOutputs | null;
  _inference_time_s?: number;
  _backend?: string;
  _model?: string;
  _parse_error?: boolean;
  _ocr_quality?: OcrQuality | null;
  _count_diagnostic?: CountDiagnostic | null;
  /** Set when some parts of the document produced no/partial LLM output. */
  /** Database id once persisted (enables History / Excel export). */
  _declaration_id?: number;
  /** Set when the result could not be saved to the database. */
  _persist_error?: string;
  _possibly_incomplete?: boolean;
  _incomplete_chunk_count?: number;
  _failed_chunks?: { part: number; reason: string }[];
  _source?: SourceInfo | null;
  _source_warnings?: string[];
}

// ── Research / Paper-Style Outputs ─────────────────────────────────────────────
// Produced by research/engine.py — see CHANGES.md for how each field maps
// back to the two reference papers.

export type PredictedSepStatus = 'Confirmed SEP' | 'Likely SEP' | 'Potential SEP' | 'Insufficient Evidence';
export type DeclaredSepStatus = 'Declared' | 'Undeclared (Blanket)';
export type VerificationStatus = 'Verified - forward alignment' | 'Predicted' | 'Stable' | 'Declining relevance';

export interface VersionEvolutionPoint {
  standard_version: string;   // "TS 36.331 v15.8.0"
  version: string;            // "v15.8.0"
  release: string;            // "Rel-15"
  year: number;
  similarity: number;
}

export interface VersionEvolution {
  standard: string;
  clause: string;             // "Clause 5.3.3"
  clause_title: string;
  points: VersionEvolutionPoint[];
  trend: 'increasing' | 'decreasing' | 'stable';
  trend_label: string;        // "Predicted essentiality increased"
}

export interface VerificationResult {
  current_version: string;
  current_version_source: 'declared' | 'earliest-in-corpus (no declared version found)';
  future_version: string | null;
  current_similarity: number | null;
  future_similarity: number | null;
  verification: string;
  status: VerificationStatus | 'Stable';
  delta: number;
}

export interface PotentialSepDetail {
  topic: string;
  reason: string;
  standard: string;
  missing_standard_section: string;
  predicted_future_version: string;
}

export interface ResearchRecord {
  index: number;
  patent_label: string;
  title: string;
  applicant: string;
  filing_date: string | null;
  publication_date: string | null;
  etsi_declaration: boolean;
  declared_standards: string[];
  declared_sep_status: DeclaredSepStatus;
  predicted_sep_status: PredictedSepStatus;
  confidence_score: number;
  recommended_standard: string;
  recommended_version: string;
  standard_clause: string;
  standard_clause_title: string;
  domain: string;
  similarity_score: number;
  similar_patent: string | null;
  similar_patent_score: number | null;
  explanation: string[];
  /** v0.4.9: exact word sequences shared by the patent and the matched clause (clause wording). */
  matched_phrases?: string[];
  /** v0.4.9: single shared technical words not already inside a phrase. */
  matched_terms?: string[];
  version_evolution: VersionEvolution;
  verification: VerificationResult;
  is_potential_sep: boolean;
  potential_sep: PotentialSepDetail | null;
}

export interface PatentClassificationRow {
  patent: string;
  probability: number;
  predicted_sep: PredictedSepStatus;
  recommended_standard: string;
  version: string;
  confidence: number;
}

export interface StandardMappingRow {
  patent: string;
  matched_standard: string;
  matched_clause: string;
  matched_clause_title?: string | null;
  similarity: number;
  matched_phrases?: string[];
  matched_terms?: string[];
  reason: string;
}

export interface PotentialSepRow {
  patent: string;
  topic: string;
  reason: string;
  missing_standard_section: string;
  predicted_future_version: string;
}

export interface VerificationRow {
  patent: string;
  current_version: string;
  future_version: string | null;
  verification: string;
  status: string;
}

export interface ResearchTables {
  patent_classification: PatentClassificationRow[];
  standard_mapping: StandardMappingRow[];
  potential_seps: PotentialSepRow[];
  verification: VerificationRow[];
}

export interface ConfusionMatrix {
  labels: [string, string];
  matrix: number[][];
  tn: number;
  fp: number;
  fn: number;
  tp: number;
}

export interface SamplePair {
  patent: string;
  family: string;
  role: 'declared' | 'control';
  similarity: number;
}

export interface ResearchMetrics {
  available: boolean;
  reason?: string;
  n_pairs?: number;
  threshold?: number;
  accuracy?: number;
  precision?: number;
  recall?: number;
  f1?: number;
  cohens_kappa?: number;
  confusion_matrix?: ConfusionMatrix;
  sample_pairs?: SamplePair[];
  methodology?: string;
}

export interface ResearchAggregates {
  n_patents?: number;
  average_similarity?: number;
  average_confidence?: number;
  n_potential_seps?: number;
  status_distribution?: Record<string, number>;
  top_standards?: [string, number][];
  top_versions?: [string, number][];
  top_applicants?: [string, number][];
  top_domains?: [string, number][];
}

export interface ResearchFigure {
  title: string;
  image: string;     // data:image/png;base64,...
  caption: string;
  error?: string;
}

export interface ResearchMeta {
  embedder_mode: string;          // "sentence-transformers" | "tfidf-fallback" | "n/a"
  corpus_size: number;
  corpus_families: string[];
  similarity_threshold: number;
  disclaimer: string;
  n_patents: number;
}

export interface ResearchOutputs {
  meta: ResearchMeta;
  records: ResearchRecord[];
  tables: ResearchTables;
  metrics: ResearchMetrics;
  aggregates: ResearchAggregates;
  figures: Record<string, ResearchFigure> | null;
}

export interface AnalysisResponse {
  declarations: Declaration[];
  errors: Array<{ filename: string; error: string }>;
  total_files: number;
  succeeded: number;
  backend: string;
  // v0.6 start/stop/pause/continue: true when a POST .../jobs/{job_id}/pause
  // or /stop request cooperatively stopped this analysis before every file
  // was processed -- see analysis.py's "start / stop / pause / continue"
  // section. `skipped_files` names the files that weren't reached yet, to
  // re-submit later under the same job_id.
  stopped?: boolean;
  skipped_files?: string[];
}

// ── UI State Types ─────────────────────────────────────────────────────────────

export type AnalysisMode = 'deep' | 'quick';
export type Theme = 'dark' | 'light';
export type LLMBackend = 'ollama' | 'llamacpp' | 'vllm';

export interface BackendStatus {
  /** 'ok' = FastAPI backend + database usable (READY or DEGRADED). */
  status: 'ok' | 'error' | 'checking';
  overall?: 'READY' | 'DEGRADED' | 'UNAVAILABLE' | string;
  backend?: string;
  /** Shared LLM health contract — see shared/llmHealth.ts. */
  llm?: import('../../shared/llmHealth').LlmHealth;
}

// GPU/VRAM auto-detection for the model picker (GET /models/recommended).
// `detected: false` means no GPU could be identified (no NVIDIA driver, a
// non-NVIDIA GPU, or a platform the backend has no probe for) — the name/
// vram_gb fields are null in that case, and `tier`/`recommended` still
// carry a conservative recommendation rather than nothing at all.
export interface GpuInfo {
  detected: boolean;
  name: string | null;
  vram_gb: number | null;
  source: string | null;
  /** v0.4.2: why detection found nothing (shown in the panel) */
  error?: string | null;
}

export interface RecommendedModelInfo {
  id: string;
  name: string;
  vram_gb: number;
  speed?: string;
  quality?: string;
}

export interface RecommendedModelResponse {
  gpu: GpuInfo;
  tier: string;
  recommended: RecommendedModelInfo | null;
  tier_models: RecommendedModelInfo[];
}

export interface FileItem {
  file: File;
  id: string;
}

export interface SearchResult {
  content: string;
  score: number;
  metadata: Record<string, unknown>;
}
