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
  pip install -r requirements.txt
  ```
  (Installs MetaTrader5 / pandas / numpy / flask. `backtrader` / `matplotlib` are
  commented out — they are for the backtest, not needed live.)

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

## 2b. Or run it from the web dashboard (configure + watch in the browser)

If you prefer not to touch files or flags, use the local control panel. Double-click
**`run_app.bat`** (or `python app/webapp.py`); it opens **http://127.0.0.1:8765** in
the VPS browser. Add one or more **accounts** (left list), set each one's settings on
the right, **Start / Stop** each independently, and watch its live output.

- **Multi-account.** Each account is fully independent — its own symbol, login,
  server, password, MT5 terminal and risk (e.g. one challenge at 1% and one funded
  account at 0.5%). The panel supervises each as its own `run_live.py` process.
- **One MT5 terminal per account.** The MetaTrader5 module allows one account per
  process/terminal, so each account must point (via **Ruta terminal64.exe**) to its
  OWN MT5 installation, each open and logged into that account. Install MT5 to a
  separate folder (or `/portable`) per account.
- The panel **supervises `run_live.py`** for you: it relaunches each bot if it ever
  exits (same auto-restart as `run_live.bat`) and streams its log into the page.
- Tick **"Arrancar al abrir la app"** on the accounts you want to come up on their
  own — combine with Task Scheduler (At log on) for reboot-proof 24/7.
- It binds to **127.0.0.1 only** (never the network), each password is sent to its
  bot via an environment variable (not the command line), and all settings are saved
  in `app/config.json` — **plain text, git-ignored; it holds your passwords.** Each
  account's output is also written to `logs/<id>.log`.

### Configurable parameters (panel fields)

Everything else (the strategy rules — risk model, FVG/sweep logic, 4 h time-stop,
weekend flat, RR, etc.) is **fixed in code** and intentionally NOT exposed — including
the poll interval (fixed at 5 s; the bot acts once per closed minute regardless). Only
the operational settings below are configurable:

These are **per account** — every account has its own independent set:

| Field | `run_live.py` flag | Default | Meaning |
|---|---|---|---|
| **Nombre** | — | `Cuenta N` | A label for the account in the panel list. |
| **Símbolo** | `--symbol` | `XAUUSD` | The tradeable symbol to run on. Use the broker's real XAUUSD, not the backtest symbol. |
| **Login** | `--login` | *(blank)* | Broker account number. Leave blank to attach to an already-logged-in terminal. |
| **Servidor** | `--server` | *(blank)* | MT5 server name, e.g. `FTMO-Demo`. Blank = use the open terminal's session. |
| **Contraseña** | (via `MT5_PASSWORD` env) | *(blank)* | Account password. Sent to the bot via env, not the command line. Leave blank when editing to keep the saved one. |
| **Ruta terminal64.exe** | `--mt5-path` | *(blank)* | Path to THIS account's MT5 terminal. **Required when running more than one account** so each bot attaches to the right terminal. |
| **Riesgo por trade (%)** | `--risk-pct` | `1.0` | Fixed-fractional risk per trade, in percent. **1% for the challenge, 0.5% once funded.** (Panel converts % → fraction.) |
| **Permitir cuenta REAL** | `--allow-real` | off | Safety guard: must be ON to trade a non-demo account. Leave OFF for the demo. |
| **Arrancar al abrir la app** | — | off | If ON, this account auto-starts whenever the panel launches (pair with Task Scheduler below for reboot-proof 24/7). |

## 3. Keep it alive across RDP / reboots

- **Disconnect** your RDP session, do **not log off** — logging off kills the
  process; disconnecting leaves it running.

### Auto-start the dashboard on boot (Task Scheduler)

So the panel (and, with *"Arrancar el bot al abrir la app"* ticked, the bot itself)
comes back automatically after a VPS reboot:

1. Open **Task Scheduler** (`taskschd.msc`) → **Create Task…** (not *Basic*).
2. **General**: name it `IFVG bot`; select **Run only when user is logged on**
   (the MT5 terminal needs a desktop session); tick **Run with highest privileges**.
3. **Triggers** → New → *Begin the task:* **At log on**, your user.
4. **Actions** → New → *Start a program* → **Program/script:** the full path to
   `run_app.bat`; **Start in:** the repo folder (e.g. `C:\...\mt5`).
5. **Conditions**: untick *Start the task only if the computer is on AC power*.
6. **Settings**: tick *If the task fails, restart every 1 minute*; set
   *If the task is already running: Do not start a new instance* (`run_app.bat`
   already self-loops).

Quick CLI equivalent (run in an elevated PowerShell, fix the path):
```
schtasks /Create /TN "IFVG bot" /TR "C:\...\mt5\run_app.bat" /SC ONLOGON /RL HIGHEST /F
```

> **Reboot survival needs a logged-on session.** "At log on" only fires when a user
> logs on. On an unattended VPS, enable **Windows auto-logon** (e.g. `netplwiz` →
> untick *Users must enter a password*, or Sysinternals **Autologon**) so that after
> a reboot: the session starts → MT5 auto-login opens the terminal → the task
> launches the dashboard → the bot auto-starts. Without auto-logon you must RDP in
> once after each reboot.

### Alternative: a real Windows service (NSSM)

- For a service that starts on boot and self-heals without a logged-on session, use
  **NSSM** (https://nssm.cc) — note MT5 itself still generally needs a desktop
  session, so auto-logon above is usually the simpler path:
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
