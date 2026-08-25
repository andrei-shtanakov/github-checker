"""Epic/defect classification of GitHub artifact bodies (epics/v1).

The grammar is NOT restated here. It is compiled at import time from the
vendored `contract_epics/classification.schema.json`, so "the snapshot
producer delegates epic grammar to epics/v1" is executable rather than
documented: editing the pinned copy is the only way to change what this
module accepts (the same idiom as dispatcher's plan-fields).

Layer boundary (ADR-ECO-010): this producer proves presence and grammar and
nothing more. ``tagged`` here means *a well-formed epic is present*; registry
membership (EP-UNKNOWN / EP-MOVED) is the consumers' layer — the value
registry (`epics.toml` in the umbrella) changes weekly, is read live by the
fleet sensor, and is never read here.
"""

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

_CONTRACT_DIR = Path(__file__).parent / "contract_epics"
_CLASSIFICATION_SCHEMA = _CONTRACT_DIR / "classification.schema.json"

Classification = Literal["tagged", "missing", "invalid", "unavailable"]
Carrier = Literal["pull_request", "issue"]
Severity = Literal["warning", "error"]


def _compiled(defs: dict[str, Any], name: str) -> re.Pattern[str]:
    """Compile one grammar from the pinned contract, failing loudly if it moved.

    A missing `$def` means the vendored copy is not the contract this code was
    written against. Falling back to a hardcoded pattern would silently
    recreate the second copy of the regex that the delegation exists to
    prevent, so this raises instead.
    """
    try:
        pattern = defs[name]["pattern"]
    except KeyError as exc:  # pragma: no cover - guards a broken vendored copy
        raise RuntimeError(
            f"vendored epics contract has no ${{defs}}/{name}.pattern "
            f"({_CLASSIFICATION_SCHEMA}); re-vendor from the pin"
        ) from exc
    return re.compile(pattern)


_DEFS = json.loads(_CLASSIFICATION_SCHEMA.read_text(encoding="utf-8"))["$defs"]
EPIC_RE = _compiled(_DEFS, "EpicId")
DEFECT_RE = _compiled(_DEFS, "DefectSlug")
ARTIFACT_URI_RE = _compiled(_DEFS, "ArtifactUri")

EPIC_KEY = "Epic"
DEFECT_KEY = "Defect"

# One `Key: value` per line, no blank line inside the block (vendored README,
# «Carriers»): a line that is not of this shape disqualifies the whole final
# paragraph from being a trailer block.
_TRAILER_LINE_RE = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9-]*): (?P<value>.+)$")


class EpicDiagnostic(BaseModel):
    """One instance of a diagnostics.yaml code, with its COMPUTED severity."""

    code: str
    severity: Severity
    message: str
    subject_uri: str | None = None
    raw: str | None = None


class EpicClassification(BaseModel):
    """The normalized per-artifact object of classification.schema.json."""

    epic: str | None
    defect: str | None
    classification: Classification
    diagnostics: list[EpicDiagnostic]
    subject_uri: str | None
    carrier: Carrier
    observed_at: str | None


def artifact_uri(repo: str, kind: Literal["pull", "issues"], number: int) -> str | None:
    """`gh://<short-name>/<kind>/<n>`, or None when outside the URI grammar.

    The contract's ArtifactUri uses the short repo name (the canonical name =
    the `git clone` directory), not `owner/repo`. A name the grammar cannot
    express (dots, uppercase) yields null — «no addressable identity», never
    a lowercased or mangled lie.
    """
    uri = f"gh://{repo.rsplit('/', 1)[-1]}/{kind}/{number}"
    return uri if ARTIFACT_URI_RE.fullmatch(uri) else None


def final_trailer_block(body: str) -> list[tuple[str, str]]:
    """(key, value) pairs of the final trailer block; empty when there is none.

    Only the last non-blank paragraph can classify (vendored README: an
    `Epic:` line mid-prose is prose). Values are whitespace-stripped — the
    Co-Authored-By convention — but never case-folded; keys are matched
    case-sensitively by the caller.
    """
    paragraphs = [
        p for p in re.split(r"\n\s*\n", body.replace("\r\n", "\n")) if p.strip()
    ]
    if not paragraphs:
        return []
    pairs: list[tuple[str, str]] = []
    for line in paragraphs[-1].splitlines():
        match = _TRAILER_LINE_RE.match(line)
        if match is None:
            return []
        pairs.append((match["key"], match["value"].strip()))
    return pairs


def _diagnostic_sort_key(diag: EpicDiagnostic) -> tuple[Any, ...]:
    """Contract ordering: (code, subject_key, subject_uri, related_uri),
    null before any string. We never emit subject_key/related_uri, but the
    comparator follows the contract so additions cannot reorder silently."""

    def null_first(value: str | None) -> tuple[int, str]:
        return (0, "") if value is None else (1, value)

    return (diag.code, null_first(None), null_first(diag.subject_uri), null_first(None))


def classify_body(
    body: str | None,
    *,
    retrieved: bool,
    subject_uri: str | None,
    carrier: Carrier,
    observed_at: str | None,
) -> EpicClassification:
    """Normalize one artifact body into the epics/v1 classification object.

    `retrieved=False` means the body was never read — `unavailable`, which is
    never counted as `missing`. `body=None` with `retrieved=True` is a body
    GitHub reports as empty: read, and carrying nothing.
    """
    diagnostics: list[EpicDiagnostic] = []

    def diag(
        code: str, severity: Severity, message: str, raw: str | None = None
    ) -> None:
        diagnostics.append(
            EpicDiagnostic(
                code=code,
                severity=severity,
                message=message,
                subject_uri=subject_uri,
                raw=raw,
            )
        )

    def result(
        epic: str | None, defect: str | None, classification: Classification
    ) -> EpicClassification:
        return EpicClassification(
            epic=epic,
            defect=defect,
            classification=classification,
            diagnostics=sorted(diagnostics, key=_diagnostic_sort_key),
            subject_uri=subject_uri,
            carrier=carrier,
            observed_at=observed_at,
        )

    if not retrieved:
        # Message pinned by the KB fixture `invalid/issue-unavailable`.
        diag("EP-UNAVAILABLE", "warning", "artifact body was not retrieved")
        return result(None, None, "unavailable")

    trailers = final_trailer_block(body or "")
    epics = tuple(value for key, value in trailers if key == EPIC_KEY)
    defects = tuple(value for key, value in trailers if key == DEFECT_KEY)

    # The defect axis fails on its own and never changes the epic
    # classification: «which stream» and «what broke» are different questions.
    defect: str | None = None
    if len(defects) > 1:
        diag(
            "EP-DEFECT-MULTIPLE",
            "error",
            f"artifact carries {len(defects)} Defect trailers ({', '.join(defects)})",
        )
    elif defects and not DEFECT_RE.fullmatch(defects[0]):
        diag(
            "EP-DEFECT-GRAMMAR",
            "error",
            f"Defect {defects[0]!r} does not match the defect slug grammar (epics/v1)",
            raw=defects[0],
        )
    elif defects:
        defect = defects[0]

    if not epics:
        # Warning, not error: escalation by `missing_error_after` is computed
        # against the live registry, which is the fleet layer's to read.
        diag("EP-MISSING", "warning", "artifact carries no Epic trailer")
        return result(None, defect, "missing")
    if len(epics) > 1:
        diag(
            "EP-MULTIPLE",
            "error",
            f"artifact carries {len(epics)} Epic trailers ({', '.join(epics)})",
        )
        return result(None, defect, "invalid")
    if not EPIC_RE.fullmatch(epics[0]):
        diag(
            "EP-GRAMMAR",
            "error",
            f"Epic {epics[0]!r} does not match <program>.<epic> (epics/v1)",
            raw=epics[0],
        )
        return result(None, defect, "invalid")
    return result(epics[0], defect, "tagged")
