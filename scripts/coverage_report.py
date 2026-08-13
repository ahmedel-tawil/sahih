#!/usr/bin/env python
"""
How much of the rule surface is curated?

Answers two questions that are easy to lose track of as the knowledge base grows:

  1. What fraction of real rules have hand-written explanations?
  2. Does every curated rule ID actually exist in the artefacts?

Question 2 matters more than it looks. A typo in a rule ID produces an entry that
never matches anything — it inflates the apparent coverage while helping nobody, and
nothing else in the test suite would catch it.

Usage:
    uv run python scripts/coverage_report.py
    uv run python scripts/coverage_report.py --uncurated       # list what is missing
    uv run python scripts/coverage_report.py --uncurated --limit 40
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RULESETS = ROOT / "rulesets"
EXPLANATIONS = ROOT / "src" / "sahih" / "data" / "explanations.toml"
PINT_VERSION = "2026.5"

#: Each layer names its rules differently, so each needs its own pattern.
LAYERS = [
    ("EN 16931", RULESETS / "en16931" / "EN16931-UBL-validation.xslt", r"\[(BR-[A-Z0-9-]+)\]"),
    (
        "PINT base",
        RULESETS / "pint-ae" / PINT_VERSION / "PINT-UBL-validation-preprocessed.xslt",
        r"\[(ibr-\d+)\]",
    ),
    (
        f"PINT AE {PINT_VERSION}",
        RULESETS / "pint-ae" / PINT_VERSION / "PINT-jurisdiction-aligned-rules.xslt",
        r"\[(ibr-\d+-ae)\]",
    ),
]


def rule_ids(path: Path, pattern: str) -> set[str]:
    if not path.is_file():
        return set()
    return set(re.findall(pattern, path.read_text(encoding="utf-8", errors="replace")))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uncurated", action="store_true", help="list uncurated rule IDs")
    parser.add_argument("--limit", type=int, default=25, help="how many to list per layer")
    args = parser.parse_args()

    if not RULESETS.is_dir():
        print("No rulesets/ — run: uv run python scripts/fetch_rulesets.py", file=sys.stderr)
        return 1

    with EXPLANATIONS.open("rb") as handle:
        curated = set(tomllib.load(handle))

    print(f"{'LAYER':<22}{'RULES':>7}{'CURATED':>9}{'COVERAGE':>10}")
    print("-" * 48)

    everything: set[str] = set()
    for name, path, pattern in LAYERS:
        ids = rule_ids(path, pattern)
        everything |= ids
        if not ids:
            print(f"{name:<22}{'—':>7}{'—':>9}{'not fetched':>10}")
            continue
        hit = len(ids & curated)
        print(f"{name:<22}{len(ids):>7}{hit:>9}{hit / len(ids) * 100:>9.1f}%")

    print("-" * 48)
    if everything:
        hits = len(everything & curated)
        print(f"{'TOTAL':<22}{len(everything):>7}{hits:>9}{hits / len(everything) * 100:>9.1f}%")

    # The integrity check. A curated ID matching nothing is a silent dead entry.
    orphans = curated - everything
    if orphans:
        print(f"\n⚠  {len(orphans)} curated ID(s) match no rule in the artefacts:")
        for rule_id in sorted(orphans):
            print(f"     {rule_id}")
        print("   Either a typo, or from a rule set version not fetched.")
    else:
        print("\n✓  Every curated rule ID exists in the artefacts.")

    if args.uncurated:
        print("\nUncurated rules — candidates for the next batch:")
        for name, path, pattern in LAYERS:
            missing = sorted(rule_ids(path, pattern) - curated)
            if not missing:
                continue
            print(f"\n  {name} ({len(missing)} uncurated)")
            for rule_id in missing[: args.limit]:
                print(f"     {rule_id}")
            if len(missing) > args.limit:
                print(f"     ... and {len(missing) - args.limit} more")

    return 0


if __name__ == "__main__":
    sys.exit(main())
