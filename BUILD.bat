@echo off
:: Double-click this to build StellarInsight.exe + installer
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1"
pause
