@echo off
REM Create a CPARAS Super Admin (first-time setup, v0.5.0). Double-click, then type a username
REM and a password at the hidden prompt. Extra options are passed on, e.g.
REM   create_admin.cmd --username admin --display-name "CPARAS Admin"
echo.
echo   CPARAS - create a Super Admin account
echo   ------------------------------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0cparas_users.ps1" create-superadmin %*
pause
