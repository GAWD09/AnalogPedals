@echo off
setlocal
:: AnalogPedals UI Launcher — launches pedals_ui.py from its own folder
cd /d "%~dp0"

echo.
echo [AnalogPedals] Checking Python...
where python >nul 2>nul
if errorlevel 1 (
    echo [AnalogPedals] ERROR: python not found in PATH
    echo [AnalogPedals] Install Python 3.11+ and add it to PATH, then rerun this bat.
    pause
    exit /b 1
)

python --version
echo [AnalogPedals] Launching UI from "%CD%"...
echo.

:: Run in the background so this launcher can report status; UI stays alive.
start "AnalogPedals UI" /D "%~dp0" python pedals_ui.py

:: Give the GUI a moment to start before we print status.
timeout /t 2 /nobreak >nul

echo [AnalogPedals] UI launch command sent.
echo [AnalogPedals] Check Windows Task Manager (Ctrl+Shift+Esc) -> Details for python.exe.
echo [AnalogPedals] If nothing appears, rerun from a normal (non-admin) CMD in this folder.
echo.
pause
