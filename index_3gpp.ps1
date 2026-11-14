<#
.SYNOPSIS
  Index a local 3GPP archive folder into the CPARAS Standards Corpus (v0.4.2).

.DESCRIPTION
  Put the extracted 3GPP archive in ONE folder (sub-folders are fine). It may
  contain any mix of:
    * 3gpp_<spec>_v<x.y.z>.json   e.g. 3gpp_21.111_v13.0.0.json
    * extracted Word files         e.g. 21111-d00.docx
    * the original archive zips    e.g. 21111-d00.zip
  Default folder: backend\data\3gpp_archive  (THREEGPP_ARCHIVE_DIR in backend\.env)

  By default only the LATEST version of each specification is indexed, so
  search results are not the same clause repeated per historical version.
  Re-running is safe: files already indexed are skipped.

  If the app is running, indexing runs inside the backend (progress is shown
  here). Otherwise it runs directly with the backend's Python.

.PARAMETER Path
  Folder to index. Default: backend\data\3gpp_archive

.PARAMETER AllVersions
  Index every version of every spec (much larger, usually not wanted).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\index_3gpp.ps1
  powershell -ExecutionPolicy Bypass -File .\index_3gpp.ps1 -Path "D:\3gpp_extracted"
#>
param(
    [string]$Path = '',
    [switch]$AllVersions
)

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $Root 'scripts\common.ps1')
$Backend = Join-Path $Root 'backend'

Write-Host ''
Write-Host '  CPARAS - index a local 3GPP archive' -ForegroundColor White
Write-Host '  ------------------------------------------------------------'

$py = Get-VenvPython $Backend
if (-not (Test-Path $py)) { Write-Fail "backend virtual environment not found ($py). Run setup.ps1 first."; exit 1 }
$envFile = Join-Path $Backend '.env'
$envVals = @{}
if (Test-Path $envFile) { $envVals = Read-DotEnv $envFile }

if (-not $Path) {
    $Path = Get-Setting $envVals 'THREEGPP_ARCHIVE_DIR' './data/3gpp_archive'
    if (-not [System.IO.Path]::IsPathRooted($Path)) { $Path = Join-Path $Backend $Path }
}
if (-not (Test-Path $Path -PathType Container)) {
    Write-Fail "Folder not found: $Path"
    Write-Info 'Create it and copy the extracted 3GPP files into it, or pass -Path "<your folder>".'
    exit 1
}
$Path = (Resolve-Path $Path).Path
$nJson = @(Get-ChildItem -Path $Path -Recurse -File -Filter '3gpp_*.json' -ErrorAction SilentlyContinue).Count
$nDocx = @(Get-ChildItem -Path $Path -Recurse -File -Filter '*.docx' -ErrorAction SilentlyContinue).Count
$nZip = @(Get-ChildItem -Path $Path -Recurse -File -Filter '*.zip' -ErrorAction SilentlyContinue).Count
Write-Ok "Folder: $Path"
Write-Info "Found $nJson 3gpp_*.json, $nDocx .docx, $nZip .zip file(s). Indexing $(if ($AllVersions) { 'every version' } else { 'the latest version of each spec' })."
if (($nJson + $nDocx + $nZip) -eq 0) { Write-Fail 'Nothing to index in that folder.'; exit 1 }

$port = [int](Get-Setting $envVals 'PORT' '8001')
$api = "http://127.0.0.1:$port/api"

function Invoke-LocalPostJson([string]$Url, [string]$Json) {
    try {
        $req = [System.Net.HttpWebRequest]::Create($Url)
        $req.Method = 'POST'; $req.Proxy = $null; $req.ContentType = 'application/json'; $req.Timeout = 30000
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Json)
        $s = $req.GetRequestStream(); $s.Write($bytes, 0, $bytes.Length); $s.Close()
        $resp = $req.GetResponse()
        $r = New-Object System.IO.StreamReader($resp.GetResponseStream(), [System.Text.Encoding]::UTF8)
        $body = $r.ReadToEnd(); $r.Close(); $resp.Close()
        return @{ Ok = $true; Body = $body }
    } catch [System.Net.WebException] {
        $body = $null
        if ($_.Exception.Response) { $r = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream()); $body = $r.ReadToEnd() }
        return @{ Ok = $false; Body = $body; Error = $_.Exception.Message }
    }
}

if ((Invoke-LocalGet "$api/health" 3).Ok) {
    Write-Ok "The app is running - indexing inside the backend ($api)."
    $payload = @{ path = $Path; all_versions = [bool]$AllVersions } | ConvertTo-Json -Compress
    $r = Invoke-LocalPostJson "$api/scrape/index-local" $payload
    if (-not $r.Ok) { Write-Fail "Could not start indexing: $($r.Error) $($r.Body)"; exit 1 }
    do {
        Start-Sleep -Seconds 5
        $st = Invoke-LocalGet "$api/scrape/index-local/status" 5
        if (-not $st.Ok) { Write-Warn2 'Backend did not answer; still waiting...'; continue }
        $j = $st.Body | ConvertFrom-Json
        Write-Host ("    {0}: {1} file(s), {2} chunk(s) indexed  {3}" -f $j.status, $j.files_indexed, $j.chunks_indexed, $j.current_file)
    } while ($j.status -eq 'running')
    if ($j.status -eq 'done') {
        Write-Ok "Done: $($j.files_indexed) file(s) indexed, $($j.chunks_indexed) chunk(s); $($j.skipped_already_indexed) already indexed, $($j.skipped_error) error(s)."
        exit 0
    }
    Write-Fail "Indexing failed: $($j.error)"; exit 1
}

Write-Info 'The app is not running - indexing directly (keep the app closed until this finishes).'
$cliArgs = @('-m', 'ingestion.spec_corpus', '--input', $Path)
if ($AllVersions) { $cliArgs += '--all-versions' }
Push-Location $Backend
try { & $py @cliArgs; $code = $LASTEXITCODE } finally { Pop-Location }
if ($code -eq 0) { Write-Ok 'Done. Start the app (start.cmd); the Standards text panel in CPARAS ISLD shows the indexed chunk count.' }
exit $code
