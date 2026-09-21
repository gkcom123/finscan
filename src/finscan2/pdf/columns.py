"""Parse a statement's column headers into month spans and end dates.

This is the piece v1 lacked, and the reason it could not tell a standalone quarter
from a year-to-date figure. An IAS 34 interim statement prints both under one
heading — "FOR THE THREE-MONTH AND SIX-MONTH PERIODS ENDED 30 JUNE 2026" — so the
basis of a value is a property of the column it was read from, never of the page.

Headers are routinely stacked across lines:

    April - June     April - June     January - June   January - June
    2026             2025             2026             2025
    (Unaudited)      (Unaudited)      (Unaudited)      (Unaudited)

so the block is flattened to one line before matching, and patterns allow any
whitespace between a month name and its year.
"""
from __future__ import annotations

import re
from calendar import monthrange
from datetime import date

from finscan2.schema import Column

MONTHS: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    # Spanish, for LatAm filings
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11,
    "diciembre": 12,
}

_MONTH_WORDS = "|".join(sorted(MONTHS, key=len, reverse=True))

MONTH_COUNT_WORDS: dict[str, int] = {
    "three": 3, "3": 3, "tres": 3,
    "six": 6, "6": 6, "seis": 6,
    "nine": 9, "9": 9, "nueve": 9,
    "twelve": 12, "12": 12, "doce": 12,
}

#: "April - June 2026", "January – September 2025". The span between the two
#: months inclusive is what the column accumulates: Apr-Jun = 3, Jan-Sep = 9.
_RANGE = re.compile(
    rf"\b({_MONTH_WORDS})\b\s*[-–—]\s*\b({_MONTH_WORDS})\b,?\s*(\d{{4}})",
    re.IGNORECASE,
)

#: "Three-month period ended 30 June 2026", "9 months ended 30 September 2025".
_MONTHS_ENDED = re.compile(
    r"\b(three|six|nine|twelve|3|6|9|12)[\s-]month?s?\b"
    rf"(?:[\s-]periods?)?\s*(?:ended|ending)\s*(\d{{1,2}})\s+\b({_MONTH_WORDS})\b,?\s*(\d{{4}})",
    re.IGNORECASE,
)

#: The same phrase with no date attached: "Three months ended" printed once above
#: two dated columns, which is how most filings lay out a comparative statement.
_MONTHS_PHRASE = re.compile(
    r"\b(three|six|nine|twelve|3|6|9|12)[\s-]month?s?\b"
    r"(?:[\s-]periods?)?\s*(?:ended|ending)\b",
    re.IGNORECASE,
)

#: A bare date: "30 September 2025", "31 December, 2024". A balance-sheet column.
_FULL_DATE = re.compile(
    rf"\b(\d{{1,2}})\s+\b({_MONTH_WORDS})\b,?\s*(\d{{4}})",
    re.IGNORECASE,
)

#: "Q2 2026", "2Q26", "2Q 2026".
_QUARTER = re.compile(r"\b(?:Q([1-4])\s*(\d{2,4})|([1-4])Q\s*(\d{2,4}))\b", re.IGNORECASE)


def _month_end(year: int, month: int) -> str:
    return date(year, month, monthrange(year, month)[1]).isoformat()


def _span(start_month: int, end_month: int) -> int:
    """Inclusive month span, wrapping a fiscal year that crosses December."""
    return (end_month - start_month) % 12 + 1


def flatten(text: str) -> str:
    """Header blocks are stacked across lines; matching needs them on one."""
    return re.sub(r"\s+", " ", (text or "").replace("|", " ")).strip()


def parse_columns(header_block: str) -> list[Column]:
    """Columns implied by a statement's header block, left to right.

    Tried in order of how much they tell us: an explicit month range, then an
    "N months ended" phrase, then a quarter label, then a bare date. The first
    pattern that matches decides, because a header mixing forms (a date line
    under a range line) would otherwise yield two columns per real column.
    """
    flat = flatten(header_block)
    if not flat:
        return []

    columns: list[Column] = []

    for m in _RANGE.finditer(flat):
        start, end, year = MONTHS[m.group(1).lower()], MONTHS[m.group(2).lower()], int(m.group(3))
        columns.append(Column(
            index=len(columns), header=m.group(0).strip(), kind="period",
            months=_span(start, end), end=_month_end(year, end),
        ))
    if columns:
        return columns

    # "Three months ended" printed once, above several dated columns. The phrase
    # supplies the span, the dates supply the columns. With two phrases over an
    # even number of dates ("three and nine months ended" above 3M, 3M, 9M, 9M),
    # the dates are split between them in printed order.
    phrases = [MONTH_COUNT_WORDS[m.group(1).lower()] for m in _MONTHS_PHRASE.finditer(flat)]
    dates = list(_FULL_DATE.finditer(flat))
    if phrases and dates and len(dates) % len(phrases) == 0:
        per_phrase = len(dates) // len(phrases)
        for position, m in enumerate(dates):
            months = phrases[position // per_phrase]
            month, year = MONTHS[m.group(2).lower()], int(m.group(3))
            columns.append(Column(
                index=len(columns), header=m.group(0).strip(), kind="period",
                months=months, end=_month_end(year, month),
            ))
        return columns

    for m in _MONTHS_ENDED.finditer(flat):
        months = MONTH_COUNT_WORDS[m.group(1).lower()]
        year, month = int(m.group(4)), MONTHS[m.group(3).lower()]
        columns.append(Column(
            index=len(columns), header=m.group(0).strip(), kind="period",
            months=months, end=_month_end(year, month),
        ))
    if columns:
        return columns

    for m in _QUARTER.finditer(flat):
        q = int(m.group(1) or m.group(3))
        raw_year = m.group(2) or m.group(4)
        year = int(raw_year) if len(raw_year) == 4 else 2000 + int(raw_year)
        columns.append(Column(
            index=len(columns), header=m.group(0).strip(), kind="period",
            months=3, end=_month_end(year, q * 3),
        ))
    if columns:
        return columns

    for m in _FULL_DATE.finditer(flat):
        day, month, year = int(m.group(1)), MONTHS[m.group(2).lower()], int(m.group(3))
        columns.append(Column(
            index=len(columns), header=m.group(0).strip(), kind="point_in_time",
            months=None, end=date(year, month, min(day, monthrange(year, month)[1])).isoformat(),
        ))
    return columns


#: "6 months as of 30/06/2026", "3 months as of 31/03/2026". Fibra Uno's form.
_MONTHS_AS_OF = re.compile(
    r"\b(three|six|nine|twelve|3|6|9|12)\s*months?\s*(?:as\s+of|ended|ending|to)\b",
    re.IGNORECASE,
)

#: "Second-quarter 2026 transactions", "first quarter 2026".
_ORDINAL_QUARTER = re.compile(
    r"\b(first|second|third|fourth|1st|2nd|3rd|4th|primer|segundo|tercer|cuarto)"
    r"[\s-]*quarter[\s-]*(\d{4})?",
    re.IGNORECASE,
)

_ORDINALS = {
    "first": 1, "1st": 1, "primer": 1,
    "second": 2, "2nd": 2, "segundo": 2,
    "third": 3, "3rd": 3, "tercer": 3,
    "fourth": 4, "4th": 4, "cuarto": 4,
}

#: A numeric date: 30/06/2026, 31-03-2026, 2026-06-30.
_NUMERIC_DATE = re.compile(r"\b(\d{1,4})[/.-](\d{1,2})[/.-](\d{2,4})\b")


def parse_numeric_date(text: str, day_first: bool | None = None) -> str | None:
    """ISO date from a numeric form, or None.

    Order is inferred from the values where it can be: a first field above 12 is a
    day, a second field above 12 is a month printed second. When both are 12 or
    under the caller's `day_first` decides — a statement's other columns usually
    settle it, since period ends cluster on the 30th and 31st.
    """
    match = _NUMERIC_DATE.search(text or "")
    if not match:
        return None
    a, b, c = (int(match.group(1)), int(match.group(2)), int(match.group(3)))

    if len(match.group(1)) == 4:                  # 2026-06-30
        year, month, day = a, b, c
    else:
        year = c if c > 99 else 2000 + c
        if a > 12:
            day, month = a, b
        elif b > 12:
            month, day = a, b
        elif day_first is False:
            month, day = a, b
        else:
            day, month = a, b                     # day-first is the wider convention
    if not (1 <= month <= 12):
        return None
    day = min(day, monthrange(year, month)[1])
    return date(year, month, day).isoformat()


def parse_single_column(header: str, day_first: bool | None = None) -> Column:
    """One column's own header text into a Column.

    Used when the layout pass has already separated the columns, so each string
    describes exactly one column and the ambiguity of a flattened block is gone.
    """
    text = flatten(header)

    months: int | None = None
    if m := _MONTHS_AS_OF.search(text):
        months = MONTH_COUNT_WORDS[m.group(1).lower()]
    elif m := _MONTHS_PHRASE.search(text):
        months = MONTH_COUNT_WORDS[m.group(1).lower()]

    end: str | None = None
    quarter = _ORDINAL_QUARTER.search(text)
    if quarter:
        months = months or 3
        year_text = quarter.group(2)
        if year_text:
            end = _month_end(int(year_text), _ORDINALS[quarter.group(1).lower()] * 3)

    if end is None:
        if m := _RANGE.search(text):
            start_month = MONTHS[m.group(1).lower()]
            end_month, year = MONTHS[m.group(2).lower()], int(m.group(3))
            months = months or _span(start_month, end_month)
            end = _month_end(year, end_month)
        elif m := _FULL_DATE.search(text):
            end = _month_end(int(m.group(3)), MONTHS[m.group(2).lower()])
            day = int(m.group(1))
            if day < 28:                          # a mid-month date is stated exactly
                end = date(int(m.group(3)), MONTHS[m.group(2).lower()], day).isoformat()
        elif iso := parse_numeric_date(text, day_first):
            end = iso
        elif m := _QUARTER.search(text):
            q = int(m.group(1) or m.group(3))
            raw_year = m.group(2) or m.group(4)
            year = int(raw_year) if len(raw_year) == 4 else 2000 + int(raw_year)
            months, end = months or 3, _month_end(year, q * 3)

    kind = "period" if months else "point_in_time"
    return Column(index=0, header=header.strip(), kind=kind, months=months, end=end)


#: Statements whose figures are flows over a period, as opposed to a position on a
#: date. Only on these does a date-only column header mean "year to date".
FLOW_STATEMENTS = {"income_statement", "comprehensive_income", "cash_flow", "equity"}


def _infer_fiscal_year_start(columns: list[Column]) -> int | None:
    """The fiscal year's first month, from any column that states its own span.

    "6 months as of 30/06/2026" fixes the year as starting in January; the same
    statement's date-only columns then inherit it.
    """
    # Only a cumulative column runs from the year's start. A three-month column is
    # a standalone quarter, and reading a fiscal year off it puts the year's start
    # at the quarter's start — which is how "As of 30/06/2026" became three months
    # instead of six on Fibra Uno's cash flow.
    for column in columns:
        if column.months and column.months > 3 and column.end:
            end_month = int(column.end[5:7])
            return (end_month - column.months) % 12 + 1
    return None


def parse_columns_from_headers(
    headers: list[str], *, statement_kind: str = "", fiscal_year_start_month: int = 1
) -> list[Column]:
    """Columns from per-column header strings produced by the layout pass."""
    # Settle day/month order once for the statement: if any column's numeric date
    # is unambiguous, every other column follows the same convention.
    day_first: bool | None = None
    for header in headers:
        if m := _NUMERIC_DATE.search(flatten(header)):
            if len(m.group(1)) != 4:
                if int(m.group(1)) > 12:
                    day_first = True
                    break
                if int(m.group(2)) > 12:
                    day_first = False
                    break

    columns: list[Column] = []
    for header in headers:
        column = parse_single_column(header, day_first)
        column.index = len(columns)
        columns.append(column)

    # On a flow statement a column headed only by a date ("As of 30/06/2026") is a
    # year-to-date figure, not a position: Fibra Uno's cash flow states the span
    # once in the page heading and never in the column. The span is therefore the
    # months from the fiscal year's start to that date — taken from a sibling
    # column that does state its own span, else from the caller's default.
    if statement_kind in FLOW_STATEMENTS:
        start = _infer_fiscal_year_start(columns) or fiscal_year_start_month
        for column in columns:
            if column.months is None and column.end:
                end_month = int(column.end[5:7])
                column.months = (end_month - start) % 12 + 1
                column.kind = "period"
    return columns


def dedupe_stacked(columns: list[Column]) -> list[Column]:
    """Drop a repeated header that a stacked block printed twice.

    Some filings restate the column dates on a second line ("30 June 2026" under
    "April - June 2026"); after flattening that reads as two columns with the same
    end date. Consecutive duplicates of header text and end date are collapsed.
    """
    out: list[Column] = []
    for col in columns:
        if out and out[-1].header.lower() == col.header.lower() and out[-1].end == col.end:
            continue
        col.index = len(out)
        out.append(col)
    return out
