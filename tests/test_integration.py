"""
Integration tests against the real, official rule sets.

These are the tests that prove sahih works on the actual thing rather than on a
toy stylesheet. They need artefacts on disk:

    uv run python scripts/fetch_rulesets.py

Without them the whole module skips, so a fresh clone still gets a green suite.
Marked ``integration`` — run just these with ``uv run pytest -m integration``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sahih import RuleSet, Severity, Validator

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parent.parent
RULESETS = ROOT / "rulesets"
PINT_VERSION = "2026.5"

PINT_BASE = RULESETS / "pint-ae" / PINT_VERSION / "PINT-UBL-validation-preprocessed.xslt"
PINT_AE = RULESETS / "pint-ae" / PINT_VERSION / "PINT-jurisdiction-aligned-rules.xslt"
SAMPLES = RULESETS / "samples" / "pint-ae" / PINT_VERSION

requires_artefacts = pytest.mark.skipif(
    not (PINT_BASE.is_file() and PINT_AE.is_file() and SAMPLES.is_dir()),
    reason="Run: uv run python scripts/fetch_rulesets.py",
)


@pytest.fixture(scope="module")
def pint_validator():
    """
    Module-scoped on purpose: compiling both PINT layers costs ~300 ms, and the
    whole design premise is compile-once-validate-many. Rebuilding per test would
    contradict the thing we are testing.
    """
    validator = Validator(
        [
            RuleSet("PINT base", PINT_BASE),
            RuleSet(f"PINT-AE {PINT_VERSION}", PINT_AE),
        ]
    )
    validator.warm_up()
    yield validator
    validator.close()


@requires_artefacts
@pytest.mark.parametrize(
    "sample",
    [
        "Standard tax invoice.xml",
        "Exports.xml",
        "Zero rated supplies.xml",
        "Supply under Reverse charge mechanism.xml",
        "Supply involving free trade zone.xml",
    ],
)
def test_official_uae_invoices_are_valid(pint_validator, sample):
    """
    Every invoice in the official UAE conformance corpus must validate clean
    against both PINT layers. A failure here means our stack is wrong, not theirs.
    """
    path = SAMPLES / sample
    if not path.is_file():
        pytest.skip(f"{sample} not fetched")

    report = pint_validator.validate(path)

    assert report.is_valid, f"{sample} reported: {[str(f) for f in report.fatals]}"
    # Both layers must actually engage — a layer firing zero rules would mean the
    # document never matched its contexts, which is silence, not success.
    for layer in report.layers:
        assert layer.fired_rules > 0, f"{layer.ruleset} evaluated nothing for {sample}"


@requires_artefacts
def test_duplicate_buyer_vat_trips_a_uae_rule(pint_validator, tmp_path):
    """
    The jurisdiction layer must actually fire, not merely load.

    ibr-179-ae: 'Buyer VAT identifier (IBT-048) MUST occur maximum once'. Duplicating
    the buyer's PartyTaxScheme block is the minimal way to violate it.
    """
    import re

    source = SAMPLES / "Standard tax invoice.xml"
    if not source.is_file():
        pytest.skip("Standard tax invoice.xml not fetched")

    xml = source.read_text(encoding="utf-8")
    match = re.search(
        r"(<cac:AccountingCustomerParty>.*?)(<cac:PartyTaxScheme>.*?</cac:PartyTaxScheme>)",
        xml,
        re.S,
    )
    assert match, "fixture shape changed — could not locate buyer PartyTaxScheme"

    mutated = tmp_path / "duplicated-buyer-vat.xml"
    mutated.write_text(xml.replace(match.group(0), match.group(0) + match.group(2), 1), "utf-8")

    report = pint_validator.validate(mutated)

    assert not report.is_valid
    rule_ids = {f.rule_id for f in report.fatals}
    assert "ibr-179-ae" in rule_ids, f"expected ibr-179-ae, got {sorted(rule_ids)}"

    # And the finding must be attributed to the jurisdiction layer, not the base.
    offender = next(f for f in report.fatals if f.rule_id == "ibr-179-ae")
    assert offender.ruleset == f"PINT-AE {PINT_VERSION}"
    assert offender.severity is Severity.FATAL


@requires_artefacts
def test_layers_report_independent_rule_counts(pint_validator):
    """
    The base layer evaluates far more rules than the jurisdiction layer. Preserving
    that asymmetry is the entire reason StackedReport keeps layers separate.
    """
    path = SAMPLES / "Standard tax invoice.xml"
    if not path.is_file():
        pytest.skip("Standard tax invoice.xml not fetched")

    report = pint_validator.validate(path)
    base = report.layer("PINT base")
    jurisdiction = report.layer(f"PINT-AE {PINT_VERSION}")

    assert base is not None and jurisdiction is not None
    assert base.fired_rules > jurisdiction.fired_rules
    assert report.fired_rules == base.fired_rules + jurisdiction.fired_rules
