@echo off
:: Double-click this to build, bump the version, and publish a GitHub Release.
:: Usage: just double-click — it auto-increments the patch number (1.0.0 -> 1.0.1)
::
:: To set an explicit version, edit the line below:
::   powershell ... -Release -Version "1.2.0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" -Release
pause
