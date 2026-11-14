# ============================================================================
# Shared helpers for setup.ps1 / start.ps1 (dot-sourced; not run directly).
# Compatible with Windows PowerShell 5.1 and PowerShell 7+ (Windows/Linux/macOS).
# ============================================================================

$script:OnWindows = ($PSVersionTable.PSEdition -eq 'Desktop') -or ($IsWindows -eq $true)

function Write-Step([string]$msg)  { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)    { Write-Host "    [ OK ] $msg" -ForegroundColor Green }
function Write-Info([string]$msg)  { Write-Host "    [INFO] $msg" }
function Write-Warn2([string]$msg) { Write-Host "    [WARN] $msg" -ForegroundColor Yellow }
function Write-Fail([string]$msg)  { Write-Host "    [FAIL] $msg" -ForegroundColor Red }

function Get-VenvPython([string]$BackendDir) {
    if ($script:OnWindows) { return (Join-Path $BackendDir '.venv\Scripts\python.exe') }
    return (Join-Path $BackendDir '.venv/bin/python')
}

# Read KEY=VALUE pairs from a .env file (comments/blank lines ignored).
function Read-DotEnv([string]$Path) {
    $vals = @{}
    if (-not (Test-Path $Path)) { return $vals }
    foreach ($line in Get-Content -Path $Path -Encoding UTF8) {
        $t = $line.Trim()
        if ($t -eq '' -or $t.StartsWith('#')) { continue }
        $i = $t.IndexOf('=')
        if ($i -lt 1) { continue }
        $k = $t.Substring(0, $i).Trim()
        $v = $t.Substring($i + 1).Trim().Trim('"').Trim("'")
        $vals[$k] = $v
    }
    return $vals
}

function Get-Setting($envVals, [string]$Key, [string]$Default) {
    $fromProcess = [Environment]::GetEnvironmentVariable($Key)
    if ($fromProcess) { return $fromProcess }
    if ($envVals.ContainsKey($Key) -and $envVals[$Key]) { return $envVals[$Key] }
    return $Default
}

# HTTP GET that never goes through a system/corporate proxy (loopback
# requests through a proxy are the classic "Ollama is running but the app
# says unreachable" cause). Returns @{ Ok; Status; Body; Error }.
function Invoke-LocalGet([string]$Url, [int]$TimeoutSec = 3) {
    try {
        $req = [System.Net.HttpWebRequest]::Create($Url)
        $req.Method = 'GET'
        $req.Proxy = $null
        $req.Timeout = $TimeoutSec * 1000
        $req.ReadWriteTimeout = $TimeoutSec * 1000
        $resp = $req.GetResponse()
        try {
            $reader = New-Object System.IO.StreamReader($resp.GetResponseStream(), [System.Text.Encoding]::UTF8)
            $body = $reader.ReadToEnd()
            $reader.Close()
            return @{ Ok = $true; Status = [int]$resp.StatusCode; Body = $body; Error = $null }
        } finally { $resp.Close() }
    } catch [System.Net.WebException] {
        $status = 0
        if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        return @{ Ok = $false; Status = $status; Body = $null; Error = $_.Exception.Message }
    } catch {
        return @{ Ok = $false; Status = 0; Body = $null; Error = $_.Exception.Message }
    }
}

# Poll a URL until it answers 2xx or the timeout expires. Returns $true/$false.
function Wait-HttpOk([string]$Url, [int]$TimeoutSec, [string]$What, $Process = $null) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $dots = 0
    while ((Get-Date) -lt $deadline) {
        $r = Invoke-LocalGet $Url 3
        if ($r.Ok -and $r.Status -ge 200 -and $r.Status -lt 300) {
            if ($dots) { Write-Host '' }
            return $true
        }
        if ($Process -ne $null -and $Process.HasExited) {
            if ($dots) { Write-Host '' }
            $code = $Process.ExitCode
            if ($code -eq $null -or "$code" -eq '') { $code = 'unknown' }
            Write-Fail "$What exited early (exit code $code)."
            return $false
        }
        Start-Sleep -Milliseconds 700
        Write-Host -NoNewline '.'
        $dots++
    }
    if ($dots) { Write-Host '' }
    Write-Fail "$What did not become ready within $TimeoutSec s ($Url)."
    return $false
}

# Which process is LISTENING on a TCP port? Returns @{ Pid; Name } or $null.
function Get-PortOwner([int]$Port) {
    try {
        if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
            $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($c) {
                $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
                $n = '?'
                if ($p) { $n = $p.ProcessName }
                return @{ Pid = $c.OwningProcess; Name = $n }
            }
            return $null
        }
    } catch { }
    if ($script:OnWindows) {
        $line = netstat -ano -p tcp 2>$null | Select-String -Pattern ("[:.]" + $Port + "\s+\S+\s+LISTENING\s+(\d+)") | Select-Object -First 1
        if ($line) {
            $procId = [int]$line.Matches[0].Groups[1].Value
            $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
            $n = '?'
            if ($p) { $n = $p.ProcessName }
            return @{ Pid = $procId; Name = $n }
        }
        return $null
    }
    # Linux/macOS
    $lsof = Get-Command lsof -ErrorAction SilentlyContinue
    if ($lsof) {
        $out = & lsof -nP ("-iTCP:" + $Port) -sTCP:LISTEN 2>$null | Select-Object -Skip 1 -First 1
        if ($out) {
            $parts = ($out -split '\s+')
            return @{ Pid = [int]$parts[1]; Name = $parts[0] }
        }
    }
    return $null
}

function Find-Ollama {
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    if ($script:OnWindows) {
        $candidates = @(
            (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'),
            (Join-Path $env:ProgramFiles 'Ollama\ollama.exe')
        )
        foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { return $c } }
    }
    return $null
}

# v0.4.2: nvidia-smi is not always on PATH on Windows (older drivers put it
# in Program Files\NVIDIA Corporation\NVSMI, DCH drivers in DriverStore).
function Find-NvidiaSmi {
    $cmd = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    if ($script:OnWindows) {
        $cands = @(
            (Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'),
            (Join-Path $env:ProgramFiles 'NVIDIA Corporation\NVSMI\nvidia-smi.exe')
        )
        foreach ($c in $cands) { if ($c -and (Test-Path $c)) { return $c } }
        $ds = Get-ChildItem -Path (Join-Path $env:SystemRoot 'System32\DriverStore\FileRepository') -Filter 'nvidia-smi.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($ds) { return $ds.FullName }
    }
    return $null
}

# Prints the NVIDIA GPU(s) Ollama can use. Returns $true when one was found.
function Show-NvidiaGpu {
    $smi = Find-NvidiaSmi
    if (-not $smi) {
        Write-Warn2 'No NVIDIA driver found (nvidia-smi). Ollama will run on the CPU. Install/update the NVIDIA driver for your RTX card (external GPUs: connect it before starting Ollama).'
        return $false
    }
    $lines = & $smi --query-gpu=name,memory.total,memory.used --format=csv,noheader 2>$null
    if (-not $lines) {
        Write-Warn2 "nvidia-smi ($smi) did not list any GPU. If this is an external GPU, check it is connected and shown in Device Manager, then restart Ollama."
        return $false
    }
    foreach ($l in @($lines)) { Write-Ok "NVIDIA GPU: $l" }
    return $true
}

# Ollama server settings from backend\.env that only take effect when the
# Ollama SERVER starts (flash attention, KV-cache type). Exported into this
# process so an Ollama started by Ensure-Ollama inherits them.
function Set-OllamaServerEnv($envVals) {
    $fa = (Get-Setting $envVals 'OLLAMA_FLASH_ATTENTION' 'false').ToLowerInvariant()
    $kv = (Get-Setting $envVals 'OLLAMA_KV_CACHE_TYPE' 'f16').ToLowerInvariant()
    if ($fa -in @('1', 'true', 'yes')) { $env:OLLAMA_FLASH_ATTENTION = '1' }
    if ($kv -and $kv -ne 'f16') { $env:OLLAMA_KV_CACHE_TYPE = $kv }
    return @{ FlashAttention = ($fa -in @('1', 'true', 'yes')); KvCacheType = $kv }
}

function Get-UrlPort([string]$Url, [int]$Default) {
    try { $u = [Uri]$Url; if ($u.Port -gt 0) { return $u.Port } } catch { }
    return $Default
}

function Normalize-ModelName([string]$Name) {
    $n = $Name.Trim().ToLowerInvariant()
    if ($n -eq '') { return '' }
    $last = ($n -split '/')[-1]
    if (-not $last.Contains(':')) { $n = "${n}:latest" }
    return $n
}

function Test-VisionModel([string]$Name, $Families) {
    $n = $Name.ToLowerInvariant()
    foreach ($h in @('moondream', 'llava', 'vision', 'minicpm-v', 'qwen2-vl', 'qwen2.5vl')) { if ($n.Contains($h)) { return $true } }
    if ($Families) { foreach ($f in $Families) { if (("$f").ToLowerInvariant() -in @('clip', 'mllama')) { return $true } } }
    return $false
}

# Make sure Ollama is answering. NEVER starts a second server when one is
# already running: "port 11434 already in use" normally means Ollama is up.
# Returns $true when /api/tags answers.
function Ensure-Ollama([string]$BaseUrl, [int]$Parallel = 1, [int]$TimeoutSec = 60) {
    $tagsUrl = "$BaseUrl/api/tags"
    $r = Invoke-LocalGet $tagsUrl 3
    if ($r.Ok) {
        Write-Ok "Ollama is already running at $BaseUrl - not starting another instance."
        $script:OllamaStartedByUs = $false
        return $true
    }
    $port = Get-UrlPort $BaseUrl 11434
    $owner = Get-PortOwner $port
    if ($owner) {
        if ($owner.Name -match 'ollama') {
            Write-Info "Ollama (PID $($owner.Pid)) holds port $port but is not answering yet - waiting for it (it may be starting or loading a model)."
            if (Wait-HttpOk $tagsUrl $TimeoutSec 'Ollama') { Write-Ok "Ollama is answering at $BaseUrl."; return $true }
            Write-Fail "Ollama is listening on port $port but $tagsUrl does not answer. Quit Ollama from the system tray (or end 'ollama' in Task Manager) and start it again."
            return $false
        }
        Write-Fail "Port $port is used by '$($owner.Name)' (PID $($owner.Pid)), not by Ollama, and $tagsUrl does not answer like Ollama."
        Write-Info "Stop that program, or run Ollama on another port (set OLLAMA_HOST=127.0.0.1:11500 before starting it) and set OLLAMA_BASE_URL=http://127.0.0.1:11500 in backend\.env."
        return $false
    }
    $exe = Find-Ollama
    if (-not $exe) {
        Write-Fail "Ollama is not running and the 'ollama' program was not found."
        Write-Info "Install it from https://ollama.com/download, then run this script again."
        return $false
    }
    Write-Info "Ollama is not running - starting it: `"$exe`" serve"
    $env:OLLAMA_NUM_PARALLEL = "$Parallel"
    $logDir = $script:LogDir
    if (-not $logDir) { $logDir = Join-Path (Get-Location) 'logs' }
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $startArgs = @{
        FilePath = $exe; ArgumentList = 'serve'; PassThru = $true
        RedirectStandardOutput = (Join-Path $logDir 'ollama.out.log')
        RedirectStandardError = (Join-Path $logDir 'ollama.err.log')
    }
    if ($script:OnWindows) { $startArgs['WindowStyle'] = 'Hidden' }
    $proc = Start-Process @startArgs
    if (Wait-HttpOk $tagsUrl $TimeoutSec 'Ollama' $proc) {
        Write-Ok "Ollama started (PID $($proc.Id)) and is answering at $BaseUrl. It keeps running after this script exits."
        $script:OllamaStartedByUs = $true
        return $true
    }
    Write-Info "See logs\ollama.err.log for Ollama's own error message."
    return $false
}

# Query /api/tags and validate the configured model. Never substitutes a model.
function Show-OllamaModels([string]$BaseUrl, [string]$ConfiguredModel, [bool]$Required) {
    $r = Invoke-LocalGet "$BaseUrl/api/tags" 5
    if (-not $r.Ok) { Write-Warn2 "Could not list models: $($r.Error)"; return $false }
    $data = $r.Body | ConvertFrom-Json
    $models = @($data.models)
    if ($models.Count -eq 0) {
        Write-Warn2 "Ollama has no models installed. Install one:  ollama pull $ConfiguredModel"
        return (-not $Required)
    }
    $text = @(); $vision = @()
    foreach ($m in $models) {
        $fam = $null
        if ($m.details) { $fam = $m.details.families }
        if (Test-VisionModel $m.name $fam) { $vision += $m.name } else { $text += $m.name }
    }
    Write-Info ("Installed text models : " + ($(if ($text.Count) { $text -join ', ' } else { 'none' })))
    if ($vision.Count) { Write-Info ("Vision (OCR-only)     : " + ($vision -join ', ')) }
    $want = Normalize-ModelName $ConfiguredModel
    $found = $false
    foreach ($m in $models) { if ((Normalize-ModelName $m.name) -eq $want) { $found = $true } }
    if ($found) {
        if (Test-VisionModel $ConfiguredModel $null) {
            Write-Warn2 "OLLAMA_MODEL '$ConfiguredModel' is a vision model; text analysis needs a text model such as qwen2.5:7b."
        } else {
            Write-Ok "Configured model '$ConfiguredModel' is installed."
        }
        return $true
    }
    Write-Warn2 "Configured model '$ConfiguredModel' (OLLAMA_MODEL) is NOT installed."
    Write-Info  "Either run:  ollama pull $ConfiguredModel"
    Write-Info  "or set OLLAMA_MODEL in backend\.env to one of the installed text models, or pick one in the UI."
    return (-not $Required)
}

function Test-PortFree([int]$Port, [string]$What) {
    $owner = Get-PortOwner $Port
    if ($owner) {
        Write-Fail "Port $Port (needed for the $What) is already used by '$($owner.Name)' (PID $($owner.Pid))."
        Write-Info "Close that program (or an already-running copy of this app) and try again."
        return $false
    }
    return $true
}

function Show-LogTail([string]$Path, [int]$Lines = 25) {
    if (Test-Path $Path) {
        Write-Host "---- last $Lines lines of $Path ----" -ForegroundColor DarkGray
        Get-Content -Path $Path -Tail $Lines | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        Write-Host "------------------------------------" -ForegroundColor DarkGray
    }
}

function Get-PythonCommand {
    # Prefer an explicit supported version via the Windows "py" launcher
    # (avoids the Microsoft Store "python" alias), then python / python3.
    $cands = @(@('py', '-3.12'), @('py', '-3.11'), @('py', '-3.10'), @('py', '-3'), @('python'), @('python3'))
    foreach ($cand in $cands) {
        $exe = $cand[0]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $pre = @()
        if ($cand.Count -gt 1) { $pre = $cand[1..($cand.Count - 1)] }
        $ver = & $exe @pre -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver) {
            return @{ Exe = $exe; Pre = $pre; Version = "$ver".Trim() }
        }
    }
    return $null
}

function Test-VersionAtLeast([string]$Version, [int]$Major, [int]$Minor) {
    $p = $Version.Split('.')
    $ma = [int]$p[0]; $mi = [int]$p[1]
    return ($ma -gt $Major) -or ($ma -eq $Major -and $mi -ge $Minor)
}

# ============================================================================
# 3GPP dataset (v0.4.4): find / configure / check the validated dataset build.
# Shared, byte-identical across CPARAS Platform, CPARAS ISLD and Initial Categoriser.
# The dataset is READ-ONLY: nothing here writes into it; the only file changed is
# backend\.env (CPARAS_DATASET_ROOT=...), and only as described on Set-DotEnvValue.
# ============================================================================

function Test-DatasetFolder([string]$Dir, [string[]]$Markers) {
    if (-not $Dir) { return $false }
    if (-not (Test-Path -LiteralPath $Dir -PathType Container)) { return $false }
    foreach ($m in $Markers) {
        if (-not (Test-Path -LiteralPath (Join-Path $Dir $m) -PathType Leaf)) { return $false }
    }
    return $true
}

# Candidate folders, best first: the folder that holds the apps, the usual build folder
# names beside the app, then any sibling folder (and its sub-folders) that contains the
# marker files. Returns full paths (possibly several, newest first among scanned ones).
function Find-CparasDataset([string]$AppRoot, [string[]]$Markers) {
    $parent = Split-Path -Parent $AppRoot
    $found = New-Object System.Collections.Generic.List[string]
    if (-not $parent -or -not (Test-Path -LiteralPath $parent -PathType Container)) { return @() }

    $named = New-Object System.Collections.Generic.List[string]
    $named.Add($parent)
    foreach ($rel in @('cparas_dataset\build_v4', 'cparas_dataset', 'build_v4', 'dataset', '3gpp_dataset', '3gpp\cparas_dataset\build_v4')) {
        $p = $parent
        foreach ($part in ($rel -split '[\\/]')) { $p = Join-Path $p $part }
        $named.Add($p)
    }
    foreach ($c in $named) {
        if ((Test-DatasetFolder $c $Markers) -and -not $found.Contains($c)) { $found.Add($c) }
    }

    $skip = @('node_modules', '.venv', '.git', '__pycache__', 'logs', 'data', 'frontend', 'backend', 'sample_data', 'scripts')
    $scanned = New-Object System.Collections.Generic.List[string]
    foreach ($d1 in @(Get-ChildItem -LiteralPath $parent -Directory -ErrorAction SilentlyContinue)) {
        if ($skip -contains $d1.Name) { continue }
        $scanned.Add($d1.FullName)
        foreach ($d2 in @(Get-ChildItem -LiteralPath $d1.FullName -Directory -ErrorAction SilentlyContinue)) {
            if ($skip -contains $d2.Name) { continue }
            $scanned.Add($d2.FullName)
        }
    }
    $hits = @()
    foreach ($c in $scanned) {
        if ((Test-DatasetFolder $c $Markers) -and -not $found.Contains($c)) {
            $hits += (Get-Item -LiteralPath (Join-Path $c $Markers[0]))
        }
    }
    foreach ($h in @($hits | Sort-Object LastWriteTime -Descending)) { $found.Add($h.DirectoryName) }
    return @($found.ToArray())
}

# Set KEY=VALUE in a .env file without disturbing anything else.
#   * key missing                         -> appended                         (returns 'added')
#   * key present with an empty value     -> the blank value is filled        (returns 'filled')
#   * key present with a different value  -> replaced ONLY when -Overwrite    (returns 'replaced'), else left alone ('kept')
# The file is written as UTF-8 without BOM and keeps its existing line endings.
function Set-DotEnvValue([string]$File, [string]$Key, [string]$Value, [bool]$Overwrite = $false) {
    $raw = ''
    if (Test-Path -LiteralPath $File) { $raw = [System.IO.File]::ReadAllText($File) }
    $nl = [Environment]::NewLine
    if ($raw.Contains("`r`n")) { $nl = "`r`n" } elseif ($raw.Contains("`n")) { $nl = "`n" }
    $lines = @()
    if ($raw.Length -gt 0) { $lines = @([regex]::Split($raw, "\r?\n")) }
    $idx = -1
    $pattern = '^\s*' + [regex]::Escape($Key) + '\s*='
    for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i] -match $pattern) { $idx = $i } }
    $status = 'added'
    if ($idx -ge 0) {
        $cur = ($lines[$idx] -replace '^[^=]*=', '').Trim().Trim('"').Trim("'")
        if ($cur -eq $Value) { return 'kept' }
        if ($cur -ne '' -and -not $Overwrite) { return 'kept' }
        $lines[$idx] = "$Key=$Value"
        if ($cur -eq '') { $status = 'filled' } else { $status = 'replaced' }
    } else {
        $list = New-Object System.Collections.Generic.List[string]
        foreach ($l in $lines) { $list.Add($l) }
        while ($list.Count -gt 0 -and $list[$list.Count - 1] -eq '') { $list.RemoveAt($list.Count - 1) }
        $list.Add("$Key=$Value")
        $lines = $list.ToArray()
    }
    $text = ($lines -join $nl)
    if (-not $text.EndsWith($nl)) { $text += $nl }
    [System.IO.File]::WriteAllText($File, $text, (New-Object System.Text.UTF8Encoding($false)))
    return $status
}

# Run a backend python module with CPARAS_DATASET_ROOT set for the child process only.
# Returns @{ Code; Out; ErrFile }. Output is captured (stdout) - stderr goes to a log file.
function Invoke-BackendModule([string]$Py, [string]$Backend, [string]$Root, [string[]]$ModuleArgs, [string]$LogDir) {
    $errFile = Join-Path $LogDir 'dataset-check.err.log'
    $prev = [Environment]::GetEnvironmentVariable('CPARAS_DATASET_ROOT')
    [Environment]::SetEnvironmentVariable('CPARAS_DATASET_ROOT', $Root)
    Push-Location $Backend
    try {
        $out = & $Py -m @ModuleArgs 2> $errFile
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
        [Environment]::SetEnvironmentVariable('CPARAS_DATASET_ROOT', $prev)
    }
    return @{ Code = $code; Out = (($out | ForEach-Object { "$_" }) -join "`n"); ErrFile = $errFile }
}

function ConvertFrom-JsonLoose([string]$Text) {
    $i = $Text.IndexOf('{')
    if ($i -lt 0) { return $null }
    try { return ($Text.Substring($i) | ConvertFrom-Json) } catch { return $null }
}

# The whole dataset step for one app. Never throws; returns @{ Root; Ok; Notes }.
#   Kind 'etsi'        : Platform / ISLD  (etsi_corpus.cli status-local / validate-local / import-local)
#   Kind 'categoriser' : Initial Categoriser (csaras.reference_cli status)
function Invoke-DatasetSetup {
    param(
        [string]$AppRoot, [string]$Backend, [string]$EnvFile, [string]$Py, [string]$LogDir,
        [string]$Kind, [string[]]$Markers, [string]$Explicit = '',
        [bool]$Validate = $false, [bool]$Import = $false, [bool]$ImportAll = $false,
        [int]$ImportLimit = 0, [string]$Versions = 'all'
    )
    $result = @{ Root = $null; Ok = $false; Notes = @() }
    $key = 'CPARAS_DATASET_ROOT'
    $envVals = Read-DotEnv $EnvFile
    $fileValue = ''
    if ($envVals.ContainsKey($key)) { $fileValue = [string]$envVals[$key] }
    $effective = Get-Setting $envVals $key ''
    $root = $null

    if ($Explicit) {
        $e = $Explicit.Trim().Trim('"').Trim("'")
        $cands = @($e, (Join-Path $e 'build_v4'), (Join-Path (Join-Path $e 'cparas_dataset') 'build_v4'), (Join-Path $e 'cparas_dataset'))
        foreach ($c in $cands) { if (Test-DatasetFolder $c $Markers) { $root = (Resolve-Path -LiteralPath $c).Path; break } }
        if (-not $root) {
            Write-Fail "-DatasetPath '$e' does not contain: $($Markers -join ', ')"
            $result.Notes += 'dataset: -DatasetPath is not a valid dataset folder'
            return $result
        }
        Write-Ok "Using the dataset folder from -DatasetPath: $root"
    } elseif ($effective -and (Test-DatasetFolder $effective $Markers)) {
        $root = (Resolve-Path -LiteralPath $effective).Path
        Write-Ok "CPARAS_DATASET_ROOT is already configured: $root"
    } else {
        if ($effective) {
            Write-Warn2 "CPARAS_DATASET_ROOT=$effective does not contain $($Markers -join ', ') - looking for the dataset next to the app instead."
        }
        $hits = @(Find-CparasDataset $AppRoot $Markers)
        if ($hits.Count -ge 1) {
            $root = $hits[0]
            Write-Ok "Found the dataset next to the app: $root"
            if ($hits.Count -gt 1) {
                Write-Warn2 "More than one dataset folder was found; using the first. Others: $((@($hits | Select-Object -Skip 1)) -join '; ')  (pick another with -DatasetPath)"
            }
        }
    }

    if (-not $root) {
        $parent = Split-Path -Parent $AppRoot
        Write-Warn2 "No 3GPP dataset found. Put the dataset build folder (the one containing $($Markers -join ', ')) next to this app in:"
        Write-Info "  $parent"
        Write-Info "  e.g. $(Join-Path (Join-Path $parent 'cparas_dataset') 'build_v4'), then re-run setup - or pass -DatasetPath <folder>."
        Write-Info 'The app works without it; the dataset features simply stay off.'
        $result.Notes += 'dataset not found (optional)'
        return $result
    }
    $result.Root = $root

    # Persist in backend\.env (existing non-empty values are only replaced for an explicit -DatasetPath).
    if (Test-Path -LiteralPath $EnvFile) {
        $st = Set-DotEnvValue $EnvFile $key $root ([bool]$Explicit)
        switch ($st) {
            'added'    { Write-Ok "Wrote CPARAS_DATASET_ROOT to backend\.env" }
            'filled'   { Write-Ok "Filled CPARAS_DATASET_ROOT in backend\.env" }
            'replaced' { Write-Ok "Updated CPARAS_DATASET_ROOT in backend\.env (was: $fileValue)" }
            default {
                if ($fileValue -and $fileValue -ne $root) {
                    Write-Warn2 "backend\.env keeps CPARAS_DATASET_ROOT=$fileValue (not changed). Re-run with -DatasetPath `"$root`" to point it here."
                }
            }
        }
    } else {
        Write-Warn2 'backend\.env not found - CPARAS_DATASET_ROOT was not saved.'
    }

    # Optional files worth a hint (never fatal here).
    foreach ($opt in @('build_report.json', 'validation_report.json')) {
        if ($Kind -eq 'etsi' -and -not (Test-Path -LiteralPath (Join-Path $root $opt))) { Write-Warn2 "$opt is missing from the dataset folder (counts cannot be cross-checked)." }
    }

    # ── Check ──────────────────────────────────────────────────────────────
    if ($Kind -eq 'categoriser') {
        $r = Invoke-BackendModule $Py $Backend $root @('csaras.reference_cli', 'status') $LogDir
        $j = ConvertFrom-JsonLoose $r.Out
        if ($r.Code -eq 0 -and $j) {
            Write-Ok "Dataset reference readable: $($j.specs) specifications, $($j.series) series (schema $($j.schema))"
            $result.Ok = $true
        } else {
            Write-Fail 'The dataset reference could not be read.'
            if ($j -and $j.error) { Write-Info $j.error }
            Show-LogTail $r.ErrFile 10
            $result.Notes += 'dataset reference unreadable'
        }
        Write-Info 'Curated reference entries always take precedence; values taken from the dataset are marked as inferred.'
        return $result
    }

    $r = Invoke-BackendModule $Py $Backend $root @('etsi_corpus.cli', 'status-local') $LogDir
    $j = ConvertFrom-JsonLoose $r.Out
    if (-not $j) {
        Write-Fail 'Could not read the dataset status.'
        Show-LogTail $r.ErrFile 15
        $result.Notes += 'dataset status check failed'
        return $result
    }
    if ($j.ok) {
        $n = ''
        if ($j.build -and $j.build.records_written) { $n = "$($j.build.records_written) records, builder $($j.build.builder_version)" }
        Write-Ok "Dataset files present and compatible ($n)"
        $result.Ok = $true
    } else {
        Write-Fail 'The dataset folder is incomplete or incompatible:'
        foreach ($p in @($j.problems)) { Write-Host "         - $($p.message)" -ForegroundColor Red }
        $result.Notes += 'dataset folder incomplete'
        return $result
    }
    foreach ($w in @($j.warnings)) { Write-Info "note: $($w.message)" }
    if ($j.imported -and $j.imported.documents -gt 0) { Write-Info "Already imported into this app: $($j.imported.documents) document(s), $($j.imported.indexed) searchable." }

    if ($Validate) {
        Write-Info 'Running the full streaming validation (reads the whole 3 GB JSONL once - several minutes)...'
        $v = Invoke-BackendModule $Py $Backend $root @('etsi_corpus.cli', 'validate-local') $LogDir
        $vj = ConvertFrom-JsonLoose $v.Out
        if ($vj -and $vj.ok) {
            Write-Ok "Full validation passed: $($vj.counts.records) records, $($vj.counts.unknown_version) with unknown version (kept), $($vj.seconds) s"
        } else {
            Write-Fail 'Full validation found problems:'
            if ($vj) { foreach ($p in @($vj.problems)) { Write-Host "         - $($p.message)" -ForegroundColor Red } } else { Show-LogTail $v.ErrFile 15 }
            $result.Ok = $false
            $result.Notes += 'dataset validation failed'
            return $result
        }
    }

    # ── Import (opt-in: it embeds/indexes thousands of documents) ───────────
    $cliArgs = @('etsi_corpus.cli', 'import-local', '--versions', $Versions)
    if ($ImportAll) { $cliArgs += '--all-rows' } elseif ($ImportLimit -gt 0) { $cliArgs += @('--limit', "$ImportLimit") }
    if ($Import -or $ImportAll) {
        if ($ImportAll) { Write-Info 'Importing and indexing the ENTIRE selected dataset - this can take hours; it is safe to stop (Ctrl+C) and re-run: it resumes.' }
        elseif ($ImportLimit -gt 0) { Write-Info "Importing the first $ImportLimit document(s)..." }
        else { Write-Info 'Importing a small trial batch (ETSI_DEFAULT_IMPORT_LIMIT, 25 by default). Use -ImportAll for everything.' }
        $prev = [Environment]::GetEnvironmentVariable('CPARAS_DATASET_ROOT')
        [Environment]::SetEnvironmentVariable('CPARAS_DATASET_ROOT', $root)
        Push-Location $Backend
        try { & $Py -m @cliArgs; $code = $LASTEXITCODE } finally { Pop-Location; [Environment]::SetEnvironmentVariable('CPARAS_DATASET_ROOT', $prev) }
        if ($code -eq 0) { Write-Ok 'Dataset import finished (see the report under backend\data\...\import_reports).' }
        else { Write-Warn2 "Dataset import finished with problems (exit code $code) - see the messages above; re-run the same command to retry only the failed/unfinished documents."; $result.Notes += 'dataset import incomplete' }
    } else {
        Write-Info 'Nothing was imported (it is a large, opt-in step). To import later, from this folder:'
        Write-Info '  powershell -ExecutionPolicy Bypass -File .\setup.ps1 -ImportDataset            (25-document trial)'
        Write-Info '  powershell -ExecutionPolicy Bypass -File .\setup.ps1 -ImportAll -Versions latest-per-release'
        Write-Info '(or call the /api/etsi-corpus local-dataset endpoints; see DATASET_INTEGRATION.md).'
    }
    return $result
}
