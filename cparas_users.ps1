<#
.SYNOPSIS
  CPARAS account administration from the command line (v0.5.0).

.DESCRIPTION
  Runs `python -m auth.cli <command>` with the backend's own Python (backend\.venv), from the
  backend folder, so it uses the same database as the app. Works whether or not the app is
  running. Passwords are typed at a hidden prompt or generated and shown once; nothing here
  contains a fixed password.

  Commands (see backend\auth\cli.py):
    create-superadmin [--username NAME] [--display-name "..."] [--generate]
    create-user --username NAME --role guest|data_entry|super_admin [--generate] [--must-change]
    seed-test-users [--prefix test_] [--reset]
    reset-password --username NAME [--generate] [--must-change]
    set-role --username NAME --role guest|data_entry|super_admin
    set-active --username NAME --active|--inactive
    list-users

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\cparas_users.ps1 list-users
  powershell -ExecutionPolicy Bypass -File .\cparas_users.ps1 reset-password --username alice --generate
#>
$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $Root 'scripts\common.ps1')
$Backend = Join-Path $Root 'backend'
$py = Get-VenvPython $Backend
if (-not (Test-Path $py)) { Write-Fail "backend virtual environment not found ($py). Run setup.cmd first."; exit 1 }
if (-not $args -or $args.Count -eq 0) { $cmdArgs = @('--help') } else { $cmdArgs = $args }
Push-Location $Backend
try {
    & $py -m auth.cli @cmdArgs
    $code = $LASTEXITCODE
} finally { Pop-Location }
exit $code
