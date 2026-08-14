"""
The rule catalogue — what the rule sets actually contain.

WHY THIS EXISTS
---------------
Two audiences, and they want the same data:

  a person   "what is this tool actually checking?"
  an agent   "give me the rules as structured data so I can reason over them"

Neither is served by a 240 KB compiled stylesheet. This reads the Schematron
**source** (`.sch`) and returns typed `Rule` objects.

SOURCE: .sch, NOT the compiled .xslt
------------------------------------
The compiled XSLT is what Saxon executes, but it is generated code — rule metadata
is scattered through `<xsl:choose>` branches and only reliably recovered by regex.
The `.sch` source carries everything as structured attributes:

    <rule context="cac:AccountingCustomerParty/cac:Party">
      <assert id="ibr-010-ae" flag="fatal" test="not(...) or ...">
        [ibr-010-ae]-Passport issuing country code (BTAE-19) MUST be there when ...
      </assert>
    </rule>

THE FIELD THAT MATTERS MOST IS `context`
----------------------------------------
`context` is the XPath the rule attaches to. It is the difference between a rule that
fires and a rule that goes silent — and silence is the failure mode validation cannot
report. A rule whose context is `cac:PartyTaxScheme` simply does not exist for a
document that has no such element, which is why a missing TRN validates clean.

With the catalogue you can answer "which rules COULD have applied to this document but
did not?" — something no validator we surveyed can do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from defusedxml import ElementTree as DefusedET

SCH_NS = "{http://purl.oclc.org/dsdl/schematron}"

#: Business-term codes referenced in rule text: IBT-048, BTAE-16, IBG-25, BT-112.
TERM = re.compile(r"\b((?:I?BT|BTAE|I?BG)-\d{2,3}(?:-\d)?)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Rule:
    """
    One assertion from a rule set.

    id
        The stable identifier — `ibr-002`, `ibr-101-ae`. This is what a finding
        reports, what an agent cites, and what `explanations.toml` keys on.

    text
        The rule's own message, as authored. Accurate, frequently opaque.

    context
        **The XPath node this rule attaches to.** If a document has no matching node
        the rule never runs, and reports nothing. See the module docstring.

    test
        The XPath expression that must hold. The precise, machine-readable statement
        of the requirement — and the most useful field for an agent, because it says
        exactly what was expected rather than describing it.

    flag
        Severity as the author set it: `fatal`, `warning`, `info`.

    kind
        `assert` fires when its test is FALSE. `report` fires when TRUE. Both are
        findings; conflating them silently loses violations.

    terms
        Business-term codes mentioned in the text, e.g. `IBT-048`. Useful for
        grouping rules by the invoice field they govern.

    ruleset
        Which layer this came from.
    """

    id: str
    text: str
    context: str
    test: str
    flag: str = "fatal"
    kind: str = "assert"
    terms: tuple[str, ...] = ()
    ruleset: str = ""
    pattern: str = ""

    @property
    def is_conditional(self) -> bool:
        """
        Does this rule only apply in certain circumstances?

        Conditional rules are the silence-prone ones: their preconditions can simply
        fail to match, in which case they pass without ever being evaluated. They are
        also the rules most worth explaining, because "why did this not fire" is
        harder to answer than "why did this fail".
        """
        lowered = self.text.lower()
        return any(w in lowered for w in (" when ", " if ", "unless", "except"))

    @property
    def is_self_evident(self) -> bool:
        """
        True when the rule's own text already tells a reader what to do.

        Roughly 55% of rules are like this — "MUST be present." needs no gloss, and
        writing one is duplication rather than explanation. Used to target curation
        at the rules that actually need it.
        """
        if self.is_conditional:
            return False
        if len(self.text) > 170 or "=" in self.text or "sum of" in self.text.lower():
            return False
        return len(self.terms) < 3

    def __str__(self) -> str:
        return f"{self.id} [{self.flag}] {self.text[:70]}"


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())


def parse_schematron(path: Path, *, ruleset: str = "") -> list[Rule]:
    """
    Read a `.sch` file into `Rule` objects.

    Raises:
        FileNotFoundError: if the source is not present. Sources are optional —
            `catalogue()` degrades to an empty list rather than failing a caller who
            only wants validation.
    """
    root = DefusedET.fromstring(path.read_bytes())
    rules: list[Rule] = []

    for pattern in root.iter(f"{SCH_NS}pattern"):
        pattern_id = pattern.get("id", "")
        for rule in pattern.iter(f"{SCH_NS}rule"):
            context = rule.get("context", "")
            for kind in ("assert", "report"):
                for node in rule.findall(f"{SCH_NS}{kind}"):
                    text = _clean("".join(node.itertext()))
                    # Rule text is authored as "[id]-message"; strip the redundant
                    # prefix so the message reads as a sentence.
                    rid = node.get("id") or ""
                    body = re.sub(rf"^\[{re.escape(rid)}\]\s*-?\s*", "", text) if rid else text
                    rules.append(
                        Rule(
                            id=rid or "(unidentified)",
                            text=body or text,
                            context=context,
                            test=_clean(node.get("test")),
                            flag=(node.get("flag") or "fatal").lower(),
                            kind=kind,
                            terms=tuple(sorted({t.upper() for t in TERM.findall(text)})),
                            ruleset=ruleset,
                            pattern=pattern_id,
                        )
                    )
    return rules


#: Where `scripts/fetch_rulesets.py` puts things.
DEFAULT_RULES_DIR = Path("rulesets")

_LAYERS = (
    ("PINT base", "PINT-UBL-validation-preprocessed.sch"),
    ("PINT-AE", "PINT-jurisdiction-aligned-rules.sch"),
)


@lru_cache(maxsize=8)
def _cached(rules_dir: str, version: str) -> tuple[Rule, ...]:
    base = Path(rules_dir) / "pint-ae" / version
    out: list[Rule] = []
    for label, filename in _LAYERS:
        path = base / filename
        if path.is_file():
            out.extend(parse_schematron(path, ruleset=f"{label} {version}".strip()))
    return tuple(out)


def catalogue(
    rules_dir: Path | str = DEFAULT_RULES_DIR,
    *,
    version: str = "2026.5",
) -> tuple[Rule, ...]:
    """
    Every rule sahih validates against, as structured data.

    Returns an empty tuple when the `.sch` sources are not present — they are fetched
    separately from the compiled stylesheets, and validation works without them.

        from sahih import catalogue
        rules = catalogue()
        conditional = [r for r in rules if r.is_conditional]
    """
    return _cached(str(rules_dir), version)


def by_id(rules_dir: Path | str = DEFAULT_RULES_DIR, *, version: str = "2026.5") -> dict[str, Rule]:
    """The catalogue keyed by rule id. Later layers win on collision."""
    return {rule.id: rule for rule in catalogue(rules_dir, version=version)}


@dataclass(frozen=True, slots=True)
class CatalogueStats:
    """A summary of what the rule sets contain."""

    total: int
    by_ruleset: dict[str, int] = field(default_factory=dict)
    by_flag: dict[str, int] = field(default_factory=dict)
    conditional: int = 0
    self_evident: int = 0
    distinct_contexts: int = 0


def stats(rules_dir: Path | str = DEFAULT_RULES_DIR, *, version: str = "2026.5") -> CatalogueStats:
    """Summarise the catalogue — useful for a dashboard or a sanity check."""
    rules = catalogue(rules_dir, version=version)
    by_ruleset: dict[str, int] = {}
    by_flag: dict[str, int] = {}
    for rule in rules:
        by_ruleset[rule.ruleset] = by_ruleset.get(rule.ruleset, 0) + 1
        by_flag[rule.flag] = by_flag.get(rule.flag, 0) + 1
    return CatalogueStats(
        total=len(rules),
        by_ruleset=by_ruleset,
        by_flag=by_flag,
        conditional=sum(1 for r in rules if r.is_conditional),
        self_evident=sum(1 for r in rules if r.is_self_evident),
        distinct_contexts=len({r.context for r in rules}),
    )
