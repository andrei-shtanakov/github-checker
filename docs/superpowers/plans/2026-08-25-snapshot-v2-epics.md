# snapshot/v2 — Epic Classification + Merged-PR Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish `contracts/snapshot/v2`: normalized epic/defect classification
(`epics/v1`) on every open Issue and PullRequest, plus a merged-PR attribution
window (`commit → PR` transport for robin), behind `snapshot --schema-version 2`,
while v1 stays the default and stays frozen.

**Architecture:** Vendor the full `epics/v1` contract surface into
`github_checker/contract_epics/` (pinned copy, dispatcher idiom). A new
`github_checker/epics.py` compiles the epic/defect grammar from the vendored
schema and classifies artifact bodies by their **final trailer block**. A new
`github_checker/snapshot_v2.py` holds the v2 models and its own fetch path
(reusing `github.py` helpers); v1 models/fetch are untouched. The CLI gains
`--schema-version {1,2}` (default 1) and `--window-days`.

**Tech Stack:** Python 3.12, pydantic v2, gh CLI (`gh api`), pytest, jsonschema (dev).

**Spec:** GitHub issue #23 (accepted as `TODO.md` `@id:snapshot-v2-epics`) +
vendored `github_checker/contract_epics/README.md` (epics/v1 normative spec).

## Global Constraints

- v1 stays byte-frozen: `tests/test_snapshot_contract.py` must keep passing unchanged.
- `classification` is the closed four-state `tagged | missing | invalid | unavailable`;
  `unavailable` NEVER collapses into `missing`.
- Raw issue/PR bodies never appear in the snapshot — only the normalized verdict.
- Grammar is compiled from the vendored copy, never restated (no second regex).
- Layer boundary: producer proves presence + grammar (`tagged` = well-formed &
  present). Registry membership (EP-UNKNOWN / EP-MOVED) is downstream — the
  registry (`epics.toml`) is read live by the sensor, never by this producer.
- Contract boundary recorded in v2 README: dispatcher consumes only the open
  planes; the attribution window is transport for robin, not state.
- EP-MISSING severity is emitted as `warning`; escalation by
  `missing_error_after` is the fleet layer's call (registry lives elsewhere).
- Line length 88; ruff + pyrefly clean; `uv run pytest` green after every task.
- Commits carry trailer `Epic: eco.epics`.

---

### Task 1: Vendor epics/v1 + integrity test (drift-control guarantee A)

**Files:**
- Create: `github_checker/contract_epics/` — full copy of
  `prograph-vault/authored/contracts/epics/v1/` (README.md,
  classification.schema.json, registry.schema.json, diagnostics.yaml,
  manifest.json, drift-control.md, fixtures/**)
- Create: `github_checker/contract_epics/PINNED.txt`
- Test: `tests/test_contract_epics_integrity.py`

**Interfaces:**
- Produces: vendored files read by Task 2 (`classification.schema.json` `$defs`)
  and by conformance tests (fixtures).

- [x] **Step 1: Copy the surface + write PINNED.txt**

```bash
cp -R ../prograph-vault/authored/contracts/epics/v1/ github_checker/contract_epics/
```

`PINNED.txt` (source commit = `git -C ../prograph-vault log -1 --format=%H -- authored/contracts/epics/v1`):

```
source: prograph-vault authored/contracts/epics/v1
commit: <resolved sha>
vendored: 2026-08-25
note: pinned copy (repo-boundaries vendoring). Do not edit here; re-vendor from
  the pin. github_checker/epics.py compiles the epic/defect grammar from this
  copy (executable delegation, same idiom as dispatcher's plan-fields), and the
  snapshot/v2 conformance tests replay the pull_request/issue fixtures.
```

- [x] **Step 2: Write the failing integrity test**

Guarantee A (offline, both directions) per vendored `drift-control.md`:
recompute sha256 per surface file, compare with vendored `manifest.json`
(surface excludes manifest.json, drift-control.md, PINNED.txt), verify the
`tree_sha256` rollup over `"<relpath>\0<filehash>\n"` sorted by path.

```python
import hashlib
import json
from pathlib import Path

VENDORED = Path(__file__).parent.parent / "github_checker" / "contract_epics"
META = {"manifest.json", "drift-control.md", "PINNED.txt"}


def _manifest() -> dict:
    return json.loads((VENDORED / "manifest.json").read_text())


def _disk_surface() -> dict[str, str]:
    files = {
        str(p.relative_to(VENDORED)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in VENDORED.rglob("*")
        if p.is_file() and str(p.relative_to(VENDORED)) not in META
    }
    return files


def test_every_manifest_file_is_present_and_intact() -> None:
    disk = _disk_surface()
    listed = {e["path"]: e["sha256"] for e in _manifest()["surface"]}
    assert listed == disk  # both directions: no missing, no foreign, no drift


def test_tree_rollup_matches() -> None:
    listed = sorted((e["path"], e["sha256"]) for e in _manifest()["surface"])
    blob = "".join(f"{p}\0{h}\n" for p, h in listed).encode()
    assert hashlib.sha256(blob).hexdigest() == _manifest()["tree_sha256"]
```

- [x] **Step 3: Run tests** — `uv run pytest tests/test_contract_epics_integrity.py -v` → PASS
  (the copy is fresh; the test now guards it forever).

- [x] **Step 4: Commit** — `vendor: pin epics/v1 contract (snapshot-v2-epics)` + Epic trailer.

---

### Task 2: `github_checker/epics.py` — grammar, trailer parser, classifier

**Files:**
- Create: `github_checker/epics.py`
- Test: `tests/test_epics.py`

**Interfaces:**
- Consumes: `github_checker/contract_epics/classification.schema.json`.
- Produces (used by Tasks 3–5):
  - `class EpicDiagnostic(BaseModel)`: `code: str`, `severity: Literal["warning","error"]`,
    `message: str`, `subject_uri: str | None = None`, `raw: str | None = None`
  - `class EpicClassification(BaseModel)`: `epic: str | None`, `defect: str | None`,
    `classification: Literal["tagged","missing","invalid","unavailable"]`,
    `diagnostics: list[EpicDiagnostic]`, `subject_uri: str | None`,
    `carrier: Literal["pull_request","issue"]`, `observed_at: str | None`
  - `def artifact_uri(repo: str, kind: Literal["pull","issues"], number: int) -> str | None`
  - `def classify_body(body: str | None, *, retrieved: bool, subject_uri: str | None,`
    `carrier: ..., observed_at: str | None) -> EpicClassification`

**Behavioral spec (pinned by tests):**
- Bodies are CRLF-normalized; trailers live ONLY in the final paragraph, and the
  paragraph counts as a trailer block only if EVERY line matches
  `^[A-Za-z][A-Za-z0-9-]*: <value>`; values are whitespace-stripped, never
  case-folded. A mid-prose `Epic:` line never classifies.
- Epic axis: none → `missing` + EP-MISSING(warning, "artifact carries no Epic trailer");
  >1 → `invalid` + EP-MULTIPLE(error, "artifact carries {n} Epic trailers ({v1, v2})");
  grammar fail → `invalid` + EP-GRAMMAR(error,
  "Epic {value!r} does not match <program>.<epic> (epics/v1)", raw=value);
  else `tagged`.
- Defect axis is orthogonal and never changes the epic classification:
  >1 → EP-DEFECT-MULTIPLE(error, "artifact carries {n} Defect trailers ({...})");
  grammar fail → EP-DEFECT-GRAMMAR(error,
  "Defect {value!r} does not match the defect slug grammar (epics/v1)", raw=value).
- `retrieved=False` → `unavailable` + EP-UNAVAILABLE(warning,
  "artifact body was not retrieved") — message byte-matches the KB fixture.
  `body=None` with `retrieved=True` is an EMPTY body (read, no tag → missing).
- Diagnostics sorted by `(code, subject_key, subject_uri, related_uri)`, null
  before any string (we produce only code+subject_uri, so code order suffices,
  but implement the comparator per contract).
- `artifact_uri` builds `gh://<repo-short-name>/<kind>/<n>`, validates against
  the vendored `ArtifactUri` pattern, returns None on mismatch (identity the
  grammar cannot express is null, not a lie).
- All grammars/enums compiled from the vendored schema `$defs` (`EpicId`,
  `DefectSlug`, `ArtifactUri`, `ClassificationState`), failing loudly if absent
  (dispatcher's `_compiled` idiom).

- [x] **Step 1: Write failing tests** — unit cases for every bullet above, plus
  KB conformance: replay `contract_epics/fixtures/valid/pr-body`,
  `valid/prose-mention-is-not-a-trailer`, `invalid/issue-unavailable` through
  `classify_body` with the fixture identities (`gh://demo/pull/7`,
  `gh://demo/issues/11`, observed `2026-08-25T00:00:00Z`) and compare
  `model_dump()` (dropping `raw is None` keys) against `expected.json`.
- [x] **Step 2: Run — FAIL (module missing).**
- [x] **Step 3: Implement `epics.py` per spec.**
- [x] **Step 4: Run — PASS; full suite green.**
- [x] **Step 5: Commit** — `feat: epics/v1 classifier over vendored grammar`.

---

### Task 3: `snapshot_v2.py` — v2 models + fetch + attribution window

**Files:**
- Create: `github_checker/snapshot_v2.py`
- Modify: `github_checker/github.py` — extract
  `async def copilot_reviews(name: str, numbers: Sequence[int], call) -> dict[int, CopilotReview]`
  out of `fetch_repo` (v1 behavior identical); v2 reuses it.
- Test: `tests/test_snapshot_v2.py`

**Interfaces:**
- Consumes: Task 2 API; `github.py`: `_gh_api`, `GhError`, `gh_ready`,
  `parse_branches`, `parse_ruleset_info`, `DEPENDABOT_LOGIN`, `copilot_reviews`;
  `localgit.local_status/remote_url`; `snapshot.discover/parse_github_remote`;
  models `Branch`, `CopilotReview`, `RulesetInfo`, `LocalStatus`, `RepoRef`.
- Produces:
  - `PullRequestV2` = v1 `PullRequest` fields + `epic: EpicClassification`
  - `IssueV2` = v1 `Issue` fields + `epic: EpicClassification`
  - `MergedPullRequest`: `number: int`, `merge_commit_sha: str | None`,
    `commit_shas: list[str]`, `commit_shas_truncated: bool`,
    `merged_at: datetime`, `epic: EpicClassification`
  - `MergedPrWindow`: `window_days: int`, `truncated: bool`,
    `prs: list[MergedPullRequest]`
  - `RepoStateV2`: v1 `RepoState` fields with `pulls: list[PullRequestV2]`,
    `issues: list[IssueV2] | None`, plus `merged: MergedPrWindow | None`
  - `RepoSnapshotV2`, `WorkspaceSnapshotV2` (`schema_version: Literal[2] = 2`)
  - `WINDOW_DAYS_DEFAULT = 30`, `PAGE_CAP = 100`
  - `async def build_snapshot_v2(root: Path, include_github: bool = True, window_days: int = WINDOW_DAYS_DEFAULT) -> WorkspaceSnapshotV2`

**Behavioral spec (pinned by tests):**
- Open planes: same calls as v1 (`pulls?state=open`, `issues?state=open`,
  branches, alerts, rulesets, copilot enrichment), but each PR/issue is
  classified from the listing payload's `body`; `retrieved = "body" in item`
  (a payload without the key is `unavailable`, a `null` body is an empty body).
- Attribution: `pulls?state=closed&sort=updated&direction=desc&per_page=100`;
  keep items with `merged_at != null` and `merged_at >= now - window_days`.
  `truncated = len(page) == PAGE_CAP and parse(page[-1]["updated_at"]) >= cutoff`
  (sound because `updated_at >= merged_at`: if the oldest-updated item seen is
  still inside the window, older merged-in-window PRs may exist unseen).
  Per PR: `pulls/{n}/commits?per_page=100` → `commit_shas`;
  `commit_shas_truncated = len == PAGE_CAP`. Merged PR bodies come from the
  closed listing and are classified the same way.
- One `observed_at` stamp per repo fetch (isoformat string of tz-aware now).
- Error isolation identical to v1: any `GhError`/exception → `RepoStateV2`
  with `error` set, no partial planes invented (`issues=None`, `merged=None`).
- `build_snapshot_v2` mirrors `build_snapshot` (discover → local/remote →
  gh gate → fetch) with `gh_error` semantics unchanged.
- Tests drive `fetch_repo_v2` through a fake `call(path)` (dict of canned
  payloads; no network), covering: tagged/missing/unavailable classification
  landing on the right artifacts, window filtering, both truncation flags in
  both states, merged PR with null `merge_commit_sha`, error isolation.

- [x] **Step 1: Write failing tests.**
- [x] **Step 2: Run — FAIL.**
- [x] **Step 3: Implement (`copilot_reviews` refactor first; v1 suite must stay green).**
- [x] **Step 4: Run — PASS; full suite green.**
- [x] **Step 5: Commit** — `feat: snapshot v2 models and fetch with attribution window`.

---

### Task 4: CLI wiring — `snapshot --schema-version {1,2} --window-days N`

**Files:**
- Modify: `github_checker/main.py` (`build_parser` snapshot subparser;
  `_run_snapshot`; dispatch in `main()`)
- Test: `tests/test_main.py` (extend)

**Interfaces:**
- Consumes: `build_snapshot_v2` (Task 3).
- Produces: `_run_snapshot(workspace, local_only, indent, schema_version, window_days)`.

**Behavioral spec:**
- `--schema-version`, `type=int`, `choices=(1, 2)`, `default=1` — v1 stays the
  default so current consumers are untouched until they migrate.
- `--window-days`, `type=int`, `default=None`. With `schema_version == 1` and a
  value given → stderr `"--window-days требует --schema-version 2"`, exit 1.
  With v2: value must be ≥ 1 (else stderr + exit 1); `None` → `WINDOW_DAYS_DEFAULT`.
- v2 path prints `build_snapshot_v2(...).model_dump_json(indent=...)`; v1 path
  byte-identical to today.
- Tests: monkeypatch `build_snapshot`/`build_snapshot_v2` to assert routing and
  arguments; refusal cases assert exit code + stderr.

- [x] Steps: failing tests → FAIL → implement → PASS → commit
  `feat: snapshot --schema-version 2 CLI`.

---

### Task 5: Freeze `contracts/snapshot/v2/` — README, schema, golden fixtures

**Files:**
- Create: `contracts/snapshot/v2/README.md`
- Create: `contracts/snapshot/v2/snapshot.schema.json` (= `WorkspaceSnapshotV2.model_json_schema()`)
- Create: `contracts/snapshot/v2/fixtures/snapshot_full.json`,
  `contracts/snapshot/v2/fixtures/snapshot_degraded.json`
- Test: `tests/test_snapshot_v2_contract.py`

**Behavioral spec:**
- README records, verbatim as contract text: the consumption boundary
  («dispatcher consumes only the open issues/PR planes; the merged-PR window is
  transport for robin, never state»), the classification layer boundary
  (`tagged` = present + grammar-valid; EP-UNKNOWN/EP-MOVED are downstream), the
  four-state semantics with `unavailable ≠ missing`, no raw bodies by design
  (volume/PII/prompt-injection), the epics/v1 pin linkage
  (version, source commit, `tree_sha256` from the vendored manifest), and v1
  coexistence (v1 remains published and default until consumers migrate).
- `snapshot_full.json` covers all four classification states (tagged PR,
  missing issue, invalid issue with EP-GRAMMAR + raw, unavailable PR with
  EP-UNAVAILABLE), a merged window with `truncated: true` in one repo and
  `false` in the other, one `commit_shas_truncated: true`, one
  `merge_commit_sha: null`.
- `snapshot_degraded.json`: `gh_error` set, all `github: null`.
- Contract test mirrors `test_snapshot_contract.py`: schema equality with the
  frozen file, fixture roundtrips, degraded is git-only, plus: the full fixture
  contains all four states, and every `unavailable` object has empty `epic`,
  an EP-UNAVAILABLE diagnostic, and is not `missing`.

- [x] Steps: failing tests → FAIL → generate schema + hand-write fixtures
  (validated by roundtrip through the models) → PASS → commit
  `feat: freeze contracts/snapshot/v2`.

---

### Task 6: Docs + plan bookkeeping

**Files:**
- Modify: `README.md` — snapshot section: the two new flags, what v2 adds, the
  v1-default migration note, pointer to `contracts/snapshot/v2/`.
- Modify: `TODO.md` — tick `@id:snapshot-v2-epics` with the PR number.

- [x] Steps: edit → `uv run pytest` (version strings in tests unaffected) →
  ruff format/check + pyrefly check → commit `docs: document snapshot v2`.

---

## Self-Review

- Issue #23 acceptance ↔ tasks: schema+fixtures incl. unavailable (T5),
  classification on Issue and PullRequest (T2+T3), attribution section with
  `window_days`/`truncated` (T3+T5), `unavailable` never folded into `missing`
  (T2 spec + T5 test), version bumped with v1 kept (T3 `Literal[2]` + default-1
  CLI in T4 + README note in T5/T6). Consumer-registry update in the KB is a
  cross-repo edit → handoff note, not a task here.
- Types consistent: `EpicClassification` produced in T2 is the type embedded in
  T3 models and serialized into T5 fixtures; `copilot_reviews` signature stated
  once and consumed in T3.
