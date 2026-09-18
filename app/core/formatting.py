"""Rendering figures for people to read.

Validation messages, query titles and reconciliation notes are quoted verbatim
into the query a state has to answer and into the report that goes to the
project's governance body. They are read by people, so they are formatted for
people.
"""

from __future__ import annotations

#: Above this, float formatting loses integer precision, so don't claim it.
_INTEGER_LIMIT = 1e15


def fmt(value: float | None) -> str:
    """Render a figure with thousands separators and no false precision.

    ``:g`` was the obvious choice and the wrong one: it switches to exponent
    form past six significant digits and rounds to six. A community-outreach
    figure in the millions came out of the cumulative-decrease rule as "fell
    from 1.02431e+06 to 1.02431e+06" -- unreadable, and identical on both sides
    of a comparison that was not.
    """
    if value is None or value != value:  # None, or NaN
        return "—"
    if float(value).is_integer() and abs(value) < _INTEGER_LIMIT:
        return f"{int(value):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")
