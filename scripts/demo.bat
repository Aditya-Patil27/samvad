@echo off
REM Start the Samvad demo. Double-click it, or run: scripts\demo.bat
REM
REM Uses the project's own Python directly, so there is no interpreter to pick
REM and nothing to switch to halfway through. Pass --mock for fixed replies.
setlocal
cd /d "%~dp0.."

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo.
  echo   No .venv found in %CD%
  echo   Run this once, then try again:   uv sync --extra dev
  echo.
  pause
  exit /b 1
)

"%PY%" scripts\demo.py %*

echo.
pause
endlocal
