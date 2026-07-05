@echo off
rem Start Moon Bridge (DS backend) when MOON_BRIDGE_EXE and MOON_BRIDGE_CONFIG are available.
if not defined MOON_BRIDGE_EXE set "MOON_BRIDGE_EXE=%~dp0..\moon-bridge\bin\moonbridge.exe"
if not defined MOON_BRIDGE_CONFIG set "MOON_BRIDGE_CONFIG=%~dp0..\moon-bridge\config.yml"
powershell -NoProfile -Command "$exe=$env:MOON_BRIDGE_EXE; $cfg=$env:MOON_BRIDGE_CONFIG; if ((Test-Path -LiteralPath $exe) -and (Test-Path -LiteralPath $cfg)) { if (-not (Get-NetTCPConnection -LocalPort 38440 -State Listen -ErrorAction SilentlyContinue)) { Start-Process -FilePath $exe -ArgumentList '-config',$cfg -WorkingDirectory (Split-Path -Parent $exe); Write-Host 'Moon Bridge starting...' } else { Write-Host 'Moon Bridge already running.' } } else { Write-Host 'Moon Bridge not configured; skipping DS backend startup.' }"
set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
  echo Missing project venv Python: "%PYTHON_EXE%"
  echo Create it from this folder before starting ai-roundtable.
  pause
  exit /b 1
)
"%PYTHON_EXE%" "%~dp0server.py"