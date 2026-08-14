# Design: `sahih.build` — from your data to compliant UBL

**Status:** proposal, not built. For review.
**Date:** 2026-08-14

---

## Why this exists

Every integrator currently writes their own JSON → UBL mapper. We tested that advice by
following it, and the mapper we wrote — a careful one — produced:

| Bug | Cost |
| --- | --- |
| `lines[0]` instead of every line | **AED 519 silently lost** |
| forgot to read the `discount` key | **AED 700 silently lost** |
| `float` money arithmetic | 5 validation errors |
| wrong payment means code | `ibr-192-ae` |
| missing gross price | `ibr-126-ae` |
| item type "Services" without accounting code | `ibr-185-ae` |

The two money bugs **validated clean**, because the emitted XML was internally
consistent — just consistent with the wrong numbers.

Every one of those is *emission*, not *field naming*. Emission is fixed by the spec and
identical for every integrator. Asking each of them to rediscover it is how the same six
bugs get written forever.

## The three tiers

The insight that drives the whole design — "mapping" is really three different things:

| Tier | Varies by client? | Who owns it |
| --- | --- | --- |
| **Emission** — element names, nesting, `Price/AllowanceCharge/BaseAmount`, mandatory structure | No. Spec-fixed. | **sahih** |
| **Binding** — "my `unit_price` is their `PriceAmount`" | Yes | **you**, as config |
| **Decision** — which VAT category, goods or services, is this an export | Yes, and undecidable from data alone | **you**, explicitly |

Previous guidance lumped all three together and pushed them to the client. Only the
middle one belongs there.

---

## Architecture

```
your JSON ──[binding]──▶ Invoice model ──[builder]──▶ UBL XML ──[validator]──▶ findings
             optional      the contract    spec-fixed
```

**The typed model is the core; the binding is sugar.** Callers who prefer code construct
the model directly. Callers with an existing JSON shape declare a binding. Both converge
on the same validated object, so there is one place where correctness is enforced.

---

## Part 1 — the model

Sketch, not final signatures:

```python
class VatCategory(StrEnum):
    STANDARD = "S"  # 5% in the UAE
    ZERO_RATED = "Z"  # taxable at 0%
    EXEMPT = "E"  # not taxable
    REVERSE_CHARGE = "AE"
    OUTSIDE_SCOPE = "O"


class ItemType(StrEnum):
    GOODS = "G"
    SERVICES = "S"


@dataclass(frozen=True)
class Line:
    name: str
    description: str
    quantity: Decimal
    unit_price: Decimal
    vat_category: VatCategory  # required — never inferred. See below.
    vat_rate: Decimal
    item_type: ItemType  # required
    unit_code: str = "H87"
    service_accounting_code: str | None = None  # required when item_type is SERVICES


@dataclass(frozen=True)
class Invoice:
    number: str
    issue_date: date
    currency: str
    seller: Party
    buyer: Party
    lines: tuple[Line, ...]  # non-empty
    due_date: date | None = None
    allowances: tuple[Allowance, ...] = ()
    # NOTE: no totals field. See "the builder computes money".
```

### Money is `Decimal`, always

The model refuses `float`. Not a lint preference. The drift is subtler than the usual
example suggests: `349.99 * 3` is exactly `1049.97`, but the VAT step
`349.99 * 5 / 100 * 3` gives `52.49850000000001`, which is the value that reached a real
document in testing and tripped three decimal-places rules.
Passing a float raises at construction rather than surfacing as a compliance error
three layers later.

### The builder computes every total

`Invoice` has no `total`, no `tax_total`, no `payable_amount`. The builder derives them
from the lines using `Decimal`, quantising once at the end.

This deletes an entire bug class. `BR-CO-15` ("totals must reconcile"), `BR-CO-16`,
`ibr-124`, `ibr-091`, `ibr-125` become **structurally impossible** rather than
validated-against. You cannot supply an inconsistent total if you cannot supply a total.

### What must never be inferred

**`vat_category` is required and has no default.** It is tempting to derive it from the
rate — 5% → `S`, 0% → `Z`. That inference is *wrong and legally consequential*:

| Rate | Could be | Difference |
| --- | --- | --- |
| 0% | `Z` zero-rated | Taxable supply. Input VAT **recoverable**. |
| 0% | `E` exempt | Not a taxable supply. Input VAT **not recoverable**. |
| 0% | `O` outside scope | Not a UAE supply at all. |

Three different legal treatments, one indistinguishable rate. A library that guesses here
is quietly making a tax decision for someone. It must be stated.

Same reasoning for `item_type`. Our demo hit this: a desert safari is genuinely a
service, item type `S` triggers `ibr-185-ae` demanding a service accounting code, and
every official UAE sample uses `G`. That is a real decision with a real answer — and the
answer belongs to the person who knows their VAT position, not to us.

---

## Part 2 — the binding

For callers with an existing JSON shape. Dotted paths, no JSONPath dependency.

```yaml
# saytech-binding.yaml
version: 1
profile: pint-ae

invoice:
  number:     invoice_number
  issue_date: issued
  due_date:   due
  currency:   currency

seller:
  name:               seller.name
  tax_id:             seller.trn
  legal_id:           seller.trade_licence
  legal_id_type:      const:Commercial/Trade license
  legal_id_authority: const:Dubai DED
  electronic_id:      seller.peppol_id
  address:
    street:      seller.street
    city:        seller.city
    subdivision: seller.emirate
    country:     const:AE

buyer:
  name:     buyer.name
  tax_id:   buyer.trn
  # ... same shape

lines:
  each:          lines            # iterate this array
  name:          name
  description:   description
  quantity:      quantity
  unit_price:    unit_price
  vat_rate:      vat_rate
  vat_category:  const:S          # a decision, stated once
  item_type:     const:G

allowances:
  each:   discounts               # absent key => no allowances, explicitly
  amount: amount
  reason: reason
```

Three constructs only:

- `field: path.to.value` — dotted lookup in your data
- `field: const:VALUE` — a literal, for decisions that do not live in your data
- `each: path` — iterate an array; sibling keys are relative to each element

**Escape hatch:** any value may instead be a Python callable, for the cases config cannot
express (a lookup table, conditional VAT category, a currency conversion).

```python
binding.override("lines.vat_category", lambda line: "Z" if line["export"] else "S")
```

---

## Part 3 — completeness guarantees

This is the part that pays for the whole design. The builder asserts, and **raises rather
than emitting a document**:

1. **Line count is preserved.** *n* source lines produce exactly *n* `InvoiceLine`
   elements. `lines[0]` cannot happen.
2. **Every declared allowance is emitted.** A discount present in your data and absent
   from the XML is an error, not an omission.
3. **Totals reconcile by construction**, because you never supply them.
4. **A required field that is absent raises.** This is the fix for the silence problem.

Guarantee 4 deserves the emphasis. Our demo removed the buyer TRN and the invoice
validated **clean** — because the rules that check it hang off a `cac:PartyTaxScheme`
context node, and with no TRN the mapper emitted no such node. No node, no rule, no
finding.

The builder inverts that. If the profile requires a buyer tax identifier and the binding
resolves to `None`, it raises `IncompleteInvoiceError` naming the field. **Refusing to
build beats building something that passes.**

That is a guarantee validation structurally cannot provide, because validation only sees
what is present.

---

## Error model

Distinct exceptions, because these have different audiences:

| Exception | Means | Who fixes it |
| --- | --- | --- |
| `BindingError` | The binding is malformed or a path resolves nowhere | Whoever wrote the binding |
| `IncompleteInvoiceError` | Required data is missing | Whoever owns the source data |
| `DecisionRequiredError` | A field needing an explicit decision was not supplied | Whoever knows the VAT position |

Never a `KeyError`, never a silent `None`.

---

## What stays out

- **PDF extraction.** Different failure model — inference, not mapping.
- **Peppol transmission.** That is an ASP's job and an accreditation burden.
- **Guessing decisions.** Better to raise than to quietly pick a tax treatment.

---

## Open questions

1. **Is the binding worth building at all in v1?** The typed model alone solves every bug
   the demo found. The binding is convenience. Shipping the model first and the binding
   after real use would be more disciplined — and would let real bindings shape the format
   rather than guesses.
2. **YAML or Python dict?** YAML needs a dependency (`pyyaml`); a dict needs none. TOML
   is stdlib but poor at deep nesting.
3. **How much does the builder validate up front** versus leaving it to the validator?
   Proposal: structural completeness only. Do not reimplement 420 rules.
4. **Credit notes.** Same model with a different type code, or a separate one?
5. **Multi-currency.** Out of v1, but the model should not preclude it.

## Recommendation

Build **Part 1 (the model + builder) first**, ship it, use it against the six demo
invoices, and let real integrations tell us what the binding should look like. Part 1
eliminates every bug the demo found. Part 2 only saves typing.
