"""Local web UI for Lucid Vault Exporter.

A FastAPI app + a single embedded HTML page so a non-CLI user can connect to Lucid (OAuth2),
optionally sign in for the browser phase, pick an output folder, choose what to export, and run
it with live progress, ETA, pause/resume/cancel, and an event console - without the terminal.

Safety: binds 127.0.0.1 only (set by `serve`); OAuth tokens live in `.lucid_tokens.json` and the
client secret in `.env` only if "remember" is set (both gitignored); the event console scrubs
secrets. StateDB is opened per request/job thread (it is not thread-safe).
"""

from __future__ import annotations

import logging
import os
import secrets as _secrets
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__
from .config import VALID_PRODUCTS, Config, ConfigError, Settings
from .control import Cancelled
from .jobs import JobLogHandler, JobRegistry, RedactionFilter, scrub_text
from .ratelimit import RateLimiter
from .state import ARTIFACT_KINDS, StateDB

log = logging.getLogger("lucid_vault_exporter.web")

TOKENS_PATH = Path(".lucid_tokens.json")
PROFILE_DIR = Path(".pw-profile")

# --- in-memory server state (single local user) -----------------------------------------
_redaction = RedactionFilter()
_registry = JobRegistry()
# connection state for the OAuth flow
_conn: dict[str, Any] = {
    "client_id": "", "client_secret": "", "state": "", "status": "idle",  # idle|pending|connected|error
    "user": None, "error": None, "server": None,
}
# browser-login state
_browser: dict[str, Any] = {"status": "idle", "error": None}  # idle|pending|logged_in|error


def _install_logging() -> None:
    handler = JobLogHandler(_registry)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_redaction)
    root = logging.getLogger()
    root.addFilter(_redaction)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _write_env(client_id: str, client_secret: str) -> None:
    """Persist client_id/secret to .env (the sanctioned, gitignored secret location)."""
    env = Path(".env")
    keep: list[str] = []
    if env.exists():
        keep = [
            ln for ln in env.read_text(encoding="utf-8").splitlines()
            if not ln.startswith(("LUCID_CLIENT_ID=", "LUCID_CLIENT_SECRET="))
        ]
    keep.append(f"LUCID_CLIENT_ID={client_id}")
    keep.append(f"LUCID_CLIENT_SECRET={client_secret}")
    env.write_text("\n".join(keep) + "\n", encoding="utf-8")


def _browse(raw: str | None) -> dict[str, Any]:
    """List immediate subfolders of `raw` on the SERVER filesystem (for the picker).
    Localhost-only by design; returns directory names only and never crashes on IO errors."""
    base = Path(raw).expanduser() if raw else Path.home()
    try:
        base = base.resolve()
        if not base.is_dir():
            base = Path.home().resolve()
    except OSError:
        base = Path.home().resolve()
    dirs: list[dict[str, str]] = []
    try:
        for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            try:
                if entry.is_dir():
                    dirs.append({"name": entry.name, "path": str(entry)})
            except OSError:
                continue
    except (PermissionError, OSError):
        pass
    parent = str(base.parent) if base.parent != base else None
    return {"path": str(base), "parent": parent, "writable": os.access(base, os.W_OK),
            "dirs": dirs[:1000]}


def _check_path(raw: str) -> dict[str, Any]:
    """Resolve a typed output path and report whether it's usable (live UI feedback)."""
    if not raw.strip():
        return {"ok": False, "abs": "", "msg": "enter a folder path"}
    p = Path(raw).expanduser()
    try:
        p = p.resolve()
    except OSError:
        return {"ok": False, "abs": raw, "msg": "invalid path"}
    exists = p.exists()
    probe = p if exists else next((a for a in p.parents if a.exists()), p)
    writable = os.access(probe, os.W_OK)
    if not writable:
        msg = "not writable"
    elif exists:
        msg = "exists - writable"
    else:
        msg = "will be created"
    return {"ok": writable, "abs": str(p), "exists": exists, "writable": writable, "msg": msg}


def _load_env_credentials() -> None:
    """Pick up client_id/secret already in .env so the UI can pre-fill and auto-connect later."""
    s = Settings()
    if s.lucid_client_id and s.lucid_client_secret:
        _conn["client_id"] = s.lucid_client_id
        _conn["client_secret"] = s.lucid_client_secret
        _redaction.set_secrets([s.lucid_client_secret])


_PAGE = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lucid Vault Exporter</title>
<style>
 :root{--b:#d0d7de;--fg:#1f2328;--mut:#656d76;--ok:#1a7f37;--bad:#cf222e;--acc:#0969da}
 *{box-sizing:border-box}
 body{font-family:system-ui,Segoe UI,Roboto,sans-serif;color:var(--fg);margin:0;background:#f6f8fa}
 .wrap{max-width:860px;margin:0 auto;padding:1.5rem}
 h1{margin:.2rem 0}
 .sub{color:var(--mut);margin:.2rem 0 1rem}
 .card{background:#fff;border:1px solid var(--b);border-radius:10px;padding:1rem 1.2rem;margin:1rem 0}
 .card h2{margin:.1rem 0 .2rem;font-size:1.05rem}
 .tip{color:var(--mut);font-size:.86rem;margin:.2rem 0 .7rem}
 label{display:block;font-size:.85rem;margin:.5rem 0 .15rem;color:var(--mut)}
 input[type=text],input[type=password],select{width:100%;padding:.5rem;border:1px solid var(--b);border-radius:6px;font:inherit}
 button{padding:.5rem 1rem;border:1px solid var(--b);border-radius:6px;background:#fff;font:inherit;cursor:pointer}
 button.primary{background:var(--acc);color:#fff;border-color:var(--acc)}
 button:disabled{opacity:.5;cursor:not-allowed}
 .row{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap}
 .pill{font-size:.8rem;padding:.1rem .5rem;border-radius:999px;border:1px solid var(--b)}
 .ok{color:var(--ok)} .bad{color:var(--bad)}
 details summary{cursor:pointer;font-weight:600}
 .bar{background:#eaeef2;border-radius:6px;height:14px;overflow:hidden}
 .fill{background:var(--ok);height:100%;width:0;transition:width .4s}
 #console{background:#0d1117;color:#c9d1d9;font-family:ui-monospace,Consolas,monospace;font-size:.8rem;
   padding:.6rem;border-radius:6px;height:220px;overflow:auto;white-space:pre-wrap}
 code{background:#eff1f3;padding:0 .25rem;border-radius:4px}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:.6rem}
 #picker{border:1px solid var(--b);border-radius:6px;max-height:180px;overflow:auto;margin-top:.4rem;display:none}
 #picker div{padding:.3rem .5rem;cursor:pointer} #picker div:hover{background:#f0f3f6}
</style></head><body><div class="wrap">
<h1>Lucid Vault Exporter</h1>
<p class="sub">Export a Lucid account into a local, Obsidian-ready vault - all from your browser. Read-only. Resumable. Runs on this machine only (127.0.0.1).</p>

<details class="card" open><summary>How this works</summary>
<p class="tip">Five steps, top to bottom:</p>
<ol class="tip">
 <li><b>Connect to Lucid</b> with an OAuth2 app (one-time setup, instructions below). This grants read-only access to your documents and folders. The API exports per-page PNG images + metadata and builds the Markdown notes.</li>
 <li><b>(Optional) Sign in for PDF/VSDX.</b> Lucid's API cannot export PDF or Visio files, so those are fetched by automating the lucid.app editor in a real browser. This needs a normal sign-in (SSO/2FA) once. Skip it if PNG + notes are enough.</li>
 <li><b>Pick an output folder.</b> A local disk is best; on a network share the vault still writes there but the small state database moves to local disk automatically.</li>
 <li><b>Choose what to export</b> and start. Watch progress, ETA, and the live console. Pause, resume, or cancel anytime - it is resumable, so a re-run continues where it left off.</li>
 <li><b>Done.</b> Open the output folder in Obsidian. An audit manifest is written under <code>_manifest/</code>.</li>
</ol>
</details>

<div class="card">
 <h2>1. Connect to Lucid</h2>
 <p class="tip">Paste your OAuth2 app credentials. <b>One-time app setup:</b> go to <code>https://lucid.app/developer</code> &rarr; create an Application &rarr; add an OAuth 2.0 client &rarr; set the redirect URI to exactly <code>http://localhost:8765/callback</code> &rarr; request scopes <code>lucidchart.document.content:readonly</code>, <code>lucidspark.document.content:readonly</code>, <code>lucidscale.document.content:readonly</code>, <code>folder:readonly</code>, <code>offline_access</code>, <code>user.profile</code>. Then copy the Client ID and Secret here.</p>
 <div class="grid">
  <div><label>Client ID</label><input id="cid" type="text" autocomplete="off"></div>
  <div><label>Client Secret</label><input id="csec" type="password" autocomplete="off"></div>
 </div>
 <label class="row" style="margin-top:.5rem"><input type="checkbox" id="remember"> Remember in <code>.env</code> (gitignored)</label>
 <div class="row" style="margin-top:.6rem">
  <button class="primary" id="connectBtn" onclick="connect()">Connect to Lucid</button>
  <span id="connState" class="pill">not connected</span>
 </div>
</div>

<div class="card">
 <h2>2. Sign in for PDF/VSDX <span class="tip">(optional)</span></h2>
 <p class="tip">Only needed for PDF and Visio (VSDX) files. Clicking this opens a real Chromium window on this machine - sign in to lucid.app normally (SSO/2FA supported). The session is saved to <code>.pw-profile/</code> for later headless runs. PNG images and notes do not need this.</p>
 <div class="row">
  <button id="loginBtn" onclick="browserLogin()">Sign in for PDF/VSDX</button>
  <span id="loginState" class="pill">unknown</span>
 </div>
</div>

<div class="card">
 <h2>3. Output folder</h2>
 <p class="tip">Where the vault is written. Prefer a local disk (e.g. <code>D:/lucid_export</code>); writing tens of thousands of small files to a network share during a long run is slow. The state database auto-moves to local disk if the share can't host it.</p>
 <label>Folder path</label>
 <div class="row"><input id="out" type="text" oninput="checkPath()" style="flex:1">
  <button onclick="togglePicker()">Browse...</button></div>
 <span id="pathMsg" class="tip"></span>
 <div id="picker"></div>
</div>

<div class="card">
 <h2>4. What to export</h2>
 <p class="tip">Pick the scope. <b>Full export</b> runs both phases; <b>API only</b> does PNG + notes (no browser needed); <b>Browser only</b> fetches PDF/VSDX for already-inventoried docs; <b>Inventory</b> just lists documents; <b>Verify</b> recounts and rewrites the manifest.</p>
 <div class="grid">
  <div><label>Command</label><select id="command">
   <option value="export">Full export (API + browser)</option>
   <option value="export_api">API only (PNG + notes)</option>
   <option value="export_browser">Browser only (PDF + VSDX)</option>
   <option value="inventory">Inventory only</option>
   <option value="verify">Verify (rewrite manifest)</option>
  </select></div>
  <div><label>Limit (0 = all; use a small number for a smoke test)</label><input id="limit" type="text" value="0"></div>
 </div>
 <label style="margin-top:.5rem">Products</label>
 <div class="row">
  <label class="row"><input type="checkbox" class="prod" value="lucidchart" checked> Lucidchart</label>
  <label class="row"><input type="checkbox" class="prod" value="lucidspark" checked> Lucidspark</label>
  <label class="row"><input type="checkbox" class="prod" value="lucidscale" checked> Lucidscale</label>
 </div>
 <label class="row" style="margin-top:.5rem"><input type="checkbox" id="force"> Force re-export everything (<code>--force</code>)</label>
</div>

<div class="card">
 <h2>5. Run</h2>
 <p class="tip">Start the export. It is resumable: already-finished artifacts are skipped on a re-run. Pause holds between documents; Cancel stops safely (partial work is kept).</p>
 <div class="row">
  <button class="primary" id="startBtn" onclick="startRun()">Start</button>
  <button id="pauseBtn" onclick="togglePause()" disabled>Pause</button>
  <button id="cancelBtn" onclick="cancelRun()" disabled>Cancel</button>
  <span id="runState" class="pill">idle</span>
 </div>
 <div style="margin-top:.7rem" class="bar"><div class="fill" id="fill"></div></div>
 <p class="tip" id="progLine">-</p>
</div>

<div class="card">
 <h2>Event console</h2>
 <p class="tip">Live log of the run. Secrets (tokens, client secret) are never shown here.</p>
 <div id="console"></div>
</div>

<p class="sub">v__LVE_VERSION__ &middot; read-only against Lucid &middot; localhost only</p>
</div>
<script>
const $=id=>document.getElementById(id);
let jobId=null, sinceLog=0, paused=false, poll=null;
async function jget(u){const r=await fetch(u);return r.json()}
async function jpost(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):null});return r}
async function refreshStatus(){
 const s=await jget('/api/status');
 $('connState').textContent=s.connected?('connected'+(s.user?(' as '+s.user):'')):'not connected';
 $('connState').className='pill '+(s.connected?'ok':'');
 $('loginState').textContent=s.browser_logged_in?'signed in':'not signed in';
 $('loginState').className='pill '+(s.browser_logged_in?'ok':'');
 if(!$('out').value && s.output_dir) {$('out').value=s.output_dir; checkPath();}
}
async function connect(){
 $('connState').textContent='connecting...';
 const r=await jpost('/api/connect',{client_id:$('cid').value,client_secret:$('csec').value,remember:$('remember').checked});
 if(!r.ok){const e=await r.json();$('connState').textContent='error: '+(e.detail||'');return}
 const {authorize_url}=await r.json();
 window.open(authorize_url,'_blank');
 const t=setInterval(async()=>{const p=await jget('/api/connect/poll');
   if(p.state==='connected'){clearInterval(t);refreshStatus()}
   else if(p.state==='error'){clearInterval(t);$('connState').textContent='error: '+(p.error||'')}},1500);
}
async function browserLogin(){
 $('loginState').textContent='opening browser...';
 await jpost('/api/browser-login');
 const t=setInterval(async()=>{const p=await jget('/api/browser-login/poll');
   if(p.state==='logged_in'){clearInterval(t);refreshStatus()}
   else if(p.state==='error'){clearInterval(t);$('loginState').textContent='error: '+(p.error||'')}},1500);
}
async function checkPath(){const p=$('out').value;if(!p){$('pathMsg').textContent='';return}
 const r=await jget('/api/check-path?path='+encodeURIComponent(p));
 $('pathMsg').textContent=r.msg+(r.abs?(' ('+r.abs+')'):'');$('pathMsg').className='tip '+(r.ok?'ok':'bad');}
async function togglePicker(){const el=$('picker');if(el.style.display==='block'){el.style.display='none';return}
 el.style.display='block';loadPicker($('out').value||'');}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
async function loadPicker(path){const r=await jget('/api/browse?path='+encodeURIComponent(path));
 let h='<div onclick="pick(\''+encodeURIComponent(r.parent||'')+'\')">.. ('+esc(r.parent||'top')+')</div>';
 h+='<div onclick="useThis(\''+encodeURIComponent(r.path)+'\')"><b>[use this folder] '+esc(r.path)+'</b></div>';
 for(const d of r.dirs){h+='<div onclick="pick(\''+encodeURIComponent(d.path)+'\')">'+esc(d.name)+'</div>'}
 $('picker').innerHTML=h;}
function pick(p){if(p)loadPicker(decodeURIComponent(p))}
function useThis(p){$('out').value=decodeURIComponent(p);checkPath();$('picker').style.display='none'}
async function startRun(){
 const products=[...document.querySelectorAll('.prod:checked')].map(c=>c.value);
 const body={command:$('command').value,options:{output_dir:$('out').value,products:products,
   limit:parseInt($('limit').value||'0',10),force:$('force').checked}};
 const r=await jpost('/api/run',body);const d=await r.json();
 if(!r.ok){$('runState').textContent='error: '+(d.detail||'');return}
 if(d.verified){$('runState').textContent='manifest rewritten';return}
 jobId=d.job_id;sinceLog=0;$('console').textContent='';
 $('startBtn').disabled=true;$('pauseBtn').disabled=false;$('cancelBtn').disabled=false;
 poll=setInterval(pollJob,1500);pollJob();
}
async function pollJob(){if(!jobId)return;const j=await jget('/api/jobs/'+jobId+'?since='+sinceLog);
 const pct=j.total?Math.round(100*j.done/j.total):0;$('fill').style.width=pct+'%';
 const eta=j.eta!=null?(' ~'+Math.round(j.eta)+'s left'):'';
 $('progLine').textContent=j.phase+': '+j.done+'/'+j.total+(j.detail?(' - '+j.detail):'')+eta+' ('+j.elapsed+'s)';
 $('runState').textContent=j.paused?'paused':j.status;
 if(j.logs&&j.logs.length){const c=$('console');c.textContent+=j.logs.join('\n')+'\n';c.scrollTop=c.scrollHeight;sinceLog=j.log_count;}
 if(['done','error','cancelled'].includes(j.status)){clearInterval(poll);
   $('startBtn').disabled=false;$('pauseBtn').disabled=true;$('cancelBtn').disabled=true;
   if(j.result)$('console').textContent+='\nResult: '+JSON.stringify(j.result)+'\n';
   if(j.error)$('console').textContent+='\nError: '+j.error+'\n';refreshStatus();}
}
async function togglePause(){if(!jobId)return;paused=!paused;await jpost('/api/jobs/'+jobId+'/'+(paused?'pause':'resume'));$('pauseBtn').textContent=paused?'Resume':'Pause';}
async function cancelRun(){if(!jobId)return;await jpost('/api/jobs/'+jobId+'/cancel');}
refreshStatus();setInterval(refreshStatus,5000);
</script></body></html>"""


def _settings() -> Settings:
    return Settings()


def _make_client(cfg: Config) -> Any:
    """Build a LucidClient from the stored OAuth token (refreshing if needed)."""
    from .lucid_client import LucidClient
    from .oauth import TokenStore, refresh

    s = _settings()
    store = TokenStore(TOKENS_PATH)
    token_url = f"{s.lucid_api_base}/oauth2/token"
    # prefer the live connection's credentials (may not be in .env unless 'remember' was set)
    cid = _conn.get("client_id") or s.lucid_client_id
    csec = _conn.get("client_secret") or s.lucid_client_secret

    def provider() -> str:
        tok = store.access_token()
        if tok:
            return tok
        return refresh(store, token_url, cid, csec)

    rl = RateLimiter(budgets={"export": cfg.rate_limit.export_per_5s,
                              "search": cfg.rate_limit.search_per_5s})
    return LucidClient(s.lucid_api_base, token_provider=provider, ratelimiter=rl)


def _start_oauth_capture(state: str) -> None:
    """Start a CallbackServer thread that captures the OAuth code and exchanges it for tokens.
    Updates _conn['status'] to connected/error. Stubbed in tests."""
    from .oauth import REDIRECT_URI, CallbackServer, TokenStore, exchange_code

    s = _settings()

    def worker() -> None:
        try:
            server = CallbackServer(state)
            code = server.wait_for_code(timeout=300.0)
            exchange_code(TokenStore(TOKENS_PATH), f"{s.lucid_api_base}/oauth2/token",
                          _conn["client_id"], _conn["client_secret"], code, REDIRECT_URI)
            _conn["status"] = "connected"
            _conn["user"] = "Lucid user"
            log.info("Connected to Lucid.")
        except Exception as exc:  # noqa: BLE001 - surface to the UI, never crash
            _conn["status"] = "error"
            _conn["error"] = scrub_text(str(exc), [_conn["client_secret"]])
            log.warning("OAuth connect failed: %s", _conn["error"])

    threading.Thread(target=worker, daemon=True).start()


def _start_browser_login() -> None:
    """Launch a headed Playwright login in a thread; update _browser status."""
    def worker() -> None:
        try:
            from .exporter_browser import PlaywrightDriver
            driver = PlaywrightDriver(PROFILE_DIR, headless=False)
            try:
                driver.wait_for_manual_login()
                _browser["status"] = "logged_in"
                log.info("Browser session saved for PDF/VSDX.")
            finally:
                driver.close()
        except Exception as exc:  # noqa: BLE001
            _browser["status"] = "error"
            _browser["error"] = str(exc)
            log.warning("Browser login failed: %s", exc)

    _browser["status"] = "pending"
    _browser["error"] = None
    threading.Thread(target=worker, daemon=True).start()


def _browser_logged_in() -> bool:
    """A persisted profile means a prior login is likely valid; status='logged_in' confirms."""
    if _browser["status"] == "logged_in":
        return True
    return PROFILE_DIR.exists()


def _build_config(opts: dict[str, Any]) -> Config:
    cfg = Config.load(Path("config.yml"))
    if opts.get("output_dir"):
        cfg.output_dir = Path(opts["output_dir"])
    if opts.get("products"):
        bad = [p for p in opts["products"] if p not in VALID_PRODUCTS]
        if bad:
            raise ConfigError(f"unknown product(s): {bad}")
        cfg.products = list(opts["products"])
    return cfg


def _run_job(job: Any, cfg: Config, command: str, options: dict[str, Any]) -> None:
    from .pipeline import refresh_notes, run_api_phase
    from .reports import write_manifest

    _redaction.set_secrets([_conn.get("client_secret") or ""])
    job.started = job.phase_started = time.monotonic()
    vault = cfg.output_dir
    limit = int(options.get("limit") or 0)
    try:
        with StateDB.open(vault) as db:
            if options.get("force"):
                db.reset_artifacts()
            if command in ("export", "export_api", "inventory"):
                client = _make_client(cfg)
                try:
                    if command == "inventory":
                        from .inventory import run_inventory
                        job.progress("inventory", 0, None, "scanning")
                        n = run_inventory(client, db, products=list(cfg.products),
                                          control=job.control)
                        job.result = {"documents": n}
                    else:
                        def on_progress(done: int, total: int, label: str) -> None:
                            job.progress("API export", done, total, label)
                        stats = run_api_phase(client, db, vault, products=list(cfg.products),
                                              progress=on_progress, control=job.control,
                                              limit=limit)
                        job.result = dict(stats)
                finally:
                    client.close()
            if command in ("export", "export_browser") and cfg.browser.enabled:
                from .exporter_browser import BrowserExporter, PlaywrightDriver
                btotal = sum(len(db.documents_missing_artifact(f)) for f in cfg.browser.formats)
                job.progress("Browser export", 0, btotal, "starting")
                driver = PlaywrightDriver(PROFILE_DIR, headless=cfg.browser.headless,
                                          failure_dir=vault / "_manifest" / "browser_failures")
                try:
                    exporter = BrowserExporter(
                        driver, db, vault, formats=list(cfg.browser.formats),
                        min_delay=cfg.browser.min_delay_seconds,
                        max_delay=cfg.browser.max_delay_seconds, control=job.control)

                    def bprog(label: str) -> None:
                        job.progress("Browser export", job.done + 1, btotal, label)

                    bstats = exporter.run(progress=bprog)
                    job.result = {**(job.result or {}), "browser": bstats}
                finally:
                    driver.close()
                refresh_notes(db, vault)
            write_manifest(db, vault)
        job.status = "done"
        log.info("%s complete.", command)
    except Cancelled:
        job.status = "cancelled"
        log.info("%s cancelled. Re-run to continue where it left off.", command)
    except Exception as exc:  # noqa: BLE001 - never crash the server
        job.error = scrub_text(str(exc), [_conn.get("client_secret") or ""])
        job.status = "error"
        log.error("%s failed: %s", command, job.error)
    finally:
        _registry.active = None


def create_app() -> Any:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse

    _install_logging()
    _load_env_credentials()
    app = FastAPI(title="Lucid Vault Exporter", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index() -> Any:
        return HTMLResponse(_PAGE.replace("__LVE_VERSION__", __version__),
                            headers={"Cache-Control": "no-store, max-age=0"})

    @app.get("/api/status")
    def status() -> JSONResponse:
        try:
            cfg = Config.load(Path("config.yml"))
            output_dir = str(cfg.output_dir)
            counts = {k: dict.fromkeys(("ok", "failed", "skipped"), 0) for k in ARTIFACT_KINDS}
            documents = 0
            with StateDB.open(cfg.output_dir) as db:
                documents = len(db.all_documents())
                for k in ARTIFACT_KINDS:
                    for stt in ("ok", "failed", "skipped"):
                        counts[k][stt] = len(db.artifacts_by_status(k, (stt,)))
        except ConfigError:
            output_dir, documents = "", 0
            counts = {k: dict.fromkeys(("ok", "failed", "skipped"), 0) for k in ARTIFACT_KINDS}
        job = _registry.active
        return JSONResponse({
            "connected": _conn["status"] == "connected",
            "user": _conn["user"],
            "browser_logged_in": _browser_logged_in(),
            "output_dir": output_dir,
            "documents": documents,
            "artifacts": counts,
            "active_job_id": job.id if job else None,
        })

    @app.post("/api/connect")
    def connect(body: dict[str, Any]) -> JSONResponse:
        from .oauth import REDIRECT_URI, build_authorize_url

        cid = (body.get("client_id") or "").strip()
        sec = (body.get("client_secret") or "").strip()
        if not cid or not sec:
            raise HTTPException(status_code=400, detail="Client ID and Client Secret are required.")
        state = _secrets.token_urlsafe(16)
        _conn.update({"client_id": cid, "client_secret": sec, "state": state,
                      "status": "pending", "user": None, "error": None})
        _redaction.set_secrets([sec])
        if body.get("remember"):
            _write_env(cid, sec)
        s = _settings()
        url = build_authorize_url(s.lucid_auth_base, cid, REDIRECT_URI, state)
        _start_oauth_capture(state)
        return JSONResponse({"authorize_url": url, "state": state})

    @app.get("/api/connect/poll")
    def connect_poll() -> JSONResponse:
        return JSONResponse({"state": _conn["status"], "user": _conn["user"],
                             "error": _conn["error"]})

    @app.post("/api/browser-login")
    def browser_login() -> JSONResponse:
        _start_browser_login()
        return JSONResponse({"ok": True})

    @app.get("/api/browser-login/poll")
    def browser_login_poll() -> JSONResponse:
        return JSONResponse({"state": _browser["status"], "error": _browser["error"]})

    @app.get("/api/browse")
    def browse(path: str | None = None) -> JSONResponse:
        return JSONResponse(_browse(path))

    @app.get("/api/check-path")
    def check_path(path: str = "") -> JSONResponse:
        return JSONResponse(_check_path(path))

    @app.post("/api/run")
    def run(body: dict[str, Any]) -> JSONResponse:
        command = body.get("command")
        if command not in ("export", "export_api", "export_browser", "inventory", "verify"):
            raise HTTPException(status_code=400, detail="Unknown command.")
        if _conn["status"] != "connected" and command != "verify":
            raise HTTPException(status_code=400, detail="Connect to Lucid first.")
        if command in ("export", "export_browser") and not _browser_logged_in():
            raise HTTPException(status_code=400,
                                detail="Sign in for PDF/VSDX first (browser login).")
        options = body.get("options", {})
        try:
            cfg = _build_config(options)
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        chk = _check_path(str(cfg.output_dir))
        if not chk["ok"]:
            raise HTTPException(status_code=400, detail=f"Output folder: {chk['msg']}")
        if command == "verify":
            from .reports import write_manifest
            with StateDB.open(cfg.output_dir) as db:
                write_manifest(db, cfg.output_dir)
            return JSONResponse({"job_id": None, "verified": True})
        if _registry.active is not None and _registry.active.status == "running":
            raise HTTPException(status_code=409, detail="A job is already running.")
        job = _registry.create(command)
        _registry.active = job
        threading.Thread(target=_run_job, args=(job, cfg, command, options), daemon=True).start()
        return JSONResponse({"job_id": job.id})

    @app.post("/api/jobs/{job_id}/{action}")
    def control_job(job_id: str, action: str) -> JSONResponse:
        job = _registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such job.")
        if action == "pause":
            job.control.pause()
        elif action == "resume":
            job.control.resume()
        elif action == "cancel":
            job.control.cancel()
        else:
            raise HTTPException(status_code=400, detail="Unknown action.")
        return JSONResponse({"ok": True, "paused": job.control.is_paused})

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str, since: int = 0) -> JSONResponse:
        job = _registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such job.")
        now = time.monotonic()
        elapsed = now - job.started if job.started else 0.0
        eta = None
        if job.total and job.done > 0 and job.status == "running":
            per_item = (now - job.phase_started) / job.done
            eta = max(0.0, (job.total - job.done) * per_item)
        return JSONResponse({
            "status": job.status, "command": job.command, "phase": job.phase,
            "paused": job.control.is_paused, "done": job.done, "total": job.total,
            "detail": job.detail, "elapsed": round(elapsed, 1),
            "eta": None if eta is None else round(eta, 1),
            "result": job.result, "error": job.error,
            "logs": job.logs[max(0, since - job.log_dropped):],
            "log_count": job.log_dropped + len(job.logs),
        })

    @app.get("/favicon.ico")
    def favicon() -> Any:
        from fastapi.responses import Response
        return Response(status_code=204)

    return app


def serve(host: str = "127.0.0.1", port: int = 8123) -> None:
    import uvicorn
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
