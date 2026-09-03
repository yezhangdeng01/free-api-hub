@echo off
setlocal
cd /d "%~dp0"
set "PY=C:\Users\JCH\miniconda3\python.exe"
if exist "%PY%" goto :havepy
set "PY=python"
:havepy
if exist ".venv\Scripts\python.exe" goto :run
echo First run: creating venv and installing dependencies...
"%PY%" -m venv .venv
if errorlevel 1 goto :err
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
if errorlevel 1 goto :err
:run
echo Starting API Hub...
echo You can MINIMIZE this window after the app opens. Do NOT close it.
".venv\Scripts\python.exe" desktop.py
set "RC=%errorlevel%"
echo.
echo Program exited (code %RC%). Press any key to close.
pause >nul
exit /b 0
:err
echo Failed. Please send the error above to AI for troubleshooting.
pause
