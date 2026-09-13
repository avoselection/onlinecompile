@echo off
setlocal
cd /d "%~dp0.."

where py >nul 2>nul
if %errorlevel% equ 0 (
  set "PYTHON=py -3"
) else (
  set "PYTHON=python"
)

%PYTHON% -c "import sys; raise SystemExit('Python 3.11 or newer is required' if sys.version_info < (3, 11) else 0)"
if errorlevel 1 goto :error

if not exist ".venv\Scripts\python.exe" (
  %PYTHON% -m venv .venv
  if errorlevel 1 goto :error
)

call ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r deploy\requirements.txt
if errorlevel 1 goto :error

echo.
echo The addresses below are available while this window stays open:
".venv\Scripts\python.exe" server.py
goto :end

:error
echo.
echo Launch failed. Install Python 3.11 or newer from https://www.python.org/downloads/ and try again.

:end
pause
