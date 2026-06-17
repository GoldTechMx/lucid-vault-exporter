# Lucid Vault Exporter

**Turn an entire Lucid (Lucidchart / Lucidspark) account into a local, portable, Obsidian-ready vault.**

Read-only, resumable, and built for accounts that are being cancelled or migrated.
Developed and maintained by **GoldTech MX**.

---

## Why does this tool exist in two phases?

The Lucid REST API can export **PNG images and JSON metadata only**.
It cannot produce PDF, VSDX, or `.lucid` files - those formats are not exposed through the API
at all. Because of that hard limit, the tool is deliberately split into two phases:

| Phase | Transport | What it exports | Speed | Resilience |
|-------|-----------|-----------------|-------|------------|
| **1 - API** | Lucid REST API | Inventory, high-res PNG per page, document metadata, Obsidian notes | Fast; 60 exports/5 s | Fully resumable via SQLite state |
| **2 - Browser** | Playwright → lucid.app UI | PDF (all products); VSDX (Lucidchart only) | Slower; human-paced with jitter | Failures recorded and retried; never block the run |

If you only need PNGs and Markdown notes you can run `--skip-browser`.
If PDFs and VSDX are critical, run both phases.

---

## What you get

- **Full account coverage** - owned documents, shared documents, and team-folder documents;
  trashed items are excluded by default.
- **Obsidian vault** - folder tree mirrors your Lucid workspace.  Each document becomes a
  `.md` note with YAML frontmatter, `![[...]]` PNG embeds (one per page), and sidecar links
  to any PDF / VSDX that was downloaded.
- **Audit manifest** under `_manifest/`:
  - `inventory.csv` - every document with metadata and per-artifact status
  - `errors.csv` - every failure with message and timestamp
  - `verification.md` - summary counts; lists docs that still need a `retry`
  - `cancellation-checklist.md` - Spanish-language pre-cancellation checklist
- **Resumable** - progress is stored in `_lucid_export_state.sqlite`; already-`ok` artifacts
  are skipped on re-run. Use `--force` to re-export everything, `retry` to reprocess only
  failed artifacts.
- **Read-only against Lucid** - the tool never writes, creates, or deletes anything on the
  Lucid side. Tokens are stored locally and are never written to logs.

---

## Creating the Lucid OAuth2 app (required before first run)

The Lucid API uses OAuth 2.0. You need to register a developer application once.

> **Note:** API key access with pre-authorized grants is an Enterprise-tier feature.
> For Pro accounts, OAuth2 is the standard path.

1. Go to **https://lucid.app/developer** and sign in.
2. Click **Create Application** and give it a name (e.g. "Vault Exporter").
3. Under the application, click **Add OAuth 2.0 Client**.
4. Set the **Redirect URI** to exactly:
   ```
   http://localhost:8765/callback
   ```
5. Request the following **scopes**:
   ```
   lucidchart.document.content:readonly
   lucidspark.document.content:readonly
   lucidscale.document.content:readonly
   folder:readonly
   offline_access
   user.profile
   ```
6. Copy the **Client ID** and **Client Secret** into your `.env` file:
   ```ini
   LUCID_CLIENT_ID=your_client_id_here
   LUCID_CLIENT_SECRET=your_client_secret_here
   ```

---

## Quick start (CLI)

```bash
# 1. Clone and install
git clone https://github.com/GoldTechMx/lucid-vault-exporter.git
cd lucid-vault-exporter
python -m pip install -e ".[browser,web]"
python -m playwright install chromium

# 2. Configure credentials
cp .env.example .env
# Edit .env: fill LUCID_CLIENT_ID and LUCID_CLIENT_SECRET

# 3. Write config.yml
lucid-vault-exporter init

# 4. Authorize via OAuth2 (opens your default browser)
lucid-vault-exporter auth

# 5. Sign in to lucid.app for Phase 2 (PDF/VSDX browser automation)
lucid-vault-exporter login

# 6. Smoke test - caps BOTH phases (API + browser) to two documents end-to-end
lucid-vault-exporter export --limit 2

# 7. Full export
lucid-vault-exporter export

# 8. Verify and write the final manifest
lucid-vault-exporter verify
```

### Command reference

| Command | What it does |
|---------|--------------|
| `init` | Writes `config.yml` in the current directory (refuses to overwrite). |
| `auth` | Runs the OAuth2 authorization flow in your browser; stores tokens in `.lucid_tokens.json`. |
| `login` | Opens a visible Chromium window so you can sign in to lucid.app (SSO/2FA supported); the session persists in `.pw-profile/` for headless Phase 2 runs. |
| `export` | Runs Phase 1 (API: inventory, PNGs, notes) then Phase 2 (browser: PDF/VSDX). |
| `export --skip-browser` | Phase 1 (API) only - no Playwright required. |
| `export --only-browser` | Phase 2 (browser) only - useful when Phase 1 already finished. |
| `export --force` | Re-exports every artifact, overwriting existing files. |
| `export --limit N` | Smoke test: caps both phases (API + browser) to the first N documents. |
| `retry` | Resets all `failed` artifacts to `pending` then runs `export` again. |
| `verify` | Recounts artifact statuses, rewrites `_manifest/verification.md`. |
| `serve` | Starts the local web UI (see below). |

---

## Web UI

```bash
lucid-vault-exporter serve            # -> http://127.0.0.1:8123  (localhost only)
```

A guided, self-service page (English, with inline tips) that runs the whole export from the
browser - no terminal needed:

- **Connect to Lucid** - paste your OAuth2 `client_id` / `client_secret`, click **Connect**, and
  authorize in the browser tab that opens. "Remember in `.env`" stores them (gitignored). The
  page includes the one-time app-setup steps (redirect URI, scopes).
- **Sign in for PDF/VSDX** (optional) - opens a real Chromium window for the lucid.app login that
  the browser phase needs. Skip it if PNG + notes are enough.
- **Output folder** - type a path or use the server-side **Browse...** picker, with live
  validation.
- **What to export** - Full / API only / Browser only / Inventory / Verify, product checkboxes,
  a `--limit` for smoke tests, and `--force`.
- **Run** - Start / Pause / Resume / Cancel with a progress bar, ETA, and a **live event
  console**. Resumable: a re-run continues where it left off.

Requires the `web` extra (`pip install "lucid-vault-exporter[web]"`). The browser phase still
uses Playwright, so also install the `browser` extra and `playwright install chromium` if you
want PDF/VSDX. The UI is localhost-only; tokens never appear in the console.

> **On a network share?** `serve` (and the browser phase) need native extension modules
> (`uvicorn[standard]`, Playwright) that Windows cannot load from a UNC/SMB path. Install and run
> from a virtualenv on a **local disk** - the source tree can stay on the share via an editable
> install. The connection is validated once per server session, so you click **Connect** after
> each `serve` even if a previous run already stored a token.

---

## Rate limits

Lucid's documented per-account limits are:

| Endpoint group | Documented limit | Tool default |
|----------------|-----------------|--------------|
| Document export (PNG) | 75 requests / 5 s | 60 / 5 s |
| Document search | 300 requests / 5 s | 240 / 5 s |

The tool stays comfortably under the published limits by default. On HTTP 429 it reads the
`Retry-After` header and pauses accordingly. For a large account (thousands of documents, many
pages each) a complete Phase 1 run may take several hours; the defaults are tuned to glide
just under the limit without manual intervention.

Both values are configurable in `config.yml`:

```yaml
rate_limit:
  export_per_5s: 60    # raise toward 75 if you want faster PNGs
  search_per_5s: 240   # raise toward 300 if inventory is slow
```

---

## Resume, force, retry, and verify

The SQLite state file (`_lucid_export_state.sqlite`) tracks every document × artifact
combination (`png`, `pdf`, `vsdx`) with statuses: `pending`, `in_progress`, `ok`, `failed`,
`skipped`.

| Scenario | What to do |
|----------|-----------|
| Run interrupted or crashed | Re-run `export` - already-`ok` artifacts are skipped automatically. |
| Want to re-export everything | `export --force` resets all statuses first. |
| Some artifacts failed | `retry` resets `failed` → `pending` and re-runs export. |
| Just want updated counts | `verify` recounts and rewrites the manifest without exporting. |

---

## Output layout

```
<output_dir>/               # default: ./exports/lucid-vault/
  Clientes/
    ACME/
      Mapa de Procesos.md              # note: YAML frontmatter + PNG embeds + sidecar links
      _assets/
        Mapa de Procesos a1b2c3d4 p1.png   # one PNG per page (page number in filename)
        Mapa de Procesos a1b2c3d4 p2.png
      Mapa de Procesos a1b2c3d4.pdf        # Phase 2 browser sidecar
      Mapa de Procesos a1b2c3d4.vsdx       # Phase 2 browser sidecar (Lucidchart only)
  Proyectos/
    Sprint Board.md
    _assets/
      Sprint Board e5f6a7b8 p1.png
    Sprint Board e5f6a7b8.pdf
  _manifest/
    inventory.csv              # full document inventory with artifact statuses
    errors.csv                 # every error with message and timestamp
    verification.md            # summary counts; lists docs needing retry
    cancellation-checklist.md  # pre-cancellation checklist
    browser_failures/          # screenshots of failed browser export attempts
      <doc_id>-pdf.png
  _lucid_export_state.sqlite   # SQLite state; never delete while a run is in progress
```

Each `.md` note looks like this:

```markdown
---
lucid_id: a1b2c3d4e5f6
title: 'Mapa de Procesos'
product: lucidchart
folder: 'Clientes/ACME'
pages: 2
created: 2024-01-15T10:30:00Z
last_modified: 2024-06-01T08:00:00Z
owner: 'user@company.com'
version: '5'
original_url: https://lucid.app/lucidchart/a1b2c3d4e5f6/edit
exported_by: lucid-vault-exporter
---

# Mapa de Procesos

> Diagrama exportado de Lucid (lucidchart). Original: https://lucid.app/lucidchart/a1b2c3d4e5f6/edit

![[Mapa de Procesos a1b2c3d4 p1.png]]

![[Mapa de Procesos a1b2c3d4 p2.png]]

## Archivos
- [[Mapa de Procesos a1b2c3d4.pdf]]
- [[Mapa de Procesos a1b2c3d4.vsdx]]
```

> The note body uses Spanish display strings (`Diagrama exportado de Lucid…`, `## Archivos`)
> matching the cancellation checklist; the YAML keys remain English for tooling.

---

## Output path and SMB share note

This tool can run with its working directory on a network share (e.g. a mapped drive on Z:),
but two things on network filesystems need care:

- **The Python virtualenv must live on a local disk.** Native extension DLLs (Playwright's
  `greenlet`, etc.) fail to load from a UNC/SMB path on Windows (`ImportError: DLL load
  failed`). Create the venv on `C:` even if the source tree is on a share - editable installs
  read the source over the network fine; only the compiled dependencies must be local.
- **SQLite cannot run on many SMB/NFS shares** (it raises `disk I/O error` because the share
  can't provide the file locking / shared-memory WAL needs). The tool handles this
  automatically: if `output_dir` is on such a share, the state DB transparently falls back to
  local disk under `%LOCALAPPDATA%\lucid-vault-exporter\state\` while the vault files still
  write to `output_dir`. You'll see a one-line warning telling you where state landed.

Even with the fallback, writing tens of thousands of small files to a share during a long
run is **slow**, so:

**Recommendation:** export to a local disk first, then copy the finished vault:

```powershell
# Export to local SSD
lucid-vault-exporter export   # with output_dir: D:/lucid_export in config.yml

# Then copy the finished vault to the network location
robocopy D:\lucid_export Z:\Vaults\lucid-vault /E /COPYALL /R:3 /W:5
```

---

## Verified against the live API and UI

The implementation has been reconciled against a real Lucid account (2026-06):

- **Document search** returns a bare JSON array; pagination is the `Link: <url>; rel="next"`
  header with a `pageToken`. Fields used (`documentId`, `title`, `product`, `parent`,
  `pageCount`, `version`, `created`, `lastModified`, `editUrl`, `trashed`) match.
- **PNG export** is `GET /documents/{id}?page=N` with `Accept: image/png` - confirmed.
- **Folder hierarchy** requires the `folder:readonly` scope. Without it, `/folders/{id}`
  returns `403 accessForbidden` and the vault is flat; with it, nested paths resolve.
- **Browser export** (PDF/VSDX) drives the editor menu: the hamburger menu → **Exportar** →
  the format (**PDF** / **Visio (VSDX)**) → the **Descargar** dialog button. Selectors live in
  `exporter_browser.py` (`SELECTORS` / `FORMAT_LABELS`); a Lucid UI change is a one-place fix.

See **[docs/lucid-api-notes.md](docs/lucid-api-notes.md)** for the full technical breakdown.
The Lucid UI is localized; the selectors target stable `data-test-id` attributes rather than
visible text where possible, but the format labels (`Exportar`, `Visio (VSDX)`) are
Spanish-UI strings - adjust them if your account renders in another language.

---

## Safety and privacy

- **Read-only against Lucid** - the tool never creates, updates, or deletes anything on the
  Lucid platform.
- **Tokens** are stored in `.lucid_tokens.json` (gitignored) and are never written to log
  output at any log level.
- **Browser session** is stored in `.pw-profile/` (gitignored). It contains cookies for
  lucid.app - treat it like a password file.
- Neither file is committed to git. The `.gitignore` excludes both by default.

---

## Installation extras

| Extra | What it adds | Install |
|-------|--------------|---------|
| `[browser]` | Playwright (required for Phase 2 PDF/VSDX) | `pip install "lucid-vault-exporter[browser]"` |
| `[web]` | FastAPI + Uvicorn (required for `serve`) | `pip install "lucid-vault-exporter[web]"` |
| `[browser,web]` | Both | `pip install "lucid-vault-exporter[browser,web]"` |

After installing `[browser]`, install the browser binary once:

```bash
python -m playwright install chromium
```

---

## Requirements

- Python 3.11+
- Lucid account (Pro or Enterprise) with API access enabled
- OAuth2 app registered at https://lucid.app/developer

---

## License

Apache-2.0 © GoldTech MX
