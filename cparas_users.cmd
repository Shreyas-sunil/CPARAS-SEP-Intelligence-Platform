@echo off
REM CPARAS account administration (v0.5.0). Example:  cparas_users.cmd list-users
REM See cparas_users.ps1 for every command.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0cparas_users.ps1" %*
