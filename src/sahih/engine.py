"""
The validation engine — where Saxon actually runs.

THE ONE PERFORMANCE FACT THAT SHAPES THIS MODULE
------------------------------------------------
Measured on the real artefacts:

    compiling a rule set     110 – 900 ms
    validating an invoice      1 –   9 ms

Compilation is 100–500x more expensive than validation. So the entire design is
"compile once, validate many". `RuleSet` holds a compiled stylesheet; `Validator`
holds a set of them and reuses them for every document. Anything that recompiles
per-invoice is doing it wrong by two orders of magnitude.

THREADING
---------
SaxonC binds to a JVM-like runtime with per-thread state. A `Validator` is *not*
thread-safe: build one per thread, or serialise access. If you use one from a
worker thread pool, call `detach_thread()` as each thread finishes so Saxon can
release its thread-local resources.

SECURITY BOUNDARY
-----------------
Saxon parses XML with its own parser, which is not `defusedxml`. Invoices come from
counterparties, so every document is pre-flighted through defused parsing before
Saxon is allowed near it (see `_assert_safe`). A legitimate UBL invoice never
contains a DTD, so rejecting them closes XXE and billion-laughs at zero cost.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from types import TracebackType

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException
from saxonche import PySaxonProcessor

from .exceptions import RuleSetError, UnsafeDocumentError, ValidationError
from .models import StackedReport, ValidationReport
from .svrl import parse_svrl


def _assert_safe(xml_bytes: bytes, *, source: str) -> None:
    """
    Reject documents that carry a DTD or entity declarations, before Saxon sees them.

    `defusedxml` raises on exactly the constructs we care about (external entities,
    entity expansion bombs, external DTD references). We parse only to trigger those
    checks and throw the tree away — Saxon does the real parse afterwards.

    Malformed-but-safe XML is deliberately *not* rejected here. That is a validation
    failure, and Saxon reports it with far better diagnostics than we could.
    """
    try:
        DefusedET.fromstring(xml_bytes)
    except DefusedXmlException as exc:
        raise UnsafeDocumentError(
            f"Refusing to validate {source}: document contains a DTD or entity "
            f"declarations, which a legitimate UBL invoice never needs ({exc})."
        ) from exc
    except Exception:
        # Not well-formed, or some other parse problem. Let Saxon produce the
        # diagnostic — it points at a line and column, which we cannot.
        return


class RuleSet:
    """
    One layer of validation — a name and a compiled stylesheet.

    The name is not decoration. It is stamped onto every `Finding` this rule set
    produces, and it is how a caller tells "your arithmetic is wrong" (EN 16931)
    from "your arithmetic is fine but you broke a UAE rule" (jurisdiction).

    Compilation is lazy: constructing a `RuleSet` is cheap, and the cost is paid on
    first use or when `Validator` warms it up.
    """

    __slots__ = ("_executable", "name", "path")

    def __init__(self, name: str, path: str | Path) -> None:
        self.name = name
        self.path = Path(path)
        self._executable = None

    def compile(self, processor: PySaxonProcessor) -> None:
        """
        Compile the stylesheet. Idempotent — calling twice is a no-op.

        Raises:
            RuleSetError: if the file is missing or Saxon rejects the XSLT.
        """
        if self._executable is not None:
            return

        if not self.path.is_file():
            raise RuleSetError(f"Rule set '{self.name}': no such file: {self.path}")

        try:
            xslt_processor = processor.new_xslt30_processor()
            self._executable = xslt_processor.compile_stylesheet(stylesheet_file=str(self.path))
        except Exception as exc:
            raise RuleSetError(
                f"Rule set '{self.name}' failed to compile ({self.path}): {exc}"
            ) from exc

        if self._executable is None:
            raise RuleSetError(f"Rule set '{self.name}' compiled to nothing ({self.path}).")

    def run(self, source_file: Path, *, processor: PySaxonProcessor) -> ValidationReport:
        """
        Validate one document against this rule set.

        Returns:
            A `ValidationReport` tagged with this rule set's name.

        Raises:
            ValidationError: if the transformation itself fails.
        """
        self.compile(processor)
        assert self._executable is not None  # compile() guarantees this or raises

        try:
            svrl = self._executable.transform_to_string(source_file=str(source_file))
        except Exception as exc:
            raise ValidationError(
                f"Rule set '{self.name}' could not process {source_file.name}: {exc}"
            ) from exc

        if svrl is None:
            raise ValidationError(
                f"Rule set '{self.name}' produced no output for {source_file.name}. "
                "The stylesheet may not be a Schematron-derived validator."
            )

        return parse_svrl(svrl, ruleset=self.name, source=source_file.name)

    @property
    def is_compiled(self) -> bool:
        return self._executable is not None

    def __repr__(self) -> str:
        state = "compiled" if self.is_compiled else "not compiled"
        return f"RuleSet(name={self.name!r}, path={str(self.path)!r}, {state})"


class Validator:
    """
    Validate invoices against a stack of rule sets.

    Typical use — note that rule set *order* is preserved in the report, so put the
    layers in the order you want them read (core first, jurisdiction last):

        with Validator([
            RuleSet("EN16931", "en16931.xslt"),
            RuleSet("PINT base", "pint-base.xslt"),
            RuleSet("PINT-AE 2026.5", "pint-ae.xslt"),
        ]) as validator:
            report = validator.validate("invoice.xml")
            print(report.is_valid)

    Use it as a context manager where you can. Saxon holds native resources, and the
    context manager guarantees they are released. If you cannot, call `close()`.
    """

    def __init__(self, rulesets: Iterable[RuleSet]) -> None:
        self.rulesets: tuple[RuleSet, ...] = tuple(rulesets)
        if not self.rulesets:
            raise RuleSetError("A Validator needs at least one rule set.")

        names = [r.name for r in self.rulesets]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            # Findings are attributed by rule set name, so duplicates would make
            # a report ambiguous about which layer failed.
            raise RuleSetError(f"Rule set names must be unique; duplicated: {sorted(duplicates)}")

        self._processor: PySaxonProcessor | None = PySaxonProcessor(license=False)

    @classmethod
    def from_paths(cls, rulesets: Mapping[str, str | Path]) -> Validator:
        """Convenience constructor: ``{"EN16931": "en16931.xslt", ...}``."""
        return cls(RuleSet(name, path) for name, path in rulesets.items())

    @property
    def _proc(self) -> PySaxonProcessor:
        if self._processor is None:
            raise ValidationError("This Validator has been closed.")
        return self._processor

    def warm_up(self) -> None:
        """
        Compile every rule set now rather than on first use.

        Worth calling explicitly in a server: it moves ~1 second of compilation out
        of the first request and into startup, where nobody is timing it.
        """
        for ruleset in self.rulesets:
            ruleset.compile(self._proc)

    def validate(self, source: str | Path) -> StackedReport:
        """
        Validate one document against every rule set, in order.

        All layers run even if an earlier one fails. That is deliberate: a caller
        fixing an invoice wants the complete picture, not one error at a time.

        Raises:
            ValidationError: the file is unreadable, or a transformation failed.
            UnsafeDocumentError: the document carries a DTD or entity declarations.
        """
        path = Path(source)
        if not path.is_file():
            raise ValidationError(f"No such file: {path}")

        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ValidationError(f"Could not read {path}: {exc}") from exc

        _assert_safe(raw, source=path.name)

        layers = [ruleset.run(path, processor=self._proc) for ruleset in self.rulesets]
        return StackedReport(layers=tuple(layers), source=path.name)

    def detach_thread(self) -> None:
        """Release Saxon's thread-local state. Call as a worker thread finishes."""
        if self._processor is not None:
            self._processor.detach_current_thread()

    def close(self) -> None:
        """Release the Saxon processor. Idempotent."""
        if self._processor is not None:
            self._processor.__exit__(None, None, None)
            self._processor = None

    def __enter__(self) -> Validator:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "open" if self._processor is not None else "closed"
        return f"Validator({len(self.rulesets)} rule sets, {state})"
