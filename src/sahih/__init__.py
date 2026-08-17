"""
sahih — validate Peppol / PINT e-invoices in Python, and explain why they failed.

صحيح — "valid", "sound", "authentic".

WHY THIS LIBRARY EXISTS
-----------------------
Peppol validation is a solved problem in Java and an unsolved one in Python. The
reason is narrow and technical: Schematron rule sets compile to XSLT 2.0, and
Python's lxml is built on libxslt, which only implements XSLT 1.0. So the entire
Python ecosystem has been unable to run the official rule sets.

sahih uses SaxonC-HE (via ``saxonche``) to clear that hurdle, then does the part
nobody else does — turning validation output into something a human can act on.

THE SHAPE OF THE PROBLEM
------------------------
An e-invoice is validated against *stacked* rule sets, not one:

    1. EN 16931          the European semantic core        (BR-*, BR-CO-*, BR-CL-*)
    2. PINT base         the international billing model
    3. Jurisdiction      country-specific rules            (e.g. ibr-*-ae for the UAE)

Each layer is versioned independently and each can fail for different reasons, so
findings stay tagged with the layer that produced them throughout.
"""

from .build import build
from .engine import RuleSet, Validator
from .exceptions import (
    RuleSetError,
    SahihError,
    UnsafeDocumentError,
    ValidationError,
)
from .explain import Explainer, Explanation, ExplanationSource, available_languages
from .model import (
    Address,
    Allowance,
    DeclaredTotals,
    IncompleteInvoiceError,
    Invoice,
    ItemType,
    LegalIdType,
    Line,
    ModelError,
    Party,
    VatCategory,
)
from .models import Finding, Severity, StackedReport, ValidationReport
from .rules import CatalogueStats, Rule, by_id, catalogue, stats
from .svrl import SVRLParseError, parse_svrl

__version__ = "0.1.0"

__all__ = [
    "Address",
    "Allowance",
    "CatalogueStats",
    "DeclaredTotals",
    "Explainer",
    "Explanation",
    "ExplanationSource",
    "Finding",
    "IncompleteInvoiceError",
    "Invoice",
    "ItemType",
    "LegalIdType",
    "Line",
    "ModelError",
    "Party",
    "Rule",
    "RuleSet",
    "RuleSetError",
    "SVRLParseError",
    "SahihError",
    "Severity",
    "StackedReport",
    "UnsafeDocumentError",
    "ValidationError",
    "ValidationReport",
    "Validator",
    "VatCategory",
    "__version__",
    "available_languages",
    "build",
    "by_id",
    "catalogue",
    "parse_svrl",
    "stats",
]
