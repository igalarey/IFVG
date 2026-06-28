@echo off
REM =====================================================================
REM  IFVG live runner - 24/7 auto-restart wrapper (Windows VPS)
REM  Relaunches run_live.py if it ever exits (crash, MT5 hiccup, reboot).
REM  All output is appended to live.log. Stop with Ctrl+C (twice).
REM
REM  Edit SYMBOL / RISK below. RISK 0.01 = 1% (challenge); 0.005 once funded.
REM =====================================================================
setlocal
cd /d "%~dp0"

set SYMBOL=XAUUSD
set RISK=0.01

:loop
echo [%date% %time%] starting run_live (%SYMBOL%, risk %RISK%) >> live.log
python run_live.py --symbol %SYMBOL% --risk-pct %RISK% >> live.log 2>&1
echo [%date% %time%] run_live exited (code %errorlevel%) - restarting in 10s >> live.log
timeout /t 10 /nobreak >nul
goto loop
