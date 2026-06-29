@echo off
REM =====================================================================
REM  IFVG bot - web dashboard launcher (Windows VPS)
REM  Starts the local control panel + supervisor (start/stop/auto-restart).
REM  Open the panel yourself at http://127.0.0.1:8765 (no auto browser).
REM
REM  NO VISIBLE WINDOW: double-click run_app.vbs instead of this .bat to
REM  run everything hidden (no cmd window on screen).
REM
REM  Reboot-proof 24/7: add run_app.vbs to Task Scheduler (At log on),
REM  and tick "Arrancar al abrir la app" on each account so the bots
REM  start on their own. Keep the RDP session disconnected, not logged off.
REM =====================================================================
setlocal
cd /d "%~dp0"
REM launchers stay silent (no browser pop-up); open the URL above manually
set IFVG_NO_BROWSER=1
:loop
python app\webapp.py
REM the panel's "Apagar todo" writes this flag -> stop the loop, do not relaunch
if exist "logs\shutdown.flag" (
  del /q "logs\shutdown.flag"
  goto :end
)
echo [%date% %time%] webapp exited (code %errorlevel%) - restarting in 10s
timeout /t 10 /nobreak >nul
goto loop
:end
