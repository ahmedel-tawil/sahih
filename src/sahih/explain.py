"""
The explanation layer — turning findings into things a person can act on.

THE PROBLEM
-----------
An official finding looks like this:

    BR-CO-15: Invoice total amount with VAT (BT-112) = Invoice total amount
    without VAT (BT-109) + Invoice total VAT amount (BT-110).

That is precise, correct, and useless to the person who has to fix it. It states the
rule that was broken, not what to do about it. Every validator we surveyed stops here.

WHAT THIS MODULE DOES
---------------------
Maps a rule identifier onto three things a finance person can use — what went wrong,
why the rule exists, and what to change. The knowledge lives in a data file
(``data/explanations.toml``), not in code, so it can be extended by someone who knows
UAE VAT without knowing Python.

DELIBERATELY DETERMINISTIC
--------------------------
No model, no API key, no network. A curated mapping is testable, reproducible, works
offline, and cannot hallucinate a fix that would put someone's filing wrong. The
tradeoff is coverage: 311 rules exist across the layers, and only the curated ones get
the full treatment. Everything else degrades to the official message, which is exactly
what other tools give you — so the floor is "no worse than the state of the art".
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from .models import Finding, StackedReport, ValidationReport

DATA_FILE = Path(__file__).parent / "data" / "explanations.toml"


class ExplanationSource(StrEnum):
    """Where an explanation came from — callers may want to show this differently."""

    #: Hand-written guidance from data/explanations.toml.
    CURATED = "curated"
    #: Fell back to the rule set's own message. Accurate, but not friendly.
    OFFICIAL = "official"


@dataclass(frozen=True, slots=True)
class Explanation:
    """
    An actionable account of one finding.

    ``summary`` / ``why`` / ``fix`` answer the three questions someone actually has,
    in the order they ask them: what broke, why does that matter, what do I change.

    ``terms`` lists the business-term codes involved (``BT-112``, ``IBT-048``,
    ``BTAE-16``). Those are the stable, language-independent handles for invoice
    fields — useful for linking straight to the offending field in a UI.
    """

    rule_id: str
    summary: str
    why: str
    fix: str
    source: ExplanationSource
    terms: tuple[str, ...] = ()
    finding: Finding | None = None

    @property
    def is_curated(self) -> bool:
        return self.source is ExplanationSource.CURATED

    def __str__(self) -> str:
        parts = [f"{self.rule_id} — {self.summary}"]
        if self.is_curated:
            parts.append(f"  Why: {self.why}")
            parts.append(f"  Fix: {self.fix}")
        return "\n".join(parts)


@lru_cache(maxsize=1)
def _load_curated() -> dict[str, dict[str, object]]:
    """
    Load and cache the curated knowledge base.

    Cached because it is read-only and parsed once per process. Failing to load is a
    packaging bug rather than a runtime condition, so it raises rather than degrading
    silently — a silently empty knowledge base would look like "no rules are curated"
    and be very hard to notice.
    """
    if not DATA_FILE.is_file():
        raise FileNotFoundError(
            f"Curated explanations missing at {DATA_FILE}. This indicates a packaging "
            "problem — the data file should ship inside the wheel."
        )
    with DATA_FILE.open("rb") as handle:
        return tomllib.load(handle)


def _normalise(text: object) -> str:
    """TOML multi-line strings carry newlines from source formatting; collapse them."""
    return " ".join(str(text).split())


class Explainer:
    """
    Turns findings into explanations.

        explainer = Explainer()
        for explanation in explainer.explain_report(report):
            print(explanation)

    Stateless and cheap to construct — the knowledge base is loaded once per process
    and shared.
    """

    def __init__(self, curated: dict[str, dict[str, object]] | None = None) -> None:
        #: Injectable so tests can supply their own knowledge base.
        self._curated = curated if curated is not None else _load_curated()

    def explain(self, finding: Finding) -> Explanation:
        """
        Explain a single finding.

        Always returns an `Explanation`. When a rule is not curated, the official
        message becomes the summary and `source` is OFFICIAL — callers can surface
        that difference, but they never have to handle a missing result.
        """
        entry = self._curated.get(finding.rule_id)

        if entry is None:
            return Explanation(
                rule_id=finding.rule_id,
                summary=finding.message or "This rule fired, but reported no message.",
                why="",
                fix="",
                source=ExplanationSource.OFFICIAL,
                finding=finding,
            )

        terms = entry.get("terms", [])
        return Explanation(
            rule_id=finding.rule_id,
            summary=_normalise(entry.get("summary", "")),
            why=_normalise(entry.get("why", "")),
            fix=_normalise(entry.get("fix", "")),
            source=ExplanationSource.CURATED,
            terms=tuple(str(t) for t in terms) if isinstance(terms, list) else (),
            finding=finding,
        )

    def explain_report(
        self,
        report: ValidationReport | StackedReport,
        *,
        blocking_only: bool = False,
    ) -> tuple[Explanation, ...]:
        """
        Explain every finding in a report, in report order.

        Args:
            blocking_only: Only explain findings that make the invoice unfit to send.
                Useful for a "what do I have to fix right now" view, as opposed to a
                full audit.
        """
        findings = report.fatals if blocking_only else report.findings
        return tuple(self.explain(f) for f in findings)

    def coverage(self) -> int:
        """How many rules currently have curated explanations."""
        return len(self._curated)

    def is_curated(self, rule_id: str) -> bool:
        return rule_id in self._curated

    def __repr__(self) -> str:
        return f"Explainer({self.coverage()} curated rules)"
