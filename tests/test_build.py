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


def test_empty_lines_is_refused():
    with pytest.raises(IncompleteInvoiceError, match="nothing to charge"):
        invoice(lines=())


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
    assert Decimal(349.99) != Decimal("349.99")
    assert str(Decimal(349.99) * 3).startswith("1049.9700000000000")

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


def test_services_require_an_accounting_code():
    with pytest.raises(IncompleteInvoiceError, match="service_accounting_code"):
        line(item_type=ItemType.SERVICES)

    ok = line(item_type=ItemType.SERVICES, service_accounting_code="996411")
    assert ok.item_type is ItemType.SERVICES


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


def test_due_date_required_when_something_is_payable():
    """
    ibr-127-ae. A conditional rule a caller cannot be expected to know — and cheaper
    to fail here than after the document has been transmitted.
    """
    with pytest.raises(IncompleteInvoiceError, match="due_date"):
        build(invoice(due_date=None))
