"""
Command line interface.

    sahih validate invoice.xml
    sahih validate invoices/*.xml --json
    sahih rules

DESIGN CONSTRAINTS
------------------
No new dependencies. sahih is a library people embed in their own systems, and
every dependency we add lands in their tree too. argparse and manual formatting
cost nothing; click and rich cost everyone who installs us.

Colour is emitted only to a real terminal, and never when NO_COLOR is set. Piping
to a file or another process gives clean plain text.

EXIT CODES
----------
    0   every document validated
    1   at least one document is invalid
    2   something prevented validation (missing file, bad rule set)

The 1-vs-2 split is the one that matters in CI: "this invoice is non-compliant" is
a finding you may want to gate a pipeline on, while "the rule set path is wrong" is
a broken build. Collapsing them into a single non-zero code makes that
undiagnosable from a pipeline log.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .binding import Mode, generate
from .engine import RuleSet, Validator
from .exceptions import SahihError
from .explain import Explainer, Explanation
from .models import StackedReport

DEFAULT_RULES_DIR = Path("rulesets")


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


class Style:
    """ANSI codes, disabled unless we are writing to a terminal that wants them."""

    def __init__(self, stream, *, force: bool | None = None) -> None:
        self._on = (
            bool(stream.isatty() and not os.environ.get("NO_COLOR")) if force is None else force
        )

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self._on else text

    def bold(self, t: str) -> str:
        return self._wrap("1", t)

    def dim(self, t: str) -> str:
        return self._wrap("2", t)

    def red(self, t: str) -> str:
        return self._wrap("31", t)

    def green(self, t: str) -> str:
        return self._wrap("32", t)

    def yellow(self, t: str) -> str:
        return self._wrap("33", t)


def wrap(text: str, width: int, indent: str) -> str:
    """Wrap prose to a width. textwrap would do, but this keeps indentation explicit."""
    import textwrap

    return textwrap.fill(text, width=width, initial_indent=indent, subsequent_indent=indent)


#: Saxon emits fully namespace-qualified XPath locations. They are correct and
#: unreadable — a single location runs past 300 characters, most of it the same
#: UBL namespace URI repeated. Strip the noise for display; the JSON output keeps
#: the original, since machines want the exact path.
_NS_PREDICATE = re.compile(r"\[namespace-uri\(\)=(['\"]).*?\1\]")
_STAR_PREFIX = re.compile(r"\*:")


def readable_location(location: str) -> str:
    """
    Turn a namespace-qualified XPath into something a human can follow.

        /*:Invoice[namespace-uri()='urn:...'][1]/*:AccountingCustomerParty[...][1]
        ->  /Invoice[1]/AccountingCustomerParty[1]
    """
    if not location:
        return ""
    simplified = _NS_PREDICATE.sub("", location)
    return _STAR_PREFIX.sub("", simplified)


def render_human(
    report: StackedReport,
    explanations: Sequence[Explanation],
    style: Style,
    *,
    width: int = 88,
) -> str:
    """Render one document's result for a person."""
    lines: list[str] = []

    verdict = style.green("VALID") if report.is_valid else style.red("INVALID")
    lines.append(f"{style.bold(report.source)} — {verdict}")

    # Per-layer rule counts. Low counts on a layer mean it barely engaged, which is
    # the "silence is not success" problem — surface it rather than hiding it.
    breakdown = "  ".join(
        f"{layer.ruleset}: {layer.fired_rules} rules, {len(layer.fatals)} fatal"
        for layer in report.layers
    )
    lines.append(style.dim(f"  {breakdown}"))

    if not explanations:
        lines.append("")
        lines.append(style.green("  Nothing to fix."))
        return "\n".join(lines)

    lines.append("")
    for explanation in explanations:
        finding = explanation.finding
        severity = finding.severity.value if finding else "fatal"
        marker = style.red("✗") if severity == "fatal" else style.yellow("!")
        layer = f" {style.dim('[' + finding.ruleset + ']')}" if finding and finding.ruleset else ""

        lines.append(f"  {marker} {style.bold(explanation.rule_id)}{layer}")
        lines.append(wrap(explanation.summary, width, "      "))

        if explanation.is_curated:
            lines.append(wrap(f"Why: {explanation.why}", width, "      "))
            lines.append(wrap(f"Fix: {explanation.fix}", width, "      "))
        else:
            # Be explicit that this is the raw rule text, not our guidance.
            lines.append(style.dim("      (no curated guidance for this rule yet)"))

        if explanation.terms:
            lines.append(style.dim(f"      Fields: {', '.join(explanation.terms)}"))
        if finding and finding.location:
            lines.append(style.dim(f"      At: {readable_location(finding.location)}"))
        lines.append("")

    return "\n".join(lines).rstrip()


def render_json(report: StackedReport, explanations: Sequence[Explanation]) -> dict:
    """Machine-readable result. Stable shape — treat this as an API."""
    return {
        "source": report.source,
        "valid": report.is_valid,
        "layers": [
            {
                "ruleset": layer.ruleset,
                "fired_rules": layer.fired_rules,
                "fatal": len(layer.fatals),
                "warning": len(layer.warnings),
            }
            for layer in report.layers
        ],
        "findings": [
            {
                "rule_id": e.rule_id,
                "severity": e.finding.severity.value if e.finding else None,
                "ruleset": e.finding.ruleset if e.finding else None,
                "location": e.finding.location if e.finding else None,
                "summary": e.summary,
                "why": e.why or None,
                "fix": e.fix or None,
                "terms": list(e.terms),
                "explanation_source": e.source.value,
            }
            for e in explanations
        ],
    }


# ---------------------------------------------------------------------------
# Rule set discovery
# ---------------------------------------------------------------------------


#: Validation profiles. A profile is a *coherent* stack of rule sets — not merely
#: every rule set we happen to have on disk.
#:
#: THIS DISTINCTION IS NOT COSMETIC. PINT is a derivative of EN 16931 that adapts
#: the core rules for each jurisdiction. Stacking raw EN 16931 underneath PINT AE
#: produces false positives: BR-CO-09 demands an ISO 3166-1 country prefix on VAT
#: identifiers, but a UAE TRN is 15 digits with no prefix, and PINT AE deliberately
#: relaxes the rule. Validating an official, valid UAE conformance invoice against
#: both stacks reports 4 fatal EN 16931 errors and 0 PINT errors.
#:
#: Reporting a compliant invoice as non-compliant is the worst failure this tool
#: could have, so profiles are explicit and never mixed.
PROFILES = ("pint-ae", "en16931")


def discover_rulesets(rules_dir: Path, profile: str | None = None) -> list[RuleSet]:
    """
    Build a coherent rule set stack.

    Args:
        rules_dir: Directory laid out by ``scripts/fetch_rulesets.py``.
        profile:   ``"pint-ae"`` or ``"en16931"``. When omitted, PINT AE is preferred
                   if present, since that is what UAE e-invoicing requires.

    Within a profile, order is core-first: a broken total makes the jurisdiction
    rules moot, so those findings should be read first.
    """
    pint_root = rules_dir / "pint-ae"
    pint_versions = (
        sorted((d for d in pint_root.iterdir() if d.is_dir()), reverse=True)
        if pint_root.is_dir()
        else []
    )
    en16931 = rules_dir / "en16931" / "EN16931-UBL-validation.xslt"

    if profile is None:
        profile = "pint-ae" if pint_versions else "en16931"

    if profile == "pint-ae":
        found: list[RuleSet] = []
        for version_dir in pint_versions[:1]:
            base = version_dir / "PINT-UBL-validation-preprocessed.xslt"
            juris = version_dir / "PINT-jurisdiction-aligned-rules.xslt"
            if base.is_file():
                found.append(RuleSet("PINT base", base))
            if juris.is_file():
                found.append(RuleSet(f"PINT-AE {version_dir.name}", juris))
        return found

    if profile == "en16931":
        return [RuleSet("EN16931", en16931)] if en16931.is_file() else []

    raise SahihError(f"Unknown profile {profile!r}. Choose from: {', '.join(PROFILES)}")


def parse_layer_args(values: Sequence[str]) -> list[RuleSet]:
    """Parse repeated ``--layer NAME=PATH`` arguments."""
    layers = []
    for value in values:
        if "=" not in value:
            raise SahihError(f"--layer expects NAME=PATH, got: {value!r}")
        name, _, path = value.partition("=")
        layers.append(RuleSet(name.strip(), path.strip()))
    return layers


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
    out, err = sys.stdout, sys.stderr
    style = Style(out, force=False if args.json else None)

    rulesets = (
        parse_layer_args(args.layer) if args.layer else discover_rulesets(args.rules, args.profile)
    )
    if not rulesets:
        print(
            f"No rule sets found in {args.rules}/\n"
            "Fetch them with:  uv run python scripts/fetch_rulesets.py\n"
            "Or point at them: sahih validate INVOICE --layer 'EN16931=path/to.xslt'",
            file=err,
        )
        return 2

    paths = [Path(p) for p in args.invoices]
    missing = [p for p in paths if not p.is_file()]
    if missing:
        for p in missing:
            print(f"No such file: {p}", file=err)
        return 2

    explainer = Explainer()
    results = []
    any_invalid = False

    with Validator(rulesets) as validator:
        validator.warm_up()
        for path in paths:
            report = validator.validate(path)
            explanations = explainer.explain_report(report, blocking_only=not args.include_warnings)
            any_invalid |= not report.is_valid

            if args.json:
                results.append(render_json(report, explanations))
            else:
                print(render_human(report, explanations, style))
                print()

    if args.json:
        json.dump(results if len(results) > 1 else results[0], out, indent=2)
        out.write("\n")

    return 1 if any_invalid else 0


def cmd_rules(args: argparse.Namespace) -> int:
    """Show what sahih knows: discovered rule sets and curation coverage."""
    style = Style(sys.stdout)
    rulesets = discover_rulesets(args.rules, args.profile)

    print(style.bold("Rule sets"))
    if rulesets:
        for ruleset in rulesets:
            print(f"  {ruleset.name:<22} {style.dim(str(ruleset.path))}")
    else:
        print(style.dim(f"  none found in {args.rules}/ — run scripts/fetch_rulesets.py"))

    explainer = Explainer()
    print()
    print(style.bold("Curated explanations"))
    print(f"  {explainer.coverage()} rules have hand-written guidance.")
    print(
        style.dim(
            "  Everything else falls back to the rule's own message.\n"
            "  Coverage detail:  uv run python scripts/coverage_report.py"
        )
    )
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    """Generate a binding file, optionally inferred from the caller's own data."""
    samples = []
    for path in args.from_ or []:
        try:
            samples.append(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Could not read {path}: {exc}", file=sys.stderr)
            return 2

    text = generate(samples, mode=Mode(args.mode))

    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        todos = text.count("TODO")
        decisions = text.count("const:???")
        print(f"Wrote {args.output} ({args.mode} mode)", file=sys.stderr)
        print(f"  {todos} TODO(s), {decisions} decision(s) to answer.", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sahih",
        description="Validate Peppol / PINT e-invoices, and explain why they failed.",
    )
    parser.add_argument("--version", action="version", version=f"sahih {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate one or more invoices")
    validate.add_argument("invoices", nargs="+", metavar="INVOICE")
    validate.add_argument(
        "--rules",
        type=Path,
        default=DEFAULT_RULES_DIR,
        metavar="DIR",
        help=f"directory holding rule sets (default: {DEFAULT_RULES_DIR}/)",
    )
    validate.add_argument(
        "--layer",
        action="append",
        metavar="NAME=PATH",
        help="use a specific rule set; repeatable. Overrides --rules discovery.",
    )
    validate.add_argument(
        "--profile",
        choices=PROFILES,
        help="which rule stack to use. Default: pint-ae when available. "
        "Profiles are never mixed — see PROFILES in cli.py for why.",
    )
    validate.add_argument("--json", action="store_true", help="machine-readable output")
    validate.add_argument(
        "--include-warnings",
        action="store_true",
        help="also report non-blocking findings",
    )
    validate.set_defaults(func=cmd_validate)

    init = sub.add_parser("init", help="generate a binding file")
    init.add_argument(
        "--mode",
        choices=[m.value for m in Mode],
        default=Mode.MINIMAL.value,
        help="minimal: only what a valid invoice requires. full: the whole menu.",
    )
    init.add_argument(
        "--from",
        dest="from_",
        nargs="+",
        metavar="SAMPLE.json",
        help="infer paths from your own data. Omit for a blank template.",
    )
    init.add_argument("-o", "--output", metavar="FILE")
    init.set_defaults(func=cmd_init)

    rules = sub.add_parser("rules", help="show rule sets and curation coverage")
    rules.add_argument("--rules", type=Path, default=DEFAULT_RULES_DIR, metavar="DIR")
    rules.add_argument("--profile", choices=PROFILES, help="show a specific profile")
    rules.set_defaults(func=cmd_rules)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SahihError as exc:
        # Our own errors are already phrased for a human. No traceback.
        print(f"{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
