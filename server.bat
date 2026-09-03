@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto :novenv
".venv\Scripts\python.exe" server.py
echo.
echo Server exited. Press any key to close.
pause >nul
exit /b 0
:novenv
echo Please run run.bat first to complete installation.
pause
