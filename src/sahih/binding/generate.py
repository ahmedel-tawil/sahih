"""
Generate a binding file — `sahih init`.

Two modes:

    minimal   only what a valid invoice requires. Every entry is needed.
    full      the whole supported menu, optional sections commented out.

Two sources:

    --from your.json    infer paths from your actual data
    (nothing)           a blank template with every path left as TODO

THE RULE THAT MAKES THIS SAFE
-----------------------------
Bindings are inferred. DECISIONS ARE NEVER INFERRED.

`vat_category` and `item_type` always emit as `???` with their options spelled out,
no matter how confident a name match looks. Guessing that 0% means "zero-rated"
rather than "exempt" would be making someone's tax decision for them — the two have
opposite consequences for input VAT recovery.

The generator's job is to do the boring 80% so that attention lands on the 20% that
carries legal weight.
"""

from __future__ import annotations

import difflib
from datetime import date
from typing import Any

from .catalogue import CONSTANTS, DERIVED, Field, Mode, Section, Tier, sections_for

#: Below this, a name match is not worth suggesting.
CONFIDENT = 0.86
PLAUSIBLE = 0.62


def flatten(data: Any, prefix: str = "") -> dict[str, str]:
    """Map dotted path -> type name. Arrays of objects recurse into the first element."""
    out: dict[str, str] = {}
    if not isinstance(data, dict):
        return out
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten(value, path + "."))
        elif isinstance(value, list):
            out[path] = "array"
            if value and isinstance(value[0], dict):
                out.update(flatten(value[0], path + "[]."))
        else:
            out[path] = type(value).__name__
    return out


def in_scope(path: str, scope: str) -> bool:
    """
    Is this source path a candidate for a field in `scope`?

    Scoping matters more than it looks. Without it a `seller` field happily binds to a
    `buyer` path because the leaf names are identical — a silent, expensive mistake.

    Three shapes:
        ""            document root: top-level scalars only, e.g. `invoice_number`
        "seller"      a nested object: paths under that prefix
        "lines[]"     inside an array: paths under that element
    """
    if scope == "":
        return "." not in path  # root-level scalars only
    if scope.endswith("[]"):
        return path.startswith(scope + ".")
    return path.startswith(scope + ".")


def best_match(field: Field, candidates: list[str], scope: str) -> tuple[str | None, float]:
    """
    Find the likeliest source path for a field, within scope.

    Deliberately does NOT fall back to the full candidate pool when a scope yields
    nothing. An out-of-scope match is worse than no match: 'nothing matched' prompts a
    human, while a wrong binding silently ships the buyer's TRN as the seller's.
    """
    pool = [c for c in candidates if in_scope(c, scope)]

    best: tuple[str | None, float] = (None, 0.0)
    for path in pool:
        leaf = path.split(".")[-1].replace("[]", "").lower()
        for alias in field.aliases:
            score = (
                1.0
                if leaf == alias.lower()
                else difflib.SequenceMatcher(None, leaf, alias.lower()).ratio()
            )
            if score > best[1]:
                best = (path, score)
    return best


def _wrap_comment(text: str, indent: str, width: int = 84) -> list[str]:
    import textwrap

    return [f"{indent}# {line}" for line in textwrap.wrap(text, width - len(indent))]


def _render_field(
    field: Field, samples: list[dict], scope: str, indent: str, strip: str = ""
) -> list[str]:
    lines: list[str] = []

    # Decisions: never inferred, always spelled out.
    if field.tier is Tier.DECISION:
        if field.note:
            lines += _wrap_comment(field.note, indent)
        for option in field.options:
            lines.append(f"{indent}#     {option}")
        lines.append(f"{indent}{field.key}: const:???        # TODO — decide")
        return lines

    if field.note:
        lines += _wrap_comment(field.note, indent)

    if not samples:
        marker = "  # TODO" if not field.optional else "  # TODO (optional)"
        lines.append(
            f"{indent}# {field.key}: <your.path>{marker}"
            if field.optional
            else f"{indent}{field.key}: <your.path>{marker}"
        )
        return lines

    candidates: list[str] = []
    for sample in samples:
        candidates.extend(flatten(sample))
    candidates = sorted(set(candidates))

    path, score = best_match(field, candidates, scope)
    shown = path[len(strip) :] if path and strip and path.startswith(strip) else path or ""

    if path and score >= CONFIDENT:
        note = "matched" if score < 1.0 else "exact match"
        present, total = path_coverage(path, samples)
        if total > 1 and present < total:
            note += f" — ONLY IN {present}/{total} SAMPLES"
        entry = f"{indent}{field.key}: {shown}  # {note}"
        lines.append(f"{indent}# {entry.strip()}" if field.optional else entry)
    elif path and score >= PLAUSIBLE:
        present, total = path_coverage(path, samples)
        cov = f", only in {present}/{total}" if total > 1 and present < total else ""
        lines.append(f"{indent}{field.key}: {shown}  # GUESS ({score:.2f}{cov}) — verify")
    else:
        prefix = "# " if field.optional else ""
        lines.append(f"{indent}{prefix}{field.key}: <your.path>  # TODO — nothing matched")
    return lines


def find_root(section: Section, samples: list[dict]) -> str | None:
    """
    Locate this section's container in the caller's data.

    Matching on the section name alone was too rigid: a system using `vendor` and
    `client` rather than `seller` and `buyer` inferred nothing at all. Section roots
    carry the common synonyms.
    """
    if not section.roots:
        return None

    keys: set[str] = set()
    for sample in samples:
        keys |= {k for k, v in (sample or {}).items() if isinstance(v, dict | list)}

    for root in section.roots:  # in priority order
        for key in sorted(keys):
            if key.lower() == root:
                return key
    for root in section.roots:
        for key in sorted(keys):
            if difflib.SequenceMatcher(None, key.lower(), root).ratio() > 0.82:
                return key
    return None


def path_coverage(path: str, samples: list[dict]) -> tuple[int, int]:
    """How many samples actually contain this path? (present, total)"""
    present = sum(1 for s in samples if path in flatten(s))
    return present, len(samples)


def schema_agreement(samples: list[dict]) -> tuple[float, list[set[str]]]:
    """
    Do the samples share a structure?

    Returns the Jaccard similarity of their top-level key sets, and the sets. A low
    score means the caller mixed genuinely different schemas — for example a B2B and
    a B2C export from the same system — and one binding cannot serve both.
    """
    if len(samples) < 2:
        return 1.0, [set(s or {}) for s in samples]
    keysets = [set(s or {}) for s in samples]
    union = set().union(*keysets)
    common = set(keysets[0]).intersection(*keysets[1:])
    return (len(common) / len(union) if union else 1.0), keysets


def _render_section(section: Section, samples: list[dict], mode: Mode) -> list[str]:
    lines: list[str] = []
    indent = "  "

    if section.optional:
        lines.append("")
        lines += _wrap_comment(
            f"OPTIONAL — {section.note or 'omit this whole section if unused.'}", ""
        )
        lines.append(f"# {section.name}:")
        # Optional sections render commented out entirely.
        for f in section.fields:
            body = _render_field(f, [], section.name, indent)
            lines += [f"# {line}" if not line.startswith("#") else line for line in body]
        return lines

    lines.append("")
    if section.note:
        lines += _wrap_comment(section.note, "")
    lines.append(f"{section.name}:")

    if section.each:
        chosen = find_root(section, samples)
        lines.append(
            f"{indent}each: {chosen or '<your.array>'}"
            + ("  # iterate this array" if chosen else "  # TODO — which array?")
        )
        strip = f"{chosen}[]." if chosen else ""
        scope = f"{chosen}[]" if chosen else ""
    elif section.name == "invoice":
        # These fields live at the document root, not under an "invoice" key.
        strip, scope = "", ""
    else:
        root = find_root(section, samples)
        if root and root != section.name:
            lines.append(f"{indent}# found as '{root}' in your data")
        strip, scope = "", root or section.name

    for f in section.fields:
        lines += _render_field(f, samples, scope, indent, strip)
    return lines


def generate(
    samples: list[dict] | None = None,
    *,
    mode: Mode = Mode.MINIMAL,
    profile: str = "pint-ae",
) -> str:
    """Produce a binding file as text."""
    samples = samples or []
    out: list[str] = []

    out.append(f"# sahih binding — generated {date.today().isoformat()}")
    out.append(f"# mode: {mode.value}")
    if samples:
        out.append(f"# inferred from {len(samples)} sample document(s)")
    else:
        out.append("# blank template — no sample data was supplied")
    out.append("#")
    out.append("# Entries marked TODO need you. Entries marked GUESS need checking.")
    out.append("# Entries marked 'const:???' are DECISIONS — sahih will not infer these,")
    out.append("# and build() refuses to run until they are answered.")
    out.append("#")
    out.append("# NOT LISTED HERE, because sahih emits them for you:")
    for name, value in CONSTANTS.items():
        out.append(f"#   constant  {name:<28} = {value}")
    out.append("#")
    out.append("# NOT LISTED HERE, because the builder computes them from your lines.")
    out.append("# Supplying these is how totals drift out of agreement:")
    for name in DERIVED:
        out.append(f"#   derived   {name}")

    if len(samples) > 1:
        score, keysets = schema_agreement(samples)
        if score < 0.7:
            common = set(keysets[0]).intersection(*keysets[1:])
            out.append("#")
            out.append("# " + "=" * 74)
            out.append("# WARNING — YOUR SAMPLES DO NOT SHARE A STRUCTURE")
            out.append(
                f"# Top-level key agreement: {score:.0%}. Only {len(common)} key(s) common to all."
            )
            out.append("#")
            out.append("# One binding cannot serve two schemas. Every path below was taken from")
            out.append(
                "# whichever sample happened to match, so it will resolve to nothing for the"
            )
            out.append(
                "# others — and a field that resolves to nothing is a field silently dropped."
            )
            out.append("#")
            out.append("# Generate one binding per schema instead:")
            out.append("#     sahih init --from b2b/*.json  -o binding.b2b.yaml")
            out.append("#     sahih init --from b2c/*.json  -o binding.b2c.yaml")
            out.append("# " + "=" * 74)

    out.append("")
    out.append("version: 1")
    out.append(f"profile: {profile}")

    for section in sections_for(mode):
        out += _render_section(section, samples, mode)

    # Anything in the user's data we did not place.
    if samples:
        placed = set()
        for line in out:
            stripped = line.strip().lstrip("# ")
            if ":" in stripped and not stripped.startswith(("#", "version", "profile")):
                placed.add(stripped.split(":", 1)[1].split("#")[0].strip())
        unplaced = []
        for sample in samples:
            for path, kind in flatten(sample).items():
                leaf = path.split(".")[-1]
                if path not in placed and not any(path in p for p in placed) and leaf not in placed:
                    unplaced.append((path, kind))
        # dict preserves first-seen order and dedupes by path in one pass.
        rows = list(dict(sorted(unplaced)).items())
        if rows:
            out.append("")
            out.append("# UNPLACED — present in your data, not bound to anything.")
            out.append("# If one of these carries money or identity, it is being dropped.")
            for path, kind in rows:
                out.append(f"#   {path}  ({kind})")

    return "\n".join(out) + "\n"
