"""
CLI tests.

The thing worth testing hardest here is exit codes. Everything else is
presentation, but exit codes are the contract a CI pipeline depends on:

    0  valid
    1  invalid
    2  could not validate

Collapsing 1 and 2 into "non-zero" would make a broken rule-set path
indistinguishable from a non-compliant invoice in a pipeline log.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sahih.cli import Style, discover_rulesets, main, parse_layer_args
from sahih.exceptions import SahihError

FIXTURES = Path(__file__).parent / "fixtures"
MINI = FIXTURES / "mini-rules.xslt"


def run(argv, capsys) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --------------------------------------------------------------------------
# Exit codes — the contract
# --------------------------------------------------------------------------


def test_valid_invoice_exits_zero(capsys):
    code, out, _ = run(
        ["validate", str(FIXTURES / "good-invoice.xml"), "--layer", f"mini={MINI}"], capsys
    )
    assert code == 0
    assert "VALID" in out


def test_invalid_invoice_exits_one(capsys):
    code, out, _ = run(
        ["validate", str(FIXTURES / "no-id-invoice.xml"), "--layer", f"mini={MINI}"], capsys
    )
    assert code == 1
    assert "INVALID" in out
    assert "TEST-01" in out


def test_missing_file_exits_two(capsys):
    code, _, err = run(["validate", str(FIXTURES / "nope.xml"), "--layer", f"mini={MINI}"], capsys)
    assert code == 2
    assert "No such file" in err


def test_no_rulesets_found_exits_two(capsys, tmp_path):
    code, _, err = run(
        ["validate", str(FIXTURES / "good-invoice.xml"), "--rules", str(tmp_path)], capsys
    )
    assert code == 2
    assert "No rule sets found" in err
    # The message must tell them how to fix it, not just that it failed.
    assert "fetch_rulesets" in err


def test_unsafe_document_exits_two(capsys):
    """An XXE payload is a setup failure, not an invalid invoice."""
    code, _, err = run(
        ["validate", str(FIXTURES / "xxe-invoice.xml"), "--layer", f"mini={MINI}"], capsys
    )
    assert code == 2
    assert "DTD or entity" in err


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def test_json_output_is_valid_and_shaped(capsys):
    code, out, _ = run(
        ["validate", str(FIXTURES / "no-id-invoice.xml"), "--layer", f"mini={MINI}", "--json"],
        capsys,
    )
    assert code == 1

    payload = json.loads(out)
    assert payload["valid"] is False
    assert payload["source"] == "no-id-invoice.xml"
    assert payload["layers"][0]["ruleset"] == "mini"

    finding = payload["findings"][0]
    assert finding["rule_id"] == "TEST-01"
    assert finding["severity"] == "fatal"
    assert finding["explanation_source"] == "official"  # TEST-01 is not curated


def test_json_never_contains_ansi_codes(capsys):
    """Colour must never leak into machine output."""
    _, out, _ = run(
        ["validate", str(FIXTURES / "good-invoice.xml"), "--layer", f"mini={MINI}", "--json"],
        capsys,
    )
    assert "\033[" not in out


def test_multiple_invoices_produce_a_json_list(capsys):
    code, out, _ = run(
        [
            "validate",
            str(FIXTURES / "good-invoice.xml"),
            str(FIXTURES / "no-id-invoice.xml"),
            "--layer",
            f"mini={MINI}",
            "--json",
        ],
        capsys,
    )
    payload = json.loads(out)
    assert isinstance(payload, list)
    assert len(payload) == 2
    # One bad document makes the whole run fail.
    assert code == 1


def test_warnings_hidden_by_default_and_shown_on_request(capsys):
    args = ["validate", str(FIXTURES / "noted-invoice.xml"), "--layer", f"mini={MINI}"]

    code, out, _ = run(args, capsys)
    assert code == 0  # a warning does not make it invalid
    assert "TEST-02" not in out

    code, out, _ = run([*args, "--include-warnings"], capsys)
    assert code == 0
    assert "TEST-02" in out


def test_uncurated_rules_are_marked_as_such(capsys):
    """A user must be able to tell our guidance from raw rule text."""
    _, out, _ = run(
        ["validate", str(FIXTURES / "no-id-invoice.xml"), "--layer", f"mini={MINI}"], capsys
    )
    assert "no curated guidance" in out


def test_per_layer_rule_counts_are_shown(capsys):
    """Low fired-rule counts are the 'silence is not success' signal."""
    _, out, _ = run(
        ["validate", str(FIXTURES / "good-invoice.xml"), "--layer", f"mini={MINI}"], capsys
    )
    assert "rules" in out and "mini" in out


# --------------------------------------------------------------------------
# rules subcommand
# --------------------------------------------------------------------------


def test_rules_command_reports_coverage(capsys, tmp_path):
    code, out, _ = run(["rules", "--rules", str(tmp_path)], capsys)
    assert code == 0
    assert "Curated explanations" in out
    assert "hand-written guidance" in out


# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------


def test_parse_layer_args():
    layers = parse_layer_args(["EN16931=/a/b.xslt", "PINT base=/c/d.xslt"])
    assert [x.name for x in layers] == ["EN16931", "PINT base"]
    assert layers[0].path == Path("/a/b.xslt")


def test_parse_layer_args_rejects_malformed():
    with pytest.raises(SahihError, match="NAME=PATH"):
        parse_layer_args(["no-equals-sign"])


def test_discover_finds_nothing_in_empty_dir(tmp_path):
    assert discover_rulesets(tmp_path) == []


def _full_ruleset_dir(tmp_path: Path) -> Path:
    (tmp_path / "en16931").mkdir()
    (tmp_path / "en16931" / "EN16931-UBL-validation.xslt").write_text("<x/>")
    version = tmp_path / "pint-ae" / "2026.5"
    version.mkdir(parents=True)
    (version / "PINT-UBL-validation-preprocessed.xslt").write_text("<x/>")
    (version / "PINT-jurisdiction-aligned-rules.xslt").write_text("<x/>")
    return tmp_path


def test_profiles_are_never_mixed(tmp_path):
    """
    REGRESSION GUARD. Stacking EN 16931 under PINT AE produces false positives:
    BR-CO-09 requires an ISO country prefix on VAT identifiers, but UAE TRNs have
    none and PINT AE relaxes the rule. An official, valid UAE conformance invoice
    reports 4 fatal EN 16931 errors under a mixed stack.

    Reporting a compliant invoice as non-compliant is the worst thing this tool
    could do, so the profiles must stay disjoint.
    """
    rules = _full_ruleset_dir(tmp_path)

    pint = [r.name for r in discover_rulesets(rules, "pint-ae")]
    assert pint == ["PINT base", "PINT-AE 2026.5"]
    assert "EN16931" not in pint

    en = [r.name for r in discover_rulesets(rules, "en16931")]
    assert en == ["EN16931"]


def test_pint_is_the_default_profile_when_available(tmp_path):
    """UAE e-invoicing is the target use case, so PINT wins when both are present."""
    names = [r.name for r in discover_rulesets(_full_ruleset_dir(tmp_path))]
    assert names == ["PINT base", "PINT-AE 2026.5"]


def test_falls_back_to_en16931_when_no_pint_present(tmp_path):
    (tmp_path / "en16931").mkdir()
    (tmp_path / "en16931" / "EN16931-UBL-validation.xslt").write_text("<x/>")
    assert [r.name for r in discover_rulesets(tmp_path)] == ["EN16931"]


def test_unknown_profile_is_rejected(tmp_path):
    with pytest.raises(SahihError, match="Unknown profile"):
        discover_rulesets(tmp_path, "nonsense")


def test_discover_orders_core_before_jurisdiction(tmp_path):
    """Core findings should be read before country-specific ones."""
    names = [r.name for r in discover_rulesets(_full_ruleset_dir(tmp_path), "pint-ae")]
    assert names.index("PINT base") < names.index("PINT-AE 2026.5")


def test_discover_prefers_highest_version(tmp_path):
    for version in ("2025.6", "2026.5"):
        d = tmp_path / "pint-ae" / version
        d.mkdir(parents=True)
        (d / "PINT-jurisdiction-aligned-rules.xslt").write_text("<x/>")

    names = [r.name for r in discover_rulesets(tmp_path)]
    assert names == ["PINT-AE 2026.5"]


class _FakeStream:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_colour_is_off_when_not_a_terminal():
    assert Style(_FakeStream(False)).red("x") == "x"


def test_colour_is_on_for_a_terminal(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert "\033[" in Style(_FakeStream(True)).red("x")


def test_no_color_env_is_respected(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert Style(_FakeStream(True)).red("x") == "x"


# --------------------------------------------------------------------------
# XPath readability
# --------------------------------------------------------------------------


def test_readable_location_strips_namespace_noise():
    from sahih.cli import readable_location

    raw = (
        "/*:Invoice[namespace-uri()='urn:oasis:names:specification:ubl:schema:xsd:Invoice-2'][1]"
        "/*:AccountingCustomerParty[namespace-uri()='urn:oasis:names:specification:ubl:"
        "schema:xsd:CommonAggregateComponents-2'][1]"
    )
    assert readable_location(raw) == "/Invoice[1]/AccountingCustomerParty[1]"


def test_readable_location_handles_empty():
    from sahih.cli import readable_location

    assert readable_location("") == ""


def test_json_keeps_the_exact_xpath(capsys):
    """Humans get the short form; machines must still get the precise path."""
    _, out, _ = run(
        ["validate", str(FIXTURES / "no-id-invoice.xml"), "--layer", f"mini={MINI}", "--json"],
        capsys,
    )
    payload = json.loads(out)
    assert payload["findings"][0]["location"] == "/Invoice[1]"
