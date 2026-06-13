"""Audit manifest under _manifest/: inventory.csv, errors.csv, verification.md,
cancellation-checklist.md. Every row traces back to a Lucid document id."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

from .state import ARTIFACT_KINDS, StateDB
from .utils import ensure_dir


def write_manifest(db: StateDB, vault_dir: Path) -> Path:
    man = ensure_dir(vault_dir / "_manifest")
    docs = db.all_documents()

    with (man / "inventory.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["document_id", "title", "product", "folder_path", "pages",
                    "created", "last_modified", "owner", "edit_url", "note_path",
                    *(f"{k}_status" for k in ARTIFACT_KINDS)])
        for d in docs:
            statuses = [
                (db.get_artifact(d["document_id"], k) or {}).get("status", "missing")
                for k in ARTIFACT_KINDS
            ]
            w.writerow([d["document_id"], d["title"], d["product"], d["folder_path"],
                        d["page_count"], d["created"], d["last_modified"], d["owner"],
                        d["edit_url"], d["note_path"], *statuses])

    with (man / "errors.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["item_id", "operation", "error_message", "timestamp"])
        for e in db.all_errors():
            w.writerow([e["item_id"], e["operation"], e["error_message"], e["timestamp"]])

    lines = [f"# Verification report\n\nGenerated: {datetime.now(UTC).isoformat()}\n",
             f"\nDocuments inventoried: **{len(docs)}**\n"]
    for kind in ARTIFACT_KINDS:
        counts: dict[str, int] = {}
        for d in docs:
            st = (db.get_artifact(d["document_id"], kind) or {}).get("status", "missing")
            counts[st] = counts.get(st, 0) + 1
        summary = ", ".join(f"{s}: {n}" for s, n in sorted(counts.items()))
        lines.append(f"- **{kind}** - {summary}\n")
    failed = [d for d in docs for k in ARTIFACT_KINDS
              if (db.get_artifact(d["document_id"], k) or {}).get("status") == "failed"]
    if failed:
        lines.append("\n## Documents with failures (run `retry`)\n")
        for d in {x["document_id"]: x for x in failed}.values():
            lines.append(f"- {d['title']} (`{d['document_id']}`)\n")
    (man / "verification.md").write_text("".join(lines), encoding="utf-8")

    (man / "cancellation-checklist.md").write_text(
        "# Checklist antes de cancelar Lucid\n\n"
        "- [ ] `lucid-vault-exporter verify` sin faltantes inesperados\n"
        "- [ ] `_manifest/verification.md` revisado (png/pdf/vsdx ok)\n"
        "- [ ] `errors.csv` revisado; fallos aceptados o reintentados con `retry`\n"
        "- [ ] Vault abierto en Obsidian y muestreado (10 docs al azar)\n"
        "- [ ] Copia de seguridad del vault fuera de la maquina de export\n"
        "- [ ] Cancelar suscripcion en lucid.app/settings\n",
        encoding="utf-8",
    )
    return man
