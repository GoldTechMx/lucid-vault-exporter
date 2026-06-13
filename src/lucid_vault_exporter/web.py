"""Localhost-only web UI: status polling + start/stop of the API phase.

The browser (Playwright) phase is intentionally CLI-only: it can require a visible window
for first login, which a web worker thread can't provide. The page makes that explicit.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from .config import Config, ConfigError, Settings
from .state import ARTIFACT_KINDS, StateDB

log = logging.getLogger("lucid_vault_exporter.web")

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Lucid Vault Exporter</title>
<style>
 body{font-family:system-ui;margin:2rem;max-width:720px}
 .bar{background:#eee;border-radius:6px;height:18px;overflow:hidden}
 .fill{background:#4a7;height:100%;width:0;transition:width .5s}
 button{padding:.5rem 1.2rem;margin-right:.5rem}
 .muted{color:#777}
</style></head><body>
<h1>Lucid Vault Exporter</h1>
<p class="muted">Fase API (inventario + PNG + notas). La fase de navegador (PDF/VSDX) se corre con
<code>lucid-vault-exporter export --only-browser</code> en la terminal.</p>
<div id="counts"></div>
<div class="bar"><div class="fill" id="fill"></div></div>
<p><button onclick="post('start')">Iniciar / Reanudar</button>
<button onclick="post('stop')">Detener</button> <span id="state"></span></p>
<script>
async function post(x){await fetch('/api/'+x,{method:'POST'});refresh()}
async function refresh(){
 const r=await fetch('/api/status');const s=await r.json();
 document.getElementById('state').textContent=s.error?('error: '+s.error):(s.running?'corriendo...':'detenido');
 const png=s.artifacts.png||{};const done=(png.ok||0)+(png.failed||0)+(png.skipped||0);
 const pct=s.documents?Math.round(100*done/s.documents):0;
 document.getElementById('fill').style.width=pct+'%';
 document.getElementById('counts').innerHTML=
  `<p>Documentos: <b>${s.documents}</b> &nbsp; PNG ok: <b>${png.ok||0}</b> &nbsp; fallidos: <b>${png.failed||0}</b> (${pct}%)</p>`;
}
setInterval(refresh,2000);refresh();
</script></body></html>"""


def create_app() -> FastAPI:
    app = FastAPI()
    run_state: dict[str, Any] = {"thread": None, "stop": False, "error": None}

    def _cfg() -> Config:
        return Config.load(Path("config.yml"))

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _PAGE

    @app.get("/api/status")
    def status() -> JSONResponse:
        try:
            cfg = _cfg()
        except ConfigError:
            return JSONResponse({"error": "no config.yml"}, status_code=400)
        with StateDB.open(cfg.output_dir) as db:
            docs = db.all_documents()
            artifacts = {
                kind: {
                    st: len(db.artifacts_by_status(kind, (st,)))
                    for st in ("ok", "failed", "skipped")
                }
                for kind in ARTIFACT_KINDS
            }
        thread = run_state["thread"]
        return JSONResponse({
            "documents": len(docs),
            "artifacts": artifacts,
            "running": bool(thread and thread.is_alive()),
            "error": run_state["error"],
        })

    @app.post("/api/start")
    def start() -> JSONResponse:
        thread = run_state["thread"]
        if thread and thread.is_alive():
            return JSONResponse({"ok": True, "already": True})
        run_state["stop"] = False

        def worker() -> None:
            from .cli import _client  # reuse wiring
            from .pipeline import run_api_phase
            from .reports import write_manifest

            run_state["error"] = None
            try:
                cfg = _cfg()
                client = _client(cfg, Settings())
                try:
                    with StateDB.open(cfg.output_dir) as db:
                        run_api_phase(client, db, cfg.output_dir, products=list(cfg.products),
                                      should_stop=lambda: bool(run_state["stop"]))
                        write_manifest(db, cfg.output_dir)
                finally:
                    client.close()
            except Exception as exc:  # noqa: BLE001 - surface any worker failure to the UI
                run_state["error"] = str(exc)[:500]
                log.warning("Web export worker failed: %s", exc)

        t = threading.Thread(target=worker, daemon=True)
        run_state["thread"] = t
        t.start()
        return JSONResponse({"ok": True})

    @app.post("/api/stop")
    def stop() -> JSONResponse:
        run_state["stop"] = True
        return JSONResponse({"ok": True})

    return app
