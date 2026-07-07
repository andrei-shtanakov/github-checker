# Local repo link & sync — design

## Goal

In the details panel (right pane), for each repository:

1. Show a clickable link to the remote GitHub repository.
2. Optionally show a local clone path (if configured).
3. For a configured local clone, show desync info (ahead/behind upstream, dirty
   working tree) and allow syncing it from the TUI.

## Decisions

- **Config format (A):** each repo becomes a TOML table `[[repos]]` with `name`
  and optional `path`. The legacy `repos = ["owner/repo", ...]` list stays
  readable via a `mode="before"` validator that coerces strings to `RepoRef`.
- **Sync semantics (C):** `s` runs `git fetch --prune` (always safe), `S` runs
  `git pull --ff-only` (safe, refuses on divergence). Desync info is computed on
  every refresh (cheap, local, no network) from the last fetched remote-tracking
  refs.
- **Interaction (A):** hotkeys `s` (Sync/fetch) and `S` (Pull/ff-only), matching
  the existing `r`/`a`/`d`/`p` style. No new buttons or screens.
- Local `path` is set by editing `repos.toml` by hand (documented in
  `repos.toml.example`). No UI to set the path (YAGNI).

## Architecture

New module `github_checker/localgit.py` isolates all local git work: subprocess
calls to `git`, synchronous, invoked via `asyncio.to_thread`. It knows nothing
about Textual or the GitHub API. Everything else plugs into the existing flow:
config → fetch → RepoState → details panel.

## Models (`models.py`)

```python
class RepoRef(BaseModel):
    name: str            # owner/repo, validated by REPO_RE
    path: Path | None = None

class Config(BaseModel):
    repos: list[RepoRef] = []      # before-validator coerces "a/b" -> RepoRef
    refresh_seconds: int = 120

class LocalStatus(BaseModel):
    branch: str | None            # current branch
    ahead: int | None             # unpushed commits (None = no upstream)
    behind: int | None            # unpulled commits
    dirty: bool                   # uncommitted changes present
    error: str | None             # not-a-git-repo / path missing / git failed
```

- `RepoState` gains `path: Path | None = None` and `local: LocalStatus | None =
  None`.
- REPO_RE validation moves from `Config.repos` to `RepoRef.name`.
- `save_config` uses `model_dump(mode="json", exclude_none=True)` so `path=None`
  is omitted and `Path` serializes to a string. The first add/remove converts a
  legacy `repos = [...]` file into `[[repos]]` tables (acceptable).

## localgit.py

```python
class LocalGitError(Exception): ...

def local_status(path: Path) -> LocalStatus:
    # git -C path rev-parse --abbrev-ref HEAD            -> branch
    # git -C path rev-list --left-right --count @{u}...HEAD -> behind, ahead
    #   (no upstream -> ahead/behind = None)
    # git -C path status --porcelain                     -> dirty
    # any failure / non-repo / missing path -> error set, never raises

def fetch(path: Path) -> None:      # git -C path fetch --prune; raises LocalGitError
def pull_ff_only(path: Path) -> None:  # git -C path pull --ff-only; raises LocalGitError
```

## Data flow (`github.py`)

- `fetch_all` / `fetch_repo` take `list[RepoRef]` instead of `list[str]`.
- Inside `fetch_repo`, if `ref.path` is set, local status is computed via
  `asyncio.to_thread(local_status, ref.path)` and stored in `RepoState.local`
  in **both** the success and API-error return paths, so desync is visible even
  when GitHub is unreachable. These are local ops (no network), cheap per
  refresh.

## Details panel (`app.py`)

- Rendering moves from `str` to a Rich `Text` object (no markup parsing, so the
  existing `[dbot]` / `[copilot]` badges stay literal). The remote link
  `https://github.com/{name}` is a real OSC 8 hyperlink (Rich link style;
  Cmd+click in iTerm2 and most macOS terminals). The URL is also plain-readable
  for copy.
- Local block (only when `path` is set):

```
andrei-shtanakov/atp-platform
https://github.com/andrei-shtanakov/atp-platform

Local: /Users/.../atp-platform
  main  ↑2 ↓1  dirty        (or "up to date" / "no upstream" / "ERROR: ...")

Pull requests:
  ...
```

- **Keys:** `s` = fetch selected, `S` = pull --ff-only. Both run in a separate
  worker (`group="local"`, does not cancel refresh); on success they recompute
  `local_status` for that one repo, update its `RepoState.local`, refresh the
  row + panel, and `notify`. Guards: no `path` -> "локальный путь не задан";
  `local.error` -> warning. Footer gains `Sync` / `Pull` labels.

## Follow-on edits

- `app.py`: `name in self._config.repos` -> compare against `r.name`;
  `ProtectionScreen(name, [r.name for r in self._config.repos])`.
- `config.py`: `add_repo` / `remove_repo` operate on `RepoRef` (add creates
  `RepoRef(name=name)`, remove filters by `r.name`).
- `repos.toml.example`: show both a remote-only entry and one with `path`.

## Testing

- New `test_localgit.py`: real temp git repos via `tmp_path` — no upstream,
  ahead/behind, dirty tree, non-git path (error), fetch/pull success + failure.
- `test_config.py`: round-trip of both legacy list and `[[repos]]` format;
  add/remove preserving paths.
- `test_models.py`: string coercion into `RepoRef`, `RepoRef.name` validation.
- `test_fetch.py`: `fetch_all` accepts `RepoRef`; local status attached.
- `test_app.py`: Text render (link present, Local block present/absent), sync
  guards.
- All under `uv run pytest`.
