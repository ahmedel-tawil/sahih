"""
The invoice model — what you hand to `build()`.

WHAT THIS IS FOR
----------------
A typed contract between your data and UBL. You populate it; sahih emits the XML.
You never write `cbc:`, never learn that the gross price lives in
`Price/AllowanceCharge/BaseAmount`, and never compute a total.

THREE RULES THAT SHAPE THE WHOLE MODEL
--------------------------------------

1. MONEY IS ``Decimal``. Passing a float raises immediately. `349.99 * 3` is
   `1049.9699999999998` in IEEE 754, which produced five validation failures when
   we tested it. Rejecting floats at construction turns a compliance error three
   layers downstream into a TypeError on the line that caused it.

2. YOU CANNOT SUPPLY A TOTAL. There is no `total` field, no `tax_amount`, no
   `payable_amount`. The builder derives every one of them from the lines. This
   makes `ibr-co-15` ("totals must reconcile") and the decimal-places rules
   structurally impossible rather than merely detected.

3. TAX DECISIONS HAVE NO DEFAULT. `vat_category` and `item_type` are required.
   See `VatCategory` for why guessing them is not acceptable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from .exceptions import SahihError


class ModelError(SahihError):
    """The invoice model was constructed with invalid or missing data."""


class IncompleteInvoiceError(ModelError):
    """
    A field required by the profile is missing.

    This is the fix for the silence problem. An invoice missing its buyer tax
    identifier validates CLEAN, because the rules that check it hang off a
    `cac:PartyTaxScheme` context node that is never emitted when the value is absent.
    No node, no rule, no finding.

    Refusing to build beats building something that passes.
    """


class VatCategory(StrEnum):
    """
    How a supply is treated for VAT. Required — never inferred from the rate.

    The rate cannot tell you the category, and the difference is not cosmetic:

        Z  zero-rated     taxable at 0%.  Input VAT IS recoverable.
        E  exempt         not taxable.    Input VAT is NOT recoverable.
        O  outside scope  not a UAE supply at all.

    All three commonly appear with a 0% rate. A library that guessed between them
    would be making a tax decision on the user's behalf, with real consequences for
    their VAT return. So the caller states it.
    """

    STANDARD = "S"
    ZERO_RATED = "Z"
    EXEMPT = "E"
    REVERSE_CHARGE = "AE"
    OUTSIDE_SCOPE = "O"


class ItemType(StrEnum):
    """
    Goods or services.

    Not cosmetic either: SERVICES additionally requires a service accounting code
    (`ibr-185-ae`). Every official UAE conformance sample uses GOODS, which is why
    our demo's desert safari — genuinely a service — needed a deliberate decision
    rather than a default.
    """

    GOODS = "G"
    SERVICES = "S"


class LegalIdType(StrEnum):
    """Which document a party's legal registration identifier refers to (BTAE-16)."""

    TRADE_LICENCE = "Commercial/Trade license"
    EMIRATES_ID = "Emirates ID"
    PASSPORT = "Passport"
    CABINET_DECISION = "Cabinet decision"


def _money(value: object, field_name: str) -> Decimal:
    """
    Coerce to Decimal, refusing float.

    ``str`` is accepted and converted because that is how money arrives from JSON
    and databases, and ``Decimal("349.99")`` is exact. ``float`` is refused because
    ``Decimal(349.99)`` is not.
    """
    if isinstance(value, float):
        raise ModelError(
            f"{field_name} was given a float ({value!r}). Money must be Decimal or str — "
            f"floats cannot represent decimal amounts exactly, and the error surfaces "
            f"later as a validation failure rather than here. Use Decimal('{value}') "
            f"or the original string."
        )
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | str):
        try:
            return Decimal(str(value))
        except Exception as exc:
            raise ModelError(f"{field_name}: {value!r} is not a valid amount ({exc})") from exc
    raise ModelError(f"{field_name}: expected Decimal, str or int, got {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class Address:
    street: str
    city: str
    subdivision: str = ""  # emirate, e.g. DXB
    country: str = "AE"  # ISO 3166-1 alpha-2
    postal_zone: str | None = None


@dataclass(frozen=True, slots=True)
class Party:
    """
    A seller or buyer.

    ``tax_id`` is the UAE TRN. It is optional in the *model* because genuinely
    untaxed parties exist (B2C individuals, non-resident buyers on an export), but
    the builder enforces the profile's requirement and raises rather than emitting
    a document with the identifying node missing.
    """

    name: str
    electronic_id: str
    address: Address
    tax_id: str | None = None
    legal_id: str | None = None
    legal_id_type: LegalIdType | None = None
    legal_id_authority: str | None = None
    electronic_id_scheme: str = "0235"


@dataclass(frozen=True, slots=True)
class Line:
    """One invoice line. Amounts are derived, never supplied."""

    name: str
    description: str
    quantity: Decimal
    unit_price: Decimal
    vat_category: VatCategory
    vat_rate: Decimal
    item_type: ItemType
    classification_code: str = "996411"
    unit_code: str = "H87"
    service_accounting_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantity", _money(self.quantity, "Line.quantity"))
        object.__setattr__(self, "unit_price", _money(self.unit_price, "Line.unit_price"))
        object.__setattr__(self, "vat_rate", _money(self.vat_rate, "Line.vat_rate"))

        if not isinstance(self.vat_category, VatCategory):
            raise ModelError(
                f"Line.vat_category must be a VatCategory, got {self.vat_category!r}. "
                "This is a tax decision and has no default — see VatCategory."
            )
        if not isinstance(self.item_type, ItemType):
            raise ModelError(f"Line.item_type must be an ItemType, got {self.item_type!r}")
        # NOTE: we deliberately do NOT check that SERVICES carries a
        # service_accounting_code. ibr-185-ae catches that, with an authoritative
        # rule id an agent can cite. Shadowing it would replace that id with our
        # prose. See "let the rules speak" in the module docstring.

    @property
    def net(self) -> Decimal:
        """Line amount excluding VAT."""
        return self.quantity * self.unit_price

    @property
    def vat(self) -> Decimal:
        """VAT on this line."""
        return self.net * self.vat_rate / Decimal(100)


@dataclass(frozen=True, slots=True)
class Allowance:
    """A document-level discount. Charges are not supported yet."""

    amount: Decimal
    reason: str
    vat_category: VatCategory
    vat_rate: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", _money(self.amount, "Allowance.amount"))
        object.__setattr__(self, "vat_rate", _money(self.vat_rate, "Allowance.vat_rate"))
        if not isinstance(self.vat_category, VatCategory):
            raise ModelError("Allowance.vat_category must be a VatCategory")


@dataclass(frozen=True, slots=True)
class Invoice:
    """
    A complete invoice, ready to build.

    Deliberately has no totals. See rule 2 in the module docstring.
    """

    seller: Party
    buyer: Party
    lines: tuple[Line, ...]
    number: str = ""
    issue_date: date | None = None
    currency: str = "AED"
    due_date: date | None = None
    allowances: tuple[Allowance, ...] = field(default_factory=tuple)
    payment_means_code: str = "1"  # UN/ECE 4461. '1' = instrument not defined.
    invoice_type_code: str = "380"  # commercial invoice
    note: str | None = None
    buyer_reference: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "lines", tuple(self.lines))
        object.__setattr__(self, "allowances", tuple(self.allowances))

        # Deliberately permissive. A missing number, issue date or line is caught by
        # ibr-002, ibr-003 and ibr-016 respectively — with rule identifiers a caller
        # (or an agent) can look up. Refusing here would replace those with our own
        # wording and stop the rule set from ever being consulted.
        if self.issue_date is not None and not isinstance(self.issue_date, date):
            # A type error, not a missing value: there is no element we could emit.
            raise ModelError(
                f"Invoice.issue_date must be a date or None, got {type(self.issue_date).__name__}"
            )
