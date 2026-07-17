# Design — `propose-pr`: scoped content-PR from an isolated worktree

> **Context (2026-07-17):** dispatcher's spec-runner config editor shipped with its
> write path gated because it relied on `open-pr` doing branch+commit+push — which
> `open-pr` deliberately never does ("never pushes" is its documented contract).
> Handoff: `prograph-vault/authored/notes/2026-07-17-github-checker-open-pr-needs-branch-commit-push.md`.
> This design adds a **new command** rather than changing `open-pr`'s semantics:
> a command that commits and pushes is a qualitatively different privilege level,
> and existing `open-pr` callers must not silently acquire side effects.
> Reviewed twice before writing (product + engineering review, 2026-07-17); all
> amendments from both reviews are folded in below.

## 1. Command contract

```
github-checker propose-pr <dir> --message <msg>
    --edit <repo-path>=<content-file> [--edit ...]
    [--if-match <repo-path>=<sha256>] [--if-match ...]
    [--branch <name>]
```

- `<dir>` — an existing git clone (same addressing as `pull`/`open-pr`).
- `--message` — required; becomes the commit message (and, via
  `gh pr create --fill`, the PR title/body).
- `--edit` — repeatable; `<repo-path>` is the file to create/replace inside the
  repo, `<content-file>` is a file on disk holding the full new content. The
  caller's live working tree is **never read as content source** — content
  arrives only through `--edit`. A duplicated `<repo-path>` across two `--edit`
  flags is an error (not last-wins). Missing parent directories of
  `<repo-path>` are created (`mkdir -p` semantics).
- `--if-match` — repeatable, optional per path: a sha256 of the base content
  the caller *saw* when rendering its edit. After fetch, the command compares
  `sha256` of the **raw blob bytes** of `origin/<default>:<repo-path>` —
  obtained via `git cat-file blob` (not `git show`, which may apply smudge
  filters), with no newline normalization and no text-mode decoding, so
  CRLF/BOM content cannot produce a false mismatch against what the caller
  hashed from disk. Mismatch — or the path being absent on the default
  branch — yields `ok=False, error="base file changed; reload required"`
  before any branch, commit, or push exists. This closes the render-vs-submit
  race in which a caller could silently revert someone else's change that
  landed on the default branch in between. **v1 limitation (explicit):**
  `--if-match` cannot express "I expect this file to NOT exist yet" — a
  create-new-file edit therefore has no stale guard in v1 (no `<absent>`
  sentinel); an `--if-match` on a path absent from the default branch is
  always a mismatch. Fine for dispatcher (its `project.yaml` always exists).
- `--branch` — optional; default `propose/<utc-timestamp>-<6 hex>` (collision-
  resistant for parallel edits; a retry after failure generates a fresh name,
  see §5 for why orphans still can't accumulate).
- Output: one JSON `ActionResult` on stdout, like `pull`/`open-pr`.

### Parsing / normalization (pinned so tests and clients can't diverge)

- `--edit` and `--if-match` values split on the **first** `=` only (content
  filenames and hashes may not contain `=` before the split point; repo-paths
  may not contain `=` at all in v1 — reject rather than guess).
- `<content-file>` must be a readable **regular file** (not a directory,
  FIFO, or dangling symlink) — checked before any git work starts.
- Duplicate-`<repo-path>` detection runs on **normalized** paths (after
  separator/`.`-segment normalization), so `a/b` and `./a//b` collide.
- `changed_paths` in the result reports normalized, repo-relative POSIX
  paths.

### Edit-path validation (defense against escapes)

A `<repo-path>` is rejected if it:
- is absolute, or contains `..` or a `.git` component;
- after joining to the worktree root and resolving, does not land strictly
  under the worktree root (closes `a/b` where `a` is a symlink pointing
  outside);
- has any existing parent component — or an existing target — that is a
  symlink.

### Branch-name validation

`--branch` is validated with `git check-ref-format --branch <name>` (no
hand-rolled regex), and additionally rejected if it names the default branch,
an existing local branch, or an existing remote branch. The command never
force-pushes and never updates an existing branch.

## 2. Mechanics

```
fetch --prune
→ resolve default branch (§3)
→ --if-match guards against origin/<default> (§1)
→ git worktree add <tmpdir> -b <branch> origin/<default>
→ write each --edit's content at its repo-path (validated per §1)
→ git add <exactly those paths>
→ no-op guard: empty diff vs origin/<default> → ok=False,
  error="no changes vs <default>"          (§4 for semantics)
→ git commit -m <message>
→ git push -u origin <branch>
→ gh pr create --fill   (run inside the worktree)
→ finally: git worktree remove --force <tmpdir>; git branch -D <branch>
  (worktree removal strictly before branch delete — git refuses to delete a
  checked-out branch)
```

**Honest isolation wording** (both reviews): the operator's **live working
tree files are never read as source content and never modified** — verified
byte-for-byte in tests. Git **refs/worktree metadata are shared and are
updated** as part of isolated PR creation: `fetch` refreshes remote refs, the
worktree adds objects and a temporary local branch to the shared `.git`. This
is the same class of metadata effect the existing `pull` already has; it is
documented, not hidden behind "the live tree is untouched".

**Committer identity:** no magic. The commit uses whatever `user.name`/
`user.email` git resolves; if unset, the commit stage fails honestly into
`ActionResult(ok=False, ...)`. No `-c user.name=...` injection.

## 3. Default-branch resolution (with fallback)

`origin/HEAD` is not guaranteed to exist (fresh `remote add`) **and can be
stale even when it exists** (the default branch was changed on GitHub after
clone — `refs/remotes/origin/HEAD` is not updated by a plain fetch).
Resolution order:

1. right after `fetch --prune`, best-effort `git remote set-head origin -a`
   (refreshes `origin/HEAD` from the remote; its own failure is non-fatal);
2. `git symbolic-ref refs/remotes/origin/HEAD` (now fresh if step 1 worked);
3. `git remote show origin` (network) or `gh repo view --json defaultBranchRef`;
4. none works → `ok=False, error="cannot determine default branch"`.

## 4. No-op semantics (decided, not accidental)

If the staged edits produce an empty diff against `origin/<default>`, the
result is `ok=False` with the **structural marker** `detail="no-op"` — the
machine-checkable contract lives in the already-existing `detail` field
(unused on failure paths today), not in error-string parsing, which is
brittle. The `error` string stays human-readable and free-form (e.g.
`"no changes vs main"`). Rationale for `ok=False`: "already applied" is
arguably a success for a caller like dispatcher, but `ok=True` without a
`pr_url` would silently break the existing "ok → show the PR URL" consumer
pattern; and with `--if-match` in use the stale-base case is caught earlier,
making a true no-op rare. Callers that want to treat it as success check
`detail == "no-op"`.

## 5. Failure handling / branch lifecycle

Every stage degrades to `ActionResult(ok=False, error=...)` — the `actions.py`
convention; the `finally` cleanup (worktree remove + local branch delete)
always runs and is **tolerant of partial progress**: a failure before
`worktree add` (e.g. an `--if-match` mismatch) means neither the worktree nor
the branch exists — each cleanup step silently skips "nothing to delete"
rather than raising a secondary error that could mask the original one.

Remote-branch lifecycle after a successful push:
- **PR created** → the remote branch stays (it is the PR head).
- **`gh pr create` failed** → best-effort `git push origin --delete <branch>`
  so retries (which use fresh generated names) cannot accumulate orphaned
  remote branches. Best-effort strictly: a cleanup failure must not mask the
  original `gh` error — in that case the result carries the original error
  AND `branch=<name>` so the caller/operator knows what was left behind.

## 6. `ActionResult` additions (additive, existing consumers unaffected)

- `branch` — the head branch name (set on success; also set on the
  cleanup-failed path of §5).
- `base_branch` — the resolved default branch.
- `commit_sha` — the pushed commit (when one was created).
- `changed_paths` — the list of repo-paths committed.

dispatcher currently parses `ok/detail/error/pr_url` and ignores the rest;
these fields are for audit value.

## 7. Invariants (unchanged from the existing whitelist's spirit)

- PR-only; never a push to the default branch (the pushed branch is always
  freshly created from `origin/<default>` and is the only ref pushed); never
  a merge.
- `pull` and `open-pr` are not touched in any way.
- No force-push, ever, anywhere.

## 8. Testing

Convention: real git (bare origin + clone via the existing `_make_pair`
fixture in `tests/test_actions.py`), with `gh` faked via
`monkeypatch.setattr(actions, "_gh", ...)` — the file's existing convention
(NOT a PATH stub; corrected per review). Git operations are always real —
this is the "the stub swallowed everything" lesson from dispatcher's gate,
where `{"ok": true}` stubs masked a feature-breaking contract mismatch.

Required cases (union of both reviews):

1. Happy path: edit lands as a commit on a fresh branch in origin; the live
   clone's working tree is byte-for-byte unchanged.
2. Live checkout dirty **on the same file** being edited: propose-pr still
   bases on `origin/<default>` and the live file stays byte-for-byte as the
   operator left it.
3. `--if-match` mismatch → `ok=False`, no branch created, no push, no PR.
4. `--branch` naming an existing local or remote branch → refusal.
5. Symlink escape (parent symlink pointing outside the worktree) → refusal,
   nothing written outside.
6. `gh pr create` fails after successful push → remote branch deleted
   (best-effort path), or — when the delete is made to fail too — result
   carries both the original error and `branch`.
7. Multiple `--edit`s in one call → single commit with all paths;
   `changed_paths` matches.
8. Duplicate `<repo-path>` across two `--edit`s → error.
9. `origin/HEAD` not set → fallback resolution still finds the default
   branch (the `_make_pair` clone may lack it — assert the fallback runs).
10. `origin/HEAD` **stale** (points at a branch that is no longer the
    remote's default) → `set-head origin -a` refresh yields the correct
    base; the commit lands on a branch off the *new* default.
11. No-op content (equal to default) → `ok=False` with `detail == "no-op"`
    (structural marker, not error-string parsing).
12. Repo-path containing a `.git` component → refusal (validation is stated
    in §1; this pins it with a test alongside the symlink case).

## 9. Out of scope (v1)

- File deletions.
- The scoped-dirty-paths mode (committing paths from the live tree) —
  explicitly rejected in favor of explicit `--edit` content passing.
- Any change to `open-pr` or `pull`.
- PR body templating beyond `--fill`.

## 10. Consumers / follow-ups

- dispatcher un-gating (its side, tracked there): flip
  `SPEC_RUNNER_CONFIG_WRITE_GATED`, rework
  `core/spec_runner_config_actions.py` to render content to a temp file and
  call `propose-pr` with `--edit project.yaml=<tmp>` + `--if-match` from the
  `base_mtime`-era content hash, emit only explicit-or-changed typed keys,
  and add a real-git integration test.
- Handoff note in prograph-vault to be updated to `status: resolved` once
  this ships.
- Optional, when dispatcher starts vendoring the actions-JSON contract:
  publish `contracts/actions/v1/` (schema + golden fixtures) following the
  existing `contracts/snapshot/v1/` pattern. Today dispatcher parses the
  `ActionResult` JSON without a pinned schema; `propose-pr` grows that
  surface, making a pinned contract worth its cost.
