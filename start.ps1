<#
.SYNOPSIS
  Start CPARAS Platform (backend + frontend) on Windows (also works with PowerShell 7 on Linux/macOS).

.DESCRIPTION
  1. Checks Python venv and Node.js          5. Starts the backend and waits for /api/health
  2. Checks Ollama; reuses a running server  6. Starts the frontend and waits for it
     (never launches a second one)           7. Prints URLs and a status summary
  3. Starts Ollama only if nothing answers   8. Ctrl+C stops backend + frontend
  4. Lists models, validates OLLAMA_MODEL       (Ollama is left running)

  Readiness is always checked by polling real endpoints with a timeout,
  never by sleeping a fixed number of seconds.

.PARAMETER BackendOnly
  Start only the backend (e.g. to use the API docs, or when serving a built frontend elsewhere).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\start.ps1
#>
param(
    [switch]$BackendOnly,
    [int]$BackendTimeoutSec = 180,
    [int]$OllamaTimeoutSec = 60
)

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $Root 'scripts\common.ps1')
$Backend = Join-Path $Root 'backend'
$Frontend = Join-Path $Root 'frontend'
$script:LogDir = Join-Path $Root 'logs'
New-Item -ItemType Directory -Force -Path $script:LogDir | Out-Null

Write-Host ''
Write-Host '  CPARAS Platform - start  (v0.5.0, launcher r4: sign-in + roles)' -ForegroundColor White
Write-Host '  ------------------------------------------------------------'

# ── 1. Python virtual environment ──────────────────────────────────────────
Write-Step 'Checking the Python environment'
$py = Get-VenvPython $Backend
if (-not (Test-Path $py)) {
    Write-Fail "backend virtual environment not found ($py)."
    Write-Info 'Run setup first:  powershell -ExecutionPolicy Bypass -File .\setup.ps1'
    exit 1
}
$pyver = & $py -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null
Write-Ok "Python $pyver ($py)"
$envFile = Join-Path $Backend '.env'
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $Backend '.env.example') $envFile
    Write-Info 'backend\.env was missing - created it from .env.example.'
}
$envVals = Read-DotEnv $envFile
$BackendPort = [int](Get-Setting $envVals 'PORT' '8000')
$BackendHost = Get-Setting $envVals 'HOST' '127.0.0.1'
$FrontendPort = 5173
$LlmBackend = Get-Setting $envVals 'LLM_BACKEND' 'ollama'
$OllamaUrl = (Get-Setting $envVals 'OLLAMA_BASE_URL' 'http://127.0.0.1:11434').TrimEnd('/')
$OllamaModel = Get-Setting $envVals 'OLLAMA_MODEL' 'qwen2.5:7b'
$Parallel = [int](Get-Setting $envVals 'OLLAMA_CONCURRENCY' '1')

# ── 2. Node.js ─────────────────────────────────────────────────────────────
if (-not $BackendOnly) {
    Write-Step 'Checking Node.js'
    $node = Get-Command node -ErrorAction SilentlyContinue
    if (-not $node) { Write-Fail 'Node.js not found. Install Node.js 18+ (https://nodejs.org) and re-run.'; exit 1 }
    $nodeVer = (& node --version).Trim()
    Write-Ok "Node.js $nodeVer"
    $vite = Join-Path $Frontend 'node_modules\vite\bin\vite.js'
    if (-not $script:OnWindows) { $vite = Join-Path $Frontend 'node_modules/vite/bin/vite.js' }
    $viteRel = if ($script:OnWindows) { 'node_modules\vite\bin\vite.js' } else { 'node_modules/vite/bin/vite.js' }
    if (-not (Test-Path $vite)) { Write-Fail 'Frontend dependencies missing. Run setup.ps1 (or: cd frontend; npm ci).'; exit 1 }
}

# ── 3-4. Ollama (reuse if running) + model validation ──────────────────────
if ($LlmBackend -eq 'ollama') {
    Write-Step 'Checking the NVIDIA GPU'
    [void](Show-NvidiaGpu)
    $srvEnv = Set-OllamaServerEnv $envVals
    Write-Step "Checking Ollama at $OllamaUrl"
    $ollamaOk = Ensure-Ollama $OllamaUrl $Parallel $OllamaTimeoutSec
    if ($ollamaOk -and -not $script:OllamaStartedByUs -and ($srvEnv.FlashAttention -or $srvEnv.KvCacheType -ne 'f16')) {
        $userFa = [Environment]::GetEnvironmentVariable('OLLAMA_FLASH_ATTENTION', 'User')
        $userKv = [Environment]::GetEnvironmentVariable('OLLAMA_KV_CACHE_TYPE', 'User')
        if (($srvEnv.FlashAttention -and -not $userFa) -or ($srvEnv.KvCacheType -ne 'f16' -and $userKv -ne $srvEnv.KvCacheType)) {
            Write-Info "Ollama was already running, so OLLAMA_FLASH_ATTENTION / OLLAMA_KV_CACHE_TYPE from backend\.env are not applied to it."
            Write-Info 'To apply them: quit Ollama (tray icon > Quit) and run start.ps1 again, or run setup.ps1 once (it stores them for the Ollama app).'
        }
    }
    if ($ollamaOk) {
        Write-Step 'Checking installed models'
        [void](Show-OllamaModels $OllamaUrl $OllamaModel $false)
    } else {
        Write-Warn2 'The local LLM is not available: CPARAS ISLD analysis needs it (CPARAS Bulk scoring does not). The app will start and show the exact problem in the UI.'
    }
} else {
    Write-Step "LLM backend is '$LlmBackend' (not Ollama) - skipping Ollama checks."
}

# ── 5. Backend ─────────────────────────────────────────────────────────────
Write-Step "Starting the backend on http://${BackendHost}:$BackendPort"
if (-not (Test-PortFree $BackendPort 'backend')) {
    $h = Invoke-LocalGet "http://127.0.0.1:$BackendPort/api/health" 3
    if ($h.Ok) { Write-Info "Something already answers /api/health on port $BackendPort - is this app already running?" }
    exit 1
}
$beOut = Join-Path $script:LogDir 'backend.log'
$beErr = Join-Path $script:LogDir 'backend.err.log'
$beArgs = @{
    FilePath = $py
    ArgumentList = @('-m', 'uvicorn', 'main:app', '--host', $BackendHost, '--port', "$BackendPort")
    WorkingDirectory = $Backend; PassThru = $true
    RedirectStandardOutput = $beOut; RedirectStandardError = $beErr
}
if ($script:OnWindows) { $beArgs['WindowStyle'] = 'Hidden' }
$backendProc = Start-Process @beArgs
Write-Info "Backend PID $($backendProc.Id) - log: logs\backend.err.log"
$healthUrl = "http://127.0.0.1:$BackendPort/api/health"
if (-not (Wait-HttpOk $healthUrl $BackendTimeoutSec 'Backend' $backendProc)) {
    Show-LogTail $beErr 30
    if (-not $backendProc.HasExited) { Stop-Process -Id $backendProc.Id -Force -ErrorAction SilentlyContinue }
    exit 1
}
Write-Ok 'Backend is ready.'

# ── 6. Frontend ────────────────────────────────────────────────────────────
$frontendProc = $null
if (-not $BackendOnly) {
    Write-Step "Starting the frontend on http://127.0.0.1:$FrontendPort"
    if (-not (Test-PortFree $FrontendPort 'frontend')) {
        Stop-Process -Id $backendProc.Id -Force -ErrorAction SilentlyContinue
        exit 1
    }
    $env:API_TARGET = "http://127.0.0.1:$BackendPort"
    $feArgs = @{
        # Relative to WorkingDirectory and quoted: Windows PowerShell 5.1 passes
        # ArgumentList array items unquoted, so an absolute path containing a
        # space (C:\Users\FIRST LAST\..., D:\CPARAS (1)\...) reached Node cut
        # at the space and failed with "Cannot find module".
        # One pre-quoted string: passed to Node verbatim by both Windows
        # PowerShell 5.1 and PowerShell 7, so no path can be split at a space.
        FilePath = 'node'; ArgumentList = "`"$viteRel`" --port $FrontendPort --strictPort"
        WorkingDirectory = $Frontend; PassThru = $true
        RedirectStandardOutput = (Join-Path $script:LogDir 'frontend.log')
        RedirectStandardError = (Join-Path $script:LogDir 'frontend.err.log')
    }
    if ($script:OnWindows) { $feArgs['WindowStyle'] = 'Hidden' }
    $frontendProc = Start-Process @feArgs
    if (-not (Wait-HttpOk "http://127.0.0.1:$FrontendPort/" 90 'Frontend' $frontendProc)) {
        Show-LogTail (Join-Path $script:LogDir 'frontend.log') 20
        Show-LogTail (Join-Path $script:LogDir 'frontend.err.log') 20
        Stop-Process -Id $backendProc.Id -Force -ErrorAction SilentlyContinue
        if (-not $frontendProc.HasExited) { Stop-Process -Id $frontendProc.Id -Force -ErrorAction SilentlyContinue }
        exit 1
    }
    Write-Ok 'Frontend is ready.'
}

# ── 7. Summary ─────────────────────────────────────────────────────────────
$h = Invoke-LocalGet $healthUrl 10
$summary = $null
if ($h.Ok) { try { $summary = $h.Body | ConvertFrom-Json } catch { } }
Write-Host ''
Write-Host '  ------------------------------------------------------------'
Write-Host '  CPARAS Platform is running' -ForegroundColor Green
if (-not $BackendOnly) { Write-Host "    App        : http://127.0.0.1:$FrontendPort" }
Write-Host "    API docs   : http://127.0.0.1:$BackendPort/api/docs"
Write-Host "    Health     : $healthUrl"
# v0.5.0: sign-in is required; say how to create the first account if there is none yet.
$st = Invoke-LocalGet "http://127.0.0.1:$BackendPort/api/auth/setup-status" 5
if ($st.Ok) {
    try {
        $sj = $st.Body | ConvertFrom-Json
        if ($sj.needs_setup) {
            Write-Host '    Sign-in    : NO ACCOUNT YET - create the first Super Admin:' -ForegroundColor Yellow
            Write-Host '                 double-click create_admin.cmd, or open the app and use "First-time setup"' -ForegroundColor Yellow
            Write-Host "                 (one-time token file: $($sj.setup_token_file))" -ForegroundColor Yellow
        } else {
            Write-Host '    Sign-in    : required - accounts: Administration > User Management (Super Admin)'
        }
    } catch { }
}
if ($summary) {
    Write-Host "    Status     : $($summary.status)"
    $l = $summary.llm
    if ($l) {
        $srv = 'not reachable'
        if ($l.reachable) { $srv = 'connected' }
        Write-Host "    LLM server : $srv ($($l.base_url))"
        if ($l.reachable) {
            Write-Host "    Models     : $(@($l.text_models).Count) text, $(@($l.vision_models).Count) vision"
            $avail = 'NOT installed'
            if ($l.configured_model_available) { $avail = 'installed' }
            Write-Host "    Default    : $($l.configured_model) ($avail)"
        } elseif ($l.error) {
            Write-Host "    LLM error  : $($l.error)" -ForegroundColor Yellow
        }
    }
}
$ri = Invoke-LocalGet "http://127.0.0.1:$BackendPort/api/retrieval-index/status" 60
if ($ri.Ok) {
    try {
        $r = $ri.Body | ConvertFrom-Json
        if ($r.ok) { Write-Host "    3GPP index : $($r.dir) ($('{0:N0}' -f $r.documents) documents)" }
        elseif ($r.configured) { Write-Host "    3GPP index : NOT usable - $($r.problems[0].message)" -ForegroundColor Yellow }
        else { Write-Host "    3GPP index : not installed - copy retrieval_data into $($r.expected_dir)" -ForegroundColor Yellow }
    } catch { }
}
Write-Host "    Logs       : $script:LogDir"
Write-Host '  Press Ctrl+C to stop the backend and frontend (Ollama keeps running).'
Write-Host '  ------------------------------------------------------------'

# ── 8. Wait; clean shutdown on Ctrl+C or if a child dies ──────────────────
try {
    while ($true) {
        Start-Sleep -Seconds 2
        if ($backendProc.HasExited) { Write-Fail "Backend stopped (exit code $($backendProc.ExitCode))."; Show-LogTail $beErr 30; break }
        if ($frontendProc -ne $null -and $frontendProc.HasExited) { Write-Fail 'Frontend stopped.'; Show-LogTail (Join-Path $script:LogDir 'frontend.err.log') 20; break }
    }
} finally {
    Write-Host "`nStopping..."
    foreach ($p in @($frontendProc, $backendProc)) {
        if ($p -ne $null -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    }
    Write-Host 'Stopped backend and frontend.'
}
