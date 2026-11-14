@echo off
REM Double-click friendly wrapper: indexes backend\data\3gpp_archive (or the folder given as -Path "...").
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0index_3gpp.ps1" %*
pause
