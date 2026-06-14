# Lucid API Notes

Technical reference for how `lucid-vault-exporter` uses the Lucid REST API and the
lucid.app browser UI. Items marked **PROVISIONAL** have not been validated against the live
endpoint and must be confirmed on the first real run (Task 15). Everything else is taken
directly from the published Lucid API documentation.

---

## Base URL and required headers

```
Base URL: https://api.lucid.co
```

Every API request must include:

```http
Authorization: Bearer <access_token>
Lucid-Api-Version: 1
```

The access token is obtained via OAuth2 (see below) and is refreshed automatically using
the stored refresh token. Tokens are **never** written to log output.

---

## Authentication — OAuth2

| Step | Value |
|------|-------|
| Authorization endpoint | `https://lucid.app/oauth2/authorize` |
| Token endpoint | `https://api.lucid.co/oauth2/token` |
| Redirect URI (must be exact) | `http://localhost:8765/callback` |
| Grant type | `authorization_code` (initial); `refresh_token` (subsequent) |

### Scopes requested

```
lucidchart.document.content:readonly
lucidspark.document.content:readonly
lucidscale.document.content:readonly
folder:readonly
offline_access
user.profile
```

`offline_access` is required to receive a refresh token so headless re-runs do not need
repeated browser authorization.

---

## Document search

```
POST https://api.lucid.co/documents/search?pageSize=200
Content-Type: application/json
```

### Request body

```json
{ "product": ["lucidchart", "lucidspark", "lucidscale"] }
```

### Rate limit

**300 requests / 5 seconds** per account (documented).
Tool default: 240 / 5 s (configurable in `config.yml`).

### Response — **PROVISIONAL**

The implementation expects a **bare JSON array** of document objects:

```json
[
  {
    "documentId": "abc123",
    "title": "My Diagram",
    "product": "lucidchart",
    "parent": { "folderId": "folder456" },
    "pageCount": 3,
    "version": 7,
    "created": "2024-01-15T10:30:00Z",
    "lastModified": "2024-06-01T08:00:00Z",
    "owner": "user@company.com",
    "editUrl": "https://lucid.app/lucidchart/abc123/edit",
    "trashed": false
  }
]
```

**FLAGS to verify on first live run:**

1. **Wrapper shape** — is the response truly a bare array, or is it wrapped in an object
   (e.g. `{ "documents": [...], "total": N }`)? If wrapped, `lucid_client.py` →
   `search_documents()` must index into the wrapper key before iterating.
2. **Field names** — confirm `documentId` vs `id`; `parent.folderId` vs `folderId` (flat);
   `editUrl` vs `edit_url` vs `editLink`.
3. **Trashed filter** — confirm that `"trashed": true` is the exact field and value used to
   indicate a trashed document.

### Pagination — **PROVISIONAL**

The implementation follows RFC 5988 `Link` headers:

```http
Link: <https://api.lucid.co/documents/search?pageSize=200&pageToken=XYZ>; rel="next"
```

When no `Link: ...; rel="next"` header is present, iteration stops.

**FLAG to verify:** Does Lucid actually use `Link` header pagination for this endpoint, or
does it use a different mechanism (e.g. a `nextPageToken` field in the response body, or a
`cursor` query parameter)? If the mechanism differs, update `_LINK_NEXT` and the pagination
loop in `LucidClient.search_documents()`.

---

## Document metadata

```
GET https://api.lucid.co/documents/{documentId}
Accept: application/json
```

Returns the same JSON object shape as a single document from the search response.
Used to refresh metadata after the browser phase (page count may change mid-run).

**Status codes:**
- `200` — OK
- `403` — access denied (shared document revoked)
- `404` — document deleted

---

## Per-page PNG export

```
GET https://api.lucid.co/documents/{documentId}?page={N}
Accept: image/png
```

`N` is 1-based. The API scales the image automatically; `png_dpi` in `config.yml` is passed
as a hint but the actual resolution is determined by Lucid.

**Rate limit:** 75 requests / 5 seconds per account (documented).
Tool default: 60 / 5 s.

**Status codes:**
- `200` — PNG bytes in body
- `403` — no export permission for this document
- `404` — page number out of range (used to detect last page when `pageCount` is unreliable)
- `406` — format not supported (should not occur for `image/png`)

The exporter iterates pages 1 … `pageCount` from document metadata. If a `404` is received
before reaching `pageCount`, iteration stops early (graceful handling for metadata lag).

---

## Folder tree

### List folder contents

```
GET https://api.lucid.co/folders/{folderId}/contents?pageSize=200
Accept: application/json
```

**Response — PROVISIONAL:** bare JSON array of items, each with at minimum:

```json
{ "id": "child_id", "type": "document|folder", "name": "Item Name" }
```

**FLAGS:**
- Confirm field names (`id` vs `folderId`/`documentId`; `type` vs `kind`)
- Confirm pagination uses the same `Link` header mechanism as document search
- Confirm `403` on a no-access folder returns immediately (no body needed)

### Get folder metadata

```
GET https://api.lucid.co/folders/{folderId}
Accept: application/json
```

Expected fields: `id`, `name`, `parent` (with `folderId` or null for root).

**Status codes:**
- `200` — folder metadata
- `403` / `404` — inaccessible or deleted; the inventory treats this as a leaf and moves on

---

## What the API cannot do — and why there is a browser phase

The following formats are **not available** through the Lucid REST API:

| Format | Available via API? |
|--------|--------------------|
| PNG (per page) | Yes |
| JSON metadata | Yes |
| PDF | **No** |
| VSDX (Visio) | **No** |
| `.lucid` native format | **No** |
| SVG | **No** |

Because PDF and VSDX are only accessible through the lucid.app web UI export dialog, the
tool uses Playwright to drive that dialog in a persistent browser profile.

---

## Browser phase — selectors and URLs (PROVISIONAL)

### Editor URL pattern

```
https://lucid.app/{product}/{documentId}/edit
```

Where `{product}` is `lucidchart`, `lucidspark`, or `lucidscale`.

**FLAG:** Validate per-product URL paths against the live UI, especially for `lucidspark`
and `lucidscale`. The URL pattern may differ (e.g. lucidspark may use a different path
segment).

### Export dialog flow

The implementation uses these Playwright selectors (defined in `SELECTORS` dict in
`exporter_browser.py`):

| Step | Selector |
|------|----------|
| Open File menu | `[data-test-id="file-menu"], span:has-text("File"), span:has-text("Archivo")` |
| Click Export item | `text=/Export\|Exportar/i` |
| Select PDF format | `text=/PDF/i` |
| Select VSDX/Visio format | `text=/Visio\|VSDX/i` |
| Click Download button | `button:has-text("Download"), button:has-text("Descargar"), button:has-text("Export"), button:has-text("Exportar")` |

**All selectors are PROVISIONAL.** Lucid updates its UI periodically and may rename or
restructure these elements. If a selector fails in production:

1. Open `exporter_browser.py`.
2. Update the relevant key in `SELECTORS`.
3. Re-run `lucid-vault-exporter retry`.

Screenshots of failed attempts are saved under `_manifest/browser_failures/` for diagnosis.

### VSDX availability

VSDX export is only meaningful for **Lucidchart** documents. Lucidspark and Lucidscale
documents are marked `skipped` for `vsdx` automatically without opening the browser.

---

## Error handling and retry behaviour

| HTTP status | Behaviour |
|-------------|-----------|
| `429` | Read `Retry-After` header; pause that many seconds; retry (up to 6 attempts) |
| `5xx` | Exponential backoff (2^attempt seconds, capped at 30 s); retry (up to 6 attempts) |
| Network error | Same backoff/retry as 5xx |
| `403` on export | Recorded as `failed`; run continues |
| `404` on page PNG | Treated as end-of-pages; not recorded as an error |

---

## Reconciliation checklist for first live run

Before running a full export on a real account, verify each item and update the source
code if the live behaviour differs from the assumption:

- [ ] **Search response shape** — bare array or wrapped object? (→ `lucid_client.py:search_documents`)
- [ ] **Search field names** — `documentId` / `id`? `parent.folderId` / `folderId`? `editUrl`?
- [ ] **Trashed field** — is `"trashed": true` the correct filter?
- [ ] **Search pagination** — `Link` header present? Same format as assumed?
- [ ] **Folder contents shape** — bare array? Item field names (`id`, `type`, `name`)?
- [ ] **Folder pagination** — `Link` header?
- [ ] **PNG export DPI** — is the `png_dpi` config value actually sent/honored?
- [ ] **Editor URL pattern** — `/{product}/{id}/edit` works for all three products?
- [ ] **File menu selector** — `[data-test-id="file-menu"]` or text fallback?
- [ ] **Export menu item** — `Export` / `Exportar` text present?
- [ ] **PDF selector** — `PDF` text in export dialog?
- [ ] **VSDX selector** — `Visio` / `VSDX` text in export dialog?
- [ ] **Download button** — selector matches the confirm/download button?
- [ ] **Rate limit values** — 75/5 s export and 300/5 s search still current?

Update `docs/lucid-api-notes.md` after each item is confirmed, removing the PROVISIONAL
label from confirmed items.

---

## Live reconciliation results (confirmed 2026-06)

Validated against a real Lucid Pro account (~450 documents):

| Item | Result |
|---|---|
| OAuth authorize / token URLs | `https://lucid.app/oauth2/authorize`, `https://api.lucid.co/oauth2/token` - correct |
| Scopes | `lucidchart/lucidspark/lucidscale.document.content:readonly`, `folder:readonly`, `offline_access`, `user.profile` - all valid. `folder:readonly` IS required for folder names (omitting it gives 403 on `/folders`). |
| Redirect URI | Must be registered **exactly** as `http://localhost:8765/callback` in the app. |
| `POST /documents/search` | Bare JSON array; **no** `/v1/` prefix needed; `Lucid-Api-Version: 1` header. |
| Pagination | `Link: <url...&pageToken=...>; rel="next"` response header. |
| Document fields | `documentId`, `title`, `product`, `parent` (folder id, may be null), `pageCount`, `version`, `created`, `lastModified`, `editUrl`, `trashed`. (`owner` only on `GET /documents/{id}`, not in search results.) |
| PNG export | `GET /documents/{id}?page=N`, `Accept: image/png` -> 200 image/png. |
| `GET /folders/{id}` | Fields `id`, `name`, `parent`, `type`, `created`, `trashed`. 403 without `folder:readonly`. |
| Browser export path | Hamburger menu (`data-test-id="header-hamburger-menu"`) -> menu item **Exportar** -> format (**PDF** / **Visio (VSDX)**, `data-test-id="menu-item-container"`, matched by substring) -> **Descargar** dialog button (`data-test-id="print-and-download-dialog-proceed-button"`). An AI popover auto-opens and must be dismissed with `Escape` first. |
| Editor URL | `https://lucid.app/{product}/{id}/edit` - matches the API `editUrl` for Lucidchart. |
| Rate limits | 75/5 s export, 300/5 s search - tool stays under (60/240) and honors `Retry-After`. |

Operational notes: SQLite can't run on some SMB shares (state DB auto-falls back to local
disk); the Python venv with native deps (Playwright/greenlet) must be on a local disk, not a
UNC path.
