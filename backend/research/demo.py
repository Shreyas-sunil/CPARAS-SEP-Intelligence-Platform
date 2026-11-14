"""
Research Module Demo / Smoke Test
==================================
Runs `research.engine.analyze_patents()` on a small set of synthetic patent
disclosures shaped exactly like the LLM pipeline's `patent_disclosures` output
(core/prompts.SCHEMA), and writes:

  - sample_research_output.json  — the full `research_outputs` payload
  - figures/*.png                — every generated figure, decoded from base64

This is useful for (a) verifying the research module end-to-end without a
running Ollama instance (it transparently falls back to a local TF-IDF
embedding if the configured embedding model is unavailable — see
research/embeddings.py), and (b) producing sample output/figures for review.

Run with:  python -m research.demo
"""

from __future__ import annotations

import asyncio
import base64
import json
import os

from research.engine import analyze_patents

SAMPLE_DISCLOSURES = [
    {
        "disclosure_number": 1,
        "explicit_data": {
            "project": "LTE-Advanced Mobility",
            "standards": ["TS 36.331 (v14.0.0)"],
            "applicant": "Northbridge Wireless Inc.",
            "application_number": "US16234511",
            "publication_number": "US20190123456A1",
            "country": "US",
            "title": "Method and apparatus for make-before-break RRC connection re-establishment",
        },
        "standards_analysis": {
            "3gpp_release_mapping": [{"release": "Rel-15", "confidence": 0.7, "reasoning": "Make-before-break terminology"}],
            "technical_domains": [{"domain": "RRC", "confidence": 0.85}],
            "network_layer_mapping": [{"layer": "RRC", "confidence": 0.8}],
            "likely_protocol_components": ["RRC connection re-establishment", "handover signalling"],
            "standard_descriptions": [{
                "specification": "TS 36.331",
                "description": "Procedure for re-establishing an RRC connection with reduced interruption time using a make-before-break mechanism during handover failure recovery.",
                "functional_role": "Mobility / connection management",
                "technology_area": "RRC",
            }],
        },
        "sep_analysis": {
            "likely_sep_strength": "High",
            "probable_implementation_scope": "Baseband modem RRC layer in UE and eNB",
            "implementation_dependency": "High",
            "probable_device_impact": ["UE modem"],
            "probable_network_impact": ["eNB RRC stack"],
            "estimated_licensing_relevance": "High",
            "confidence": 0.78,
        },
        "geographic_analysis": {
            "jurisdiction_strength": "High",
            "enforcement_significance": "High",
            "regional_strategy_inference": "US-focused enforcement",
        },
        "inferred_fields": {
            "probable_title": "Make-before-break RRC connection re-establishment",
            "probable_technology_focus": "Reducing handover interruption time via pre-established target cell connection before releasing the source connection.",
            "probable_use_cases": ["Seamless mobility for voice and data sessions"],
            "probable_claim_scope": "Covers UE and network procedures for maintaining a source RRC connection while establishing a target connection prior to handover completion.",
        },
    },
    {
        "disclosure_number": 2,
        "explicit_data": {
            "project": "NR Initial Access",
            "standards": ["TS 38.321"],
            "applicant": "Helion Semiconductor",
            "application_number": "US17889201",
            "publication_number": "US20210345678A1",
            "country": "US",
            "title": "Two-step random access procedure combining preamble and payload transmission",
        },
        "standards_analysis": {
            "3gpp_release_mapping": [{"release": "Rel-16", "confidence": 0.8, "reasoning": "2-step RACH / MsgA-MsgB terminology"}],
            "technical_domains": [{"domain": "MAC", "confidence": 0.88}],
            "network_layer_mapping": [{"layer": "MAC", "confidence": 0.85}],
            "likely_protocol_components": ["Random access", "MsgA", "MsgB"],
            "standard_descriptions": [{
                "specification": "TS 38.321",
                "description": "Two-step random access type combining the preamble and payload (MsgA) with a single response (MsgB) to reduce latency for small uplink transmissions.",
                "functional_role": "Initial access / latency reduction",
                "technology_area": "MAC",
            }],
        },
        "sep_analysis": {
            "likely_sep_strength": "High",
            "probable_implementation_scope": "UE and gNB MAC layer random access procedures",
            "implementation_dependency": "High",
            "probable_device_impact": ["UE modem"],
            "probable_network_impact": ["gNB scheduler"],
            "estimated_licensing_relevance": "High",
            "confidence": 0.82,
        },
        "geographic_analysis": {
            "jurisdiction_strength": "High",
            "enforcement_significance": "Medium",
            "regional_strategy_inference": "Global filing strategy",
        },
        "inferred_fields": {
            "probable_title": "Two-step random access for latency reduction",
            "probable_technology_focus": "Combining preamble and payload in a single uplink transmission (MsgA) with a consolidated network response (MsgB).",
            "probable_use_cases": ["URLLC small data transmission", "Initial access latency reduction"],
            "probable_claim_scope": "Covers UE procedures for MsgA construction and gNB procedures for MsgB response in two-step random access.",
        },
    },
    {
        "disclosure_number": 3,
        "explicit_data": {
            "project": "Massive MIMO Feedback",
            "standards": ["TS 38.214"],
            "applicant": "Meridian Mobile Systems",
            "application_number": "US17112233",
            "publication_number": "US20220098765A1",
            "country": "US",
            "title": "Enhanced Type-II CSI codebook with frequency-domain compression",
        },
        "standards_analysis": {
            "3gpp_release_mapping": [{"release": "Rel-16", "confidence": 0.75, "reasoning": "Enhanced Type-II codebook terminology"}],
            "technical_domains": [{"domain": "PHY", "confidence": 0.9}],
            "network_layer_mapping": [{"layer": "PHY", "confidence": 0.85}],
            "likely_protocol_components": ["CSI reporting", "PDSCH", "MU-MIMO"],
            "standard_descriptions": [{
                "specification": "TS 38.214",
                "description": "Frequency-domain compressed CSI feedback for enhanced Type-II codebooks, improving multi-user MIMO spectral efficiency for massive MIMO deployments.",
                "functional_role": "Channel state feedback",
                "technology_area": "PHY",
            }],
        },
        "sep_analysis": {
            "likely_sep_strength": "Medium",
            "probable_implementation_scope": "UE PHY layer CSI computation and gNB scheduler",
            "implementation_dependency": "Medium",
            "probable_device_impact": ["UE modem"],
            "probable_network_impact": ["gNB MIMO scheduler"],
            "estimated_licensing_relevance": "Medium",
            "confidence": 0.65,
        },
        "geographic_analysis": {
            "jurisdiction_strength": "Medium",
            "enforcement_significance": "Medium",
            "regional_strategy_inference": "EU and US filings",
        },
        "inferred_fields": {
            "probable_title": "Frequency-domain compressed CSI feedback",
            "probable_technology_focus": "Compressing CSI feedback in the frequency domain for higher-resolution multi-user MIMO precoding.",
            "probable_use_cases": ["Massive MIMO downlink throughput improvement"],
            "probable_claim_scope": "Covers UE-side CSI compression and gNB-side reconstruction for Type-II codebook reporting.",
        },
    },
    {
        "disclosure_number": 4,
        "explicit_data": {
            "project": "5G Core Slicing",
            "standards": [],
            "applicant": "Continental Core Networks Ltd.",
            "application_number": "US17556677",
            "publication_number": "US20230011223A1",
            "country": "US",
            "title": "Method for limiting registered devices per network slice based on operator policy",
        },
        "standards_analysis": {
            "3gpp_release_mapping": [{"release": "Rel-16", "confidence": 0.6, "reasoning": "Slice admission control terminology"}],
            "technical_domains": [{"domain": "Core Network", "confidence": 0.8}],
            "network_layer_mapping": [{"layer": "NAS/Core", "confidence": 0.7}],
            "likely_protocol_components": ["Network slicing", "AMF", "NSSAI"],
            "standard_descriptions": [{
                "specification": "Unclear — blanket declaration",
                "description": "Limiting the number of registered UEs or established PDU sessions per network slice instance based on operator-configured policy thresholds.",
                "functional_role": "Slice admission control",
                "technology_area": "Core Network",
            }],
        },
        "sep_analysis": {
            "likely_sep_strength": "Medium",
            "probable_implementation_scope": "AMF / Core network slice management functions",
            "implementation_dependency": "Medium",
            "probable_device_impact": [],
            "probable_network_impact": ["AMF", "NSACF"],
            "estimated_licensing_relevance": "Medium",
            "confidence": 0.55,
        },
        "geographic_analysis": {
            "jurisdiction_strength": "Medium",
            "enforcement_significance": "Low",
            "regional_strategy_inference": "US filing only",
        },
        "inferred_fields": {
            "probable_title": "Network slice admission control",
            "probable_technology_focus": "Operator-configurable thresholds limiting registrations and PDU sessions per network slice.",
            "probable_use_cases": ["Multi-tenant 5G core network resource protection"],
            "probable_claim_scope": "Covers AMF/NSACF procedures for evaluating and enforcing per-slice registration limits.",
        },
    },
    {
        "disclosure_number": 5,
        "explicit_data": {
            "project": "NR Mobility Robustness",
            "standards": ["TS 38.331"],
            "applicant": "Northbridge Wireless Inc.",
            "application_number": "US17778899",
            "publication_number": "US20230199887A1",
            "country": "US",
            "title": "UE-evaluated conditional handover execution based on pre-configured target cell conditions",
        },
        "standards_analysis": {
            "3gpp_release_mapping": [{"release": "Rel-16", "confidence": 0.82, "reasoning": "Conditional handover terminology"}],
            "technical_domains": [{"domain": "RRC", "confidence": 0.9}],
            "network_layer_mapping": [{"layer": "RRC", "confidence": 0.88}],
            "likely_protocol_components": ["Conditional handover", "RRC reconfiguration", "PSCell change"],
            "standard_descriptions": [{
                "specification": "TS 38.331",
                "description": "Conditional Handover and Conditional PSCell Change/Addition, where the UE evaluates and executes a pre-configured reconfiguration once execution conditions are met.",
                "functional_role": "Mobility robustness",
                "technology_area": "RRC",
            }],
        },
        "sep_analysis": {
            "likely_sep_strength": "High",
            "probable_implementation_scope": "UE and gNB RRC layer mobility procedures",
            "implementation_dependency": "High",
            "probable_device_impact": ["UE modem"],
            "probable_network_impact": ["gNB RRC stack"],
            "estimated_licensing_relevance": "High",
            "confidence": 0.8,
        },
        "geographic_analysis": {
            "jurisdiction_strength": "High",
            "enforcement_significance": "High",
            "regional_strategy_inference": "Global filing strategy",
        },
        "inferred_fields": {
            "probable_title": "Conditional handover execution",
            "probable_technology_focus": "UE autonomously executing a pre-configured handover once network-defined trigger conditions are satisfied.",
            "probable_use_cases": ["Handover robustness in high-mobility scenarios"],
            "probable_claim_scope": "Covers UE evaluation logic and network configuration signalling for conditional reconfiguration execution.",
        },
    },
    {
        "disclosure_number": 6,
        "explicit_data": {
            "project": "NTN Registration",
            "standards": ["TS 24.501 (v15.3.0)"],
            "applicant": "Helion Semiconductor",
            "application_number": "US17990011",
            "publication_number": "US20240056789A1",
            "country": "US",
            "title": "UE registration procedure for non-terrestrial NG-RAN access with extended timers",
        },
        "standards_analysis": {
            "3gpp_release_mapping": [{"release": "Rel-16", "confidence": 0.7, "reasoning": "Satellite NG-RAN registration"}],
            "technical_domains": [{"domain": "NAS", "confidence": 0.85}],
            "network_layer_mapping": [{"layer": "NAS", "confidence": 0.82}],
            "likely_protocol_components": ["Registration procedure", "AMF", "satellite access"],
            "standard_descriptions": [{
                "specification": "TS 24.501",
                "description": "UE registration via satellite NG-RAN access, including extended timer values to accommodate long propagation delay during the registration procedure.",
                "functional_role": "Mobility management",
                "technology_area": "NAS",
            }],
        },
        "sep_analysis": {
            "likely_sep_strength": "Medium",
            "probable_implementation_scope": "UE and AMF NAS registration signalling",
            "implementation_dependency": "Medium",
            "probable_device_impact": ["UE modem"],
            "probable_network_impact": ["AMF"],
            "estimated_licensing_relevance": "Medium",
            "confidence": 0.6,
        },
        "geographic_analysis": {
            "jurisdiction_strength": "Medium",
            "enforcement_significance": "Medium",
            "regional_strategy_inference": "US and EU filings",
        },
        "inferred_fields": {
            "probable_title": "Satellite NG-RAN registration with extended timers",
            "probable_technology_focus": "Adapting NAS registration timers for satellite access propagation delay.",
            "probable_use_cases": ["Non-terrestrial network connectivity"],
            "probable_claim_scope": "Covers UE and AMF timer configuration and registration message handling for satellite access.",
        },
    },
    {
        "disclosure_number": 7,
        "explicit_data": {
            "project": "URLLC Retransmission",
            "standards": ["TS 36.321"],
            "applicant": "Meridian Mobile Systems",
            "application_number": "US18001122",
            "publication_number": "US20240233445A1",
            "country": "US",
            "title": "HARQ retransmission scheme for configured-grant PUSCH supporting ultra-reliable low-latency communication",
        },
        "standards_analysis": {
            "3gpp_release_mapping": [{"release": "Rel-16", "confidence": 0.65, "reasoning": "Configured grant + URLLC HARQ terminology"}],
            "technical_domains": [{"domain": "MAC", "confidence": 0.83}],
            "network_layer_mapping": [{"layer": "MAC", "confidence": 0.8}],
            "likely_protocol_components": ["HARQ", "Configured Grant PUSCH", "URLLC"],
            "standard_descriptions": [{
                "specification": "TS 36.321 (cited) — content closely matches NR MAC HARQ enhancements",
                "description": "HARQ process handling for configured grant PUSCH retransmissions with multiple active HARQ feedback codebooks to reduce latency for ultra-reliable low-latency communication.",
                "functional_role": "Retransmission scheduling",
                "technology_area": "MAC",
            }],
        },
        "sep_analysis": {
            "likely_sep_strength": "Medium",
            "probable_implementation_scope": "UE and gNB MAC HARQ entities",
            "implementation_dependency": "Medium",
            "probable_device_impact": ["UE modem"],
            "probable_network_impact": ["gNB scheduler"],
            "estimated_licensing_relevance": "Medium",
            "confidence": 0.58,
        },
        "geographic_analysis": {
            "jurisdiction_strength": "Medium",
            "enforcement_significance": "Medium",
            "regional_strategy_inference": "US filing",
        },
        "inferred_fields": {
            "probable_title": "Configured-grant PUSCH HARQ retransmission for URLLC",
            "probable_technology_focus": "Multiple active HARQ feedback codebooks for configured-grant retransmissions targeting URLLC latency budgets.",
            "probable_use_cases": ["Industrial automation URLLC"],
            "probable_claim_scope": "Covers MAC-layer HARQ process management for configured grant retransmissions.",
        },
    },
    {
        "disclosure_number": 8,
        "explicit_data": {
            "project": "Device UI",
            "standards": ["TS 38.300"],
            "applicant": "Lumen Devices Corp.",
            "application_number": "US18112233",
            "publication_number": "US20240345566A1",
            "country": "US",
            "title": "Method for adjusting display brightness based on ambient light sensor readings",
        },
        "standards_analysis": {
            "3gpp_release_mapping": [{"release": "Unclear", "confidence": 0.2, "reasoning": "No clear 3GPP mapping"}],
            "technical_domains": [{"domain": "Architecture", "confidence": 0.2}],
            "network_layer_mapping": [{"layer": "Application", "confidence": 0.1}],
            "likely_protocol_components": [],
            "standard_descriptions": [{
                "specification": "TS 38.300",
                "description": "General mobile device feature unrelated to RAN protocol operation; weak/borderline standards relevance.",
                "functional_role": "Device UX",
                "technology_area": "Application layer",
            }],
        },
        "sep_analysis": {
            "likely_sep_strength": "Low",
            "probable_implementation_scope": "Device application processor",
            "implementation_dependency": "Low",
            "probable_device_impact": ["UE display driver"],
            "probable_network_impact": [],
            "estimated_licensing_relevance": "Low",
            "confidence": 0.2,
        },
        "geographic_analysis": {
            "jurisdiction_strength": "Low",
            "enforcement_significance": "Low",
            "regional_strategy_inference": "US filing only",
        },
        "inferred_fields": {
            "probable_title": "Ambient-light-based display brightness adjustment",
            "probable_technology_focus": "Adjusting screen brightness using ambient light sensor data on a mobile device.",
            "probable_use_cases": ["Battery life optimization", "Display readability"],
            "probable_claim_scope": "Covers sensor-driven display brightness control loops on a mobile device.",
        },
    },
]


async def main() -> None:
    """Run the research engine on the bundled sample disclosures and write the JSON result and figures
    to research/_demo_output/ (developer demo; `python -m research.demo`).
    """
    outputs = await analyze_patents(SAMPLE_DISCLOSURES, generate_figures=True)

    out_dir = os.path.join(os.path.dirname(__file__), "_demo_output")
    fig_dir = os.path.join(out_dir, "figures")
    os.makedirs(fig_dir, exist_ok=True)

    figures = outputs.pop("figures") or {}
    figure_meta = {}
    for key, fig in figures.items():
        if "image" not in fig:
            figure_meta[key] = fig
            continue
        b64 = fig["image"].split(",", 1)[1]
        path = os.path.join(fig_dir, f"{key}.png")
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        figure_meta[key] = {"title": fig["title"], "caption": fig["caption"], "file": f"figures/{key}.png"}

    outputs["figures"] = figure_meta

    with open(os.path.join(out_dir, "sample_research_output.json"), "w") as f:
        json.dump(outputs, f, indent=2)

    print(f"Embedder mode: {outputs['meta']['embedder_mode']}")
    print(f"Patents analyzed: {outputs['meta']['n_patents']}")
    print(f"Figures generated: {list(figure_meta.keys())}")
    print(f"Metrics: {outputs['metrics']}")
    print(f"Wrote output to: {out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
