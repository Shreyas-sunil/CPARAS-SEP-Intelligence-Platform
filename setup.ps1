<#
.SYNOPSIS
  First-time setup for CPARAS Platform on Windows (also works with PowerShell 7 on Linux/macOS).

.DESCRIPTION
  Checks Python (3.10 - 3.12) and Node.js 18+, creates backend\.venv and installs the
  Python packages, creates backend\.env from .env.example (never overwrites
  an existing one), installs the frontend packages, checks Ollama and the
  configured model, Tesseract OCR, and downloads the embedding model for offline use.
  Safe to re-run.

  3GPP dataset: keep the dataset build folder (e.g. cparas_dataset\build_v4) in the same parent folder as the
  CPARAS app folders, e.g.  <parent>\CPARAS_Platform_Revamped, <parent>\CPARAS_ISLD, <parent>\Initial_Categoriser,
  <parent>\cparas_dataset\build_v4. Setup finds it, saves the path in backend\.env and checks it. The dataset is
  never copied or modified.

.PARAMETER PullModel
  Download OLLAMA_MODEL with `ollama pull` if it is not installed (several GB).
.PARAMETER SkipEmbeddingDownload
  Do not download the sentence-transformers embedding model (EMBEDDING_MODEL).

.PARAMETER DatasetPath
  Folder with the validated 3GPP dataset build (the one containing etsi_dataset.jsonl). Normally NOT needed:
  setup looks for it next to this app folder (see below) and writes CPARAS_DATASET_ROOT into backend\.env.
  Giving -DatasetPath overrides a CPARAS_DATASET_ROOT already in backend\.env.
.PARAMETER SkipDataset
  Skip the whole 3GPP dataset step.
.PARAMETER ValidateDataset
  Also run the full streaming validation of the dataset (reads the multi-GB JSONL once; several minutes).
.PARAMETER ImportDataset
  Import + index a small trial batch (ETSI_DEFAULT_IMPORT_LIMIT, 25 by default) of the dataset.
.PARAMETER ImportLimit
  With -ImportDataset: import this many documents instead.
.PARAMETER ImportAll
  Import + index the whole selected dataset (hours). Resumable: re-run to continue.
.PARAMETER Versions
  Which versions of each specification to import: all (default), latest, latest-per-release.
  Records whose version is Unknown are always kept.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\setup.ps1 -PullModel
#>
param(
    [switch]$PullModel,
    [switch]$NoCudaTorch,
    [switch]$SkipEmbeddingDownload,
    [string]$DatasetPath = '',
    [switch]$SkipDataset,
    [switch]$ValidateDataset,
    [switch]$ImportDataset,
    [int]$ImportLimit = 0,
    [switch]$ImportAll,
    [ValidateSet('all', 'latest', 'latest-per-release')]
    [string]$Versions = 'all'
)

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $Root 'scripts\common.ps1')
$Backend = Join-Path $Root 'backend'
$Frontend = Join-Path $Root 'frontend'
$script:LogDir = Join-Path $Root 'logs'
New-Item -ItemType Directory -Force -Path $script:LogDir | Out-Null
$problems = @()

Write-Host ''
Write-Host '  CPARAS Platform - setup' -ForegroundColor White
Write-Host '  ------------------------------------------------------------'

# ── Python ─────────────────────────────────────────────────────────────────
Write-Step 'Checking Python'
$pyInfo = Get-PythonCommand
if (-not $pyInfo) {
    Write-Fail 'Python was not found. Install Python 3.10 - 3.12 from https://www.python.org/downloads/ (tick "Add python.exe to PATH").'
    exit 1
}
if (-not (Test-VersionAtLeast $pyInfo.Version 3 10)) {
    Write-Fail "Python $($pyInfo.Version) is too old; CPARAS Platform needs Python 3.10 - 3.12."
    exit 1
}
if (Test-VersionAtLeast $pyInfo.Version 3 13) {
    Write-Warn2 "Python $($pyInfo.Version) is newer than the tested range (3.10 - 3.12); some pinned packages may not have wheels for it yet. If installation fails, install Python 3.12 and re-run (the 'py' launcher will pick it)."
}
Write-Ok "Python $($pyInfo.Version) ($($pyInfo.Exe) $($pyInfo.Pre -join ' '))"

# ── Node.js ────────────────────────────────────────────────────────────────
Write-Step 'Checking Node.js'
$node = Get-Command node -ErrorAction SilentlyContinue
$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $node -or -not $npm) {
    Write-Fail 'Node.js (with npm) was not found. Install Node.js 18 or newer (LTS) from https://nodejs.org and re-run.'
    exit 1
}
$nodeVer = (& node --version).Trim()
if ([int]($nodeVer.TrimStart('v').Split('.')[0]) -lt 18) { Write-Fail "Node.js $nodeVer is too old; install Node.js 18+."; exit 1 }
Write-Ok "Node.js $nodeVer"

# ── Backend virtual environment + packages ─────────────────────────────────
Write-Step 'Setting up the backend (Python virtual environment + packages)'
$py = Get-VenvPython $Backend
if (-not (Test-Path $py)) {
    & $pyInfo.Exe @($pyInfo.Pre) -m venv (Join-Path $Backend '.venv')
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $py)) { Write-Fail 'Could not create backend\.venv.'; exit 1 }
    Write-Ok 'Created backend\.venv'
} else {
    Write-Ok 'backend\.venv already exists'
}
& $py -m pip install --upgrade pip --disable-pip-version-check -q
Write-Info 'Installing Python packages (this can take several minutes the first time)...'
& $py -m pip install -r (Join-Path $Backend 'requirements.txt') --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Write-Fail 'pip install failed - see the messages above.'; exit 1 }
& $py -c "import fastapi, uvicorn, sqlalchemy, httpx; print('imports ok')" | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Fail 'Backend packages were installed but cannot be imported.'; exit 1 }
Write-Ok 'Backend packages installed'

$envFile = Join-Path $Backend '.env'
if (Test-Path $envFile) {
    Write-Ok 'backend\.env already exists - left unchanged'
} else {
    Copy-Item (Join-Path $Backend '.env.example') $envFile
    Write-Ok 'Created backend\.env from .env.example (edit it to change the model, ports, ...)'
}
$envVals = Read-DotEnv $envFile
# v0.4.2: add the new GPU/reproducibility settings to an existing .env
# (existing values are never changed).
$added = @()
foreach ($kv in @(@('OLLAMA_FLASH_ATTENTION', 'true'), @('OLLAMA_KV_CACHE_TYPE', 'q8_0'))) {
    if (-not $envVals.ContainsKey($kv[0])) {
        $raw = [System.IO.File]::ReadAllText($envFile)
        $sep = if ($raw.Length -gt 0 -and -not $raw.EndsWith("`n")) { [Environment]::NewLine } else { '' }
        [System.IO.File]::AppendAllText($envFile, "$sep$($kv[0])=$($kv[1])" + [Environment]::NewLine)
        $added += $kv[0]
    }
}
if ($added.Count) { Write-Ok "Added to backend\.env: $($added -join ', ')"; $envVals = Read-DotEnv $envFile }
if ((Get-Setting $envVals 'EMBEDDING_DEVICE' 'auto') -eq 'cpu') {
    Write-Info 'backend\.env has EMBEDDING_DEVICE=cpu - change it to auto to run embeddings on the GPU.'
}

# ── GPU: CUDA build of PyTorch for the embedding model (v0.4.2) ─────────────
# pip installs the CPU-only PyTorch on Windows by default, so the embedding
# model (BAAI/bge-m3) never used the GPU. With an NVIDIA driver present,
# install the CUDA build. Skip with -NoCudaTorch.
Write-Step 'Checking the NVIDIA GPU'
$hasNvidia = Show-NvidiaGpu
if ($hasNvidia -and -not $NoCudaTorch) {
    & $py -c "import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Ok 'PyTorch can use the GPU (CUDA build already installed)'
    } else {
        Write-Info 'Installing the CUDA build of PyTorch (about 2.5 GB, one time)...'
        & $py -m pip install --upgrade "torch>=2.7,<3.0" --index-url https://download.pytorch.org/whl/cu128 --disable-pip-version-check
        & $py -c "import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { Write-Ok 'PyTorch now uses the GPU for embeddings' }
        else { Write-Warn2 'CUDA PyTorch is not usable here - embeddings stay on the CPU (analysis still works).'; $problems += 'embeddings on CPU' }
    }
}

# Ollama server options from backend\.env, stored as user environment
# variables so the Ollama tray app also picks them up (after it restarts).
$srvEnv = Set-OllamaServerEnv $envVals
if ($script:OnWindows) {
    if ($srvEnv.FlashAttention -and -not [Environment]::GetEnvironmentVariable('OLLAMA_FLASH_ATTENTION', 'User')) {
        [Environment]::SetEnvironmentVariable('OLLAMA_FLASH_ATTENTION', '1', 'User')
        Write-Ok 'Stored OLLAMA_FLASH_ATTENTION=1 for the Ollama app (takes effect after Ollama restarts)'
    }
    if ($srvEnv.KvCacheType -ne 'f16' -and [Environment]::GetEnvironmentVariable('OLLAMA_KV_CACHE_TYPE', 'User') -ne $srvEnv.KvCacheType) {
        [Environment]::SetEnvironmentVariable('OLLAMA_KV_CACHE_TYPE', $srvEnv.KvCacheType, 'User')
        Write-Ok "Stored OLLAMA_KV_CACHE_TYPE=$($srvEnv.KvCacheType) for the Ollama app (takes effect after Ollama restarts)"
    }
}

# ── Frontend packages ──────────────────────────────────────────────────────
Write-Step 'Installing frontend packages (npm ci)'
Push-Location $Frontend
try {
    & npm ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { Write-Fail 'npm ci failed - see the messages above.'; exit 1 }
} finally { Pop-Location }
Write-Ok 'Frontend packages installed'

# ── Ollama + model ─────────────────────────────────────────────────────────
$OllamaUrl = (Get-Setting $envVals 'OLLAMA_BASE_URL' 'http://127.0.0.1:11434').TrimEnd('/')
$OllamaModel = Get-Setting $envVals 'OLLAMA_MODEL' 'qwen2.5:7b'
Write-Step "Checking Ollama ($OllamaUrl)"
if (-not (Find-Ollama) -and -not (Invoke-LocalGet "$OllamaUrl/api/tags" 3).Ok) {
    Write-Warn2 'Ollama is not installed. Install it from https://ollama.com/download (CPARAS ISLD analysis needs it).'
    $problems += 'Ollama not installed'
} elseif (Ensure-Ollama $OllamaUrl 1 60) {
    $haveModel = Show-OllamaModels $OllamaUrl $OllamaModel $true
    if (-not $haveModel) {
        if ($PullModel) {
            Write-Info "Downloading $OllamaModel (this can take a while)..."
            & (Find-Ollama) pull $OllamaModel
            if ($LASTEXITCODE -eq 0) { Write-Ok "$OllamaModel installed" } else { Write-Fail "ollama pull $OllamaModel failed"; $problems += "model $OllamaModel missing" }
        } else {
            Write-Info "Re-run with -PullModel to download it, or run:  ollama pull $OllamaModel"
            $problems += "model $OllamaModel not installed"
        }
    }
} else {
    $problems += 'Ollama not reachable'
}

# ── OCR (Tesseract) ────────────────────────────────────────────────────────
Write-Step 'Checking Tesseract OCR (needed for scanned PDFs)'
$tessCfg = Get-Setting $envVals 'TESSERACT_CMD' ''
$tess = $null
if ($tessCfg -and (Test-Path $tessCfg)) { $tess = $tessCfg }
if (-not $tess) { $c = Get-Command tesseract -ErrorAction SilentlyContinue; if ($c) { $tess = $c.Source } }
if (-not $tess -and $script:OnWindows) {
    foreach ($cand in @("$env:ProgramFiles\Tesseract-OCR\tesseract.exe", "${env:ProgramFiles(x86)}\Tesseract-OCR\tesseract.exe", "$env:LOCALAPPDATA\Programs\Tesseract-OCR\tesseract.exe")) {
        if ($cand -and (Test-Path $cand)) { $tess = $cand; break }
    }
}
if ($tess) {
    Write-Ok "Tesseract found: $tess (auto-detected by the backend; TESSERACT_CMD only needed for other locations)"
} else {
    Write-Warn2 'Tesseract not found. Scanned (image-only) PDFs need it: Windows installer https://github.com/UB-Mannheim/tesseract/wiki (text PDFs, CSV and Excel work without it).'
    $problems += 'Tesseract OCR not installed (scanned PDFs only)'
}

# ── Embedding model (offline use) ──────────────────────────────────────────
$embModel = Get-Setting $envVals 'EMBEDDING_MODEL' 'BAAI/bge-m3'
if ($SkipEmbeddingDownload) {
    Write-Step "Skipping the embedding model download ($embModel)"
} else {
    Write-Step "Downloading the embedding model $embModel (~2 GB, once) for offline use"
    $code = "import sys; from huggingface_hub import snapshot_download; p = snapshot_download(repo_id=sys.argv[1]); print(p)"
    & $py -c $code $embModel
    if ($LASTEXITCODE -eq 0) { Write-Ok "Embedding model cached - the backend will load it without network access." }
    else {
        Write-Warn2 "Could not download $embModel (no internet?). Research analytics fall back to TF-IDF; semantic search / Text Matching need the model. Re-run setup when online, or copy the Hugging Face cache from another machine."
        $problems += "embedding model $embModel not downloaded"
    }
}

# ── 3GPP dataset (v0.4.4) ──────────────────────────────────────────────────
# Keep the dataset build folder beside the app folders; setup finds it, saves CPARAS_DATASET_ROOT in
# backend\.env and checks it. Importing is opt-in (-ImportDataset / -ImportAll).
if ($SkipDataset) {
    Write-Step 'Skipping the 3GPP dataset step (-SkipDataset)'
} else {
    Write-Step 'Checking the 3GPP dataset'
    $ds = Invoke-DatasetSetup -AppRoot $Root -Backend $Backend -EnvFile $envFile -Py $py -LogDir $script:LogDir `
        -Kind 'etsi' -Markers @('etsi_dataset.jsonl') -Explicit $DatasetPath `
        -Validate ([bool]$ValidateDataset) -Import ([bool]$ImportDataset) -ImportAll ([bool]$ImportAll) `
        -ImportLimit $ImportLimit -Versions $Versions
    foreach ($n in $ds.Notes) { $problems += $n }
    if ($ds.Root -and -not (Test-Path -LiteralPath (Join-Path $ds.Root 'standards_corpus_3gpp.json'))) {
        Write-Warn2 'standards_corpus_3gpp.json is missing - CPARAS Bulk will keep using its built-in illustrative corpus.'
    }
}

# ── Summary ────────────────────────────────────────────────────────────────
Write-Host ''
Write-Host '  ------------------------------------------------------------'
if ($problems.Count) {
    Write-Host '  Setup finished with warnings:' -ForegroundColor Yellow
    foreach ($p in $problems) { Write-Host "    - $p" -ForegroundColor Yellow }
} else {
    Write-Host '  Setup complete.' -ForegroundColor Green
}
Write-Host '  Start the app with:   powershell -ExecutionPolicy Bypass -File .\start.ps1   (or double-click start.cmd)'
# v0.5.0: CPARAS requires sign-in. The first Super Admin is created once, here or in the browser.
Write-Host '  First time? Create the Super Admin account:  double-click create_admin.cmd'
Write-Host '             (or open the app and use "First-time setup"; test accounts: seed_test_users.cmd)'
Write-Host '  ------------------------------------------------------------'
