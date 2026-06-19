@echo off
title Trading Buddy
cd /d "%~dp0"
cls
echo ===============================================================
echo                      TRADING  BUDDY
echo ---------------------------------------------------------------
echo   Starting up... please wait.
echo.
echo   KEEP THIS WINDOW OPEN during market hours (9:15 AM - 3:30 PM).
echo   Watch your phone (Telegram) for messages.
echo   You can close this window after 3:30 PM.
echo ===============================================================
echo.
".venv\Scripts\python.exe" main.py
echo.
echo ---------------------------------------------------------------
echo   Trading Buddy has stopped. You can close this window.
echo ---------------------------------------------------------------
pause
