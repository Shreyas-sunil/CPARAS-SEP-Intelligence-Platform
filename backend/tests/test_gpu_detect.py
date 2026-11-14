"""
Tests for core/gpu_detect.py's pure parsing functions.

Run: python -m pytest backend/tests/test_gpu_detect.py -q
 or: python3 backend/tests/_run_all.py

Only the pure parsing functions are covered here (no subprocess calls) —
`detect_gpu`/`_detect_nvidia`/`_detect_apple_unified_memory` shell out to
`nvidia-smi`/`sysctl`, which aren't available (or meaningful to assert on)
in a CI sandbox. Those thin wrappers are exercised manually against real
hardware instead, matching how this codebase already treats other
subprocess/network-calling code (e.g. research/build_corpus.py's crawlers).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.gpu_detect import (  # noqa: E402
    _parse_nvidia_smi_output,
    _parse_sysctl_memsize,
)


# ── nvidia-smi parsing ────────────────────────────────────────────────────────

def test_parses_single_gpu_output():
    out = "NVIDIA GeForce RTX 3080, 10240\n"
    result = _parse_nvidia_smi_output(out)
    assert result == {"detected": True, "name": "NVIDIA GeForce RTX 3080", "vram_gb": 10.0, "source": "nvidia-smi"}


def test_multi_gpu_output_uses_the_largest_gpu_not_the_sum():
    """Multi-GPU VRAM doesn't pool for a single model's weights the way
    system RAM does — summing across GPUs would overstate what a single-
    GPU-resident model can actually use. v0.4.2: the LARGEST GPU is used
    (an external RTX is often listed after a small internal one)."""
    out = "NVIDIA GeForce RTX 4090, 24564\nNVIDIA GeForce RTX 3060, 12288\n"
    result = _parse_nvidia_smi_output(out)
    assert result["name"] == "NVIDIA GeForce RTX 4090"
    assert result["vram_gb"] == 24.0


def test_handles_trailing_newline_and_whitespace():
    out = "  NVIDIA GeForce RTX 3090 , 24576 \n\n"
    result = _parse_nvidia_smi_output(out)
    assert result["name"] == "NVIDIA GeForce RTX 3090"
    assert result["vram_gb"] == 24.0


def test_empty_output_returns_none():
    assert _parse_nvidia_smi_output("") is None
    assert _parse_nvidia_smi_output("   \n") is None


def test_malformed_output_returns_none():
    assert _parse_nvidia_smi_output("not csv at all") is None
    assert _parse_nvidia_smi_output("Name Only") is None
    assert _parse_nvidia_smi_output("GPU Name, not-a-number") is None


def test_zero_or_negative_vram_returns_none():
    """A driver/parsing glitch reporting 0 MB VRAM must not produce a
    confidently wrong 'detected: True, vram_gb: 0.0' recommendation base."""
    assert _parse_nvidia_smi_output("Some GPU, 0") is None


# ── sysctl (Apple unified memory) parsing ────────────────────────────────────

def test_parses_sysctl_memsize_with_chip_name():
    result = _parse_sysctl_memsize("34359738368", "Apple M2 Pro")
    assert result == {
        "detected": True, "name": "Apple M2 Pro", "vram_gb": 32.0,
        "source": "unified-memory (sysctl)",
    }


def test_sysctl_falls_back_to_generic_name_when_chip_name_missing():
    result = _parse_sysctl_memsize("17179869184", None)
    assert result["vram_gb"] == 16.0
    assert "Apple Silicon" in result["name"]


def test_sysctl_non_numeric_output_returns_none():
    assert _parse_sysctl_memsize("not a number", "Apple M2") is None
    assert _parse_sysctl_memsize("", "Apple M2") is None


def test_sysctl_zero_returns_none():
    assert _parse_sysctl_memsize("0", "Apple M2") is None
