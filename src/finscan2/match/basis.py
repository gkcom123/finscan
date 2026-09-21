"""Turn a cumulative figure into a standalone-period one.

    n      = months_covered / cadence - 1
    value  = printed - sum(the model's own prior n columns of the same row)

A six-month figure into a quarterly model subtracts one prior column, nine months
subtracts two, a full year into Q4 subtracts three. Q1 gives n = 0 and is a no-op
with no special case.

The prior figures come from the workbook, not from a second reading of the filing:
the model's own record of what it already holds is the only number that makes the
column add up. When that record is missing or does not fit the fiscal year, this
refuses and the row is left blank — a plausible wrong figure in a financial model
is worse than an empty cell with an error beside it.
"""
from __future__ import annotations

from datetime import date

from finscan2.match.schema import Adjustment
from finscan2.schema import Issue


def periods_to_subtract(months: int, cadence_months: int) -> int | None:
    """How many prior columns a figure of `months` needs removed. None if unclean."""
    if cadence_months <= 0 or months <= 0:
        return None
    ratio = months / cadence_months
    if abs(ratio - round(ratio)) > 0.15:
        return None
    return max(0, round(ratio) - 1)


def fiscal_year_start(period_end: str, months: int) -> date:
    """The first day the cumulative figure covers."""
    end = date.fromisoformat(period_end)
    year, month = end.year, end.month - months + 1
    while month < 1:
        year, month = year - 1, month + 12
    return date(year, month, 1)


def prior_columns(period_dates: dict[int, str], period_end: str,
                  cadence_months: int, count: int) -> tuple[list[int], str | None]:
    """The `count` model columns immediately before `period_end`, newest last.

    Chosen by date, never by position: a model that keeps an older block to the
    right of the live one would otherwise contribute stale columns.
    """
    if count == 0:
        return [], None
    try:
        target = date.fromisoformat(period_end)
    except ValueError:
        return [], f"'{period_end}' is not a date"

    dated = []
    for col, iso in period_dates.items():
        try:
            dated.append((date.fromisoformat(iso), col))
        except (ValueError, TypeError):
            continue
    earlier = sorted(d for d in dated if d[0] < target)
    if len(earlier) < count:
        return [], (f"only {len(earlier)} dated column(s) exist before {period_end}; "
                    f"{count} needed to recover the standalone figure")

    chosen = earlier[-count:]

    # Every subtracted column must lie inside the same fiscal year as the figure,
    # or the subtraction silently absorbs a full-year or LTM column.
    window_start = fiscal_year_start(period_end, count * cadence_months + cadence_months)
    outside = [d.isoformat() for d, _ in chosen if d < window_start]
    if outside:
        return [], (f"column(s) dated {', '.join(outside)} fall outside the fiscal "
                    f"year beginning {window_start.isoformat()}")

    # And they must be one cadence apart, or a period is missing from the model.
    sequence = [d for d, _ in chosen] + [target]
    for earlier_date, later_date in zip(sequence, sequence[1:]):
        gap = round((later_date - earlier_date).days / 30.44)
        if abs(gap - cadence_months) > 1:
            return [], (f"columns dated {earlier_date} and {later_date} are {gap} month(s) "
                        f"apart, not the expected {cadence_months}")

    return [col for _, col in chosen], None


def decumulate(printed: float, prior_values: dict[int, float | None],
               columns: list[int], row: int,
               column_letters: dict[int, str] | None = None
               ) -> tuple[float | None, Adjustment | None, Issue | None]:
    """printed minus the prior columns, or a refusal naming what was missing."""
    letters = column_letters or {}
    missing = [c for c in columns if prior_values.get(c) is None]
    if missing:
        names = ", ".join(letters.get(c, str(c)) for c in missing)
        return None, None, Issue(
            code="decumulation_missing_prior", severity="error",
            message=f"row {row}: column(s) {names} hold no number, so the prior "
                    f"period(s) cannot be subtracted. Left blank.")

    subtracted = [{"column": letters.get(c, str(c)), "value": prior_values[c]}
                  for c in columns]
    total = sum(prior_values[c] or 0.0 for c in columns)
    value = printed - total

    detail = (f"de-cumulated: {printed:,.2f} - "
              + " - ".join(f"{s['column']} {s['value']:,.2f}" for s in subtracted)
              + f" = {value:,.2f}")
    adjustment = Adjustment(kind="decumulate", detail=detail, subtracted=subtracted)

    issue = None
    if abs(value) > abs(printed):
        issue = Issue(
            code="decumulation_implausible", severity="warning",
            message=f"row {row}: the standalone figure ({value:,.2f}) is larger than "
                    f"the cumulative one it came from ({printed:,.2f}). Written, but "
                    f"verify against the filing.")
    return value, adjustment, issue
