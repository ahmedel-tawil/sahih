"""
The vocabulary of validation.

Three ideas, and everything else in sahih is built on them:

  Severity   how badly a rule was broken
  Finding    one rule that fired against one invoice
  Report     everything that fired, for one invoice, against one rule set

WHY THIS EXISTS AS ITS OWN MODULE
---------------------------------
Schematron reports results in an XML dialect called SVRL. SVRL is a fine wire
format and a terrible thing to program against — it is deeply nested, namespaced,
and speaks in XPath. Every consumer of this library would otherwise write the same
awkward XML-walking code.

So sahih translates SVRL into these plain Python objects exactly once, at the
boundary, and nothing downstream ever sees XML again. The explanation layer, the
CLI, and the MCP server all consume `Finding` objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    """
    How serious a fired rule is.

    This comes from the SVRL ``flag`` attribute, which the rule author sets.
    In Peppol and PINT rule sets the convention is:

      FATAL    the document is non-compliant and will be rejected
      WARNING  legal, but likely wrong or deprecated
      INFO     advisory only

    Practical consequence: an invoice is fit to send when it has zero FATAL
    findings. Warnings do not block, but ignoring them is usually how you end
    up with a rejection later, when a downstream party is stricter than the spec.
    """

    FATAL = "fatal"
    WARNING = "warning"
    INFO = "info"

    @classmethod
    def from_svrl(cls, flag: str | None) -> Severity:
        """
        Map an SVRL ``flag`` value onto this enum.

        Unknown or missing flags become FATAL deliberately. If a rule set uses a
        severity we do not recognise, treating it as blocking is the safe default —
        the alternative is silently passing an invoice we did not understand.
        """
        if not flag:
            return cls.FATAL
        try:
            return cls(flag.strip().lower())
        except ValueError:
            return cls.FATAL


@dataclass(frozen=True, slots=True)
class Finding:
    """
    One rule that fired against one invoice.

    THE FIELDS, AND WHY EACH MATTERS
    --------------------------------
    rule_id
        The stable identifier, e.g. ``BR-CO-15`` (EN 16931 arithmetic) or
        ``ibr-179-ae`` (a UAE jurisdiction rule). This is the primary key of the
        whole domain: it is what you look up, group by, and eventually explain.

    severity
        See `Severity`. Determines whether this blocks sending.

    message
        The rule author's own text. Accurate, and usually close to unreadable for
        anyone who is not a Peppol implementer — which is precisely the problem
        the explanation layer exists to solve.

    location
        An XPath pointing at the offending node in the invoice, e.g.
        ``/Invoice[1]/cac:AccountingCustomerParty[1]``. This is what lets a caller
        highlight the actual field rather than saying "something is wrong somewhere".

    test
        The XPath expression that evaluated false. This is the machine-readable
        statement of what was expected, and it is the raw material the explanation
        layer reasons over.

    ruleset
        Which layer produced this finding — EN 16931, PINT base, or the jurisdiction
        rules. Invoices are validated against stacked rule sets, and without this
        you cannot tell a European core violation from a UAE-specific one. They have
        very different fixes.
    """

    rule_id: str
    severity: Severity
    message: str
    location: str = ""
    test: str = ""
    ruleset: str = ""

    @property
    def is_blocking(self) -> bool:
        """True when this finding alone makes the invoice unfit to send."""
        return self.severity is Severity.FATAL

    def __str__(self) -> str:
        where = f" at {self.location}" if self.location else ""
        return f"[{self.severity.value}] {self.rule_id}: {self.message}{where}"


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """
    The result of validating one invoice against one rule set.

    ``fired_rules`` deserves a note, because it is the field people skip and then
    misread their own results. It counts how many rules were *evaluated*, not how
    many failed. Schematron rules are overwhelmingly conditional — most say
    "when X is present, then Y must hold". If the precondition never matches, the
    rule never fires and stays silent.

    So a clean report with a low ``fired_rules`` count is not reassurance. It often
    means the document was malformed enough that whole families of rules never
    applied. That distinction is invisible in every validator we surveyed, and it is
    a real source of false confidence for the person filing.
    """

    findings: tuple[Finding, ...] = field(default_factory=tuple)
    fired_rules: int = 0
    ruleset: str = ""
    source: str = ""

    @property
    def is_valid(self) -> bool:
        """True when nothing blocking fired. Warnings do not count against validity."""
        return not any(f.is_blocking for f in self.findings)

    @property
    def fatals(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.FATAL)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.WARNING)

    def __str__(self) -> str:
        verdict = "valid" if self.is_valid else "INVALID"
        return (
            f"{self.source or 'document'} — {verdict} against {self.ruleset or 'ruleset'} "
            f"({self.fired_rules} rules fired, "
            f"{len(self.fatals)} fatal, {len(self.warnings)} warning)"
        )


@dataclass(frozen=True, slots=True)
class StackedReport:
    """
    The result of validating one invoice against a *stack* of rule sets.

    WHY THIS IS NOT JUST A MERGED LIST OF FINDINGS
    ----------------------------------------------
    Real validation runs several layers — EN 16931, then PINT base, then the
    jurisdiction rules. We could flatten everything into one `ValidationReport`
    and rely on `Finding.ruleset` to keep attribution. That loses something
    important: the per-layer ``fired_rules`` count.

    Per §"conditional rules" in the design notes, the number of rules that
    *evaluated* in each layer is diagnostic. An invoice that fires 104 EN 16931
    rules but only 3 jurisdiction rules is telling you the jurisdiction layer
    barely engaged — which usually means a missing identifier upstream, not
    compliance. Flattening hides that.

    So this keeps each layer's report intact and offers aggregate views on top.
    """

    layers: tuple[ValidationReport, ...] = field(default_factory=tuple)
    source: str = ""

    @property
    def findings(self) -> tuple[Finding, ...]:
        """Every finding across all layers, in layer order."""
        return tuple(f for layer in self.layers for f in layer.findings)

    @property
    def fired_rules(self) -> int:
        """Total rules evaluated across all layers."""
        return sum(layer.fired_rules for layer in self.layers)

    @property
    def is_valid(self) -> bool:
        """True only when every layer is clean of blocking findings."""
        return all(layer.is_valid for layer in self.layers)

    @property
    def fatals(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.FATAL)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.WARNING)

    def layer(self, name: str) -> ValidationReport | None:
        """Fetch one layer's report by rule set name."""
        for layer in self.layers:
            if layer.ruleset == name:
                return layer
        return None

    def __str__(self) -> str:
        verdict = "valid" if self.is_valid else "INVALID"
        breakdown = ", ".join(
            f"{layer.ruleset}: {len(layer.fatals)}F/{layer.fired_rules}r" for layer in self.layers
        )
        return f"{self.source or 'document'} — {verdict} [{breakdown}]"
