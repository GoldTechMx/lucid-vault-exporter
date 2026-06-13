from pathlib import Path

from lucid_vault_exporter.inventory import run_inventory
from lucid_vault_exporter.state import StateDB


class FakeClient:
    def __init__(self):
        self.folders = {
            "f1": {"id": "f1", "name": "Clientes", "parent": None},
            "f2": {"id": "f2", "name": "ACME", "parent": "f1"},
            # cycle: f3 -> f4 -> f3
            "f3": {"id": "f3", "name": "Loop", "parent": "f4"},
            "f4": {"id": "f4", "name": "Loop2", "parent": "f3"},
        }
        self.docs = [
            {"documentId": "d1", "title": "Mapa", "product": "lucidchart",
             "parent": "f2", "pageCount": 2, "created": "2020-01-01T00:00:00Z",
             "lastModified": "2021-01-01T00:00:00Z", "editUrl": "https://lucid.app/x/d1"},
            {"documentId": "d2", "title": "Suelto", "product": "lucidspark", "parent": None},
            {"documentId": "d3", "title": "Ciclico", "product": "lucidchart", "parent": "f3"},
        ]

    def search_documents(self, **kw):
        yield from self.docs

    def get_folder(self, folder_id):
        return self.folders.get(folder_id)


def test_inventory_persists_docs_with_folder_paths(tmp_path: Path):
    with StateDB(tmp_path / "s.sqlite") as db:
        count = run_inventory(FakeClient(), db, products=["lucidchart", "lucidspark"])
        assert count == 3
        assert db.get_document("d1")["folder_path"] == "Clientes/ACME"
        assert db.get_document("d2")["folder_path"] == ""  # root
        # cycle must terminate, not hang; path is best-effort
        assert db.get_document("d3") is not None


def test_inventory_is_idempotent(tmp_path: Path):
    with StateDB(tmp_path / "s.sqlite") as db:
        run_inventory(FakeClient(), db, products=["lucidchart"])
        run_inventory(FakeClient(), db, products=["lucidchart"])
        assert len(db.all_documents()) == 3


def test_inventory_nested_under_cached_ancestor(tmp_path: Path):
    # f1=Clientes (root) -> f2=ACME -> f5=Sub. d1 under f2 (caches f1,f2), d2 under f5.
    class C:
        folders = {
            "f1": {"id": "f1", "name": "Clientes", "parent": None},
            "f2": {"id": "f2", "name": "ACME", "parent": "f1"},
            "f5": {"id": "f5", "name": "Sub", "parent": "f2"},
        }
        docs = [
            {"documentId": "d1", "title": "A", "product": "lucidchart", "parent": "f2"},
            {"documentId": "d2", "title": "B", "product": "lucidchart", "parent": "f5"},
        ]
        def search_documents(self, **kw):
            yield from self.docs
        def get_folder(self, fid):
            return self.folders.get(fid)
    from lucid_vault_exporter.inventory import run_inventory
    from lucid_vault_exporter.state import StateDB
    with StateDB(tmp_path / "s.sqlite") as db:
        run_inventory(C(), db, products=["lucidchart"])
        assert db.get_document("d1")["folder_path"] == "Clientes/ACME"
        # THE KEY ASSERTION — d2 must get the FULL nested path, not a truncated one:
        assert db.get_document("d2")["folder_path"] == "Clientes/ACME/Sub"
        # and the memoised folder f5 must have the correct full path:
        assert db.get_folder("f5")["path"] == "Clientes/ACME/Sub"
