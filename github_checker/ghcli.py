"""Synchronous `gh` invocation against a local clone.

Shared by the whitelist actions and the merge gate so both get the same
hardening: a missing binary or a timeout is a failed process, never an
exception that would escape a verb and break the JSON contract.
"""

import json
import subprocess
from pathlib import Path

GH_TIMEOUT = 60


def run_gh(
    path: Path, *args: str, binary: str = "gh", timeout: int = GH_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    """Run gh in *path*; never raises — failures surface as returncode 127."""
    try:
        return subprocess.run(
            [binary, *args],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        # OSError (not just FileNotFoundError): a huge --body argv can hit
        # E2BIG, and a restricted binary can hit PermissionError — both are
        # real failure modes for issue_create's unbounded prose, and both
        # must become a failed process, not an exception past this "never
        # raises" guarantee. FileNotFoundError is already an OSError
        # subclass, so this widens the net rather than replacing a case.
        return subprocess.CompletedProcess(
            [binary, *args], returncode=127, stdout="", stderr=str(err)
        )


def repo_slug(path: Path, *, binary: str = "gh") -> tuple[str, str] | None:
    """`(owner, name)` of the clone's GitHub repo, or None if unresolvable."""
    proc = run_gh(path, "repo", "view", "--json", "owner,name", binary=binary)
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
        return data["owner"]["login"], data["name"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
