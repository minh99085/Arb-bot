@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\START.ps1" %*
if errorlevel 1 pause
