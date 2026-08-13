# sahih

**صحيح** — *valid, sound, authentic.*

Validate Peppol / PINT e-invoices in Python — and explain why they failed.

> **Status:** early development. The SVRL layer works; the validation engine and
> explanation layer are being built. Not yet published to PyPI.

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

Today, the SVRL layer:

```python
from sahih import parse_svrl

report = parse_svrl(svrl_output, ruleset="PINT-AE 2026.5", source="invoice.xml")

print(report)  # invoice.xml — INVALID against PINT-AE 2026.5 ...
print(report.is_valid)  # False
for finding in report.fatals:
    print(finding.rule_id, finding.location)
```

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
- [ ] Validation engine (Saxon/XSLT execution)
- [ ] Layered rule set management and versioning
- [ ] Explanation layer — actionable diagnosis, not rule text
- [ ] CLI
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
