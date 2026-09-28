@echo off
REM ==========================================================================
REM  DHYAAN VOICE BRAIN - DESKTOP APP LAUNCHER
REM  Double-click this (or the "Dhyaan VB" desktop icon) to start the services
REM  and open the dashboard in a clean standalone app window.
REM ==========================================================================
title Dhyaan Voice Brain - App
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_voicebrain_app.ps1"
