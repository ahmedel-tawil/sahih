"""
Build UBL from the model — the part every integrator would otherwise rewrite.

WHY THIS IS IN THE LIBRARY
--------------------------
UBL emission does not vary between callers. The element names, the nesting, the fact
that an item's gross price lives in `Price/AllowanceCharge/BaseAmount` — all fixed by
the spec, identical for everyone. We tested the alternative by writing a careful
mapper by hand, and it lost AED 519 (read `lines[0]`) and AED 700 (ignored the
discount), both of which VALIDATED CLEAN because the emitted XML was internally
consistent with the wrong numbers.

WHAT THE BUILDER GUARANTEES
---------------------------
1. Every line you supply appears in the output. Asserted, not assumed.
2. Every allowance you supply appears in the output.
3. Totals reconcile, because you never supply them — they are derived here.
4. A required field that is missing RAISES, rather than producing a document that
   passes validation because the checking rule had no context node to attach to.

Guarantee 4 is the important one. It is the only defence against the failure mode
validation cannot see: an invoice that is silently incomplete.

XML SAFETY
----------
Built with ElementTree, not string formatting. A description containing `&` or `<`
would produce malformed XML from an f-string template — and the demo mapper we wrote
by hand had exactly that latent bug.
"""

from __future__ import annotations

import uuid
from decimal import ROUND_HALF_UP, Decimal
from xml.etree import ElementTree as ET

from .model import (
    Allowance,
    IncompleteInvoiceError,
    Invoice,
    Line,
    Party,
    VatCategory,
)

UBL = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"

#: Fixed by the profile. The caller never supplies these.
PROFILE_CONSTANTS = {
    "pint-ae": {
        "CustomizationID": "urn:peppol:pint:billing-1@ae-1",
        "ProfileID": "urn:peppol:bis:billing",
        "ProfileExecutionID": "00000000",
    }
}

TWO_PLACES = Decimal("0.01")


def q(amount: Decimal) -> str:
    """
    Quantise to 2 decimal places and render.

    Every amount goes through here exactly once, at emission. Rounding earlier lets
    error accumulate across lines; rounding nowhere emits values like `52.4985` —
    four decimal places, which trips ibr-091, ibr-124 and ibr-125.
    """
    return str(amount.quantize(TWO_PLACES, rounding=ROUND_HALF_UP))


def _el(parent: ET.Element, ns: str, tag: str, text: object = None, **attrs) -> ET.Element:
    node = ET.SubElement(parent, f"{{{ns}}}{tag}", {k: str(v) for k, v in attrs.items()})
    if text is not None:
        node.text = str(text)
    return node


# ---------------------------------------------------------------------------
# Completeness — the guarantees
# ---------------------------------------------------------------------------


def _require_party(party: Party, role: str, *, needs_tax_id: bool) -> None:
    """
    Check a party carries what the profile requires.

    ``needs_tax_id`` is False for parties that are legitimately untaxed — a B2C
    individual, a non-resident buyer on an export. It is the caller's decision, made
    explicitly, rather than something inferred from the value being absent.
    """
    if not party.name:
        raise IncompleteInvoiceError(f"{role}.name is required.")
    if not party.electronic_id:
        raise IncompleteInvoiceError(f"{role}.electronic_id is required (ibt-049).")

    if needs_tax_id and not party.tax_id:
        raise IncompleteInvoiceError(
            f"{role}.tax_id (TRN) is missing.\n"
            f"  This would NOT be caught by validation. With no tax id, sahih emits no\n"
            f"  cac:PartyTaxScheme node, and the rules that check the {role}'s TRN have\n"
            f"  no context to attach to — so they never run and the invoice validates\n"
            f"  clean. Supply the TRN, or pass needs_{role}_tax_id=False if this party\n"
            f"  is genuinely untaxed (B2C individual, non-resident on an export)."
        )

    if party.legal_id and not party.legal_id_type:
        raise IncompleteInvoiceError(
            f"{role}.legal_id was supplied without legal_id_type. The receiver cannot "
            f"tell whether that number is a trade licence, an Emirates ID or a passport "
            f"(BTAE-16)."
        )


def _check_emitted(root: ET.Element, invoice: Invoice) -> None:
    """
    Verify the document actually contains what was handed in.

    This exists because of a specific, measured failure: a hand-written mapper read
    `lines[0]` and silently dropped AED 519, producing an invoice that validated
    perfectly. Counting is cheap; silent data loss is not.
    """
    emitted_lines = len(root.findall(f"{{{CAC}}}InvoiceLine"))
    if emitted_lines != len(invoice.lines):
        raise IncompleteInvoiceError(
            f"Built {emitted_lines} invoice line(s) from {len(invoice.lines)} supplied. "
            "This is a bug in sahih — please report it."
        )

    doc_allowances = [
        node
        for node in root.findall(f"{{{CAC}}}AllowanceCharge")
        if (ci := node.find(f"{{{CBC}}}ChargeIndicator")) is not None and ci.text == "false"
    ]
    if len(doc_allowances) != len(invoice.allowances):
        raise IncompleteInvoiceError(
            f"Built {len(doc_allowances)} allowance(s) from {len(invoice.allowances)} "
            "supplied. This is a bug in sahih — please report it."
        )


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------


def _tax_groups(invoice: Invoice) -> dict[tuple[VatCategory, Decimal], dict[str, Decimal]]:
    """
    Group lines and allowances by (category, rate).

    UBL requires one `TaxSubtotal` per distinct combination. Emitting one subtotal for
    a mixed-rate invoice is a correctness bug that only shows up once someone bills a
    zero-rated item alongside a standard-rated one.
    """
    groups: dict[tuple[VatCategory, Decimal], dict[str, Decimal]] = {}

    for line in invoice.lines:
        key = (line.vat_category, line.vat_rate)
        bucket = groups.setdefault(key, {"taxable": Decimal(0), "tax": Decimal(0)})
        bucket["taxable"] += line.net

    for allowance in invoice.allowances:
        key = (allowance.vat_category, allowance.vat_rate)
        bucket = groups.setdefault(key, {"taxable": Decimal(0), "tax": Decimal(0)})
        bucket["taxable"] -= allowance.amount

    # Tax is computed on the group's NET taxable base — after allowances. Computing it
    # per line and summing would tax the pre-discount amount, which is the classic
    # cause of ibr-co-15.
    for (_, rate), bucket in groups.items():
        bucket["tax"] = bucket["taxable"] * rate / Decimal(100)

    return groups


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def _party(parent: ET.Element, party: Party, tag: str, currency: str) -> None:
    wrapper = _el(parent, CAC, tag)
    node = _el(wrapper, CAC, "Party")

    _el(node, CBC, "EndpointID", party.electronic_id, schemeID=party.electronic_id_scheme)

    address = _el(node, CAC, "PostalAddress")
    _el(address, CBC, "StreetName", party.address.street)
    _el(address, CBC, "CityName", party.address.city)
    if party.address.postal_zone:
        _el(address, CBC, "PostalZone", party.address.postal_zone)
    if party.address.subdivision:
        _el(address, CBC, "CountrySubentity", party.address.subdivision)
    country = _el(address, CAC, "Country")
    _el(country, CBC, "IdentificationCode", party.address.country)

    # Emitted only when a tax id exists. Its ABSENCE is what makes the checking
    # rules go silent, which is why _require_party refuses to get here without one
    # unless the caller said the party is untaxed.
    if party.tax_id:
        scheme = _el(node, CAC, "PartyTaxScheme")
        _el(scheme, CBC, "CompanyID", party.tax_id)
        _el(_el(scheme, CAC, "TaxScheme"), CBC, "ID", "VAT")

    legal = _el(node, CAC, "PartyLegalEntity")
    _el(legal, CBC, "RegistrationName", party.name)
    if party.legal_id:
        attrs = {"schemeAgencyID": "TL"}
        if party.legal_id_authority:
            attrs["schemeAgencyName"] = party.legal_id_authority
        _el(legal, CBC, "CompanyID", party.legal_id, **attrs)


def _line(parent: ET.Element, index: int, line: Line, currency: str) -> None:
    node = _el(parent, CAC, "InvoiceLine")
    _el(node, CBC, "ID", index)  # derived: sequence, never supplied
    _el(node, CBC, "InvoicedQuantity", line.quantity, unitCode=line.unit_code)
    _el(node, CBC, "LineExtensionAmount", q(line.net), currencyID=currency)

    item = _el(node, CAC, "Item")
    _el(item, CBC, "Description", line.description)
    _el(item, CBC, "Name", line.name)

    classification = _el(item, CAC, "CommodityClassification")
    _el(classification, CBC, "CommodityCode", line.item_type.value)
    _el(classification, CBC, "ItemClassificationCode", line.classification_code, listID="HS")

    category = _el(item, CAC, "ClassifiedTaxCategory")
    _el(category, CBC, "ID", line.vat_category.value)
    _el(category, CBC, "Percent", line.vat_rate)
    _el(_el(category, CAC, "TaxScheme"), CBC, "ID", "VAT")

    price = _el(node, CAC, "Price")
    _el(price, CBC, "PriceAmount", q(line.unit_price), currencyID=currency)
    _el(price, CBC, "BaseQuantity", 1, unitCode=line.unit_code)
    # ibr-126-ae wants the item GROSS price too. With no separate gross concept in
    # most source systems, net and gross coincide and the allowance is zero.
    charge = _el(price, CAC, "AllowanceCharge")
    _el(charge, CBC, "ChargeIndicator", "false")
    _el(charge, CBC, "Amount", q(Decimal(0)), currencyID=currency)
    _el(charge, CBC, "BaseAmount", q(line.unit_price), currencyID=currency)

    extension = _el(node, CAC, "ItemPriceExtension")
    _el(extension, CBC, "Amount", q(line.net + line.vat), currencyID=currency)
    _el(_el(extension, CAC, "TaxTotal"), CBC, "TaxAmount", q(line.vat), currencyID=currency)


def _allowance(parent: ET.Element, allowance: Allowance, currency: str) -> None:
    node = _el(parent, CAC, "AllowanceCharge")
    _el(node, CBC, "ChargeIndicator", "false")  # false = allowance (discount)
    _el(node, CBC, "AllowanceChargeReason", allowance.reason)
    _el(node, CBC, "Amount", q(allowance.amount), currencyID=currency)
    category = _el(node, CAC, "TaxCategory")
    _el(category, CBC, "ID", allowance.vat_category.value)
    _el(category, CBC, "Percent", allowance.vat_rate)
    _el(_el(category, CAC, "TaxScheme"), CBC, "ID", "VAT")


def build(
    invoice: Invoice,
    *,
    profile: str = "pint-ae",
    seller_tax_id_required: bool = True,
    buyer_tax_id_required: bool = True,
) -> bytes:
    """
    Emit a compliant UBL invoice.

    Args:
        invoice: The model to render.
        profile: Which profile's constants to stamp in.
        seller_tax_id_required: A seller issuing a UAE tax invoice must be registered,
            so this defaults to True and should rarely change.
        buyer_tax_id_required: Set False for a genuinely untaxed buyer — a B2C
            individual, or a non-resident on an export. Making this explicit means the
            caller states the situation rather than discovering later that whole rule
            families went silent.

    Returns:
        UTF-8 encoded UBL XML.

    Raises:
        IncompleteInvoiceError: something required is missing. This is deliberate —
            refusing to build beats emitting a document that validates only because
            the missing data took its checking rules with it.
    """
    constants = PROFILE_CONSTANTS.get(profile)
    if constants is None:
        raise ValueError(f"Unknown profile {profile!r}. Known: {sorted(PROFILE_CONSTANTS)}")

    _require_party(invoice.seller, "seller", needs_tax_id=seller_tax_id_required)
    _require_party(invoice.buyer, "buyer", needs_tax_id=buyer_tax_id_required)

    for prefix, ns in (("", UBL), ("cac", CAC), ("cbc", CBC)):
        ET.register_namespace(prefix, ns)

    root = ET.Element(f"{{{UBL}}}Invoice")
    cur = invoice.currency

    for tag, value in constants.items():
        _el(root, CBC, tag, value)

    _el(root, CBC, "ID", invoice.number)
    # Deterministic from the invoice number: the same invoice built twice is
    # byte-identical, which makes diffing and caching meaningful.
    _el(root, CBC, "UUID", str(uuid.uuid5(uuid.NAMESPACE_DNS, invoice.number)))
    _el(root, CBC, "IssueDate", invoice.issue_date.isoformat())
    if invoice.due_date:
        _el(root, CBC, "DueDate", invoice.due_date.isoformat())
    _el(root, CBC, "InvoiceTypeCode", invoice.invoice_type_code)
    if invoice.note:
        _el(root, CBC, "Note", invoice.note)
    _el(root, CBC, "DocumentCurrencyCode", cur)
    if invoice.buyer_reference:
        _el(root, CBC, "BuyerReference", invoice.buyer_reference)

    _party(root, invoice.seller, "AccountingSupplierParty", cur)
    _party(root, invoice.buyer, "AccountingCustomerParty", cur)

    means = _el(root, CAC, "PaymentMeans")
    _el(means, CBC, "PaymentMeansCode", invoice.payment_means_code)

    for allowance in invoice.allowances:
        _allowance(root, allowance, cur)

    groups = _tax_groups(invoice)
    total_tax = sum((g["tax"] for g in groups.values()), Decimal(0))

    tax_total = _el(root, CAC, "TaxTotal")
    _el(tax_total, CBC, "TaxAmount", q(total_tax), currencyID=cur)
    _el(tax_total, CBC, "TaxIncludedIndicator", "false")
    for (category, rate), bucket in groups.items():
        subtotal = _el(tax_total, CAC, "TaxSubtotal")
        _el(subtotal, CBC, "TaxableAmount", q(bucket["taxable"]), currencyID=cur)
        _el(subtotal, CBC, "TaxAmount", q(bucket["tax"]), currencyID=cur)
        cat = _el(subtotal, CAC, "TaxCategory")
        _el(cat, CBC, "ID", category.value)
        _el(cat, CBC, "Percent", rate)
        _el(_el(cat, CAC, "TaxScheme"), CBC, "ID", "VAT")

    # Every total below is derived. The caller cannot supply any of them, which is
    # what makes ibr-co-15 unreachable rather than merely tested for.
    line_total = sum((line.net for line in invoice.lines), Decimal(0))
    allowance_total = sum((a.amount for a in invoice.allowances), Decimal(0))
    net = line_total - allowance_total

    totals = _el(root, CAC, "LegalMonetaryTotal")
    _el(totals, CBC, "LineExtensionAmount", q(line_total), currencyID=cur)
    _el(totals, CBC, "TaxExclusiveAmount", q(net), currencyID=cur)
    _el(totals, CBC, "TaxInclusiveAmount", q(net + total_tax), currencyID=cur)
    if invoice.allowances:
        _el(totals, CBC, "AllowanceTotalAmount", q(allowance_total), currencyID=cur)
    _el(totals, CBC, "PayableAmount", q(net + total_tax), currencyID=cur)

    # ibr-127-ae: a due date is required once there is anything to pay. Conditional
    # rules like this are exactly what a caller cannot be expected to know, and the
    # failure is far cheaper here than after a document has been transmitted.
    if (net + total_tax) > 0 and invoice.due_date is None:
        raise IncompleteInvoiceError(
            "Invoice.due_date is missing, and the payable amount is "
            f"{q(net + total_tax)} {cur}. A due date is required whenever there is an "
            "amount due (ibr-127-ae)."
        )

    for index, line in enumerate(invoice.lines, start=1):
        _line(root, index, line, cur)

    _check_emitted(root, invoice)

    ET.indent(root, space="  ")
    body: bytes = ET.tostring(root, encoding="utf-8")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + body
