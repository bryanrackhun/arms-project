"""Template context shared by every ARMS page.

The workshop quotes every price in Myanmar Kyat (MMK). Rather than hard-coding
the currency symbol into each template, it is exposed here as ``currency`` so a
single constant in :mod:`core.services` stays the one source of truth, and every
screen - booking, invoices, payroll and the parts list - cannot drift apart.
"""

from .models import CURRENCY_CODE


def currency(request):
    """Add ``currency`` (the ISO code) to every template's context."""
    return {"currency": CURRENCY_CODE}