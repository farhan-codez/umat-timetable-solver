@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title UMaT-SRID Timetable Builder

rem ---- first run: create the Python environment if needed ----
if exist ".venv\Scripts\python.exe" goto :start

echo.
echo   First run - setting up the app. This can take a few minutes.
echo.
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m venv .venv
) else (
    python -m venv .venv 2>nul
)
if not exist ".venv\Scripts\python.exe" goto :noPython
echo   Installing required packages...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto :noInstall

:start
echo.
echo  ============================================================
echo   UMaT-SRID Timetable Builder
echo  ============================================================
echo.
echo   Open your browser and go to: http://127.0.0.1:8000
echo   (or http://<this-computer-ip>:8000 from other computers)
echo.
echo   To stop the app, close this window.
echo.
echo   If your firewall asks, allow access so other computers can view it.
echo.

rem ---- load optional .env (STUDENT_APP_URL, STUDENT_APP_PUBLISH_SECRET) ----
if exist ".env" (
    for /f "tokens=1,* delims==" %%A in ('findstr /b /v "#" ".env"') do set "%%A=%%B"
)

".venv\Scripts\python.exe" -m uvicorn web.main:app --host 0.0.0.0 --port 8000
echo.
echo   The server has stopped.
pause
exit /b 0

:noPython
echo.
echo   Python is not installed on this computer.
echo   Please install it from https://www.python.org/downloads/
echo   (tick "Add python to PATH"), then double-click this file again.
pause
exit /b 1

:noInstall
echo.
echo   Could not install the required packages. Check your internet
echo   connection, then double-click this file again.
pause
exit /b 1
