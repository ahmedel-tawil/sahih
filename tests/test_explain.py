"""
Tests for the explanation layer.

Two things matter here and they pull in opposite directions:

  1. Curated explanations must be actually useful — present, non-trivial, and
     phrased as guidance rather than a restatement of the rule.
  2. Uncurated rules must degrade cleanly, never raise, never return None.

The second is what keeps the library honest as rule sets grow past our coverage.
"""

from __future__ import annotations

import pytest

from sahih import Finding, Severity, StackedReport, ValidationReport
from sahih.explain import Explainer, ExplanationSource


def finding(rule_id: str, message: str = "Official rule text.", **kw) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=kw.pop("severity", Severity.FATAL),
        message=message,
        **kw,
    )


@pytest.fixture(scope="module")
def explainer() -> Explainer:
    return Explainer()


# --------------------------------------------------------------------------
# Curated path
# --------------------------------------------------------------------------


def test_curated_rule_gets_full_treatment(explainer):
    result = explainer.explain(finding("BR-CO-15"))

    assert result.source is ExplanationSource.CURATED
    assert result.is_curated
    assert result.summary
    assert result.why
    assert result.fix
    # The business terms let a UI jump straight to the offending field.
    assert "BT-112" in result.terms


def test_curated_summary_is_plain_language_not_rule_restatement(explainer):
    """
    The whole point is to not sound like the spec. If the summary is just the rule
    text with the ID stripped, this layer is adding nothing.
    """
    result = explainer.explain(finding("BR-CO-15"))

    # Real words a finance person would use, not business-term codes.
    assert "add up" in result.summary.lower() or "not equal" in result.summary.lower()
    # Term codes belong in `terms`, not in prose meant for humans.
    assert "BT-112" not in result.summary


def test_uae_jurisdiction_rule_is_curated(explainer):
    result = explainer.explain(finding("ibr-179-ae"))

    assert result.is_curated
    assert "IBT-048" in result.terms
    # The fix should point at the likely cause, not just restate the constraint.
    assert "duplicate" in result.fix.lower() or "PartyTaxScheme" in result.fix


def test_multiline_toml_strings_are_collapsed(explainer):
    """TOML multi-line strings carry source newlines; prose must arrive clean."""
    result = explainer.explain(finding("BR-01"))

    assert "\n" not in result.why
    assert "  " not in result.why  # no doubled spaces from wrapping


# --------------------------------------------------------------------------
# Fallback path — the important one
# --------------------------------------------------------------------------


def test_uncurated_rule_falls_back_to_official_message(explainer):
    result = explainer.explain(finding("BR-99", message="Some rule nobody curated yet."))

    assert result.source is ExplanationSource.OFFICIAL
    assert not result.is_curated
    assert result.summary == "Some rule nobody curated yet."
    assert result.why == ""
    assert result.fix == ""


def test_explain_never_returns_none(explainer):
    """Callers must never have to handle a missing explanation."""
    for rule_id in ("BR-CO-15", "TOTALLY-MADE-UP", "", "ibr-999-ae"):
        assert explainer.explain(finding(rule_id)) is not None


def test_finding_with_no_message_still_explains(explainer):
    result = explainer.explain(finding("UNKNOWN-1", message=""))

    assert result.source is ExplanationSource.OFFICIAL
    assert result.summary  # some placeholder, never blank


def test_original_finding_is_attached(explainer):
    """The caller needs location and ruleset, which live on the Finding."""
    f = finding("BR-02", location="/Invoice[1]", ruleset="EN16931")
    result = explainer.explain(f)

    assert result.finding is f
    assert result.finding.location == "/Invoice[1]"


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------


def test_explain_report_preserves_order():
    report = ValidationReport(
        findings=(finding("BR-02"), finding("BR-CO-15"), finding("BR-03")),
        ruleset="EN16931",
    )
    results = Explainer().explain_report(report)

    assert [r.rule_id for r in results] == ["BR-02", "BR-CO-15", "BR-03"]


def test_blocking_only_filters_warnings():
    report = ValidationReport(
        findings=(
            finding("BR-02", severity=Severity.FATAL),
            finding("BR-51", severity=Severity.WARNING),
        )
    )
    explainer = Explainer()

    assert len(explainer.explain_report(report)) == 2
    blocking = explainer.explain_report(report, blocking_only=True)
    assert [r.rule_id for r in blocking] == ["BR-02"]


def test_works_across_stacked_layers():
    """Explanations must span layers — that is how real validation reports arrive."""
    stacked = StackedReport(
        layers=(
            ValidationReport(findings=(finding("BR-CO-15"),), ruleset="EN16931"),
            ValidationReport(findings=(finding("ibr-179-ae"),), ruleset="PINT-AE"),
        )
    )
    results = Explainer().explain_report(stacked)

    assert [r.rule_id for r in results] == ["BR-CO-15", "ibr-179-ae"]
    assert all(r.is_curated for r in results)


# --------------------------------------------------------------------------
# Knowledge base integrity
# --------------------------------------------------------------------------


def test_every_curated_entry_is_complete(explainer):
    """
    A half-written entry is worse than none — it looks curated but says nothing.
    This guards the data file as it grows.
    """
    from sahih.explain import _load_curated

    for rule_id, entry in _load_curated().items():
        assert entry.get("summary"), f"{rule_id} has no summary"
        assert entry.get("why"), f"{rule_id} has no why"
        assert entry.get("fix"), f"{rule_id} has no fix"
        assert len(str(entry["summary"])) > 20, f"{rule_id} summary is too thin"
        assert isinstance(entry.get("terms", []), list), f"{rule_id} terms must be a list"


def test_coverage_is_reported(explainer):
    assert explainer.coverage() > 10
    assert explainer.is_curated("BR-CO-15")
    assert not explainer.is_curated("BR-NONSENSE")


def test_knowledge_base_can_be_injected():
    """Tests and downstream users can supply their own mappings."""
    custom = {"X-1": {"summary": "Custom summary here.", "why": "Because.", "fix": "Do it."}}
    result = Explainer(curated=custom).explain(finding("X-1"))

    assert result.is_curated
    assert result.summary == "Custom summary here."


# ==========================================================================
# Languages
# ==========================================================================


def test_arabic_is_available():
    from sahih import available_languages

    langs = available_languages()
    assert langs[0] == "en"  # base language first
    assert "ar" in langs


def test_requested_language_wins_when_translated():
    result = Explainer(language="ar").explain(finding("ibr-101-ae"))

    assert result.source is ExplanationSource.CURATED
    assert result.language == "ar"
    assert result.is_translated
    assert "الجهة" in result.summary


def test_untranslated_rule_falls_back_to_base_language():
    """
    Better English than nothing — but `source` and `language` say so, so a caller
    rendering RTL can set dir="ltr" on that one string instead of mangling it.
    """
    result = Explainer(language="ar").explain(finding("BR-CO-15"))

    assert result.source is ExplanationSource.CURATED_FALLBACK
    assert result.language == "en"
    assert result.is_curated  # still curated…
    assert not result.is_translated  # …just not in Arabic
    assert "add up" in result.summary


def test_uncurated_rule_is_english_even_in_arabic_mode():
    """
    Rule text is OpenPeppol's and English only. Never claim otherwise — the language
    field is what stops a UI applying RTL to a Latin string.
    """
    result = Explainer(language="ar").explain(finding("ibr-999", message="English rule text."))

    assert result.source is ExplanationSource.OFFICIAL
    assert result.language == "en"


def test_unknown_language_degrades_to_base_without_raising():
    """A missing translation file is not an error — coverage is expected to be uneven."""
    ex = Explainer(language="fr")
    assert ex.translated_count() == 0
    assert ex.explain(finding("ibr-101-ae")).language == "en"


def test_terms_are_shared_not_duplicated_per_language():
    """
    Business-term codes are language-independent identifiers. Repeating them per
    language would only create drift.
    """
    en = Explainer().explain(finding("ibr-101-ae"))
    ar = Explainer(language="ar").explain(finding("ibr-101-ae"))
    assert ar.terms == en.terms == ("BTAE-11", "BTAE-16")


def test_arabic_entries_are_complete_and_correspond_to_english():
    """
    Guards two kinds of drift: a half-written entry, and a key that exists in Arabic
    but not in English (a typo that would silently never be used).
    """
    from sahih.explain import _load_curated

    base = _load_curated()
    arabic = _load_curated("ar")

    assert arabic, "Arabic file should not be empty"
    for rule_id, entry in arabic.items():
        assert rule_id in base, f"{rule_id} exists in Arabic but not English"
        for field in ("summary", "why", "fix"):
            assert entry.get(field), f"{rule_id} has no {field}"
            assert len(str(entry[field])) > 20, f"{rule_id} {field} is too thin"


def test_only_live_rules_are_translated():
    """
    The 11 EN 16931 entries never fire under pint-ae. Translating them would be the
    exact waste this project keeps trying to avoid.
    """
    from sahih.explain import _load_curated

    dead = {
        "BR-01",
        "BR-02",
        "BR-03",
        "BR-06",
        "BR-07",
        "BR-11",
        "BR-51",
        "BR-63",
        "BR-CL-17",
        "BR-CO-15",
        "BR-CO-16",
    }
    assert not (set(_load_curated("ar")) & dead)
