"""Drift-control guarantee A for the vendored epics/v1 contract.

Offline integrity only (see the vendored drift-control.md): the vendored
surface is re-hashed and compared with the vendored manifest.json in BOTH
directions — a missing file, a foreign file, and a changed file are three
different lies and each must fail. This proves the copy is intact; it proves
nothing about whether canon moved (guarantee B is advisory and lives with a
canon checkout, never in required CI).
"""

import hashlib
import json
from pathlib import Path

VENDORED = Path(__file__).parent.parent / "github_checker" / "contract_epics"

# The surface excludes the drift-control meta files and the vendor-only pin —
# exactly the exclusion the manifest itself declares in surface_note.
META = {"manifest.json", "drift-control.md", "PINNED.txt"}


def _manifest() -> dict:
    """The vendored fingerprint (parsed fresh per test: no shared state)."""
    return json.loads((VENDORED / "manifest.json").read_text(encoding="utf-8"))


def _disk_surface() -> dict[str, str]:
    """sha256 of every vendored surface file, keyed by relative path."""
    return {
        str(p.relative_to(VENDORED)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in VENDORED.rglob("*")
        if p.is_file() and str(p.relative_to(VENDORED)) not in META
    }


def test_every_manifest_file_is_present_and_intact() -> None:
    listed = {entry["path"]: entry["sha256"] for entry in _manifest()["surface"]}
    assert listed == _disk_surface(), (
        "vendored epics/v1 surface diverged from its manifest — do not edit "
        "the copy in place; re-vendor from the pin (see PINNED.txt)"
    )


def test_tree_rollup_matches() -> None:
    listed = sorted(
        (entry["path"], entry["sha256"]) for entry in _manifest()["surface"]
    )
    blob = "".join(f"{path}\0{digest}\n" for path, digest in listed).encode()
    assert hashlib.sha256(blob).hexdigest() == _manifest()["tree_sha256"]
