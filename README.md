# sahih

**صحيح** — *valid, sound, authentic.*

Validate Peppol / PINT e-invoices in Python — and explain why they failed.

> **Status:** v0.1.0 — working end to end. Validates real invoices against the official
> UAE conformance corpus, explains 18 rules in plain language, ships a CLI. Not yet on
> PyPI. Expect the API to move before 1.0.

---

## Why this exists

Peppol e-invoice validation is a solved problem in Java and an unsolved one in Python.

The reason is narrow and technical. Schematron rule sets compile to **XSLT 2.0**.
Python's `lxml` is built on libxslt, which implements **XSLT 1.0 only**. So the
entire Python ecosystem has been unable to run the official rule sets — and a PyPI
search for `peppol` returns nothing production-ready.

`sahih` clears that hurdle with SaxonC-HE (`saxonche`), then does the part nobody
else does: turns validation output into something a human can act on.

This matters right now because the **UAE e-invoicing mandate** is live — pilot from
July 2026, mandatory for large businesses from **1 January 2027**, with penalties for
non-compliance — and most finance and ERP integration work in the region is Python.

## The shape of the problem

An e-invoice is validated against **stacked** rule sets, not one:

| Layer | What it is | Example rule IDs |
| --- | --- | --- |
| 1. EN 16931 | The European semantic core | `BR-01`, `BR-CO-15`, `BR-CL-17` |
| 2. PINT base | The international billing model | `PINT-*` |
| 3. Jurisdiction | Country-specific rules | `ibr-179-ae` (UAE) |

Each layer is versioned independently, and each fails for different reasons with
different fixes. `sahih` keeps every finding tagged with the layer that produced it.

## The problem nobody else solves

Most Schematron rules are **conditional** — "when the scheme identifier is `0235`,
then the buyer registration identifier must be present."

Remove the scheme identifier entirely and the rule does not fail. It simply never
fires. Your invoice "passes."

This means a clean validation report with a low fired-rule count is not reassurance —
it often means the document was malformed enough that entire families of rules never
applied. Every validator we surveyed reports silence identically to success.

`sahih` tracks which rules actually evaluated, so it can say:

> Your invoice passed, but the buyer registration rules never applied because the
> scheme identifier is missing.

## Install

Not yet on PyPI. For development:

```bash
git clone https://github.com/ahmedel-tawil/sahih.git
cd sahih
uv sync
```

## Usage

### Command line

```bash
sahih validate invoice.xml
sahih validate invoices/*.xml --json
sahih rules
```

Exit codes: `0` valid, `1` invalid, `2` could not validate. The 1-vs-2 split matters in
CI — a non-compliant invoice is a finding you may gate on; a wrong rule-set path is a
broken build.

### From your application

```python
from sahih import RuleSet, Validator, Explainer

# Compile once at startup — compiling costs ~300ms, validating costs ~5ms.
validator = Validator(
    [
        RuleSet("PINT base", "rulesets/pint-ae/2026.5/PINT-UBL-validation-preprocessed.xslt"),
        RuleSet("PINT-AE 2026.5", "rulesets/pint-ae/2026.5/PINT-jurisdiction-aligned-rules.xslt"),
    ]
)
validator.warm_up()
explainer = Explainer()

# Then per request — invoice XML straight from the request body, no temp file.
report = validator.validate_bytes(request_body, name="INV-2026-0042")

if not report.is_valid:
    for e in explainer.explain_report(report, blocking_only=True):
        print(e.rule_id, e.summary, e.fix)
```

`validate_bytes()` takes bytes or str; `validate()` takes a path. `Validator` is **not
thread-safe** — build one per worker, or serialise access.

### VAT-inclusive pricing

Tourism and retail routinely quote "AED 350 all in". A UBL invoice always carries
tax-**exclusive** prices, so that has to be back-computed:

```python
Line(..., unit_price=Decimal("350.00"), vat_rate=Decimal("5"), price_includes_tax=True)
```

The conversion starts from the **line total**, not the unit price, and that ordering
is the whole point:

```
line gross = 3 x 350.00 = 1050.00
line net   = 1050.00 / 1.05 = 1000.00
unit net   = 1000.00 / 3 = 333.333333
```

Converting the unit price first gives `333.33 x 3 = 999.99`, so a customer quoted
1050.00 is billed 1049.99. One fils, every line, forever.

Note `ibt-148` "Item **gross** price" means *before line discount*, **not** *including
tax* — a different sense of the word that is easy to misread. There is no
tax-inclusive price field in UBL at all.

`TaxIncludedIndicator` stays `false`, as it is in all seven official UAE samples.
Setting it `true` short-circuits `ibr-co-13` and `ibr-co-15` — it switches the totals
arithmetic checks off rather than changing how prices are expressed.

### Validating beats building

`validate()` / `validate_bytes()` judge a document **exactly as given**. That is the
primary use, and the only one where the arithmetic rules can actually fail —
`ibr-co-13`, `ibr-co-15`, `ibr-co-16`, `ibr-123`, `ibr-125` and friends check totals
that *you* supplied.

`build()` computes every total, so those rules always agree by construction. That is
convenient when you are constructing a document, and useless when you are checking
one. Never route a supplied invoice through `build()` to validate it — recomputing a
caller's total silently repairs their bug and reports compliant.

If you integrate via the model rather than raw XML, pass `DeclaredTotals` so your
figures are emitted verbatim and judged:

```python
from sahih import DeclaredTotals

invoice = Invoice(..., declared=DeclaredTotals(payable=Decimal("1857.41")))
```

Every field is optional and independent — declare what your system holds, the rest
stays derived. Declaring a total is deliberate, so you still cannot supply an
inconsistent one by accident.

## What sahih takes as input, and what it deliberately doesn't

**sahih validates UBL XML.** That is the whole input contract. Worth being explicit
about, because real systems hold invoices in other shapes.

| You have | What to do |
| --- | --- |
| UBL XML (file, bytes, HTTP body, DB column) | `validate()` / `validate_bytes()` — this is sahih |
| JSON from your own system or an ERP | **Map it to UBL first.** Separate concern — see below |
| A hybrid PDF (PDF/A-3 with embedded XML) | Extract the embedded XML, then validate that |
| A plain PDF or a scan | Extraction is guesswork. See the warning below |

### Why mapping is not in scope

Turning your JSON into UBL is a **mapping** problem: your field names, your tax logic,
your business rules. It is specific to each system, it changes when your schema changes,
and getting it wrong is a data problem.

Validation is a different kind of thing: deterministic, defined by a published spec,
identical for everyone. Mixing the two would let a mapping bug silently produce a wrong
compliance verdict — the failure would read as "your invoice is non-compliant" when the
truth is "we built the XML wrong". Those need different people looking at them.

So build the UBL yourself, then hand it to sahih. The two stay separately debuggable.

### ⚠️ On PDFs

Two completely different cases, and conflating them is dangerous:

**Hybrid PDF** — PDF/A-3 carrying an embedded XML invoice (the Factur-X / ZUGFeRD
pattern). The real invoice data is *in* the file. Extract it, validate that.
Deterministic and safe.

**Plain PDF or a scan** — there is no invoice data in there, only a picture of one.
Anything recovered is inference, whether from layout heuristics, OCR, or a model. That
is useful for drafting and it must **never** produce a compliance verdict. "Valid" on
reconstructed data means "the XML we guessed at is well-formed" — which is not what the
person reading it will think it means.

If you build PDF intake, keep extraction visible as its own step and let a human confirm
the figures before the result is treated as an invoice.

## Development

```bash
uv sync              # install project + dev group
uv run pytest        # tests
uv run ruff check .  # lint
uv run ruff format . # format
uv run mypy src      # type check
```

## Roadmap

- [x] SVRL parsing into structured findings
- [x] Validation engine (Saxon/XSLT execution)
- [x] Layered rule sets with profiles that never mix
- [x] Explanation layer — actionable diagnosis, not rule text
- [x] CLI
- [x] In-memory validation for application use
- [ ] Let the rules speak: stop pre-empting rules that fire on their own
- [ ] Curation targeting: 136 of 304 pint-ae rules are worth explaining; 5 done.
      The other 168 are self-evident and deliberately left to their own text.
- [ ] Port or drop 11 curated EN 16931 rules that never fire under `pint-ae`
- [ ] Arabic explanations (Arabic invoice *content* already works)
- [ ] PDF intake (hybrid PDF/A-3 only; scans stay out of the verdict)
- [ ] MCP server
- [ ] Publish to PyPI

## Rule set licensing

`sahih` does not vendor rule artefacts. The official Schematron and XSLT files are
published by [OpenPeppol](https://peppol.org) and distributed via
[`phax/phive-rules`](https://github.com/phax/phive-rules). Redistribution terms for
those artefacts are being confirmed; until then they are fetched rather than bundled.

## Scope

`sahih` validates and explains. It is a **developer tool**, not an Accredited Service
Provider, not a filing system of record, and not tax advice. It does not transmit
invoices over the Peppol network.

## License

Apache-2.0
