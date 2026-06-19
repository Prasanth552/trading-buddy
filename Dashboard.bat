@echo off
title Trading Buddy - Dashboard
cd /d "%~dp0"
cls
echo ===============================================================
echo               TRADING  BUDDY  -  DASHBOARD
echo ---------------------------------------------------------------
echo   Opening the dashboard in your browser...
echo   If the page shows an error, wait 3 seconds and refresh.
echo   Keep this window open while you use the dashboard.
echo ===============================================================
start "" http://localhost:8000
".venv\Scripts\python.exe" main.py --dashboard
echo.
echo Dashboard stopped. You can close this window.
pause
