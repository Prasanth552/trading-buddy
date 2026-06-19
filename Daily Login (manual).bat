@echo off
title Trading Buddy - Login
cd /d "%~dp0"
cls
echo ===============================================================
echo                TRADING  BUDDY  -  DAILY LOGIN
echo ---------------------------------------------------------------
echo   A login link will appear below.
echo   1. Copy it into your web browser and log in to Zerodha.
echo   2. After login the page will say "site can't be reached" -
echo      that's normal.
echo   3. Copy the long "request_token" value from the address bar
echo      and paste it here, then press Enter.
echo ===============================================================
echo.
".venv\Scripts\python.exe" main.py --login
echo.
pause
