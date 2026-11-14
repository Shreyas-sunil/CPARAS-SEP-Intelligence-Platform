"""v0.4.4 - the dataset step of scripts/common.ps1 (find the dataset beside the app, write backend\\.env).
Runs the real PowerShell functions; skipped when no PowerShell (pwsh / powershell) is installed."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

PS = shutil.which("pwsh") or shutil.which("powershell")
APP = Path(__file__).resolve().parent.parent.parent          # project root (contains scripts/ and backend/)
COMMON = APP / "scripts" / "common.ps1"
pytestmark = pytest.mark.skipif(PS is None, reason="PowerShell not installed")


def _ps(script: str) -> str:
    code = f". '{COMMON}'\n" + script
    r = subprocess.run([PS, "-NoProfile", "-NonInteractive", "-Command", code], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr + r.stdout
    return r.stdout.strip()


def _marker_for_app():
    return "spec_reference_3gpp.json" if "Categoriser" in APP.name else "etsi_dataset.jsonl"


def test_set_dotenv_value_cases(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b"A=1\r\nCPARAS_DATASET_ROOT=\r\nB=2\r\n")
    out = _ps(f"Set-DotEnvValue '{env}' 'CPARAS_DATASET_ROOT' 'D:\\x y\\build_v4' $false")
    assert out == "filled"
    assert env.read_bytes().decode("utf-8") == "A=1\r\nCPARAS_DATASET_ROOT=D:\\x y\\build_v4\r\nB=2\r\n"      # CRLF kept, other keys untouched
    assert _ps(f"Set-DotEnvValue '{env}' 'CPARAS_DATASET_ROOT' 'E:\\other' $false") == "kept"            # non-empty is never overwritten
    assert b"E:" not in env.read_bytes()
    assert _ps(f"Set-DotEnvValue '{env}' 'CPARAS_DATASET_ROOT' 'E:\\other' $true") == "replaced"           # unless explicit
    assert _ps(f"Set-DotEnvValue '{env}' 'NEW_KEY' 'v' $false") == "added"
    assert env.read_bytes().endswith(b"NEW_KEY=v\r\n")
    assert not env.read_bytes().startswith(b"\xef\xbb\xbf")                                                 # no BOM


def test_find_dataset_beside_the_app(tmp_path):
    m = _marker_for_app()
    app = tmp_path / APP.name
    app.mkdir()
    other = tmp_path / "SomethingElse" / "nested"
    other.mkdir(parents=True)
    ds = tmp_path / "cparas_dataset" / "build_v4"
    ds.mkdir(parents=True)
    (ds / m).write_text("{}", encoding="utf-8")
    out = _ps(f"Find-CparasDataset '{app}' @('{m}') | ForEach-Object {{ $_ }}")
    assert out.splitlines()[0] == str(ds)
    # not found -> empty result, no exception
    ds2 = tmp_path / "empty"
    ds2.mkdir()
    shutil.rmtree(tmp_path / "cparas_dataset")
    assert _ps(f"@(Find-CparasDataset '{app}' @('{m}')).Count") == "0"


def test_dataset_folder_requires_marker(tmp_path):
    m = _marker_for_app()
    (tmp_path / m).write_text("{}", encoding="utf-8")
    assert _ps(f"Test-DatasetFolder '{tmp_path}' @('{m}')") == "True"
    assert _ps(f"Test-DatasetFolder '{tmp_path}' @('nope.json')") == "False"
