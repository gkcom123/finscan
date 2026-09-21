"""Accounting identities over the resolved values, before anything is written.

These catch the class of error no caption check can: every row matched a real line,
every column was the right period, and the set still does not add up. Almarai's tax
row understated by exactly the Zakat line for weeks — each figure individually
defensible, the subtotal wrong.

Deliberately warnings, not refusals. An identity fails whenever a component is
deliberately blank, which is a legitimate state in this pipeline, so a failure is
something to read in the report rather than a reason to withhold a whole column.
Each failure names the rows and the gap, so it can be checked in seconds.
"""
from __future__ import annotations

from dataclasses import dataclass

from finscan2.match.schema import Values
from finscan2.schema import Issue

#: Relative tolerance. Filings round; a rounding difference is not a finding.
TOLERANCE = 0.005


@dataclass
class Identity:
    """`total` should equal the sum of `parts`, by caption."""

    name: str
    total: str
    parts: tuple[str, ...]


#: Matched on normalized workbook captions, so they travel across companies. A
#: company whose captions differ simply has no check here, which is honest — a
#: silently skipped identity is better than one asserted against the wrong rows.
IDENTITIES = (
    Identity("profit after tax", "profit for the year",
             ("profit before zakat and income tax", "zakat and income tax")),
    Identity("gross profit", "gross profit", ("revenue", "cost of sales")),
    Identity("attributable profit", "profit for the year",
             ("profit loss attributable to shareholders",
              "profit loss attributable to non controlling interests")),
)


def _by_label(values: Values) -> dict[str, float]:
    from finscan2.model.schema import normalize_label

    return {normalize_label(v.label): v.value for v in values.values
            if v.ok and v.value is not None}


def check(values: Values) -> list[Issue]:
    """Every identity whose rows are all present, and whether each holds."""
    figures = _by_label(values)
    issues: list[Issue] = []

    for identity in IDENTITIES:
        if identity.total not in figures:
            continue
        missing = [p for p in identity.parts if p not in figures]
        if missing:
            continue

        total = figures[identity.total]
        parts = [figures[p] for p in identity.parts]
        # Expenses may be stored negative and added, or positive and subtracted;
        # both conventions are tested so a model's own sign habit is not a failure.
        added = sum(parts)
        subtracted = parts[0] - sum(parts[1:])
        gap = min(abs(total - added), abs(total - subtracted))
        scale = max(abs(total), 1.0)
        if gap / scale <= TOLERANCE:
            continue

        detail = " + ".join(f"{p:,.2f}" for p in parts)
        issues.append(Issue(
            code="identity_failed", severity="warning",
            message=f"{identity.name}: '{identity.total}' is {total:,.2f} but its parts "
                    f"give {added:,.2f} ({detail}) — a gap of {abs(total - added):,.2f}. "
                    f"Check the rows {', '.join(identity.parts)}."))

    return issues


def coverage(values: Values) -> Issue | None:
    """A run that resolved almost nothing is a configuration failure, not a filing."""
    total = len(values.values)
    if not total:
        return Issue(code="nothing_to_resolve", severity="error",
                     message="The mapping produced no rows to resolve.")
    resolved = len(values.resolved)
    if resolved / total < 0.5:
        return Issue(
            code="low_coverage", severity="warning",
            message=f"Only {resolved} of {total} mapped rows resolved. That usually "
                    f"means the filing's captions have changed or the wrong statement "
                    f"was matched — re-run learn before trusting this column.")
    return None
