"""
app/webapp.py  —  local control hub + 24/7 supervisor for the trading bots
==========================================================================
A small Flask app you open in the VPS browser to organise STRATEGIES, configure
their ACCOUNTS, start/stop and WATCH each live bot — no editing Python or flags.

HIERARCHY:  Hub → Strategy → Accounts
  * A STRATEGY groups accounts that run the same rule. Each strategy points to a
    runner script (default run_live.py = the IFVG rule). Add new strategies as
    you build new runners.
  * An ACCOUNT is fully independent: its own symbol / login / server / password /
    MT5 terminal (--mt5-path) / risk / real-or-demo. Each is supervised as its
    own runner child process. (MetaTrader5 allows one account per process/
    terminal, so N accounts = N terminals = N processes.)

It does NOT re-implement any strategy. It SUPERVISES the runner: launches each
account, streams its output into the page, and relaunches it on exit. Stopping a
bot leaves any open trade protected by its broker-side SL/TP.

SECURITY (read once):
  * Binds to 127.0.0.1 only — the panel shows broker credentials and controls
    live trading, so it is NOT exposed to the network.
  * Each password is passed to its child via an env var, not the command line.
  * config.json stores everything INCLUDING passwords in plain text on the VPS.
    It is git-ignored. Treat the VPS as a machine that holds them.

RUN:  python app/webapp.py   (or run_app.vbs / run_app.bat)  ->  http://127.0.0.1:8765
"""
import json
import os
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque

from flask import Flask, jsonify, request

# ── paths ────────────────────────────────────────────────────────────────────
APP_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(APP_DIR)            # run_live.py + ifvg/ live here
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOG_DIR = os.path.join(REPO_ROOT, "logs")       # per-account live logs (<id>.log)
os.makedirs(LOG_DIR, exist_ok=True)
SHUTDOWN_FLAG = os.path.join(LOG_DIR, "shutdown.flag")   # tells run_app.bat to stop

HOST, PORT = "127.0.0.1", 8765
RESTART_BACKOFF_S = 10          # wait before relaunching a crashed bot (like the .bat)
LOG_LINES = 4000                # ring-buffer of recent output kept in memory, per account

# one strategy = a named group of accounts sharing a runner script
DEFAULT_STRATEGY = {"name": "IFVG", "runner": "run_live.py"}
# one account's operational settings (the strategy itself is fixed in code)
DEFAULT_ACCOUNT = {
    "name": "Cuenta",
    "symbol": "XAUUSD",
    "login": "",                # broker account number (optional if terminal logged in)
    "password": "",             # stored plain in config.json; sent to child via env
    "server": "",               # e.g. FTMO-Demo
    "mt5_path": "",             # path to THIS account's terminal64.exe (one per account)
    "risk_pct": 1.0,            # PERCENT per trade (1.0 = 1%); 0.5 once funded
    "allow_real": False,        # must be on to trade a non-demo account
    "autostart": False,         # start this account when the app launches
}


# ── config persistence (strategies -> accounts) ──────────────────────────────
def new_id():
    return uuid.uuid4().hex[:8]


def load_config():
    cfg = {"strategies": []}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
        except Exception:
            cfg = {"strategies": []}
    if "strategies" not in cfg:                  # migrate an old flat-accounts config
        accs = cfg.get("accounts", [])
        cfg = {"strategies": [{**DEFAULT_STRATEGY, "id": new_id(), "accounts": accs}]}
    if not cfg["strategies"]:                     # always seed the IFVG strategy
        cfg["strategies"].append({**DEFAULT_STRATEGY, "id": new_id(), "accounts": []})
    for s in cfg["strategies"]:
        s.setdefault("id", new_id())
        s.setdefault("name", "Estrategia")
        s.setdefault("runner", "run_live.py")
        s.setdefault("accounts", [])
        for a in s["accounts"]:
            a.setdefault("id", new_id())
            for k, v in DEFAULT_ACCOUNT.items():
                a.setdefault(k, v)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


def find_strategy(cfg, sid):
    return next((s for s in cfg["strategies"] if s["id"] == sid), None)


def find_account(cfg, aid):
    """Return (strategy, account) for a global account id, or (None, None)."""
    for s in cfg["strategies"]:
        for a in s["accounts"]:
            if a["id"] == aid:
                return s, a
    return None, None


def ping_path(account_id):
    return os.path.join(LOG_DIR, f"{account_id}.ping")


def build_cmd_env(strat, acc):
    """The runner command + environment for one account. Poll is NOT exposed
    (fixed at the runner's default); the password goes via env, not argv."""
    runner = os.path.join(REPO_ROOT, strat.get("runner") or "run_live.py")
    cmd = [sys.executable, "-u", runner,
           "--symbol", str(acc.get("symbol") or "XAUUSD"),
           "--risk-pct", str(float(acc.get("risk_pct", 1.0)) / 100.0)]   # % -> fraction
    if str(acc.get("login", "")).strip():
        cmd += ["--login", str(acc["login"]).strip()]
    if str(acc.get("server", "")).strip():
        cmd += ["--server", str(acc["server"]).strip()]
    if str(acc.get("mt5_path", "")).strip():
        cmd += ["--mt5-path", str(acc["mt5_path"]).strip()]
    if acc.get("allow_real"):
        cmd += ["--allow-real"]
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["IFVG_PING_FILE"] = ping_path(acc["id"])     # touch it -> bot replies HEALTH
    if str(acc.get("password", "")).strip():
        env["MT5_PASSWORD"] = str(acc["password"]).strip()
    return cmd, env


# ── one supervised bot process (per account) ─────────────────────────────────
class BotRunner:
    """Owns one account's child process. `desired` is what the user wants; a
    background thread reconciles reality to it (start, restart-on-crash, stop)."""

    def __init__(self, account_id):
        self.id = account_id
        self.desired = False
        self.alive = True
        self.proc = None
        self.lock = threading.Lock()
        self.seq = 0
        self.lines = deque(maxlen=LOG_LINES)
        self.status = {"equity": None, "position": None, "started_at": None,
                       "health": None, "warning": None}
        self.log_path = os.path.join(LOG_DIR, f"{account_id}.log")
        threading.Thread(target=self._supervise, daemon=True).start()

    # --- output handling ---
    def _emit(self, text):
        with self.lock:
            self.seq += 1
            self.lines.append((self.seq, text))
            self._parse_status(text)
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except Exception:
            pass

    def _parse_status(self, text):
        for key in ("eq=", "equity="):               # bot logs eq=, legacy equity=
            if key in text:
                try:
                    self.status["equity"] = text.split(key)[1].split()[0]
                except Exception:
                    pass
                break
        # position state from the lifecycle events the bot logs
        if "FILLED:" in text or "resuming open" in text or "heartbeat: in " in text:
            self.status["position"] = "open"
        elif "CLOSED:" in text or "heartbeat: flat" in text:
            self.status["position"] = "flat"
        elif "position=" in text:                     # legacy heartbeat
            try:
                self.status["position"] = text.split("position=")[1].split()[0]
            except Exception:
                pass
        # health / warnings (HEALTH [tag] ok=True/False | … | WARN: …)
        if "HEALTH" in text and "ok=" in text:
            self.status["health"] = text.split("] ", 1)[-1] if "] " in text else text
            self.status["warning"] = (text.split("WARN:", 1)[1].strip()
                                      if "ok=False" in text and "WARN:" in text else None)

    # --- process lifecycle ---
    def _start(self):
        strat, acc = find_account(load_config(), self.id)
        if acc is None:                      # account was deleted
            self.desired = False
            return
        cmd, env = build_cmd_env(strat, acc)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            cmd, cwd=REPO_ROOT, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1, creationflags=flags)
        self.status["started_at"] = time.time()
        self._emit(f"[app] started bot (pid {self.proc.pid})")
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc):
        try:
            for line in iter(proc.stdout.readline, ""):
                if line:
                    self._emit(line.rstrip("\n"))
        except Exception as exc:
            self._emit(f"[app] output reader error: {exc!r}")

    def _supervise(self):
        while self.alive:
            try:
                running = self.proc is not None and self.proc.poll() is None
                if self.desired and not running:
                    if self.proc is not None:        # it crashed/exited -> backoff
                        self._emit(f"[app] bot exited (code {self.proc.poll()}); "
                                   f"restarting in {RESTART_BACKOFF_S}s")
                        self.proc = None
                        for _ in range(RESTART_BACKOFF_S):
                            if not (self.desired and self.alive):
                                break
                            time.sleep(1)
                    if self.desired and self.alive:
                        self._start()
                elif (not self.desired or not self.alive) and running:
                    self._emit("[app] stopping bot")
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=10)
                    except Exception:
                        self.proc.kill()
                    self.proc = None
                    self.status.update(equity=None, position=None, started_at=None)
            except Exception as exc:
                self._emit(f"[app] supervisor error: {exc!r}")
            time.sleep(1)

    # --- public API ---
    def start(self):
        self.desired = True

    def stop(self):
        self.desired = False

    def dispose(self):
        self.desired = False
        self.alive = False

    def snapshot(self, after):
        with self.lock:
            new = [{"seq": s, "text": t} for s, t in self.lines if s > after]
            running = self.proc is not None and self.proc.poll() is None
            return {
                "running": running, "desired": self.desired,
                "pid": self.proc.pid if running else None,
                "equity": self.status["equity"], "position": self.status["position"],
                "health": self.status["health"], "warning": self.status["warning"],
                "lines": new, "last": self.seq,
            }


# ── manager: one BotRunner per account, kept in sync with the config ─────────
class Manager:
    def __init__(self):
        self.runners = {}
        self.lock = threading.Lock()
        self.reconcile()

    def _all_account_ids(self, cfg):
        return {a["id"] for s in cfg["strategies"] for a in s["accounts"]}

    def reconcile(self):
        cfg = load_config()
        ids = self._all_account_ids(cfg)
        with self.lock:
            for aid in ids:
                if aid not in self.runners:
                    self.runners[aid] = BotRunner(aid)
            for aid in list(self.runners):
                if aid not in ids:
                    self.runners[aid].dispose()
                    del self.runners[aid]

    def get(self, aid):
        return self.runners.get(aid)

    def summary(self):
        cfg = load_config()
        out = []
        for s in cfg["strategies"]:
            for a in s["accounts"]:
                r = self.runners.get(a["id"])
                running = bool(r and r.proc is not None and r.proc.poll() is None)
                out.append({
                    "id": a["id"], "strategy_id": s["id"],
                    "name": a["name"], "symbol": a["symbol"],
                    "running": running, "desired": bool(r and r.desired),
                    "pid": r.proc.pid if running else None,
                    "equity": r.status["equity"] if r else None,
                    "position": r.status["position"] if r else None,
                    "warning": r.status["warning"] if r else None,
                })
        return out


MANAGER = Manager()
app = Flask(__name__)


# ── routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return PAGE


@app.route("/strategies")
def strategies():
    cfg = load_config()
    return jsonify([{"id": s["id"], "name": s["name"], "runner": s["runner"],
                     "n_accounts": len(s["accounts"])} for s in cfg["strategies"]])


@app.route("/strategy", methods=["POST"])
def strategy_save():
    cfg = load_config()
    f = request.form
    sid = (f.get("id") or "").strip()
    s = find_strategy(cfg, sid) if sid else None
    if s is None:
        s = {**DEFAULT_STRATEGY, "id": new_id(), "accounts": []}
        cfg["strategies"].append(s)
    s["name"] = (f.get("name") or s.get("name") or "Estrategia").strip()
    s["runner"] = (f.get("runner") or s.get("runner") or "run_live.py").strip()
    save_config(cfg)
    MANAGER.reconcile()
    return jsonify({"ok": True, "id": s["id"]})


@app.route("/strategy/delete", methods=["POST"])
def strategy_delete():
    cfg = load_config()
    sid = (request.form.get("id") or "").strip()
    s = find_strategy(cfg, sid)
    if s is not None:
        for a in s["accounts"]:                  # stop every bot in the strategy
            r = MANAGER.get(a["id"])
            if r:
                r.dispose()
        cfg["strategies"] = [x for x in cfg["strategies"] if x["id"] != sid]
    save_config(cfg)
    MANAGER.reconcile()
    return jsonify({"ok": True})


@app.route("/accounts")
def accounts():
    """Accounts of one strategy, WITHOUT the password (only whether one is set)."""
    cfg = load_config()
    s = find_strategy(cfg, (request.args.get("strategy") or "").strip())
    out = []
    for a in (s["accounts"] if s else []):
        d = {k: a.get(k) for k in ("id", "name", "symbol", "login", "server",
                                   "mt5_path", "risk_pct", "allow_real", "autostart")}
        d["has_password"] = bool(str(a.get("password", "")).strip())
        out.append(d)
    return jsonify(out)


@app.route("/account", methods=["POST"])
def account_save():
    cfg = load_config()
    f = request.form
    aid = (f.get("id") or "").strip()
    strat, acc = find_account(cfg, aid) if aid else (None, None)
    if acc is None:                              # create inside the given strategy
        strat = find_strategy(cfg, (f.get("strategy_id") or "").strip())
        if strat is None:
            return jsonify({"ok": False, "msg": "unknown strategy"}), 400
        acc = {**DEFAULT_ACCOUNT, "id": new_id()}
        strat["accounts"].append(acc)
    acc["name"] = (f.get("name") or acc.get("name") or "Cuenta").strip()
    acc["symbol"] = (f.get("symbol") or "XAUUSD").strip() or "XAUUSD"
    acc["login"] = (f.get("login") or "").strip()
    acc["server"] = (f.get("server") or "").strip()
    acc["mt5_path"] = (f.get("mt5_path") or "").strip()
    pw = f.get("password", "")
    if pw != "":                                 # blank keeps the saved password
        acc["password"] = pw
    try:
        acc["risk_pct"] = float(f.get("risk_pct", acc.get("risk_pct", 1.0)))
    except ValueError:
        pass
    acc["allow_real"] = f.get("allow_real") == "on"
    acc["autostart"] = f.get("autostart") == "on"
    save_config(cfg)
    MANAGER.reconcile()
    return jsonify({"ok": True, "id": acc["id"]})


@app.route("/account/delete", methods=["POST"])
def account_delete():
    cfg = load_config()
    aid = (request.form.get("id") or "").strip()
    r = MANAGER.get(aid)
    if r:
        r.dispose()
    strat, acc = find_account(cfg, aid)
    if strat is not None:
        strat["accounts"] = [a for a in strat["accounts"] if a["id"] != aid]
    save_config(cfg)
    MANAGER.reconcile()
    return jsonify({"ok": True})


def _runner_from_request():
    aid = (request.form.get("id") or request.args.get("id") or "").strip()
    return MANAGER.get(aid)


@app.route("/start", methods=["POST"])
def start():
    r = _runner_from_request()
    if r:
        r.start()
    return jsonify({"ok": bool(r)})


@app.route("/stop", methods=["POST"])
def stop():
    r = _runner_from_request()
    if r:
        r.stop()
    return jsonify({"ok": bool(r)})


@app.route("/ping", methods=["POST"])
def ping():
    """Ask the running bot for a fresh HEALTH line (it watches the ping file)."""
    aid = (request.form.get("id") or request.args.get("id") or "").strip()
    r = MANAGER.get(aid)
    running = r and r.proc is not None and r.proc.poll() is None
    if not running:
        return jsonify({"ok": False, "msg": "bot not running"})
    try:
        open(ping_path(aid), "w").close()
    except Exception as exc:
        return jsonify({"ok": False, "msg": repr(exc)})
    return jsonify({"ok": True})


@app.route("/shutdown", methods=["POST"])
def shutdown():
    """Stop every bot and shut the panel down. Writes a flag so run_app.bat does
    NOT relaunch it — restart by opening run_app.vbs (or run_app.bat) again."""
    for r in list(MANAGER.runners.values()):
        r.dispose()
        if r.proc is not None and r.proc.poll() is None:
            try:
                r.proc.terminate()
            except Exception:
                pass
    try:
        open(SHUTDOWN_FLAG, "w").close()
    except Exception:
        pass

    def _bye():
        time.sleep(1.0)
        os._exit(0)
    threading.Thread(target=_bye, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/status")
def status():
    return jsonify(MANAGER.summary())


@app.route("/log")
def log():
    after = request.args.get("after", 0, type=int)
    r = _runner_from_request()
    if not r:
        return jsonify({"lines": [], "last": 0, "running": False, "desired": False,
                        "pid": None, "equity": None, "position": None,
                        "health": None, "warning": None})
    return jsonify(r.snapshot(after))


# ── page ─────────────────────────────────────────────────────────────────────
PAGE = r"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trading Hub</title>
<style>
 :root{
  --bg:#0c0d10; --panel:#131419; --panel2:#181a20; --raise:#1e2027;
  --line:#23252c; --line2:#2d2f38; --text:#e7e9ee; --muted:#8b919c; --faint:#5b616c;
  --accent:#5b93f0; --live:#46c08a; --danger:#e5564e; --warn:#e3a857;
  --mono:ui-monospace,"JetBrains Mono","SF Mono",Menlo,Consolas,monospace;
 }
 *{box-sizing:border-box;margin:0;padding:0}
 body{font:14px/1.5 -apple-system,"Segoe UI",Inter,system-ui,Roboto,sans-serif;background:var(--bg);color:var(--text);height:100vh;overflow:hidden;-webkit-font-smoothing:antialiased}
 button{font:inherit;cursor:pointer;border:0;background:none;color:inherit}
 input{font:inherit}
 .micro{font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--faint);font-weight:600}
 .muted{color:var(--muted)} .mono{font-family:var(--mono)}

 .app{display:grid;grid-template-columns:236px 1fr;height:100vh}

 /* sidebar */
 .side{background:var(--panel);border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
 .brand{display:flex;align-items:center;gap:10px;padding:16px 18px 14px}
 .brand .mk{width:22px;height:22px;border-radius:6px;background:linear-gradient(135deg,var(--accent),#7ab0ff);position:relative;flex:0 0 auto}
 .brand .mk::after{content:"";position:absolute;inset:6px;border-radius:3px;background:var(--panel)}
 .brand b{font-size:14px;letter-spacing:-.01em}
 .side .lbl{padding:10px 18px 6px}
 .slist{flex:1;overflow:auto;padding:0 10px}
 .sitem{display:flex;align-items:center;gap:9px;padding:9px 10px;border-radius:8px;cursor:pointer;color:var(--muted);position:relative}
 .sitem:hover{background:var(--panel2);color:var(--text)}
 .sitem.sel{background:var(--panel2);color:var(--text)}
 .sitem.sel::before{content:"";position:absolute;left:-10px;top:8px;bottom:8px;width:3px;border-radius:2px;background:var(--accent)}
 .sitem .nm{flex:1;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .sitem .cnt{font-size:11px;color:var(--faint);font-variant-numeric:tabular-nums}
 .dot{width:7px;height:7px;border-radius:50%;background:var(--faint);flex:0 0 auto}
 .dot.live{background:var(--live);box-shadow:0 0 0 3px rgba(70,192,138,.16)}
 .dot.warn{background:var(--warn)} .dot.wait{background:var(--accent)}
 .side .foot{border-top:1px solid var(--line);padding:12px;display:flex;flex-direction:column;gap:9px}
 .addbtn{display:flex;align-items:center;gap:7px;justify-content:center;padding:9px;border-radius:8px;border:1px dashed var(--line2);color:var(--muted);font-weight:500;font-size:13px}
 .addbtn:hover{color:var(--text);border-color:var(--faint)}

 /* main */
 .main{display:flex;flex-direction:column;min-width:0;min-height:0}
 .top{height:54px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;padding:0 20px;flex:0 0 auto}
 .top .title{font-weight:600;font-size:15px}
 .top .sub{color:var(--faint);font-size:12px}
 .top .sp{flex:1}
 .top .stat{color:var(--muted);font-size:12px;display:flex;gap:6px;align-items:center}
 .panes{flex:1;display:grid;grid-template-columns:280px 1fr;min-height:0}

 /* account column */
 .acol{border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0;background:var(--panel)}
 .acol .head{display:flex;align-items:center;justify-content:space-between;padding:14px 16px 10px}
 .alist{flex:1;overflow:auto;padding:0 10px 12px;display:flex;flex-direction:column;gap:6px}
 .acard{padding:11px 12px;border:1px solid var(--line);border-radius:9px;cursor:pointer;background:var(--panel2);transition:border-color .12s}
 .acard:hover{border-color:var(--line2)}
 .acard.sel{border-color:var(--accent);background:var(--raise)}
 .acard .r1{display:flex;align-items:center;gap:8px}
 .acard .nm{flex:1;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .acard .r2{display:flex;align-items:center;gap:8px;margin-top:6px}
 .chip{font-size:11px;color:var(--muted);background:#22242b;border:1px solid var(--line);padding:1px 7px;border-radius:5px;font-family:var(--mono)}
 .acard .st{font-size:11px;color:var(--faint);margin-left:auto}
 .iconbtn{width:26px;height:26px;border-radius:6px;display:flex;align-items:center;justify-content:center;color:var(--muted);border:1px solid var(--line2);font-size:12px}
 .iconbtn:hover{color:var(--text);background:var(--panel2)}
 .iconbtn.go:hover{color:var(--live);border-color:var(--live)} .iconbtn.no:hover{color:var(--danger);border-color:var(--danger)}

 /* detail */
 .detail{display:flex;flex-direction:column;min-height:0;min-width:0;padding:18px 20px;gap:14px;overflow:auto}
 .dhead{display:flex;align-items:center;gap:12px}
 .dhead h2{font-size:16px;font-weight:600}
 .badge{font-size:11px;font-weight:600;padding:3px 9px;border-radius:999px;letter-spacing:.04em;background:#24262d;color:var(--muted)}
 .badge.live{background:rgba(70,192,138,.14);color:var(--live)}
 .badge.wait{background:rgba(91,147,240,.14);color:var(--accent)}
 .tiles{display:flex;gap:10px;margin-left:auto}
 .tile{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:7px 12px;min-width:84px}
 .tile .k{font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--faint)}
 .tile .v{font-family:var(--mono);font-size:14px;margin-top:2px;font-variant-numeric:tabular-nums}
 .banner{background:rgba(229,86,78,.1);border:1px solid rgba(229,86,78,.4);color:#ffb3ae;padding:9px 12px;border-radius:8px;font-size:13px;font-weight:500}
 .card{background:var(--panel);border:1px solid var(--line);border-radius:10px}
 .card .ct{padding:11px 15px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:10px}
 .card .ct b{font-size:13px;font-weight:600}
 .card .cb{padding:15px}
 form .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px 14px}
 form label{display:block}
 form .lab{font-size:11px;color:var(--muted);margin-bottom:5px;letter-spacing:.02em}
 input[type=text],input[type=number],input[type=password]{width:100%;padding:8px 10px;background:var(--bg);border:1px solid var(--line2);border-radius:7px;color:var(--text);outline:none}
 input:focus{border-color:var(--accent)}
 .full{grid-column:1/-1}
 .switch{display:flex;align-items:center;gap:9px;cursor:pointer;user-select:none}
 .switch input{position:absolute;opacity:0}
 .track{width:36px;height:20px;border-radius:999px;background:#2a2d35;position:relative;transition:background .15s;flex:0 0 auto}
 .track::after{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:#9aa0ab;transition:.15s}
 .switch input:checked+.track{background:var(--accent)} .switch input:checked+.track::after{left:18px;background:#fff}
 .switch.danger input:checked+.track{background:var(--danger)}
 .hint{color:var(--faint);font-size:12px;margin-top:8px}
 .actions{display:flex;gap:9px;margin-top:16px;flex-wrap:wrap}
 .btn{padding:9px 15px;border-radius:8px;font-weight:600;font-size:13px;border:1px solid transparent;display:inline-flex;align-items:center;gap:7px}
 .btn:disabled{opacity:.4;cursor:not-allowed}
 .btn.primary{background:var(--accent);color:#0a1020}
 .btn.go{background:var(--live);color:#04140d}
 .btn.no{background:var(--danger);color:#fff}
 .btn.ghost{background:transparent;border-color:var(--line2);color:var(--text)}
 .btn.ghost:hover{border-color:var(--faint)}
 .btn.gho-danger{background:transparent;border-color:rgba(229,86,78,.4);color:#ef8b85}
 .btn.gho-danger:hover{background:rgba(229,86,78,.1)}
 .logwrap{flex:1;min-height:160px}
 .log{height:100%;min-height:140px;overflow:auto;background:#090a0d;border:1px solid var(--line);border-radius:10px;padding:12px 14px;font-family:var(--mono);font-size:12px;line-height:1.55;white-space:pre-wrap;word-break:break-word;color:#c5cad3}
 .hl{color:var(--faint);font-family:var(--mono);font-size:11px}
 .empty{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;color:var(--faint);text-align:center;padding:30px}
 .hidden{display:none!important}

 /* modal */
 .overlay{position:fixed;inset:0;background:rgba(6,7,10,.62);backdrop-filter:blur(3px);display:flex;align-items:center;justify-content:center;z-index:50;animation:fade .12s ease}
 .modal{background:var(--panel2);border:1px solid var(--line2);border-radius:14px;width:430px;max-width:92vw;box-shadow:0 24px 70px rgba(0,0,0,.55);overflow:hidden;animation:pop .14s ease}
 @keyframes fade{from{opacity:0}} @keyframes pop{from{opacity:0;transform:translateY(6px) scale(.99)}}
 .modal .mh{padding:17px 20px 4px;font-size:15px;font-weight:600}
 .modal .mb{padding:8px 20px 4px;color:var(--muted);font-size:13.5px;line-height:1.6}
 .modal .mb input{margin-top:6px}
 .modal .mf{padding:16px 20px;display:flex;justify-content:flex-end;gap:10px}
 .scroll::-webkit-scrollbar{width:10px;height:10px}
 .scroll::-webkit-scrollbar-thumb{background:#262932;border-radius:6px;border:2px solid transparent;background-clip:padding-box}
</style></head><body>
<div class="app">
 <aside class="side">
   <div class="brand"><div class="mk"></div><b>Trading Hub</b></div>
   <div class="lbl micro">Estrategias</div>
   <div class="slist scroll" id="slist"></div>
   <div class="foot">
     <button class="addbtn" onclick="openStrategyModal()">+ Nueva estrategia</button>
     <div class="stat muted" id="gstat" style="font-size:12px;padding:0 4px"></div>
     <button class="btn gho-danger" onclick="confirmShutdown()" style="justify-content:center">Apagar todo</button>
   </div>
 </aside>

 <main class="main">
   <div class="top">
     <div><div class="title" id="stratName">—</div><div class="sub" id="stratSub"></div></div>
     <div class="sp"></div>
     <button class="iconbtn" title="Renombrar estrategia" onclick="openStrategyModal(true)">✎</button>
     <button class="iconbtn no" title="Eliminar estrategia" onclick="confirmDeleteStrategy()">🗑</button>
   </div>
   <div class="panes">
     <div class="acol">
       <div class="head"><span class="micro">Cuentas</span>
         <button class="iconbtn go" title="Añadir cuenta" onclick="addAccount()">＋</button></div>
       <div class="alist scroll" id="alist"></div>
     </div>

     <div class="detail scroll hidden" id="detail">
       <div class="dhead">
         <h2 id="dName">—</h2><span class="badge" id="dBadge">—</span>
         <div class="tiles">
           <div class="tile"><div class="k">Equity</div><div class="v" id="tEq">—</div></div>
           <div class="tile"><div class="k">Posición</div><div class="v" id="tPos">—</div></div>
           <div class="tile"><div class="k">PID</div><div class="v" id="tPid">—</div></div>
         </div>
       </div>
       <div class="banner hidden" id="dWarn"></div>

       <div class="card">
         <div class="ct"><b>Configuración</b><span class="sp" style="flex:1"></span>
           <span class="hl" id="dHealth"></span></div>
         <div class="cb">
           <form id="cfg" autocomplete="off">
             <input type="hidden" name="id" id="fid"><input type="hidden" name="strategy_id" id="fsid">
             <div class="grid">
               <label><div class="lab">Nombre</div><input type="text" name="name" id="fname"></label>
               <label><div class="lab">Símbolo</div><input type="text" name="symbol" id="fsymbol"></label>
               <label><div class="lab">Login</div><input type="text" name="login" id="flogin"></label>
               <label><div class="lab">Servidor</div><input type="text" name="server" id="fserver"></label>
               <label><div class="lab">Contraseña</div><input type="password" name="password" id="fpassword"></label>
               <label><div class="lab">Riesgo por trade (%)</div><input type="number" step="0.05" name="risk_pct" id="frisk"></label>
               <label class="full"><div class="lab">Ruta terminal64.exe (una por cuenta)</div><input type="text" name="mt5_path" id="fmt5"></label>
             </div>
             <div style="display:flex;gap:26px;margin-top:14px;flex-wrap:wrap">
               <label class="switch danger"><input type="checkbox" name="allow_real" id="freal"><span class="track"></span><span>Permitir cuenta REAL</span></label>
               <label class="switch"><input type="checkbox" name="autostart" id="fauto"><span class="track"></span><span>Arrancar al abrir la app</span></label>
             </div>
             <div class="actions">
               <button type="button" class="btn primary" onclick="saveAccount()">Guardar</button>
               <button type="button" class="btn go" id="bStart" onclick="startBot()">Arrancar</button>
               <button type="button" class="btn no" id="bStop" onclick="stopBot()">Parar</button>
               <button type="button" class="btn ghost" id="bPing" onclick="pingBot()">Ping</button>
               <button type="button" class="btn gho-danger" style="margin-left:auto" onclick="confirmDeleteAccount()">Eliminar cuenta</button>
             </div>
           </form>
         </div>
       </div>

       <div class="card logwrap" style="display:flex;flex-direction:column">
         <div class="ct"><b>Salida en vivo</b></div>
         <div style="flex:1;padding:10px"><div class="log scroll" id="log"></div></div>
       </div>
     </div>

     <div class="empty hidden" id="empty"></div>
   </div>
 </main>
</div>
<div class="overlay hidden" id="overlay"><div class="modal" id="modal"></div></div>

<script>
let STRATS=[], ACCS=[], STAT={}, selS=null, selA=null, logLast=0;
const $=id=>document.getElementById(id);
const esc=s=>(s==null?'':(''+s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function statusOf(id){const s=STAT[id]||{};return s.running?['EN MARCHA','live']:(s.desired?['reiniciando','wait']:['parado','']);}
function form(d){const fd=new FormData();for(const k in d)fd.append(k,d[k]);return fd;}
function post(u,d){return fetch(u,{method:'POST',body:form(d||{})}).then(r=>r.json());}

/* ---- modal framework ---- */
function showModal(html){$('modal').innerHTML=html;$('overlay').classList.remove('hidden');}
function closeModal(){$('overlay').classList.add('hidden');}
$('overlay').addEventListener('mousedown',e=>{if(e.target===$('overlay'))closeModal();});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal();});
function confirmModal({title,body,confirm='Confirmar',danger=true,onOk}){
  showModal(`<div class="mh">${esc(title)}</div><div class="mb">${body}</div>
    <div class="mf"><button class="btn ghost" onclick="closeModal()">Cancelar</button>
    <button class="btn ${danger?'no':'primary'}" id="mOk">${esc(confirm)}</button></div>`);
  $('mOk').onclick=()=>{closeModal();onOk&&onOk();};
}

/* ---- sidebar / strategies ---- */
function liveCount(sid){return Object.values(STAT).filter(s=>s.strategy_id===sid&&s.running).length;}
function warnIn(sid){return Object.values(STAT).some(s=>s.strategy_id===sid&&s.warning);}
function renderStrats(){
  const L=$('slist');L.innerHTML='';
  let totLive=0;
  for(const s of STRATS){
    const live=liveCount(s.id);totLive+=live;
    const cls=live?'live':(warnIn(s.id)?'warn':'');
    const d=document.createElement('div');d.className='sitem'+(s.id===selS?' sel':'');
    d.onclick=()=>selectStrategy(s.id);
    d.innerHTML=`<span class="dot ${cls}"></span><span class="nm">${esc(s.name)}</span>
      <span class="cnt">${live?live+'/':''}${s.n_accounts}</span>`;
    L.appendChild(d);
  }
  $('gstat').textContent=`${STRATS.length} estrategia(s) · ${totLive} bot(s) en marcha`;
}
function curStrat(){return STRATS.find(s=>s.id===selS);}
function selectStrategy(id){
  selS=id;selA=null;
  const s=curStrat();
  $('stratName').textContent=s?s.name:'—';
  $('stratSub').textContent=s?('runner: '+s.runner):'';
  renderStrats();
  loadAccounts();
}

/* ---- accounts ---- */
function loadStrategies(){return fetch('/strategies').then(r=>r.json()).then(d=>{
  STRATS=d;
  if(!selS||!curStrat())selS=STRATS.length?STRATS[0].id:null;
  const s=curStrat();
  $('stratName').textContent=s?s.name:'—';$('stratSub').textContent=s?('runner: '+s.runner):'';
  renderStrats();
});}
function loadAccounts(){
  if(!selS){ACCS=[];renderAccts();showEmpty();return;}
  return fetch('/accounts?strategy='+selS).then(r=>r.json()).then(d=>{
    ACCS=d;
    if(selA&&!ACCS.find(a=>a.id===selA))selA=null;
    if(!selA&&ACCS.length)selectAccount(ACCS[0].id);
    else if(!ACCS.length)showEmpty();
    renderAccts();
  });
}
function showEmpty(){
  selA=null;$('detail').classList.add('hidden');
  const e=$('empty');e.classList.remove('hidden');
  e.innerHTML=`<div style="font-size:15px;color:var(--muted)">Sin cuentas en esta estrategia</div>
    <div>Añade una cuenta con el botón ＋ de la izquierda.</div>`;
}
function renderAccts(){
  const L=$('alist');L.innerHTML='';
  if(!ACCS.length){return;}
  for(const a of ACCS){
    const s=STAT[a.id]||{},[txt,cls]=statusOf(a.id);
    const dot=s.warning?'warn':cls;
    const d=document.createElement('div');d.className='acard'+(a.id===selA?' sel':'');
    d.onclick=()=>selectAccount(a.id);
    d.innerHTML=`<div class="r1"><span class="dot ${dot}"></span><span class="nm">${esc(a.name)}</span>
      <button class="iconbtn ${s.desired?'no':'go'}" title="${s.desired?'Parar':'Arrancar'}"
        onclick="event.stopPropagation();${s.desired?'stopBot':'startBot'}('${a.id}')">${s.desired?'■':'▶'}</button></div>
      <div class="r2"><span class="chip">${esc(a.symbol)}</span><span class="chip">${a.risk_pct}%</span>
      <span class="st">${esc(txt)}</span></div>`;
    L.appendChild(d);
  }
}
function selectAccount(id){
  selA=id;logLast=0;$('log').textContent='';setHealth('',null);
  const a=ACCS.find(x=>x.id===id);if(!a)return;
  $('empty').classList.add('hidden');$('detail').classList.remove('hidden');
  $('fid').value=a.id;$('fsid').value=selS;$('fname').value=a.name;$('fsymbol').value=a.symbol;
  $('flogin').value=a.login||'';$('fserver').value=a.server||'';$('fmt5').value=a.mt5_path||'';
  $('frisk').value=a.risk_pct;$('freal').checked=!!a.allow_real;$('fauto').checked=!!a.autostart;
  $('fpassword').value='';$('fpassword').placeholder=a.has_password?'•••••• (guardada)':'';
  $('dName').textContent=a.name;
  renderAccts();
}
function addAccount(){
  if(!selS)return;
  post('/account',{strategy_id:selS,name:'Cuenta '+(ACCS.length+1)}).then(j=>{
    selA=j.id;loadAccounts();
  });
}
function saveAccount(){
  if(!selA&&!$('fid').value)return;
  fetch('/account',{method:'POST',body:new FormData($('cfg'))}).then(r=>r.json()).then(()=>{
    $('fpassword').value='';loadStrategies().then(loadAccounts);
  });
}
function confirmDeleteAccount(){
  const a=ACCS.find(x=>x.id===selA);if(!a)return;
  confirmModal({title:'Eliminar cuenta',body:`Se eliminará <b>${esc(a.name)}</b> y se detendrá su bot. Las posiciones abiertas siguen protegidas por su SL/TP en el broker.`,
    confirm:'Eliminar',onOk:()=>post('/account/delete',{id:selA}).then(()=>{selA=null;loadStrategies().then(loadAccounts);})});
}
function startBot(id){post('/start',{id:id||selA});}
function stopBot(id){post('/stop',{id:id||selA});}
function pingBot(){if(!selA)return;post('/ping',{id:selA}).then(j=>{if(!j.ok)setHealth('ping: '+(j.msg||'—'),'No se pudo hacer ping ('+(j.msg||'bot parado')+')');});}
function setHealth(h,w){$('dHealth').textContent=h||'';const b=$('dWarn');
  if(w){b.textContent='⚠ '+w;b.classList.remove('hidden');}else b.classList.add('hidden');}

/* ---- strategy modals ---- */
function openStrategyModal(edit){
  const s=edit?curStrat():null;if(edit&&!s)return;
  showModal(`<div class="mh">${edit?'Renombrar estrategia':'Nueva estrategia'}</div>
    <div class="mb">Nombre<input type="text" id="msName" value="${esc(s?s.name:'')}" placeholder="p. ej. IFVG">
      <div style="margin-top:12px">Script runner<input type="text" id="msRunner" value="${esc(s?s.runner:'run_live.py')}"></div>
      <div class="hint">El runner es el script Python que ejecuta la estrategia (por defecto run_live.py = IFVG).</div></div>
    <div class="mf"><button class="btn ghost" onclick="closeModal()">Cancelar</button>
      <button class="btn primary" id="msOk">${edit?'Guardar':'Crear'}</button></div>`);
  $('msName').focus();
  $('msOk').onclick=()=>{
    const d={name:$('msName').value,runner:$('msRunner').value};if(edit)d.id=s.id;
    post('/strategy',d).then(j=>{closeModal();if(!edit)selS=j.id;loadStrategies().then(loadAccounts);});
  };
}
function confirmDeleteStrategy(){
  const s=curStrat();if(!s)return;
  confirmModal({title:'Eliminar estrategia',body:`Se eliminará <b>${esc(s.name)}</b> y todas sus cuentas, deteniendo sus bots.`,
    confirm:'Eliminar',onOk:()=>post('/strategy/delete',{id:s.id}).then(()=>{selS=null;selA=null;loadStrategies().then(loadAccounts);})});
}
function confirmShutdown(){
  confirmModal({title:'Apagar todo',
    body:'Se pararán <b>todos los bots</b> de todas las estrategias y se cerrará el panel. Las posiciones abiertas siguen protegidas por su SL/TP en el broker.<br><br>Para volver, abre <b>run_app.vbs</b>.',
    confirm:'Apagar todo',onOk:()=>{fetch('/shutdown',{method:'POST'}).catch(()=>{}).finally(()=>{
      document.body.innerHTML='<div style="height:100vh;display:flex;align-items:center;justify-content:center;color:var(--muted);font:15px system-ui;text-align:center">Panel apagado y bots parados.<br>Puedes cerrar esta pestaña. Para volver, abre <b style="color:var(--text)">run_app.vbs</b>.</div>';});}});
}

/* ---- poll ---- */
function tick(){
  fetch('/status').then(r=>r.json()).then(list=>{
    STAT={};for(const s of list)STAT[s.id]=s;
    renderStrats();renderAccts();
    if(selA){const s=STAT[selA]||{},[txt,cls]=statusOf(selA);
      const b=$('dBadge');b.textContent=txt;b.className='badge'+(cls?' '+cls:'');
      $('tEq').textContent=s.equity??'—';$('tPos').textContent=s.position??'—';$('tPid').textContent=s.pid??'—';
      $('bStart').disabled=!!s.desired;$('bStop').disabled=!s.desired;$('bPing').disabled=!s.running;}
  }).catch(()=>{});
  if(selA){fetch('/log?id='+selA+'&after='+logLast).then(r=>r.json()).then(s=>{
    if(s.lines&&s.lines.length){const L=$('log');const bot=L.scrollTop+L.clientHeight>=L.scrollHeight-30;
      for(const l of s.lines){L.textContent+=l.text+'\n';logLast=l.seq;}if(bot)L.scrollTop=L.scrollHeight;}
    setHealth(s.health,s.warning);
  }).catch(()=>{});}
  setTimeout(tick,1500);
}
loadStrategies().then(loadAccounts).then(tick);
</script>
</body></html>"""


def main():
    try:                                  # clear any stale shutdown flag from a prior run
        os.remove(SHUTDOWN_FLAG)
    except OSError:
        pass
    cfg = load_config()
    for s in cfg["strategies"]:
        for a in s["accounts"]:
            if a.get("autostart"):
                r = MANAGER.get(a["id"])
                if r:
                    r.start()
    # auto-open the panel on launch — skip it for unattended/boot runs by setting
    # IFVG_NO_BROWSER=1 (so a hidden Task Scheduler start does not pop a browser).
    if not os.environ.get("IFVG_NO_BROWSER"):
        try:
            webbrowser.open(f"http://{HOST}:{PORT}")
        except Exception:
            pass
    app.run(host=HOST, port=PORT, threaded=True, debug=False)


if __name__ == "__main__":
    main()
