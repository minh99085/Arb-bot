@echo off
REM Double-click or run from cmd — starts paper arb bot in Docker
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0START.ps1"
if errorlevel 1 pause
