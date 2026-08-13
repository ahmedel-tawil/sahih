"""
Engine tests — Saxon actually runs here.

These use a miniature rule set (`fixtures/mini-rules.xslt`) rather than the real
876 KB artefacts, so the suite stays fast. Tests against the genuine EN 16931 and
PINT AE rule sets live in `test_integration.py` and are opt-in.
"""

from __future__ import annotations

import pytest

from sahih import (
    RuleSet,
    RuleSetError,
    Severity,
    UnsafeDocumentError,
    ValidationError,
    Validator,
)

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"
MINI_RULES = FIXTURES / "mini-rules.xslt"


@pytest.fixture
def validator():
    """A Validator over the mini rule set, closed after each test."""
    v = Validator([RuleSet("mini", MINI_RULES)])
    yield v
    v.close()


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_clean_invoice_is_valid(validator):
    report = validator.validate(FIXTURES / "good-invoice.xml")

    assert report.is_valid
    assert report.findings == ()
    assert report.fired_rules == 1
    assert report.source == "good-invoice.xml"


def test_failed_assert_is_caught(validator):
    report = validator.validate(FIXTURES / "no-id-invoice.xml")

    assert not report.is_valid
    assert len(report.fatals) == 1

    finding = report.fatals[0]
    assert finding.rule_id == "TEST-01"
    assert finding.severity is Severity.FATAL
    assert finding.location == "/Invoice[1]"
    # The rule set name must be stamped on, or stacked reports lose attribution.
    assert finding.ruleset == "mini"


def test_successful_report_is_caught_as_warning(validator):
    """A <report> that fires is a finding too — and a warning does not block."""
    report = validator.validate(FIXTURES / "noted-invoice.xml")

    assert report.is_valid  # warnings do not invalidate
    assert len(report.warnings) == 1
    assert report.warnings[0].rule_id == "TEST-02"


# --------------------------------------------------------------------------
# Stacked rule sets — the real shape of validation
# --------------------------------------------------------------------------


def test_layers_are_kept_separate_and_ordered():
    with Validator([RuleSet("layer-a", MINI_RULES), RuleSet("layer-b", MINI_RULES)]) as v:
        report = v.validate(FIXTURES / "no-id-invoice.xml")

        assert [layer.ruleset for layer in report.layers] == ["layer-a", "layer-b"]
        # Same rule set twice, so each layer finds the same violation.
        assert len(report.fatals) == 2
        assert report.fired_rules == 2  # summed across layers

        # Per-layer counts survive aggregation — this is why StackedReport exists.
        assert report.layer("layer-a").fired_rules == 1
        assert report.layer("layer-b") is not None
        assert report.layer("nope") is None


def test_all_layers_run_even_when_an_early_one_fails():
    """
    A caller fixing an invoice wants the complete picture, not one error at a time.
    """
    with Validator([RuleSet("first", MINI_RULES), RuleSet("second", MINI_RULES)]) as v:
        report = v.validate(FIXTURES / "no-id-invoice.xml")

    assert len(report.layers) == 2
    assert all(len(layer.fatals) == 1 for layer in report.layers)


def test_duplicate_ruleset_names_are_rejected():
    """Findings are attributed by name, so duplicates would make reports ambiguous."""
    with pytest.raises(RuleSetError, match="unique"):
        Validator([RuleSet("same", MINI_RULES), RuleSet("same", MINI_RULES)])


def test_validator_needs_at_least_one_ruleset():
    with pytest.raises(RuleSetError, match="at least one"):
        Validator([])


# --------------------------------------------------------------------------
# Security boundary
# --------------------------------------------------------------------------


def test_xxe_document_is_refused_before_saxon_sees_it(validator):
    """
    Invoices arrive from counterparties. A document declaring an external entity is
    rejected outright — a legitimate UBL invoice never needs a DTD.
    """
    with pytest.raises(UnsafeDocumentError, match="DTD or entity"):
        validator.validate(FIXTURES / "xxe-invoice.xml")


# --------------------------------------------------------------------------
# Failure modes that are setup problems, not validation outcomes
# --------------------------------------------------------------------------


def test_missing_invoice_raises_validation_error(validator):
    with pytest.raises(ValidationError, match="No such file"):
        validator.validate(FIXTURES / "does-not-exist.xml")


def test_missing_ruleset_file_raises_ruleset_error():
    with (
        Validator([RuleSet("ghost", FIXTURES / "nope.xslt")]) as v,
        pytest.raises(RuleSetError, match="no such file"),
    ):
        v.validate(FIXTURES / "good-invoice.xml")


def test_using_a_closed_validator_raises():
    v = Validator([RuleSet("mini", MINI_RULES)])
    v.close()
    with pytest.raises(ValidationError, match="closed"):
        v.validate(FIXTURES / "good-invoice.xml")


def test_close_is_idempotent():
    v = Validator([RuleSet("mini", MINI_RULES)])
    v.close()
    v.close()  # must not raise


# --------------------------------------------------------------------------
# Compile-once-validate-many — the performance premise of the whole module
# --------------------------------------------------------------------------


def test_ruleset_compiles_once_and_is_reused(validator):
    ruleset = validator.rulesets[0]
    assert not ruleset.is_compiled

    validator.validate(FIXTURES / "good-invoice.xml")
    assert ruleset.is_compiled
    executable = ruleset._executable

    validator.validate(FIXTURES / "no-id-invoice.xml")
    # Same compiled object — a recompile per invoice would be ~100x slower.
    assert ruleset._executable is executable


def test_warm_up_compiles_ahead_of_first_use(validator):
    assert not validator.rulesets[0].is_compiled
    validator.warm_up()
    assert validator.rulesets[0].is_compiled


def test_from_paths_constructor():
    with Validator.from_paths({"mini": MINI_RULES}) as v:
        assert [r.name for r in v.rulesets] == ["mini"]
        assert v.validate(FIXTURES / "good-invoice.xml").is_valid
