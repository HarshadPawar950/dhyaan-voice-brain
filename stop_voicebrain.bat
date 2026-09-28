@echo off
REM ==========================================================================
REM  DHYAAN VOICE BRAIN - STOP
REM  Double-click this file to shut down Catcher + Dashboard + Tunnel.
REM ==========================================================================
title Dhyaan Voice Brain - Stop
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_voicebrain.ps1"
echo.
echo Press any key to close this window...
pause >nul
