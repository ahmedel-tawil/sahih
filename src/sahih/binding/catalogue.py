"""
The field catalogue — what a binding can contain, and who provides each field.

WHERE THIS COMES FROM
---------------------
Not from judgement. Both tiers are derived from OpenPeppol's own conformance
invoices, which are the authoritative statement of what a valid document looks like:

    MINIMAL  <- "Standard invoice Mandatory fields.xml"   59 elements, fires  56 rules
    FULL     <- "Standard.invoice.-.Extensive.xml"       123 elements, fires 279 rules

Both validate clean. The minimal file IS the floor, by their definition.

THE THING TO UNDERSTAND ABOUT "FULL"
------------------------------------
There is no such document as a "full invoice". The extensive sample is one rich
example, not a superset — many fields are mutually exclusive by construction:

    a tax exemption reason        only exists for exempt categories
    PaymentMandate                excludes PayeeFinancialAccount (different means)
    TaxRepresentativeParty        only when you actually have one
    delivery details              meaningful for goods, rarely for services

So FULL is a MENU, not a template. Generating it produces optional sections that are
commented out and must be deliberately switched on. Emitting them all as active
bindings would produce a document that cannot validate.

FIELD TIERS
-----------
    CONSTANT   fixed by the profile. sahih emits it; the user never sees it.
    DERIVED    computed from the lines. The user MUST NOT supply it — supplying
               totals is how BR-CO-15 and the float bugs happen.
    SUPPLIED   genuinely from the user's data. This is what a binding binds.
    DECISION   supplied, but not inferable from data. Never guessed. See vat_category.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class Tier(StrEnum):
    CONSTANT = "constant"
    DERIVED = "derived"
    SUPPLIED = "supplied"
    DECISION = "decision"


class Mode(StrEnum):
    MINIMAL = "minimal"
    FULL = "full"


@dataclass(frozen=True)
class Field:
    """One bindable field."""

    key: str  # dotted key in the binding file
    tier: Tier
    mode: Mode  # MINIMAL fields also appear in FULL
    aliases: tuple[str, ...] = ()  # names we recognise in user data
    note: str = ""
    options: tuple[str, ...] = ()  # for DECISION fields
    optional: bool = False  # FULL-only sections default to commented-out


@dataclass(frozen=True)
class Section:
    """A group of fields, rendered together."""

    name: str
    fields: tuple[Field, ...]
    each: str | None = None  # binds over an array
    note: str = ""
    optional: bool = False
    #: Names this section's container might have in a caller's data. Without these,
    #: scoping only matched a literal `seller`/`buyer`/`lines` key, so a system using
    #: `vendor`/`client`/`items` inferred nothing at all.
    roots: tuple[str, ...] = ()


def _f(key, tier, mode=Mode.MINIMAL, aliases=(), note="", options=(), optional=False) -> Field:
    return Field(key, tier, mode, tuple(aliases), note, tuple(options), optional)


# ---------------------------------------------------------------------------
# MINIMAL — every field in the official mandatory-fields invoice that the user
# must actually provide. Constants and derived values are listed for
# documentation but never rendered as bindable entries.
# ---------------------------------------------------------------------------

INVOICE = Section(
    "invoice",
    (
        _f(
            "number",
            Tier.SUPPLIED,
            aliases=("invoice_number", "number", "invoice_no", "id", "doc_number"),
        ),
        _f("issue_date", Tier.SUPPLIED, aliases=("issue_date", "issued", "date", "invoice_date")),
        _f("due_date", Tier.SUPPLIED, aliases=("due_date", "due", "payment_due")),
        _f("currency", Tier.SUPPLIED, aliases=("currency", "currency_code", "ccy")),
        _f(
            "buyer_reference",
            Tier.SUPPLIED,
            Mode.FULL,
            ("buyer_reference", "po_number", "customer_ref"),
            note="Buyer's own reference (IBT-010). Often a PO number.",
            optional=True,
        ),
        _f(
            "note",
            Tier.SUPPLIED,
            Mode.FULL,
            ("note", "notes", "remarks", "comment"),
            note="Free-text note (IBT-022).",
            optional=True,
        ),
        _f(
            "tax_point_date",
            Tier.SUPPLIED,
            Mode.FULL,
            ("tax_point_date", "supply_date", "service_date"),
            note="Date of supply, when it differs from the issue date.",
            optional=True,
        ),
    ),
)

_PARTY_FIELDS = (
    _f("name", Tier.SUPPLIED, aliases=("name", "legal_name", "company_name", "registration_name")),
    _f(
        "tax_id",
        Tier.SUPPLIED,
        aliases=("trn", "tax_id", "vat_number", "tax_registration_number"),
        note="UAE TRN, 15 digits. Absence makes whole rule families go silent.",
    ),
    _f(
        "legal_id",
        Tier.SUPPLIED,
        aliases=("trade_licence", "trade_license", "licence_no", "registration_number"),
        note="Trade licence / Emirates ID / passport number.",
    ),
    _f(
        "legal_id_type",
        Tier.DECISION,
        aliases=("id_type", "legal_id_type"),
        note="Which document legal_id refers to.",
        options=("Commercial/Trade license", "Emirates ID", "Passport", "Cabinet decision"),
    ),
    _f(
        "legal_id_authority",
        Tier.SUPPLIED,
        aliases=("authority", "issuing_authority", "licence_authority"),
        note="Required when legal_id_type is a trade licence (ibr-101-ae).",
    ),
    _f("electronic_id", Tier.SUPPLIED, aliases=("peppol_id", "endpoint_id", "electronic_address")),
    _f("street", Tier.SUPPLIED, aliases=("street", "address", "street_name", "address_line1")),
    _f("city", Tier.SUPPLIED, aliases=("city", "city_name", "town")),
    _f(
        "subdivision",
        Tier.SUPPLIED,
        aliases=("emirate", "state", "region", "subdivision", "country_subentity"),
    ),
    _f(
        "postal_zone",
        Tier.SUPPLIED,
        Mode.FULL,
        ("postal_code", "postcode", "zip", "postal_zone"),
        optional=True,
    ),
    _f("email", Tier.SUPPLIED, Mode.FULL, ("email", "contact_email"), optional=True),
    _f(
        "telephone",
        Tier.SUPPLIED,
        Mode.FULL,
        ("phone", "telephone", "contact_phone"),
        optional=True,
    ),
)

SELLER = Section(
    "seller",
    _PARTY_FIELDS,
    roots=("seller", "vendor", "supplier", "issuer", "from", "merchant", "provider"),
)
BUYER = Section(
    "buyer",
    _PARTY_FIELDS,
    roots=("buyer", "client", "customer", "recipient", "to", "purchaser", "billed_to"),
)

LINES = Section(
    "lines",
    (
        _f("name", Tier.SUPPLIED, aliases=("name", "title", "item_name", "product")),
        _f("description", Tier.SUPPLIED, aliases=("description", "desc", "details")),
        _f("quantity", Tier.SUPPLIED, aliases=("quantity", "qty", "units", "count")),
        _f("unit_price", Tier.SUPPLIED, aliases=("unit_price", "price", "rate", "amount_per_unit")),
        _f(
            "vat_rate",
            Tier.SUPPLIED,
            aliases=("vat_rate", "tax_rate", "vat_percent", "tax_percent"),
        ),
        _f(
            "vat_category",
            Tier.DECISION,
            aliases=("vat_category", "tax_category"),
            note="A TAX DECISION, not a field. 0% may be Z, E or O — three different "
            "legal treatments. sahih will never infer this.",
            options=(
                "S  standard rated",
                "Z  zero-rated (taxable at 0%, input VAT recoverable)",
                "E  exempt (input VAT NOT recoverable)",
                "AE reverse charge",
                "O  outside scope",
            ),
        ),
        _f(
            "item_type",
            Tier.DECISION,
            aliases=("item_type", "type", "kind"),
            note="'S' additionally requires service_accounting_code (ibr-185-ae).",
            options=("G  goods", "S  services"),
        ),
        _f(
            "classification_code", Tier.SUPPLIED, aliases=("hs_code", "classification", "item_code")
        ),
        _f(
            "unit_code",
            Tier.SUPPLIED,
            Mode.FULL,
            ("unit", "uom", "unit_code"),
            note="UN/ECE Rec 20 code. Defaults to H87 (piece).",
            optional=True,
        ),
        _f(
            "service_accounting_code",
            Tier.SUPPLIED,
            Mode.FULL,
            ("sac", "service_code"),
            note="Required when item_type is 'S'.",
            optional=True,
        ),
        _f(
            "buyers_item_id",
            Tier.SUPPLIED,
            Mode.FULL,
            ("buyer_item_id", "customer_sku"),
            optional=True,
        ),
        _f(
            "sellers_item_id",
            Tier.SUPPLIED,
            Mode.FULL,
            ("sku", "item_id", "product_code"),
            optional=True,
        ),
    ),
    each="lines",
    note="Bound ONCE as a template. Applies to every element of the array.",
    roots=("lines", "items", "line_items", "details", "rows", "products", "entries"),
)

ALLOWANCES = Section(
    "allowances",
    (
        _f("amount", Tier.SUPPLIED, aliases=("amount", "value", "discount_amount")),
        _f("reason", Tier.SUPPLIED, aliases=("reason", "description", "note")),
        _f(
            "vat_category",
            Tier.DECISION,
            aliases=("vat_category",),
            note="An allowance carries its own VAT category.",
            options=("S", "Z", "E", "AE", "O"),
        ),
    ),
    each="discounts",
    note="Document-level discounts. Omit the section entirely if you have none.",
    optional=True,
    roots=("discounts", "discount", "allowances", "rebate", "rebates", "deductions"),
)

PAYMENT = Section(
    "payment",
    (
        _f(
            "means_code",
            Tier.SUPPLIED,
            aliases=("payment_means", "payment_method", "means_code"),
            note="UN/ECE 4461. '1' = not defined, '30' = credit transfer (needs an account), "
            "'55' = debit card.",
        ),
        _f(
            "account_id",
            Tier.SUPPLIED,
            Mode.FULL,
            ("iban", "account", "account_id"),
            note="Required when means_code is 30 (ibr-192-ae).",
            optional=True,
        ),
        _f("terms", Tier.SUPPLIED, Mode.FULL, ("payment_terms", "terms"), optional=True),
    ),
)

DELIVERY = Section(
    "delivery",
    (
        _f(
            "date",
            Tier.SUPPLIED,
            Mode.FULL,
            ("delivery_date", "actual_delivery_date"),
            optional=True,
        ),
        _f("city", Tier.SUPPLIED, Mode.FULL, ("delivery_city",), optional=True),
        _f("country", Tier.SUPPLIED, Mode.FULL, ("delivery_country",), optional=True),
    ),
    note="Where and when the supply happened. Meaningful for goods; rarely for services.",
    optional=True,
)

REFERENCES = Section(
    "references",
    (
        _f(
            "order_id",
            Tier.SUPPLIED,
            Mode.FULL,
            ("order_id", "po_number", "purchase_order"),
            optional=True,
        ),
        _f("contract_id", Tier.SUPPLIED, Mode.FULL, ("contract", "contract_id"), optional=True),
        _f("project_id", Tier.SUPPLIED, Mode.FULL, ("project", "project_id"), optional=True),
    ),
    note="Cross-references to other documents.",
    optional=True,
)

SECTIONS: tuple[Section, ...] = (
    INVOICE,
    SELLER,
    BUYER,
    LINES,
    ALLOWANCES,
    PAYMENT,
    DELIVERY,
    REFERENCES,
)

#: Emitted by sahih, never bound. Listed so the generated file can say so.
CONSTANTS = {
    "CustomizationID": "urn:peppol:pint:billing-1@ae-1",
    "ProfileID": "urn:peppol:bis:billing",
    "ProfileExecutionID": "00000000",
    "InvoiceTypeCode": "380 (commercial invoice)",
    "TaxScheme/ID": "VAT",
    "Country/IdentificationCode": "AE",
}

#: Computed from the lines. Supplying these is how BR-CO-15 and float bugs happen.
DERIVED = (
    "LineExtensionAmount",
    "TaxExclusiveAmount",
    "TaxInclusiveAmount",
    "PayableAmount",
    "TaxTotal/TaxAmount",
    "TaxSubtotal/TaxableAmount",
    "TaxSubtotal/TaxAmount",
    "InvoiceLine/ID",
    "ItemPriceExtension/Amount",
    "Price/AllowanceCharge/BaseAmount",
    "UUID",
    "AllowanceTotalAmount",
)


def sections_for(mode: Mode) -> list[Section]:
    """Sections relevant to a mode, with fields filtered to that mode."""
    out = []
    for section in SECTIONS:
        if mode is Mode.MINIMAL and section.optional:
            continue
        fields = tuple(f for f in section.fields if mode is Mode.FULL or f.mode is Mode.MINIMAL)
        if fields:
            # dataclasses.replace keeps every other attribute. An explicit constructor
            # call here silently dropped `roots` the moment that field was added — the
            # exact bug that made vendor/client/items inference fail.
            out.append(replace(section, fields=fields))
    return out
