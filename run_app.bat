@echo off
REM =====================================================================
REM  IFVG bot - web dashboard launcher (Windows VPS)
REM  Starts the local control panel and opens it in the browser.
REM  The dashboard itself supervises the bot (start/stop/auto-restart).
REM
REM  Reboot-proof 24/7: add this .bat to Task Scheduler (At log on),
REM  and tick "Arrancar el bot al abrir la app" in the panel so the bot
REM  starts on its own. Keep the RDP session disconnected, not logged off.
REM =====================================================================
setlocal
cd /d "%~dp0"
:loop
python app\webapp.py
echo [%date% %time%] webapp exited (code %errorlevel%) - restarting in 10s
timeout /t 10 /nobreak >nul
goto loop
