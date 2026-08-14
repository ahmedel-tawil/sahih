#!/usr/bin/env python
"""
Fetch official validation artefacts into ./rulesets/ (gitignored).

WHY FETCH RATHER THAN VENDOR
----------------------------
The rule sets are published by OpenPeppol and redistributed through
`phax/phive-rules` (Apache-2.0). The *repository* license is clear; the
redistribution terms for OpenPeppol's artefacts themselves are not yet confirmed.
Until they are, sahih fetches artefacts at setup time instead of bundling them.
Slightly worse ergonomics, zero licensing exposure.

Usage:
    uv run python scripts/fetch_rulesets.py
    uv run python scripts/fetch_rulesets.py --pint-version 2026.3
"""

from __future__ import annotations

import argparse
import io
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "rulesets"

PHIVE_RAW = (
    "https://raw.githubusercontent.com/phax/phive-rules/master/"
    "phive-rules-peppol-pint/src/main/resources/external/schematron/pint-ae"
)
PHIVE_SCH = (
    "https://raw.githubusercontent.com/phax/phive-rules/master/"
    "phive-rules-peppol-pint/src/test/resources/external/rule-source/pint-ae"
)
PHIVE_TESTS = (
    "https://raw.githubusercontent.com/phax/phive-rules/master/"
    "phive-rules-peppol-pint/src/test/resources/external/test-files/pint-ae"
)
EN16931_ZIP = (
    "https://github.com/ConnectingEurope/eInvoicing-EN16931/releases/download/"
    "validation-1.3.16/en16931-ubl-1.3.16.zip"
)

#: A representative slice of the official UAE conformance corpus. The full set is
#: ~40 files; these five exercise distinctly different VAT treatments.
UAE_SAMPLES = [
    "Standard tax invoice.xml",
    "Exports.xml",
    "Zero rated supplies.xml",
    "Supply under Reverse charge mechanism.xml",
    "Supply involving free trade zone.xml",
]


def get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    print(f"  {path.relative_to(ROOT)}  ({len(data) / 1024:,.0f} KB)")


def fetch_pint_ae(version: str) -> None:
    print(f"\nPINT AE {version} — rule sets")
    for name in ("PINT-UBL-validation-preprocessed.xslt", "PINT-jurisdiction-aligned-rules.xslt"):
        write(DEST / "pint-ae" / version / name, get(f"{PHIVE_RAW}/{version}/billing/{name}"))

    # The Schematron SOURCE, not just the compiled XSLT. It carries the rule context,
    # the test expression and the severity flag as structured attributes — everything
    # the catalogue needs, and far more reliable than regexing compiled output.
    print(f"\nPINT AE {version} — rule sources (.sch) for the catalogue")
    for name in ("PINT-UBL-validation-preprocessed.sch", "PINT-jurisdiction-aligned-rules.sch"):
        try:
            write(DEST / "pint-ae" / version / name, get(f"{PHIVE_SCH}/{version}/billing/{name}"))
        except Exception as exc:
            print(f"  (skipped {name}: {exc})")

    print(f"\nPINT AE {version} — official UAE conformance invoices")
    for sample in UAE_SAMPLES:
        quoted = urllib.parse.quote(sample)
        try:
            data = get(f"{PHIVE_TESTS}/{version}/billing/inv/{quoted}")
        except Exception as exc:
            print(f"  (skipped {sample}: {exc})")
            continue
        write(DEST / "samples" / "pint-ae" / version / sample, data)


def fetch_en16931() -> None:
    print("\nEN 16931 — validation artefacts 1.3.16")
    archive = zipfile.ZipFile(io.BytesIO(get(EN16931_ZIP)))
    for member in archive.namelist():
        if member.endswith("EN16931-UBL-validation.xslt"):
            write(DEST / "en16931" / "EN16931-UBL-validation.xslt", archive.read(member))
            return
    print("  WARNING: expected XSLT not found in archive", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pint-version", default="2026.5", help="PINT AE version (default: 2026.5)"
    )
    parser.add_argument("--skip-en16931", action="store_true")
    args = parser.parse_args()

    print(f"Fetching validation artefacts into {DEST.relative_to(ROOT)}/")
    try:
        fetch_pint_ae(args.pint_version)
        if not args.skip_en16931:
            fetch_en16931()
    except Exception as exc:
        print(f"\nFailed: {exc}", file=sys.stderr)
        return 1

    print("\nDone. Integration tests will now run:  uv run pytest -m integration")
    return 0


if __name__ == "__main__":
    sys.exit(main())
