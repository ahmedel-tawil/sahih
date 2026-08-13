"""
Exceptions.

Deliberately few. Every one of these means "sahih could not produce a verdict" —
which is categorically different from "the invoice is invalid". An invalid invoice
is a normal, expected result and comes back as a `ValidationReport`, never an
exception. If you catch one of these, something is wrong with the *setup*, not
with the document's compliance.
"""

from __future__ import annotations


class SahihError(Exception):
    """Base for everything sahih raises."""


class RuleSetError(SahihError):
    """
    A rule set could not be loaded or compiled.

    Usually a missing file, or an XSLT that Saxon rejects. Not a validation outcome.
    """


class ValidationError(SahihError):
    """
    A document could not be validated at all.

    Distinct from "the document is invalid". This means the transformation itself
    failed — malformed XML, an unreadable file, a Saxon dynamic error.
    """


class UnsafeDocumentError(ValidationError):
    """
    The document was rejected before validation on security grounds.

    Raised when input contains a DTD or entity declarations. Invoices arrive from
    counterparties, and XML entity expansion is a live attack surface (XXE,
    billion-laughs). A legitimate UBL invoice never needs a DTD, so refusing them
    costs nothing and closes the hole.
    """
