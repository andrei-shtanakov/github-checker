"""Headless workspace snapshot: state of every repo in a polyrepo root as JSON.

Designed for agents and scripts (fleet-check), not for humans: no TUI, no
config file — repositories are discovered by scanning `<workspace>/*/.git`.
GitHub data is optional: without an authenticated gh CLI the snapshot
degrades to local git state and records the reason in `gh_error`.
"""

import asyncio
import re
import socket
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from github_checker.github import fetch_all, gh_ready
from github_checker.localgit import local_status, remote_url
from github_checker.models import LocalStatus, RepoRef, RepoState

_GITHUB_REMOTE_RE = re.compile(
    r"^(?:git@github\.com:|https://github\.com/|ssh://git@github\.com/)"
    r"(?P<slug>[\w.-]+/[\w.-]+?)(?:\.git)?/?$"
)


def parse_github_remote(url: str) -> str | None:
    """Extract 'owner/repo' from a GitHub remote URL, else None."""
    match = _GITHUB_REMOTE_RE.match(url.strip())
    return match.group("slug") if match else None


class RepoSnapshot(BaseModel):
    """One workspace repository: local git state plus optional GitHub state."""

    dir: str
    remote: str | None
    local: LocalStatus
    github: RepoState | None = None


class WorkspaceSnapshot(BaseModel):
    """Full fleet state; `host` marks whose clones the local data describes.

    The JSON shape is a frozen contract (`contracts/snapshot/v1/`): consumers
    key off `schema_version`, and any breaking change to this model or its
    parts must ship as v2 alongside v1 — never as an edit to v1.
    """

    schema_version: Literal[1] = 1
    workspace: Path
    host: str
    generated_at: datetime
    gh_error: str | None
    repos: list[RepoSnapshot]


def discover(root: Path) -> list[Path]:
    """Top-level directories of *root* that are git repos (dir or file .git)."""
    return sorted(p.parent for p in root.glob("*/.git"))


async def build_snapshot(root: Path, include_github: bool = True) -> WorkspaceSnapshot:
    """Collect local state for every repo, plus GitHub state when gh is ready."""
    root = root.resolve()
    dirs = discover(root)
    locals_ = await asyncio.gather(*(asyncio.to_thread(local_status, d) for d in dirs))
    remotes = await asyncio.gather(*(asyncio.to_thread(remote_url, d) for d in dirs))
    slugs = [parse_github_remote(url) if url else None for url in remotes]

    gh_error = gh_ready() if include_github else "skipped (--local-only)"
    states: dict[str, RepoState] = {}
    if gh_error is None:
        refs = [RepoRef(name=slug) for slug in slugs if slug]
        states = {state.name: state for state in await fetch_all(refs)}

    repos = [
        RepoSnapshot(
            dir=d.name,
            remote=slug,
            local=local,
            github=states.get(slug) if slug else None,
        )
        for d, slug, local in zip(dirs, slugs, locals_)
    ]
    return WorkspaceSnapshot(
        workspace=root,
        host=socket.gethostname(),
        generated_at=datetime.now(),
        gh_error=gh_error,
        repos=repos,
    )
