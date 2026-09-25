@echo off
setlocal
chcp 65001 >nul
title AllDayRecording services

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\restart-allday-services.ps1"
if errorlevel 1 (
  echo.
  echo Startup failed. See the error above and state\runtime-logs logs.
  pause
)

endlocal
