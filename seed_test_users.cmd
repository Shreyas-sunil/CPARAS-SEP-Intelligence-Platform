@echo off
REM Create one TEST account per role (test_guest, test_data_entry, test_super_admin) with random
REM passwords that are printed once (v0.5.0). Run again with --reset to get new passwords.
REM Deactivate or delete these accounts before using CPARAS in production.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0cparas_users.ps1" seed-test-users %*
pause
