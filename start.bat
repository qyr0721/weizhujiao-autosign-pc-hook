@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PY=C:\Users\Admin\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo   starting...
echo.

"%PY%" run.py %*

echo.
echo   ------------------------------
echo   process exited. proxy may still be running.
echo   ------------------------------
pause
