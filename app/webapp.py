"""
app/webapp.py  —  local web dashboard + 24/7 supervisor for the IFVG live bot
=============================================================================
A small Flask app you open in the VPS browser to configure, start/stop and
WATCH the live bot(s) — no editing Python or command-line flags.

MULTI-ACCOUNT: it manages a LIST of accounts, each fully independent (its own
symbol / login / server / password / MT5 terminal / risk / real-or-demo). Each
account is supervised as its own run_live.py child process pointed at its own
MT5 terminal via --mt5-path. (The MetaTrader5 Python module allows one account
per process/terminal, so N accounts = N terminals = N processes.)

It does NOT re-implement the strategy. It SUPERVISES run_live.py: launches each
account with its settings, streams its output into the page, and (like
run_live.bat) relaunches it if it ever exits while you want it running. Stopping
a bot leaves any open trade protected by its broker-side SL/TP; the next start
re-seeds already-traded zones (see run_live.py).

SECURITY (read once):
  * Binds to 127.0.0.1 only — the panel shows broker credentials and controls
    live trading, so it is NOT exposed to the network. View it in the VPS's own
    browser (over your RDP session).
  * Each account's password is passed to its child via an environment variable,
    not the command line (so it does not show in the process list).
  * config.json stores all accounts INCLUDING passwords in plain text on the
    VPS. It is git-ignored. Treat the VPS as a machine that holds them.

RUN:  python app/webapp.py        (or double-click run_app.bat)
      then open http://127.0.0.1:8765
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

from flask import Flask, jsonify, render_template_string, request

# ── paths ────────────────────────────────────────────────────────────────────
APP_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(APP_DIR)            # run_live.py + ifvg/ live here
RUN_LIVE = os.path.join(REPO_ROOT, "run_live.py")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOG_DIR = os.path.join(REPO_ROOT, "logs")       # per-account live logs (<id>.log)
os.makedirs(LOG_DIR, exist_ok=True)

HOST, PORT = "127.0.0.1", 8765
RESTART_BACKOFF_S = 10          # wait before relaunching a crashed bot (like the .bat)
LOG_LINES = 4000                # ring-buffer of recent output kept in memory, per account

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


# ── config persistence (a list of accounts) ─────────────────────────────────
def new_id():
    return uuid.uuid4().hex[:8]


def load_config():
    cfg = {"accounts": []}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
        except Exception:
            cfg = {"accounts": []}
    if "accounts" not in cfg:
        # migrate an old single-account config into the new list shape
        if "symbol" in cfg:
            acc = {**DEFAULT_ACCOUNT, "id": new_id()}
            for k in DEFAULT_ACCOUNT:
                if k in cfg:
                    acc[k] = cfg[k]
            cfg = {"accounts": [acc]}
        else:
            cfg = {"accounts": []}
    for a in cfg["accounts"]:
        a.setdefault("id", new_id())
        for k, v in DEFAULT_ACCOUNT.items():
            a.setdefault(k, v)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


def get_account(cfg, aid):
    return next((a for a in cfg["accounts"] if a["id"] == aid), None)


def build_cmd_env(acc):
    """The run_live.py command + environment for one account. Poll is NOT exposed
    (fixed at run_live.py's default); the password goes via env, not argv."""
    cmd = [sys.executable, "-u", RUN_LIVE,
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
        self.status = {"equity": None, "position": None, "started_at": None}
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
        if "equity=" in text:
            try:
                self.status["equity"] = text.split("equity=")[1].split()[0]
            except Exception:
                pass
        if "position=" in text:
            try:
                self.status["position"] = text.split("position=")[1].split()[0]
            except Exception:
                pass

    # --- process lifecycle ---
    def _start(self):
        acc = get_account(load_config(), self.id)
        if acc is None:                      # account was deleted
            self.desired = False
            return
        cmd, env = build_cmd_env(acc)
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
                "lines": new, "last": self.seq,
            }


# ── manager: one BotRunner per account, kept in sync with the config ─────────
class Manager:
    def __init__(self):
        self.runners = {}
        self.lock = threading.Lock()
        self.reconcile()

    def reconcile(self):
        cfg = load_config()
        ids = {a["id"] for a in cfg["accounts"]}
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
        for a in cfg["accounts"]:
            r = self.runners.get(a["id"])
            running = bool(r and r.proc is not None and r.proc.poll() is None)
            out.append({
                "id": a["id"], "name": a["name"], "symbol": a["symbol"],
                "running": running, "desired": bool(r and r.desired),
                "equity": r.status["equity"] if r else None,
                "position": r.status["position"] if r else None,
            })
        return out


MANAGER = Manager()
app = Flask(__name__)


# ── routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/accounts")
def accounts():
    """All accounts WITHOUT the password (only whether one is set)."""
    cfg = load_config()
    out = []
    for a in cfg["accounts"]:
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
    acc = get_account(cfg, aid) if aid else None
    if acc is None:
        acc = {**DEFAULT_ACCOUNT, "id": new_id()}
        cfg["accounts"].append(acc)
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
        r.dispose()                              # stop the bot before removing it
    cfg["accounts"] = [a for a in cfg["accounts"] if a["id"] != aid]
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


@app.route("/status")
def status():
    return jsonify(MANAGER.summary())


@app.route("/log")
def log():
    after = request.args.get("after", 0, type=int)
    r = _runner_from_request()
    if not r:
        return jsonify({"lines": [], "last": 0, "running": False, "desired": False,
                        "pid": None, "equity": None, "position": None})
    return jsonify(r.snapshot(after))


# ── page (inline; no external assets so it works offline on the VPS) ─────────
PAGE = r"""
<!doctype html><html lang="es"><head><meta charset="utf-8">
<title>IFVG bots</title>
<style>
 :root{--bg:#0f1115;--card:#1a1d24;--fg:#e6e6e6;--mut:#8b93a1;--ok:#28c76f;--off:#6b7280;--bad:#ea5455;--acc:#3b82f6}
 *{box-sizing:border-box} body{margin:0;font:14px/1.45 system-ui,Segoe UI,Arial;background:var(--bg);color:var(--fg)}
 header{display:flex;align-items:center;gap:12px;padding:11px 18px;border-bottom:1px solid #262a33}
 h1{font-size:16px;margin:0;font-weight:600}
 .sub{color:var(--mut);font-size:12px}
 .wrap{display:grid;grid-template-columns:300px 1fr;gap:16px;padding:16px;height:calc(100vh - 50px)}
 .card{background:var(--card);border:1px solid #262a33;border-radius:10px;padding:14px}
 .col{display:flex;flex-direction:column;min-height:0}
 label{display:block;margin:9px 0 3px;color:var(--mut);font-size:12px}
 input[type=text],input[type=number],input[type=password]{width:100%;padding:7px 9px;background:#11141a;border:1px solid #2b303b;border-radius:7px;color:var(--fg)}
 .row{display:flex;gap:10px}.row>div{flex:1}
 .chk{display:flex;align-items:center;gap:8px;margin-top:10px;color:var(--fg)}
 button{cursor:pointer;border:0;border-radius:8px;padding:8px 13px;font-weight:600;color:#fff;font-size:13px}
 button:disabled{opacity:.4;cursor:not-allowed}
 .btns{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}
 .start{background:var(--ok)} .stop{background:var(--bad)} .save{background:var(--acc)} .del{background:#3a3f4b}
 .add{background:var(--acc);width:100%;margin-bottom:10px}
 .warn{color:var(--bad);font-size:12px;margin-top:8px}
 .list{overflow:auto;display:flex;flex-direction:column;gap:7px}
 .item{padding:9px 11px;border:1px solid #2b303b;border-radius:9px;cursor:pointer;background:#12151b}
 .item.sel{border-color:var(--acc);background:#161b27}
 .item .top{display:flex;align-items:center;gap:8px}
 .item .nm{font-weight:600;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .item .meta{color:var(--mut);font-size:12px;margin-top:3px;display:flex;gap:12px}
 .dot{width:9px;height:9px;border-radius:50%;flex:0 0 auto;background:var(--off)}
 .dot.on{background:var(--ok)} .dot.wait{background:var(--acc)}
 .mini{padding:3px 8px;font-size:12px;border-radius:6px}
 .empty{color:var(--mut);font-size:13px;text-align:center;padding:20px}
 .detail{display:grid;grid-template-rows:auto 1fr;gap:14px;min-height:0}
 .badge{padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;background:var(--off)}
 .stat{display:flex;gap:16px;color:var(--mut);font-size:12px;margin-left:auto}.stat b{color:var(--fg)}
 #log{height:100%;overflow:auto;background:#0a0c10;border-radius:8px;padding:10px;font:12px/1.5 Consolas,monospace;white-space:pre-wrap;border:1px solid #1d2027}
 .logcard{display:flex;flex-direction:column;min-height:0}.ttl{font-size:12px;color:var(--mut);margin:0 0 8px}
 .hidden{display:none}
</style></head><body>
<header><h1>IFVG bots</h1><span class="sub" id="hcount">—</span></header>
<div class="wrap">
 <div class="card col">
   <button class="add" onclick="addAccount()">+ Añadir cuenta</button>
   <div class="list" id="list"></div>
 </div>
 <div class="card col detail" id="detail">
   <div>
    <div style="display:flex;align-items:center;gap:10px">
      <span id="badge" class="badge">—</span>
      <div class="stat"><span>equity <b id="eq">—</b></span><span>posición <b id="pos">—</b></span><span>pid <b id="pid">—</b></span></div>
    </div>
    <form id="cfg">
     <input type="hidden" name="id" id="fid">
     <div class="row">
      <div><label>Nombre</label><input type="text" name="name" id="fname"></div>
      <div><label>Símbolo</label><input type="text" name="symbol" id="fsymbol"></div>
     </div>
     <div class="row">
      <div><label>Login</label><input type="text" name="login" id="flogin"></div>
      <div><label>Servidor</label><input type="text" name="server" id="fserver"></div>
     </div>
     <label>Contraseña</label><input type="password" name="password" id="fpassword">
     <label>Ruta terminal64.exe (uno por cuenta)</label><input type="text" name="mt5_path" id="fmt5">
     <label>Riesgo por trade (%)</label><input type="number" step="0.05" name="risk_pct" id="frisk">
     <label class="chk"><input type="checkbox" name="allow_real" id="freal"> Permitir cuenta REAL</label>
     <label class="chk"><input type="checkbox" name="autostart" id="fauto"> Arrancar al abrir la app</label>
     <div id="warn" class="warn hidden">⚠ Cuenta real activada: operará con dinero real.</div>
     <div class="btns">
      <button type="button" class="save"  onclick="saveAccount()">Guardar</button>
      <button type="button" class="start" id="bStart" onclick="startBot()">▶ Arrancar</button>
      <button type="button" class="stop"  id="bStop"  onclick="stopBot()">■ Parar</button>
      <button type="button" class="del"   onclick="delAccount()">Eliminar</button>
     </div>
    </form>
   </div>
   <div class="card logcard"><p class="ttl">Salida en vivo</p><div id="log"></div></div>
 </div>
 <div class="card col hidden" id="noSel"><div class="empty">Selecciona o añade una cuenta.</div></div>
</div>
<script>
let ACC=[], STAT={}, sel=null, logLast=0;

function badge(id){const s=STAT[id]||{};return s.running?['EN MARCHA','on']:(s.desired?['reiniciando…','wait']:['PARADO','']);}
function renderList(){
  const L=document.getElementById('list');
  document.getElementById('hcount').textContent=ACC.length+' cuenta(s), '+Object.values(STAT).filter(s=>s.running).length+' en marcha';
  if(!ACC.length){L.innerHTML='<div class="empty">Sin cuentas todavía.</div>';return;}
  L.innerHTML='';
  for(const a of ACC){
    const s=STAT[a.id]||{}, [txt,cls]=badge(a.id);
    const d=document.createElement('div'); d.className='item'+(a.id===sel?' sel':'');
    d.onclick=()=>select(a.id);
    d.innerHTML=`<div class="top"><span class="dot ${cls}"></span><span class="nm">${esc(a.name)}</span>
      <button class="mini ${s.desired?'stop':'start'}" onclick="event.stopPropagation();${s.desired?'stopBot':'startBot'}('${a.id}')">${s.desired?'■':'▶'}</button></div>
      <div class="meta"><span>${esc(a.symbol)}</span><span>${a.risk_pct}%</span><span>${esc(txt)}</span></div>`;
    L.appendChild(d);
  }
}
function select(id){
  sel=id; logLast=0; document.getElementById('log').textContent='';
  const a=ACC.find(x=>x.id===id); if(!a)return;
  document.getElementById('detail').classList.remove('hidden');
  document.getElementById('noSel').classList.add('hidden');
  fid.value=a.id; fname.value=a.name; fsymbol.value=a.symbol; flogin.value=a.login||'';
  fserver.value=a.server||''; fmt5.value=a.mt5_path||''; frisk.value=a.risk_pct;
  freal.checked=!!a.allow_real; fauto.checked=!!a.autostart;
  fpassword.value=''; fpassword.placeholder=a.has_password?'•••••• (guardada)':'';
  warn.classList.toggle('hidden',!a.allow_real);
  renderList();
}
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
freal_change();function freal_change(){document.addEventListener('change',e=>{if(e.target.id==='freal')warn.classList.toggle('hidden',!e.target.checked);});}

function loadAccounts(){return fetch('/accounts').then(r=>r.json()).then(a=>{ACC=a;
  if(sel&&!ACC.find(x=>x.id===sel))sel=null;
  if(!sel&&ACC.length)select(ACC[0].id);
  if(!ACC.length){document.getElementById('detail').classList.add('hidden');document.getElementById('noSel').classList.remove('hidden');}
  renderList();});}
function addAccount(){const fd=new FormData();fd.append('name','Cuenta '+(ACC.length+1));
  fetch('/account',{method:'POST',body:fd}).then(r=>r.json()).then(j=>loadAccounts().then(()=>select(j.id)));}
function saveAccount(){const fd=new FormData(document.getElementById('cfg'));
  fetch('/account',{method:'POST',body:fd}).then(r=>r.json()).then(j=>{fpassword.value='';loadAccounts();});}
function delAccount(){if(!sel||!confirm('¿Eliminar esta cuenta?'))return;const fd=new FormData();fd.append('id',sel);
  fetch('/account/delete',{method:'POST',body:fd}).then(()=>{sel=null;loadAccounts();});}
function startBot(id){const fd=new FormData();fd.append('id',id||sel);fetch('/start',{method:'POST',body:fd});}
function stopBot(id){const fd=new FormData();fd.append('id',id||sel);fetch('/stop',{method:'POST',body:fd});}

function tick(){
  fetch('/status').then(r=>r.json()).then(list=>{
    STAT={}; for(const s of list)STAT[s.id]=s; renderList();
    if(sel){const s=STAT[sel]||{};const [txt,cls]=badge(sel);
      const b=document.getElementById('badge');b.textContent=txt;
      b.style.background=cls==='on'?'var(--ok)':(cls==='wait'?'var(--acc)':'var(--off)');
      eq.textContent=s.equity??'—';pos.textContent=s.position??'—';pid.textContent=s.pid??'—';
      bStart.disabled=!!s.desired;bStop.disabled=!s.desired;}
  }).catch(()=>{});
  if(sel){fetch('/log?id='+sel+'&after='+logLast).then(r=>r.json()).then(s=>{
    if(s.lines&&s.lines.length){const log=document.getElementById('log');
      const atBottom=log.scrollTop+log.clientHeight>=log.scrollHeight-30;
      for(const l of s.lines){log.textContent+=l.text+'\n';logLast=l.seq;}
      if(atBottom)log.scrollTop=log.scrollHeight;}
  }).catch(()=>{});}
  setTimeout(tick,1500);
}
loadAccounts().then(tick);
</script>
</body></html>
"""


def main():
    cfg = load_config()
    for a in cfg["accounts"]:
        if a.get("autostart"):
            r = MANAGER.get(a["id"])
            if r:
                r.start()
    try:
        webbrowser.open(f"http://{HOST}:{PORT}")
    except Exception:
        pass
    app.run(host=HOST, port=PORT, threaded=True, debug=False)


if __name__ == "__main__":
    main()
