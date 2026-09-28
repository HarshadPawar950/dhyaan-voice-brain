@echo off
REM ==========================================================================
REM  DHYAAN VOICE BRAIN - ONE-CLICK STARTUP
REM  Double-click this file. It brings up Catcher + Dashboard + Tunnel,
REM  waits for each to be healthy, and prints the webhook URL for Bolna.
REM ==========================================================================
title Dhyaan Voice Brain - Startup
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_voicebrain.ps1"
echo.
echo Services keep running in the background after this window closes.
echo Press any key to close this window...
pause >nul
