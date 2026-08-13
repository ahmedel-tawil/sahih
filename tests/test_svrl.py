"""
Tests for SVRL parsing.

These are pure unit tests — no Saxon, no rule sets, no network. They pin down the
translation from SVRL to `Finding` objects using hand-written SVRL fragments, so
they run in milliseconds and fail for exactly one reason.

Integration tests that run real rule sets through Saxon live separately, because
they are slower and need artefacts downloaded.
"""

from __future__ import annotations

import pytest

from sahih import Severity, parse_svrl
from sahih.svrl import SVRLParseError


def svrl(body: str) -> str:
    """Wrap SVRL fragments in the minimum valid envelope."""
    return (
        '<svrl:schematron-output xmlns:svrl="http://purl.oclc.org/dsdl/svrl">'
        f"{body}"
        "</svrl:schematron-output>"
    )


FAILED_ASSERT = """
  <svrl:fired-rule context="/Invoice"/>
  <svrl:failed-assert id="BR-CO-15" flag="fatal"
                      location="/Invoice[1]/cbc:TaxInclusiveAmount[1]"
                      test="xs:decimal(a) = xs:decimal(b) + xs:decimal(c)">
    <svrl:text>
        [BR-CO-15]-Invoice total amount with VAT (BT-112) =
        Invoice total amount without VAT (BT-109) + Invoice total VAT amount (BT-110).
    </svrl:text>
  </svrl:failed-assert>
"""


def test_parses_a_failed_assert():
    report = parse_svrl(svrl(FAILED_ASSERT), ruleset="EN16931", source="inv.xml")

    assert len(report.findings) == 1
    finding = report.findings[0]

    assert finding.rule_id == "BR-CO-15"
    assert finding.severity is Severity.FATAL
    assert finding.is_blocking
    assert finding.location == "/Invoice[1]/cbc:TaxInclusiveAmount[1]"
    assert finding.ruleset == "EN16931"
    # Whitespace from the source XML must be collapsed.
    assert "\n" not in finding.message
    assert finding.message.startswith("[BR-CO-15]-Invoice total amount with VAT")


def test_counts_fired_rules_separately_from_findings():
    """
    fired-rule means 'evaluated', not 'failed'. Conflating the two is the single
    most common misreading of a validation report.
    """
    body = '<svrl:fired-rule context="/Invoice"/>' * 5 + FAILED_ASSERT
    report = parse_svrl(svrl(body))

    assert report.fired_rules == 6  # five standalone + the one inside the fragment
    assert len(report.findings) == 1


def test_successful_report_is_also_a_finding():
    """
    Schematron <report> fires when its test is TRUE. It is still a finding.
    Dropping successful-report elements silently loses real violations.
    """
    body = """
      <svrl:successful-report id="PEPPOL-EN16931-R053" flag="warning">
        <svrl:text>Only one tax total with tax subtotals should be provided.</svrl:text>
      </svrl:successful-report>
    """
    report = parse_svrl(svrl(body))

    assert len(report.findings) == 1
    assert report.findings[0].rule_id == "PEPPOL-EN16931-R053"
    assert report.findings[0].severity is Severity.WARNING


def test_warnings_do_not_invalidate_the_document():
    body = """
      <svrl:failed-assert id="SOFT-1" flag="warning">
        <svrl:text>Deprecated code used.</svrl:text>
      </svrl:failed-assert>
    """
    report = parse_svrl(svrl(body))

    assert report.is_valid is True
    assert len(report.warnings) == 1
    assert len(report.fatals) == 0


def test_clean_document_yields_empty_valid_report():
    report = parse_svrl(svrl('<svrl:fired-rule context="/Invoice"/>'), source="ok.xml")

    assert report.findings == ()
    assert report.is_valid is True
    assert report.fired_rules == 1


@pytest.mark.parametrize(
    ("flag", "expected"),
    [
        ("fatal", Severity.FATAL),
        ("warning", Severity.WARNING),
        ("info", Severity.INFO),
        ("FATAL", Severity.FATAL),  # case-insensitive
        (" warning ", Severity.WARNING),  # tolerant of stray whitespace
        (None, Severity.FATAL),  # missing flag -> assume blocking
        ("nonsense", Severity.FATAL),  # unknown flag -> assume blocking
    ],
)
def test_severity_mapping_defaults_to_blocking(flag, expected):
    """
    Unknown severities must not silently downgrade to 'passing'. If we do not
    understand a rule set's flag, treating it as fatal is the safe failure mode.
    """
    assert Severity.from_svrl(flag) is expected


def test_missing_rule_id_is_marked_not_blank():
    body = (
        '<svrl:failed-assert flag="fatal"><svrl:text>No id here.</svrl:text></svrl:failed-assert>'
    )
    report = parse_svrl(svrl(body))

    assert report.findings[0].rule_id == "(unidentified-rule)"


def test_malformed_xml_raises():
    with pytest.raises(SVRLParseError):
        parse_svrl("<not-closed>")


def test_report_str_is_human_readable():
    report = parse_svrl(svrl(FAILED_ASSERT), ruleset="EN16931", source="inv.xml")
    text = str(report)

    assert "INVALID" in text
    assert "inv.xml" in text
    assert "EN16931" in text
