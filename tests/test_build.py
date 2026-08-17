"""
Tests for the model and builder.

Organised around the four guarantees, because those are the reason this module
exists rather than living in every integrator's codebase:

    1. every line supplied is emitted
    2. every allowance supplied is emitted
    3. totals reconcile, because the caller cannot supply them
    4. missing required data RAISES instead of producing a document that
       validates only because the checking rules lost their context node

Each of these traces to a measured failure in a hand-written mapper.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest

from sahih import (
    Address,
    Allowance,
    DeclaredTotals,
    IncompleteInvoiceError,
    Invoice,
    ItemType,
    LegalIdType,
    Line,
    ModelError,
    Party,
    VatCategory,
    build,
)

CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"


def party(**over) -> Party:
    base = {
        "name": "SayTech Excursions FZ-LLC",
        "electronic_id": "1357902468",
        "address": Address("Sheikh Zayed Road", "Dubai", "DXB"),
        "tax_id": "100123456700003",
        "legal_id": "112345678900003",
        "legal_id_type": LegalIdType.TRADE_LICENCE,
        "legal_id_authority": "Dubai DED",
    }
    return Party(**{**base, **over})


def line(qty="3", price="349.99", **over) -> Line:
    base = {
        "name": "Desert Safari (Adult)",
        "description": "Desert safari with dune bashing",
        "quantity": Decimal(qty),
        "unit_price": Decimal(price),
        "vat_category": VatCategory.STANDARD,
        "vat_rate": Decimal("5"),
        "item_type": ItemType.GOODS,
    }
    return Line(**{**base, **over})


def invoice(**over) -> Invoice:
    base = {
        "number": "SAY-2026-00417",
        "issue_date": date(2026, 8, 14),
        "due_date": date(2026, 8, 28),
        "seller": party(),
        "buyer": party(
            name="Gulf Corporate Travel LLC", electronic_id="1345678901", tax_id="134567890123003"
        ),
        "lines": (line(),),
    }
    return Invoice(**{**base, **over})


def amount(xml: bytes, tag: str) -> Decimal:
    m = re.search(rf'<cbc:{tag} currencyID="[A-Z]+">([\d.-]+)</cbc:{tag}>'.encode(), xml)
    assert m, f"{tag} not found in output"
    return Decimal(m.group(1).decode())


# ==========================================================================
# Guarantee 1 — every line is emitted
# ==========================================================================


def test_every_line_is_emitted():
    """
    The AED 519 bug. A hand-written mapper read lines[0] and silently dropped two
    lines, producing an invoice that validated perfectly for the wrong amount.
    """
    inv = invoice(lines=(line("4", "349.99"), line("2", "199.50"), line("1", "120.00")))
    xml = build(inv)

    assert xml.count(b"<cac:InvoiceLine>") == 3
    # 4*349.99 + 2*199.50 + 1*120.00
    assert amount(xml, "LineExtensionAmount") == Decimal("1918.96")


def test_line_ids_are_sequential():
    xml = build(invoice(lines=(line(), line(), line()))).decode()
    ids = re.findall(r"<cac:InvoiceLine>\s*<cbc:ID>(\d+)</cbc:ID>", xml)
    assert ids == ["1", "2", "3"]


def test_empty_lines_builds_so_the_rules_can_object():
    """
    We do NOT refuse this. ibr-016 and ibr-151-ae catch it, and an authoritative
    rule id is more useful than our prose — especially to an agent, which can look
    up 'ibr-016' but can only paraphrase a sentence.
    """
    xml = build(invoice(lines=()))
    assert b"<cac:InvoiceLine>" not in xml


# ==========================================================================
# Guarantee 2 — every allowance is emitted
# ==========================================================================


def test_discount_is_emitted_and_reduces_the_total():
    """
    The AED 700 bug. The hand-written mapper ignored the discount key entirely and
    overcharged by 700, with a perfectly valid invoice.
    """
    inv = invoice(
        lines=(line("20", "349.99"),),
        allowances=(
            Allowance(Decimal("700.00"), "Group booking 20+", VatCategory.STANDARD, Decimal("5")),
        ),
    )
    xml = build(inv)

    assert b"AllowanceChargeReason" in xml
    assert amount(xml, "LineExtensionAmount") == Decimal("6999.80")  # 20 * 349.99
    assert amount(xml, "TaxExclusiveAmount") == Decimal("6299.80")  # minus the discount
    assert amount(xml, "AllowanceTotalAmount") == Decimal("700.00")


def test_vat_is_charged_on_the_post_discount_base():
    """
    Taxing the pre-discount amount is the classic cause of ibr-co-15. VAT must be
    computed on the net taxable base after allowances.
    """
    inv = invoice(
        lines=(line("20", "349.99"),),
        allowances=(
            Allowance(Decimal("700.00"), "Group booking", VatCategory.STANDARD, Decimal("5")),
        ),
    )
    xml = build(inv)

    # 5% of 6299.80, not of 6999.80
    assert amount(xml, "TaxAmount") == Decimal("314.99")


# ==========================================================================
# Guarantee 3 — totals reconcile because they cannot be supplied
# ==========================================================================


def test_invoice_has_no_totals_to_supply():
    """The API makes ibr-co-15 unreachable rather than merely detected."""
    for forbidden in ("total", "tax_amount", "payable_amount", "tax_total"):
        assert forbidden not in Invoice.__dataclass_fields__


def test_totals_reconcile():
    xml = build(invoice(lines=(line("3", "349.99"),)))

    net = amount(xml, "TaxExclusiveAmount")
    tax = amount(xml, "TaxAmount")
    gross = amount(xml, "TaxInclusiveAmount")
    payable = amount(xml, "PayableAmount")

    assert net + tax == gross == payable


def test_float_money_is_refused_at_construction():
    """Money must arrive as Decimal or str. Floats are refused at the boundary."""
    # Passed straight to the model — note that Decimal(349.99) would silently
    # SUCCEED and carry the imprecision forward, which is exactly the trap.
    with pytest.raises(ModelError, match="float"):
        line(unit_price=349.99)

    with pytest.raises(ModelError, match="float"):
        line(quantity=3.0)

    with pytest.raises(ModelError, match="float"):
        Allowance(700.0, "x", VatCategory.STANDARD, Decimal("5"))


def test_decimal_from_float_is_the_trap_we_are_closing():
    """
    Decimal(float) does not raise. It preserves the float's error faithfully, which is
    why "just wrap it in Decimal" is not a fix and the guard belongs in the model.

    Worth being precise about where float actually bites, because the obvious guess is
    wrong: `349.99 * 3` is exactly 1049.97 in float. The damage in our demo came from
    the VAT step, where dividing by 100 introduces error that no rounding step caught.
    """
    # The multiplication people expect to break does not.
    assert 349.99 * 3 == 1049.97

    # The VAT calculation does. This is the value that reached the demo's XML.
    assert repr(349.99 * 5 / 100 * 3) == "52.49850000000001"

    # And Decimal(float) inherits the imprecision rather than curing it.
    assert Decimal(349.99) != Decimal("349.99")  # noqa: RUF032 - the point of the test
    assert str(Decimal(349.99) * 3).startswith("1049.9700000000000")  # noqa: RUF032

    # Exact arithmetic, then a single quantisation at emission.
    assert str(Decimal("349.99") * 3) == "1049.97"
    assert line(unit_price="349.99").net == Decimal("1049.97")
    assert line(unit_price="349.99").vat == Decimal("52.4985")  # quantised on output


def test_amounts_are_quantised_to_two_places():
    xml = build(invoice(lines=(line("3", "349.99"),)))
    for value in re.findall(rb'currencyID="AED">([\d.-]+)<', xml):
        decimals = value.decode().split(".")
        assert len(decimals) == 1 or len(decimals[1]) <= 2, value


def test_mixed_vat_rates_produce_separate_subtotals():
    """
    UBL wants one TaxSubtotal per (category, rate). Emitting a single subtotal for a
    mixed invoice only breaks once someone bills zero-rated alongside standard.
    """
    inv = invoice(
        lines=(
            line("1", "100.00", vat_category=VatCategory.STANDARD, vat_rate=Decimal("5")),
            line("1", "200.00", vat_category=VatCategory.ZERO_RATED, vat_rate=Decimal("0")),
        )
    )
    xml = build(inv)

    assert xml.count(b"<cac:TaxSubtotal>") == 2
    assert amount(xml, "TaxAmount") == Decimal("5.00")  # only the standard-rated line


# ==========================================================================
# Guarantee 4 — missing data raises rather than going silent
# ==========================================================================


def test_missing_buyer_tax_id_raises_instead_of_validating_clean():
    """
    THE central guarantee. Without a tax id no cac:PartyTaxScheme is emitted, the
    rules that check it have no context node, and the invoice validates clean. The
    builder refuses instead.
    """
    with pytest.raises(IncompleteInvoiceError) as exc:
        build(invoice(buyer=party(name="Gulf Corporate", tax_id=None)))

    message = str(exc.value)
    assert "buyer.tax_id" in message
    assert "would NOT be caught by validation" in message


def test_untaxed_buyer_is_allowed_when_stated_explicitly():
    """B2C individuals and non-resident export buyers genuinely have no TRN."""
    xml = build(
        invoice(buyer=party(name="Ahmed Hassan", tax_id=None, legal_id=None, legal_id_type=None)),
        buyer_tax_id_required=False,
    )
    assert b"AccountingCustomerParty" in xml
    # And the absence is real, not papered over.
    assert xml.count(b"<cac:PartyTaxScheme>") == 1  # seller only


def test_legal_id_without_type_raises():
    with pytest.raises(IncompleteInvoiceError, match="legal_id_type"):
        build(invoice(seller=party(legal_id="112345678900003", legal_id_type=None)))


def test_vat_category_must_be_explicit():
    with pytest.raises(ModelError, match="tax decision"):
        line(vat_category="S")  # a bare string, not the enum


def test_services_without_an_accounting_code_is_left_to_the_rules():
    """ibr-185-ae catches this. Shadowing it would hide the rule id."""
    built = line(item_type=ItemType.SERVICES)
    assert built.item_type is ItemType.SERVICES
    assert built.service_accounting_code is None


# ==========================================================================
# Correctness details
# ==========================================================================


def test_xml_special_characters_are_escaped():
    """
    A description containing & or < would produce malformed XML from an f-string
    template — the latent bug in the hand-written mapper.
    """
    xml = build(invoice(lines=(line(description="Safari & BBQ <premium>"),)))

    assert b"&amp;" in xml
    assert b"&lt;premium&gt;" in xml
    assert b"<premium>" not in xml

    from defusedxml import ElementTree as ET

    ET.fromstring(xml)  # must parse


def test_output_is_deterministic():
    """Same invoice built twice is byte-identical, so diffs and caching mean something."""
    assert build(invoice()) == build(invoice())


def test_unknown_profile_is_rejected():
    with pytest.raises(ValueError, match="Unknown profile"):
        build(invoice(), profile="pint-xx")


def test_missing_due_date_builds_so_ibr_127_ae_can_report_it():
    """We stopped shadowing this rule. The document builds; validation objects."""
    xml = build(invoice(due_date=None))
    assert b"<cbc:DueDate>" not in xml


# ==========================================================================
# Let the rules speak — what we deliberately DO NOT check
# ==========================================================================


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("no invoice number", {"number": ""}),
        ("no issue date", {"issue_date": None}),
        ("no due date", {"due_date": None}),
        ("no lines", {"lines": ()}),
    ],
)
def test_absent_data_still_builds(label, kwargs):
    """
    Each of these is caught by the rule set (ibr-002, ibr-003, ibr-127-ae, ibr-016).
    Refusing here would mean the rule set is never consulted, and the caller gets our
    wording instead of an identifier they can look up.
    """
    build(invoice(**kwargs))


def test_missing_number_omits_the_element_entirely():
    assert b"<cbc:ID>" not in build(invoice(number="")).split(b"<cac:")[0]


def test_uuid_is_stable_even_without_a_number():
    assert build(invoice(number="")) == build(invoice(number=""))


@pytest.mark.parametrize(
    ("legal_type", "code"),
    [
        (LegalIdType.TRADE_LICENCE, b"TL"),
        (LegalIdType.EMIRATES_ID, b"EID"),
        (LegalIdType.PASSPORT, b"PAS"),
        (LegalIdType.CABINET_DECISION, b"CD"),
    ],
)
def test_legal_id_type_maps_to_the_right_scheme_code(legal_type, code):
    """
    schemeAgencyID was hardcoded 'TL', so an Emirates ID emitted as a trade licence —
    silently, since the document is well-formed either way and no rule objects.

    The codes are not guessable. They come from ibr-183-ae's own test expression,
    which enumerates them: ("TL", "CL", "EID", "PAS", "CD").
    """
    xml = build(invoice(seller=party(legal_id_type=legal_type)))
    assert b'schemeAgencyID="' + code + b'"' in xml


# ==========================================================================
# Declared totals — the JSON path as a real validator
# ==========================================================================


def test_totals_are_derived_when_nothing_is_declared():
    xml = build(invoice(lines=(line("3", "349.99"),)))
    assert amount(xml, "PayableAmount") == Decimal("1102.47")


def test_declared_total_is_emitted_verbatim():
    """
    The caller's figure, unchanged. Recomputing it would silently repair their bug and
    report compliant — the worst answer to 'is this invoice correct?'
    """
    xml = build(invoice(declared=DeclaredTotals(payable=Decimal("9999.99"))))
    assert amount(xml, "PayableAmount") == Decimal("9999.99")


def test_declared_values_are_not_quantised():
    """
    A caller writing 1102.4685 is telling us that is what their system produced.
    ibr-125 exists to say so; rounding here would hide the defect being asked about.
    """
    xml = build(invoice(declared=DeclaredTotals(tax_amount=Decimal("52.4985"))))
    assert b"52.4985" in xml


def test_declaration_is_partial_and_independent():
    """Declare what you hold; the rest stays derived."""
    xml = build(
        invoice(lines=(line("3", "349.99"),), declared=DeclaredTotals(payable=Decimal("1.00")))
    )

    assert amount(xml, "PayableAmount") == Decimal("1.00")  # declared
    assert amount(xml, "LineExtensionAmount") == Decimal("1049.97")  # still derived


def test_declared_totals_reject_float():
    with pytest.raises(ModelError, match="float"):
        DeclaredTotals(payable=1102.47)


def test_empty_declaration_behaves_as_none():
    a = build(invoice(declared=DeclaredTotals()))
    b = build(invoice())
    assert a == b
    assert DeclaredTotals().is_empty


def test_allowance_total_can_be_declared_without_allowances():
    xml = build(invoice(declared=DeclaredTotals(allowance_total=Decimal("50.00"))))
    assert amount(xml, "AllowanceTotalAmount") == Decimal("50.00")


# ==========================================================================
# VAT-inclusive pricing — "AED 350 all in"
# ==========================================================================


def incl(qty, price, rate="5", **over):
    return line(qty, price, vat_rate=Decimal(rate), price_includes_tax=True, **over)


def test_inclusive_price_lands_exactly_on_the_quoted_total():
    """
    A customer quoted 3 x AED 350 all in expects to pay 1050.00. Anything else is a
    conversation, however small.
    """
    ln = incl("3", "350.00")
    assert ln.gross == Decimal("1050.00")
    assert ln.net == Decimal("1000.00")
    assert ln.vat == Decimal("50.00")
    assert ln.net + ln.vat == ln.gross

    xml = build(invoice(lines=(ln,)))
    assert amount(xml, "PayableAmount") == Decimal("1050.00")


def test_computing_from_the_unit_price_would_lose_a_fils():
    """
    Why `net` starts from the LINE total rather than converting the unit price first.
    333.33 x 3 is 999.99, so the customer is billed 1049.99 having been quoted 1050.00.
    """
    naive_unit = (Decimal("350.00") / Decimal("1.05")).quantize(Decimal("0.01"))
    assert naive_unit * 3 == Decimal("999.99")  # the wrong way
    assert incl("3", "350.00").net == Decimal("1000.00")  # the way we do it


def test_awkward_inclusive_price_still_reconciles():
    ln = incl("3", "349.99")
    assert ln.gross == Decimal("1049.97")
    assert ln.net + ln.vat == ln.gross


def test_zero_rated_inclusive_price_is_unchanged():
    ln = incl("2", "500.00", rate="0", vat_category=VatCategory.ZERO_RATED)
    assert ln.net == Decimal("1000.00")
    assert ln.vat == Decimal("0.00")


def test_unit_net_price_is_emitted_at_higher_precision():
    """
    Prices are not subject to the 2-decimal limit (that is ibr-091/123/124/125 on the
    document totals). Rounding the price to 2 places reintroduces the drift.
    """
    ln = incl("3", "350.00")
    assert ln.unit_net_price == Decimal("333.333333")
    assert b"333.333333" in build(invoice(lines=(ln,)))


def test_exclusive_pricing_is_unaffected():
    ln = line("3", "349.99")
    assert ln.price_includes_tax is False
    assert ln.net == Decimal("1049.97")
    assert ln.gross == ln.net + ln.vat


def test_inclusive_and_exclusive_lines_can_be_mixed():
    xml = build(invoice(lines=(line("1", "100.00"), incl("1", "105.00"))))
    # 100.00 exclusive + 100.00 back-computed = 200.00 net
    assert amount(xml, "LineExtensionAmount") == Decimal("200.00")
    assert amount(xml, "PayableAmount") == Decimal("210.00")
