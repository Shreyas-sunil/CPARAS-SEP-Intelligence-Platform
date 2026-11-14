"""
GPU Detection
=============
Best-effort local GPU/VRAM detection. Used for two things:

  1. the model picker's "Recommended for this machine" suggestion, and
  2. (v0.4.2) sizing each model's context window so model + KV cache fit in
     VRAM — see core/ollama_runtime.py. If this reports "no GPU", Ollama is
     NOT prevented from using the GPU; only the sizing falls back to a
     conservative default.

Probes, in order:

  * NVIDIA — `nvidia-smi`. v0.4.2: looked up on PATH **and** in the places
    the Windows driver installs it but does not always put on PATH
    (System32, `Program Files\\NVIDIA Corporation\\NVSMI`, the DriverStore
    `nv*` folders), plus WSL's `/usr/lib/wsl/lib`. Run in a worker thread
    with `subprocess.run`, so it works on every asyncio event loop (the
    asyncio subprocess API raises NotImplementedError on Windows' Selector
    loop, which `uvicorn --reload` uses — that used to be reported as
    "No GPU detected"). With several NVIDIA GPUs (e.g. an external RTX next
    to a small internal one) the one with the MOST VRAM is reported, which
    is also the one Ollama prefers.
  * Apple Silicon — unified memory via `sysctl hw.memsize`.
  * Windows (any vendor) — the display-adapter registry key
    `HardwareInformation.qwMemorySize`, read through PowerShell. Catches a
    GPU whose `nvidia-smi` is missing/broken.

Every probe is wrapped to never raise. When nothing is found, `error` says
what was tried so the UI can show *why* instead of a bare "No GPU detected".
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import platform
import shutil
import subprocess
import time
from typing import Optional

logger = logging.getLogger(__name__)

_SUBPROCESS_TIMEOUT_S = 8.0
_CACHE_TTL_S = 60.0
_cache: dict = {"at": 0.0, "value": None}


def _run_sync(cmd: list[str]) -> tuple[Optional[str], Optional[str]]:
    """Run a command; returns (stdout, None) or (stdout_or_None, reason)."""
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=_SUBPROCESS_TIMEOUT_S, **kwargs)
    except FileNotFoundError:
        return None, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return None, f"{os.path.basename(cmd[0])} timed out after {_SUBPROCESS_TIMEOUT_S:.0f}s"
    except Exception as e:  # detection must never raise
        return None, f"{os.path.basename(cmd[0])} failed: {type(e).__name__}: {e}"
    out = proc.stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        # nvidia-smi exits non-zero when ONE of several GPUs has a problem
        # but still prints the healthy ones — keep the output.
        return out, f"{os.path.basename(cmd[0])} exited {proc.returncode}" + (f": {err[0][:200]}" if err else "")
    return out, None


async def _run(cmd: list[str]) -> tuple[Optional[str], Optional[str]]:
    """Run a detection command in a worker thread; (stdout, error message)."""
    return await asyncio.to_thread(_run_sync, cmd)


# ── NVIDIA ───────────────────────────────────────────────────────────────────

def _nvidia_smi_candidates() -> list[str]:
    """Places nvidia-smi may live: PATH first, then the usual Windows driver locations (it is often not
    on PATH there).
    """
    seen: list[str] = []

    def add(p: Optional[str]) -> None:
        """Append an existing, not-yet-listed candidate path."""
        if p and p not in seen and (os.path.isfile(p) or p == "nvidia-smi"):
            seen.append(p)

    add(shutil.which("nvidia-smi"))
    if os.name == "nt":
        sysroot = os.environ.get("SystemRoot", r"C:\Windows")
        add(os.path.join(sysroot, "System32", "nvidia-smi.exe"))
        for pf in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                   os.environ.get("ProgramW6432", r"C:\Program Files")):
            add(os.path.join(pf, "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe"))
        for p in sorted(glob.glob(os.path.join(sysroot, "System32", "DriverStore", "FileRepository",
                                               "nv*", "nvidia-smi.exe")), reverse=True):
            add(p)
    else:
        for p in ("/usr/bin/nvidia-smi", "/usr/local/bin/nvidia-smi", "/usr/lib/wsl/lib/nvidia-smi"):
            add(p)
    if not seen:
        seen.append("nvidia-smi")
    return seen


def _parse_nvidia_smi_output(text: str) -> dict | None:
    """
    Parse `nvidia-smi --query-gpu=name,memory.total[,memory.free]
    --format=csv,noheader,nounits` output.

    With several GPUs the one with the most VRAM is returned: VRAM doesn't
    pool across GPUs for one model, and Ollama itself schedules a model onto
    the largest GPU first. `gpu_count` / `all_gpus` list every GPU seen.
    """
    if not text or not text.strip():
        return None
    gpus = []
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2 or not parts[0]:
            continue
        try:
            total_mb = float(parts[1])
        except ValueError:
            continue
        if total_mb <= 0:
            continue
        free_mb = None
        if len(parts) >= 3:
            try:
                free_mb = float(parts[2])
            except ValueError:
                free_mb = None
        gpus.append((parts[0], total_mb, free_mb))
    if not gpus:
        return None
    name, total_mb, free_mb = max(gpus, key=lambda g: g[1])
    out = {"detected": True, "name": name, "vram_gb": round(total_mb / 1024, 1), "source": "nvidia-smi"}
    if free_mb is not None:
        out["vram_free_gb"] = round(free_mb / 1024, 1)
    if len(gpus) > 1:
        out["gpu_count"] = len(gpus)
        out["all_gpus"] = [{"name": n, "vram_gb": round(t / 1024, 1)} for n, t, _ in gpus]
    return out


async def _detect_nvidia(reasons: list[str]) -> dict | None:
    """NVIDIA GPU name / total / free memory from the first working nvidia-smi; failures are collected
    in `reasons` for the UI.
    """
    for exe in _nvidia_smi_candidates():
        out, err = await _run([exe, "--query-gpu=name,memory.total,memory.free",
                               "--format=csv,noheader,nounits"])
        parsed = _parse_nvidia_smi_output(out) if out else None
        if parsed:
            if exe != "nvidia-smi" and os.path.basename(exe).lower().startswith("nvidia-smi"):
                parsed["nvidia_smi_path"] = exe
            return parsed
        if err:
            reasons.append(err)
    return None


# ── Apple Silicon ────────────────────────────────────────────────────────────

def _parse_sysctl_memsize(text: str, chip_name: str | None) -> dict | None:
    """Parse `sysctl -n hw.memsize` output (bytes, as a bare integer string)."""
    if not text or not text.strip().isdigit():
        return None
    total_bytes = int(text.strip())
    if total_bytes <= 0:
        return None
    total_gb = round(total_bytes / (1024 ** 3), 1)
    name = (chip_name or "").strip() or "Apple Silicon (unified memory)"
    return {"detected": True, "name": name, "vram_gb": total_gb, "source": "unified-memory (sysctl)"}


async def _detect_apple_unified_memory(reasons: list[str]) -> dict | None:
    """Apple Silicon: unified memory size and chip name via sysctl (None on other systems)."""
    if platform.system() != "Darwin":
        return None
    out, _ = await _run(["sysctl", "-n", "hw.memsize"])
    if not out:
        return None
    chip, _ = await _run(["sysctl", "-n", "machdep.cpu.brand_string"])
    return _parse_sysctl_memsize(out, chip)


# ── Windows registry (any vendor) ────────────────────────────────────────────

_WIN_GPU_PS = (
    "$k='HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}\\0*';"
    "Get-ItemProperty -Path $k -ErrorAction SilentlyContinue | "
    "Select-Object DriverDesc,@{n='mem';e={$_.'HardwareInformation.qwMemorySize'}} | "
    "ConvertTo-Json -Compress"
)


_OLLAMA_CAPABLE = ("nvidia", "geforce", "rtx", "quadro", "tesla", "radeon")


def _parse_windows_registry_gpus(text: str) -> dict | None:
    """Parse the PowerShell registry query output into the largest discrete GPU's name and memory
    (fallback when nvidia-smi is unavailable).
    """
    if not text or not text.strip():
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict):
        data = [data]
    gpus = []
    for d in data or []:
        if not isinstance(d, dict):
            continue
        name = (d.get("DriverDesc") or "").strip()
        mem = d.get("mem")
        if isinstance(mem, list):  # REG_BINARY comes back as a byte list
            try:
                mem = int.from_bytes(bytes(mem), "little")
            except Exception:
                mem = None
        try:
            mem = int(mem) if mem is not None else 0
        except (TypeError, ValueError):
            mem = 0
        # Only GPUs Ollama can run models on (NVIDIA CUDA / AMD ROCm);
        # integrated Intel graphics and the basic display driver don't count.
        if name and mem > 0 and any(t in name.lower() for t in _OLLAMA_CAPABLE):
            gpus.append((name, mem))
    if not gpus:
        return None
    name, mem = max(gpus, key=lambda g: g[1])
    out = {"detected": True, "name": name, "vram_gb": round(mem / 1024 ** 3, 1), "source": "windows-registry"}
    if len(gpus) > 1:
        out["gpu_count"] = len(gpus)
        out["all_gpus"] = [{"name": n, "vram_gb": round(m / 1024 ** 3, 1)} for n, m in gpus]
    return out


async def _detect_windows_registry(reasons: list[str]) -> dict | None:
    """Windows fallback GPU detection through the display-adapter registry keys."""
    if os.name != "nt":
        return None
    out, err = await _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _WIN_GPU_PS])
    parsed = _parse_windows_registry_gpus(out or "")
    if not parsed and err:
        reasons.append(err)
    return parsed


async def detect_gpu() -> dict:
    """
    Best-effort GPU/VRAM detection for this machine.

    Returns {"detected": True, "name", "vram_gb", "source", [vram_free_gb,
    gpu_count, all_gpus]} on success, or {"detected": False, "name": None,
    "vram_gb": None, "source": None, "error": "<what was tried>"}.
    """
    reasons: list[str] = []
    for detector in (_detect_nvidia, _detect_apple_unified_memory, _detect_windows_registry):
        try:
            result = await detector(reasons)
        except Exception as e:  # pragma: no cover - belt-and-braces
            reasons.append(f"{detector.__name__}: {type(e).__name__}: {e}")
            result = None
        if result:
            return result
    error = "; ".join(dict.fromkeys(reasons)) or "no NVIDIA driver (nvidia-smi) found"
    logger.info(f"GPU detection found no GPU: {error}")
    return {"detected": False, "name": None, "vram_gb": None, "source": None, "error": error}


async def detect_gpu_cached(max_age_s: float = _CACHE_TTL_S) -> dict:
    """detect_gpu() memoised for `max_age_s` (it shells out; the per-request
    context sizing must not run nvidia-smi on every LLM call)."""
    now = time.monotonic()
    if _cache["value"] is not None and now - _cache["at"] < max_age_s:
        return _cache["value"]
    value = await detect_gpu()
    _cache.update(at=now, value=value)
    return value
