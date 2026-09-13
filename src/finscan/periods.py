"""Does this filing actually belong in the next column?

The failure this prevents: a model whose last quarterly column is Jun-2025, and
a filing for Mar-2026. Appending "the next column" silently books Q1-26 numbers
into the Sep-25 slot, and every ratio, LTM and growth figure downstream is then
wrong in a way no arithmetic check can see — the numbers are internally
consistent, they are just in the wrong place.

So the column's own date header is compared against the period the PDF reports,
and a gap is a blocking error rather than a warning.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

CADENCE_DAYS = {"quarter": 91, "half_year": 182, "nine_months": 273, "year": 365}
TOLERANCE_DAYS = 20        # month-end drift, 52/53-week years, 4-4-5 calendars


def parse_date(value: str | date | datetime | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y", "%d-%b-%Y", "%d %B %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return None


def infer_cadence_days(dates: list[date]) -> int | None:
    """Spacing of the existing columns, taken as the model's own cadence."""
    if len(dates) < 2:
        return None
    ordered = sorted(dates)
    gaps = [(b - a).days for a, b in zip(ordered, ordered[1:]) if 20 < (b - a).days < 400]
    if not gaps:
        return None
    gaps.sort()
    return gaps[len(gaps) // 2]          # median resists a one-off irregular gap


@dataclass
class PeriodCheck:
    ok: bool
    code: str
    message: str
    last_column_date: date | None = None
    expected_date: date | None = None
    reported_date: date | None = None
    periods_off: float | None = None


def check_continuity(
    period_dates: dict[int, str],
    reference_col: int,
    reported_period_end: str | None,
    period_type: str = "quarter",
) -> PeriodCheck:
    """Compare the filing's period end against the slot it would land in."""
    reported = parse_date(reported_period_end)
    known = {c: parse_date(d) for c, d in period_dates.items()}
    known = {c: d for c, d in known.items() if d}

    if not known:
        return PeriodCheck(True, "no_column_dates",
                           "The sheet has no dated period headers, so continuity could not be "
                           "checked. Confirm the target column by eye.")
    if reported is None:
        return PeriodCheck(True, "no_reported_date",
                           "The filing did not state a period end date, so continuity could not "
                           "be checked. Confirm the target column by eye.")

    last_date = known.get(reference_col) or max(known.values())
    cadence = infer_cadence_days(list(known.values())) or CADENCE_DAYS.get(period_type, 91)
    expected = _add_days(last_date, cadence)
    drift = (reported - expected).days
    periods_off = drift / cadence

    if abs(drift) <= TOLERANCE_DAYS:
        return PeriodCheck(True, "period_continuous",
                           f"Filing period {reported} follows the last column ({last_date}).",
                           last_date, expected, reported, 0.0)

    if reported <= last_date:
        return PeriodCheck(
            False, "period_already_present",
            f"The filing is for {reported}, but the model already runs to {last_date}. "
            f"This period is not new — appending it would duplicate or back-date data.",
            last_date, expected, reported, periods_off)

    missing = round(periods_off)
    return PeriodCheck(
        False, "period_gap",
        f"The filing is for {reported}, but the next column after {last_date} is {expected}. "
        f"About {missing} period(s) are missing from the model. Writing here would book "
        f"{reported:%b-%Y} data into the {expected:%b-%Y} slot. Fill the gap first, or point "
        f"FinScan at the right column.",
        last_date, expected, reported, periods_off)


def months_from_cadence_days(days: int) -> int:
    """Snap a measured day-count cadence to the nearest sane period length in months.

    A leap year makes a measured quarterly cadence 366 days instead of ~91; snapping to
    whole months first is what keeps period arithmetic sane (see _add_days below), and the
    same snapping is reused by excel/cumulative.py to turn a filing's own "months ended"
    disclosure into a number of periods relative to this model's own quarter length.
    """
    return min((1, 3, 6, 9, 12), key=lambda m: abs(days / 30.44 - m))


def _add_days(d: date, days: int) -> date:
    """Advance one period.

    Done in calendar months, not days: a leap year makes the measured cadence
    366 days, and adding that to 31-Dec lands on 1-Jan of the year after next.
    So the day count is snapped to the nearest sane period length first.
    """
    from calendar import monthrange

    months = months_from_cadence_days(days)
    total = d.month - 1 + months
    year, month = d.year + total // 12, total % 12 + 1
    last_day = monthrange(year, month)[1]
    day = last_day if d.day == monthrange(d.year, d.month)[1] else min(d.day, last_day)
    return date(year, month, day)
