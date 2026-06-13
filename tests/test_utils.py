from pathlib import Path

from lucid_vault_exporter.utils import ensure_dir, sanitize_filename, unique_path


def test_sanitize_strips_windows_forbidden_chars():
    assert sanitize_filename('a<b>:c"/d\\e|f?g*h') == "a_b__c__d_e_f_g_h"


def test_sanitize_trims_dots_spaces_and_length():
    assert sanitize_filename("  name. ") == "name"
    assert len(sanitize_filename("x" * 300)) <= 120


def test_sanitize_empty_falls_back():
    assert sanitize_filename("???") == "untitled"


def test_ensure_dir_creates(tmp_path: Path):
    target = tmp_path / "a" / "b"
    ensure_dir(target)
    assert target.is_dir()


def test_sanitize_preserves_boundary_underscores_from_substitution():
    # forbidden chars at the boundaries become underscores and are kept
    assert sanitize_filename("<hello>") == "_hello_"
    # but an all-underscore result still falls back to untitled
    assert sanitize_filename("<>") == "untitled"


def test_unique_path_suffixes(tmp_path: Path):
    p = tmp_path / "doc.md"
    p.write_text("x")
    assert unique_path(tmp_path / "doc.md").name == "doc 2.md"
