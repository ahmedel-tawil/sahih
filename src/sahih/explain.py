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

DATA_DIR = Path(__file__).parent / "data"
DATA_FILE = DATA_DIR / "explanations.toml"

#: The language every entry is written in, and the fallback for all others.
BASE_LANGUAGE = "en"


class ExplanationSource(StrEnum):
    """
    Where an explanation came from — callers may want to show these differently.

    The three-step fallback exists because coverage is uneven in two dimensions at
    once: which rules are curated, and which languages they are curated in.
    """

    #: Hand-written guidance, in the language that was asked for.
    CURATED = "curated"
    #: Curated, but only in the base language. Better than nothing; say so in the UI.
    CURATED_FALLBACK = "curated-fallback"
    #: The rule set's own message. Accurate, unfriendly, and ENGLISH ONLY —
    #: OpenPeppol publishes no translated rule text.
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
    #: The language this text is actually in — not necessarily the one requested.
    #: A caller rendering Arabic needs to know when a string came back English, so it
    #: can set `dir="ltr"` on that one and not mangle the layout.
    language: str = BASE_LANGUAGE

    @property
    def is_curated(self) -> bool:
        return self.source in (ExplanationSource.CURATED, ExplanationSource.CURATED_FALLBACK)

    @property
    def is_translated(self) -> bool:
        """True when the text is in the requested language rather than a fallback."""
        return self.source is ExplanationSource.CURATED

    def __str__(self) -> str:
        parts = [f"{self.rule_id} — {self.summary}"]
        if self.is_curated:
            parts.append(f"  Why: {self.why}")
            parts.append(f"  Fix: {self.fix}")
        return "\n".join(parts)


@lru_cache(maxsize=8)
def _load_curated(language: str = BASE_LANGUAGE) -> dict[str, dict[str, object]]:
    """
    Load and cache one language's knowledge base.

    The base language is required — its absence is a packaging bug and raises, because
    a silently empty knowledge base looks exactly like "no rules are curated" and is
    very hard to notice. Other languages are optional and return {} when absent.
    """
    path = DATA_FILE if language == BASE_LANGUAGE else DATA_DIR / f"explanations.{language}.toml"
    if not path.is_file():
        if language == BASE_LANGUAGE:
            raise FileNotFoundError(
                f"Curated explanations missing at {path}. This indicates a packaging "
                "problem — the data file should ship inside the wheel."
            )
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def available_languages() -> tuple[str, ...]:
    """Languages with a curated file present, base language first."""
    others = sorted(
        p.stem.split(".", 1)[1]
        for p in DATA_DIR.glob("explanations.*.toml")
        if p.name != DATA_FILE.name
    )
    return (BASE_LANGUAGE, *others)


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

    def __init__(
        self,
        curated: dict[str, dict[str, object]] | None = None,
        *,
        language: str = BASE_LANGUAGE,
    ) -> None:
        #: Injectable so tests can supply their own knowledge base.
        self.language = language
        self._base = curated if curated is not None else _load_curated()
        self._translated = (
            {} if curated is not None or language == BASE_LANGUAGE else _load_curated(language)
        )
        #: Union of ids known in ANY language — what `is_curated` answers against.
        self._curated = {**self._base, **self._translated}

    def explain(self, finding: Finding) -> Explanation:
        """
        Explain a single finding.

        Three-step fallback, and `source` says which step was taken:

            CURATED           we have it in the language you asked for
            CURATED_FALLBACK  curated, but only in the base language
            OFFICIAL          the rule's own message (always English)

        Always returns an `Explanation` — a caller never has to handle a missing one.
        """
        translated = self._translated.get(finding.rule_id)
        base = self._base.get(finding.rule_id)
        entry = translated or base

        if entry is None:
            return Explanation(
                rule_id=finding.rule_id,
                summary=finding.message or "This rule fired, but reported no message.",
                why="",
                fix="",
                source=ExplanationSource.OFFICIAL,
                finding=finding,
                language=BASE_LANGUAGE,  # rule text is English; never claim otherwise
            )

        # Terms are language-independent and live once, in the base file.
        terms = (base or entry).get("terms", [])
        return Explanation(
            rule_id=finding.rule_id,
            summary=_normalise(entry.get("summary", "")),
            why=_normalise(entry.get("why", "")),
            fix=_normalise(entry.get("fix", "")),
            source=(
                ExplanationSource.CURATED
                if translated or self.language == BASE_LANGUAGE
                else ExplanationSource.CURATED_FALLBACK
            ),
            terms=tuple(str(t) for t in terms) if isinstance(terms, list) else (),
            finding=finding,
            language=self.language if translated else BASE_LANGUAGE,
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

    def translated_count(self) -> int:
        """How many entries exist in the requested language."""
        return len(self._translated)

    def entries(self) -> tuple[Explanation, ...]:
        """
        Every curated explanation, as data.

        For listing what guidance exists — in a UI, in docs, or to an agent deciding
        whether it needs to fall back to the rule's own wording. Each entry has no
        attached `finding`, since these are the knowledge base rather than results.
        """
        out = []
        for rule_id in sorted(self._curated):
            translated = self._translated.get(rule_id)
            base = self._base.get(rule_id)
            entry = translated or base or {}
            terms = (base or entry).get("terms", [])
            out.append(
                Explanation(
                    rule_id=rule_id,
                    summary=_normalise(entry.get("summary", "")),
                    why=_normalise(entry.get("why", "")),
                    fix=_normalise(entry.get("fix", "")),
                    source=(
                        ExplanationSource.CURATED
                        if translated or self.language == BASE_LANGUAGE
                        else ExplanationSource.CURATED_FALLBACK
                    ),
                    terms=tuple(str(t) for t in terms) if isinstance(terms, list) else (),
                    language=self.language if translated else BASE_LANGUAGE,
                )
            )
        return tuple(out)

    def is_curated(self, rule_id: str) -> bool:
        return rule_id in self._curated

    def __repr__(self) -> str:
        return (
            f"Explainer(language={self.language!r}, {self.coverage()} curated, "
            f"{self.translated_count()} translated)"
        )
