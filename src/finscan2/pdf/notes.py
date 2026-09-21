"""Note tables — the figures a model needs that the primary statements omit.

Almarai's D&A for the period is printed only in the segment note, so a model row
for it stays blank however good the caption matching is. Notes are therefore in
scope, but narrowly: a numbered note, its title, and the tables under it.

The segment table's shape is unlike a primary statement's. Its COLUMNS are
segments and its PERIODS are row headings:

        Dairy and Juice   Bakery   Protein   Other   Total
    30 June 2026
    Revenue                7,937,368  ...            12,469,479
    Total Assets          23,726,722  ...            42,808,312
    31 December 2025
    Total Assets          22,267,954  ...            39,966,898

So one note block yields one Statement PER PERIOD HEADING, each carrying the
segment columns. The period's span is taken from the filing's own cumulative
column ending on the same date — a note reports the period then ended, and the
primary statements already say how long that is.

Rows mix bases: `Revenue` is a flow over the period, `Total Assets` a balance on
its last day. That is not resolved here. The columns carry both an end date and a
span, and the mapping's `basis` decides which is used — so a balance row matches
on the date alone and is never de-cumulated.
"""
from __future__ import annotations

import re

from finscan2.pdf import repair
from finscan2.pdf.columns import parse_numeric_date
from finscan2.pdf.statements import looks_like_data_row, parse_row
from finscan2.schema import Column, Issue, Statement, StatementRow

#: "10. SEGMENT REPORTING", "9. EARNINGS PER SHARE (Continued…)"
_NOTE_HEADING = re.compile(r"^\s*(\d{1,2})\.\s+([A-Z][A-Z0-9 ,'\-&/()….]{4,})\s*$")

#: A period acting as a row heading: "30 June 2026", "31 December 2025".
_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")
_PERIOD_HEADING = re.compile(
    r"^\s*(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\s+(\d{4})\s*$", re.IGNORECASE)

#: The line naming the segments. Several capitalised words, no amounts.
_TOTAL_HEADER = re.compile(r"\btotal\b", re.IGNORECASE)

MIN_NOTE_ROWS = 2


def _period_heading(line: str) -> str | None:
    match = _PERIOD_HEADING.match(line)
    if not match:
        return None
    day, month, year = match.group(1), match.group(2).lower(), match.group(3)
    return parse_numeric_date(f"{day}/{_MONTHS.index(month) + 1}/{year}")


def note_blocks(text: str) -> list[tuple[int, str, list[str]]]:
    """(number, title, lines) for every numbered note on the page."""
    lines = text.splitlines()
    starts: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = _NOTE_HEADING.match(line)
        if match:
            starts.append((index, int(match.group(1)), match.group(2).strip()))

    blocks = []
    for position, (index, number, title) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        blocks.append((number, title, lines[index + 1:end]))
    return blocks


def _segment_names(lines: list[str]) -> list[str]:
    """The segment column names, gathered from the header lines above the data.

    Column-major again: "Dairy and Juice" is split across two lines with "Bakery"
    and the rest. Rather than reconstruct the geometry, the words of every header
    line before the first period heading are collected and split on the known
    anchor — the last name is always the total.
    """
    header: list[str] = []
    for line in lines:
        if _period_heading(line) or looks_like_data_row(line):
            break
        stripped = line.strip()
        if stripped and not re.fullmatch(r"[\$'0-9\s]+", stripped):
            header.append(stripped)
    return header


def extract_notes(page_no: int, text: str,
                  span_by_end: dict[str, int] | None = None
                  ) -> tuple[list[Statement], list[Issue]]:
    """Every note table on one page, and every figure that could not be trusted."""
    statements: list[Statement] = []
    issues: list[Issue] = []
    span_by_end = span_by_end or {}

    for number, title, lines in note_blocks(text):
        header_lines = _segment_names(lines)
        names = _has_total_column(header_lines)

        current_end: str | None = None
        current_rows: list[StatementRow] = []

        def flush(end: str | None, rows: list[StatementRow]) -> None:
            if not end or len(rows) < MIN_NOTE_ROWS:
                return
            months = span_by_end.get(end)
            width = max(len(r.values) for r in rows)

            # A row printing a dash for some segments parses short: "Share of
            # Results of Associate - - - (1,222) (1,222)" gives two values, and
            # leaving them at indices 0 and 1 puts the TOTAL under the first
            # segment. Filings right-align their columns, so a short row is missing
            # its leading entries — and the padding is only kept when the row still
            # adds up.
            kept: list[StatementRow] = []
            for row in rows:
                if len(row.values) < width:
                    padded = [None] * (width - len(row.values)) + list(row.values)
                    if not repair.totals_agree(padded, min_numbers=2):
                        issues.append(Issue(
                            code="note_row_unaligned", severity="warning", page=page_no,
                            message=f"Note {number}, '{row.caption}': {len(row.values)} "
                                    f"value(s) where the table has {width} columns, and "
                                    f"right-aligning them does not reproduce the printed "
                                    f"total. The row was not recorded."))
                        continue
                    row = StatementRow(caption=row.caption, values=padded, raw=row.raw)
                kept.append(row)
            if len(kept) < MIN_NOTE_ROWS:
                return
            rows = kept

            columns = _columns_for(names, width, months, end)
            statements.append(Statement(
                page=page_no, kind="other", note=number,
                title=f"Note {number}: {title}",
                heading=end, columns=columns, rows=list(rows)))
            if months is None:
                issues.append(Issue(
                    code="note_span_unknown", severity="warning", page=page_no,
                    message=f"Note {number} reports a period ending {end}, but no "
                            f"primary statement column ends there, so the span of "
                            f"that period is unknown. Flow rows from this note "
                            f"cannot be de-cumulated."))

        for line in lines:
            end = _period_heading(line)
            if end:
                flush(current_end, current_rows)
                current_end, current_rows = end, []
                continue
            if not looks_like_data_row(line):
                continue

            usable, was_repaired, why = repair.verified(line, _values_of)
            row = parse_row(usable)
            if row is None:
                continue
            if repair.looks_severed(line) and not was_repaired:
                issues.append(Issue(
                    code="note_row_unreliable", severity="warning", page=page_no,
                    message=f"Note {number}, '{row.caption}': {why}. The row was not "
                            f"recorded, because its figures cannot be trusted."))
                continue
            current_rows.append(row)

        flush(current_end, current_rows)

    return statements, issues


def _values_of(line: str) -> list[float | None]:
    row = parse_row(line)
    return row.values if row else []


def _has_total_column(header_lines: list[str]) -> bool:
    """True when the header's last word is "Total".

    The note header is column-major and split across lines, so reconstructing each
    segment's name from it is guesswork. One fact is reliable and is the only one
    used: whether the rightmost column is the total — which is the column a
    company-level model reads.
    """
    for line in header_lines:
        words = [w.strip(" *") for w in line.split() if w.strip()]
        if words and words[-1].lower() == "total":
            return True
    return False


def _columns_for(has_total: bool, width: int, months: int | None,
                 end: str) -> list[Column]:
    """Columns for a note table, naming only what is known.

    The total is placed by POSITION — the last column — not by counting header
    words, because the header's word count and the table's column count disagree
    whenever a segment name runs to two words.
    """
    names = [f"segment {i + 1}" for i in range(width)]
    if has_total and width:
        names[-1] = "Total"
    return [Column(index=i, header=names[i], months=months, end=end, kind="period")
            for i in range(width)]
