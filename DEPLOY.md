# Deploying the IFVG bot 24/7 (Windows VPS)

The live bot (`run_live.py`) is an external Python process that **attaches to a
running MT5 terminal** and sends orders through it. It runs the exact same rule as
the backtest (limit entry at the FVG, 0.5%/1% risk, break-even, 4 h time-stop,
17 h-Friday flat — no weekend holding).

## 1. Prerequisites on the VPS

- **Windows VPS**, on 24/7 (gold trades ~23 h/day, Sun–Fri).
- **MetaTrader 5** installed and **logged into `FTMO-Demo`**, terminal **open**.
  - Set it to **auto-login** (save password) so it survives a VPS reboot.
- **Algo Trading enabled**: the toolbar button green **and**
  `Tools → Options → Expert Advisors → Allow algorithmic trading` ✔.
  (Without this you get order `retcode 10027`.)
- **XAUUSD** visible in Market Watch with tick history.
- **Python 3** with the live dependencies only:
  ```
  pip install MetaTrader5 pandas numpy
  ```
  (`backtrader` / `matplotlib` are for the backtest, not needed live.)

## 2. Run it (recommended: the auto-restart wrapper)

Double-click **`run_live.bat`** (or run it in `cmd`). It relaunches the bot if it
ever exits (crash, MT5 hiccup, reboot) and appends everything to `live.log`.
Edit the two variables at the top of the file:

```bat
set SYMBOL=XAUUSD
set RISK=0.01        REM 1% for the challenge; 0.005 once funded
```

Manual one-off (no auto-restart):
```bat
python run_live.py --symbol XAUUSD --risk-pct 0.01
```

> Use the **real, tradeable `XAUUSD`** — not the frozen `XAUUSD2020_FTMO` backtest
> symbol. The bot refuses a non-demo account unless you pass `--allow-real`.

## 3. Keep it alive across RDP / reboots

- **Disconnect** your RDP session, do **not log off** — logging off kills the
  process; disconnecting leaves it running.
- For a true service that starts on boot and self-heals, use **NSSM**
  (https://nssm.cc):
  ```
  nssm install IFVGbot "C:\Path\to\python.exe" "C:\...\mt5\run_live.py --symbol XAUUSD --risk-pct 0.01"
  nssm set IFVGbot AppDirectory "C:\...\mt5"
  nssm set IFVGbot AppStdout "C:\...\mt5\live.log"
  nssm set IFVGbot AppStderr "C:\...\mt5\live.log"
  nssm start IFVGbot
  ```
  NSSM restarts it automatically if it stops. (The `run_live.bat` loop is a fine
  simpler alternative.)

## 4. Robustness built in

- **Auto-reconnect**: if the terminal drops, the loop keeps retrying and resumes.
- **Per-iteration error handling**: one bad tick/API call is logged and skipped,
  never crashes the bot.
- **Restart-safe / near-stateless**: open trades are read back from MT5 by magic
  number, so after any restart the bot resumes managing them (time-stop /
  break-even / Friday-flat); the 1 h zones rebuild from recent bars.
- **Hourly heartbeat** in `live.log` so you can see it is alive when idle.

## 5. Monitoring

```powershell
Get-Content live.log -Tail 40 -Wait     # live tail
```
You should see order placements, closes, and an hourly `heartbeat` line.

## 6. Reminders

- **Risk**: 1% (`--risk-pct 0.01`) for the challenge to reach +10% faster; drop to
  **0.5%** the moment the account is funded.
- **Expect live to run below the backtest** — the edge is thin vs transaction
  costs; the demo is the real test of fill quality. See `STRATEGY.md §9`.
