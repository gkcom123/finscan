"""A filing published as an Excel workbook, read into a PdfDoc.

Some issuers file their statements as a workbook: one sheet per statement, the
title in the first rows, the column headers below it, then one line per row.
PTT Exploration and Production's `FINANCIAL_STATEMENTS.XLSX` is one: its balance
sheet, income statements (three- and six-month) and cash flow each fill a sheet,
in US dollars and Thai baht side by side.

The cell values are the filing's own figures, so nothing here is read by a model
or verified against a page. The layout is read, not assumed:

* a statement's title is the lines of text above its header block;
* the header block is every row between the title and the first line carrying
  figures; a merged header cell ("Unit: Thousand US Dollar") applies to every
  column it spans;
* a statement printed over several pages of one sheet repeats its title and
  header, and is joined; a different statement on the same sheet starts a new one;
* a column named "Note(s)" is not a figure column.

The result is the same PdfDoc the PDF readers build, so mappings, period
selection and de-cumulation work unchanged. Both currencies are kept, with the
currency in each column's header: a mapping row picks one with `"column": "US
Dollar"`.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from finscan.model.workbook import open_workbook
from finscan.pdf.columns import parse_single_column
from finscan.pdf.statements import classify
from finscan.schema import Column, Issue, PdfDoc, Statement, StatementRow

_STATEMENT_TITLE = re.compile(
    r"\bstatements?\s+of\s+(?:financial\s+position|income|comprehensive\s+income|"
    r"profit\s+or\s+loss|cash\s+flows?|changes\s+in\s+equity)\b|\bbalance\s+sheets?\b",
    re.IGNORECASE)
_NOTES_HEADER = re.compile(r"^\s*notes?\s*$", re.IGNORECASE)
_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")
_UNIT = re.compile(r"\b(thousand|million|billion)s?\b", re.IGNORECASE)
_UNIT_NAME = {"thousand": "thousands", "million": "millions", "billion": "billions"}

#: A header ends the title block this many lines up: company, statement, period.
_TITLE_LINES = 3


def is_workbook(path: str | Path) -> bool:
    return str(path).lower().endswith((".xlsx", ".xlsm"))


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _year_like(value: Any) -> bool:
    return _number(value) and float(value).is_integer() and 1990 <= value <= 2100


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()


def _cells(worksheet) -> list[tuple[int, dict[int, Any]]]:
    rows = []
    for row in worksheet.iter_rows():
        cells = {c.column: (_text(c.value) if isinstance(c.value, str) else c.value)
                 for c in row if c.value is not None
                 and not (isinstance(c.value, str) and not c.value.strip())}
        rows.append((row[0].row, cells))
    return rows


def _merged_text(worksheet) -> dict[tuple[int, int], str]:
    """Every cell a merged range covers, mapped to the text its top-left cell holds."""
    out: dict[tuple[int, int], str] = {}
    for rng in worksheet.merged_cells.ranges:
        top = worksheet.cell(rng.min_row, rng.min_col).value
        if top is None:
            continue
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                out[(r, c)] = _text(top) if isinstance(top, str) else str(top)
    return out


def _is_title(lines: list[str]) -> bool:
    return any(_STATEMENT_TITLE.search(line) for line in lines)


def _is_data(cells: dict[int, Any]) -> bool:
    """A labelled line carrying a figure that is not merely a year."""
    has_label = any(isinstance(v, str) for v in cells.values())
    return has_label and any(_number(v) and not _year_like(v) for v in cells.values())


class _Block:
    def __init__(self, title_lines: list[str]):
        self.title_lines = title_lines
        self.header: list[tuple[int, dict[int, Any]]] = []
        #: ("heading", text) or ("row", row number, cells)
        self.body: list[tuple] = []


def _blocks(rows: list[tuple[int, dict[int, Any]]]) -> list[_Block]:
    blocks: list[_Block] = []
    current: _Block | None = None
    in_header = False
    pending: list[str] = []          # consecutive lines of text in column A alone

    def title_of(lines: list[str]) -> list[str]:
        return lines[-_TITLE_LINES:]

    for number, cells in rows:
        if not cells:
            continue
        only_a = list(cells) == [1] and isinstance(cells[1], str)
        if only_a:
            pending.append(cells[1])
            continue
        data = _is_data(cells)
        if data:
            if current is None:
                continue
            in_header = False
            current.body.extend(("heading", text) for text in pending)
            pending = []
            current.body.append(("row", number, cells))
            continue
        if in_header and current is not None:
            current.header.append((number, cells))
            continue
        # Not a figure line, not alone in column A: a new title's header block, or a
        # heading printed in another column.
        if pending and _is_title(pending) and (current is None
                                               or _is_title(title_of(pending))):
            current = _Block(title_of(pending))
            blocks.append(current)
            pending = []
            in_header = True
            current.header.append((number, cells))
            continue
        if current is not None:
            current.body.extend(("heading", text) for text in pending)
            pending = []
            text = next((v for v in cells.values() if isinstance(v, str)), None)
            if text and not any(_number(v) for v in cells.values()):
                current.body.append(("heading", text))
    return blocks


def _scope(header_text: str) -> str:
    if re.search(r"\bconsolidated\b", header_text, re.IGNORECASE):
        return "Consolidated"
    if re.search(r"\b(separate|the company)\b", header_text, re.IGNORECASE):
        return "Separate"
    return ""


def _period(title: str) -> Column:
    return parse_single_column(title)


def _columns(block: _Block, value_cols: list[int], merged: dict[tuple[int, int], str],
             title: str) -> list[Column]:
    period = _period(title)
    out: list[Column] = []
    for index, col in enumerate(value_cols):
        parts: list[str] = []
        for number, cells in block.header:
            value = cells.get(col)
            if value is None:
                value = merged.get((number, col))
            if value is None:
                continue
            parts.append(str(int(value)) if _year_like(value) else _text(value))
        header = " ".join(dict.fromkeys(parts))
        own = parse_single_column(header)
        if own.end:
            months = own.months or (period.months if period.kind == "period" else None)
            kind, end = ("period" if months else "point_in_time"), own.end
        elif (year := _YEAR.search(header)) and period.end:
            end = date.fromisoformat(period.end).replace(year=int(year.group(1))).isoformat()
            months, kind = period.months, period.kind
        else:
            months, kind, end = None, "point_in_time", None
        out.append(Column(index=index, header=header, kind=kind, months=months, end=end))
    return out


def _statement(ordinal: int, sheet: str, block: _Block,
               merged: dict[tuple[int, int], str]) -> Statement | None:
    header_text = " ".join(_text(v) for _, cells in block.header for v in cells.values()
                           if isinstance(v, str))
    notes = {col for _, cells in block.header for col, v in cells.items()
             if isinstance(v, str) and _NOTES_HEADER.match(v)}
    rows = [item for item in block.body if item[0] == "row"]
    counts = Counter(col for _, _, cells in rows for col, v in cells.items()
                     if _number(v) and not _year_like(v) and col not in notes)
    value_cols = sorted(col for col, n in counts.items() if n >= 2 or len(rows) < 2)
    if not value_cols:
        return None
    first_value = value_cols[0]

    lines = block.title_lines
    title = " ".join(([_scope(header_text)] if _scope(header_text) else [])
                     + (lines[1:] if len(lines) >= _TITLE_LINES else lines))
    columns = _columns(block, value_cols, merged, title)

    out_rows: list[StatementRow] = []
    last_heading = ""
    previous_was_heading = False
    for item in block.body:
        if item[0] == "heading":
            last_heading, previous_was_heading = item[1], True
            continue
        _, number, cells = item
        label_cell = next(((c, v) for c, v in sorted(cells.items())
                           if isinstance(v, str) and c < first_value), None)
        if label_cell is None:
            previous_was_heading = False
            continue
        caption = label_cell[1]
        heading = last_heading if previous_was_heading else ""
        raw_label = label_cell[1]
        values = [cells.get(col) if _number(cells.get(col)) else None for col in value_cols]
        # A caption wrapped over two lines: "Rights to receive reimbursement from" /
        # "   decommissioning funds". The continuation is printed indented.
        if heading and raw_label[:1].islower():
            caption = f"{heading} {caption}"
        out_rows.append(StatementRow(caption=caption, values=values,
                                     raw=f"{sheet}!{number}", heading=heading))
        previous_was_heading = False
    if not out_rows:
        return None

    unit = _UNIT.search(header_text)
    return Statement(page=ordinal, kind=classify(title), title=title, heading=sheet,
                     columns=columns, rows=out_rows,
                     units=_UNIT_NAME[unit.group(1).lower()] if unit else None)


def is_filing_workbook(path: str | Path) -> bool:
    """True for a workbook of statements, False for an analyst's model.

    A model has sheets of its own (a `Model` sheet); a filing's sheets open with a
    statement title in column A.
    """
    try:
        book = open_workbook(path, data_only=True, read_only=True)
    except Exception:
        return False
    try:
        if any(ws.title.strip().lower() == "model" for ws in book.worksheets):
            return False
        for ws in book.worksheets:
            lines = []
            for row in ws.iter_rows(min_row=1, max_row=6, max_col=1, values_only=True):
                if row and isinstance(row[0], str):
                    lines.append(_text(row[0]))
            if _is_title(lines):
                return True
        return False
    finally:
        book.close()


def read_workbook(path: str | Path, sha: str) -> PdfDoc:
    """Read a filing workbook into the statements a PDF reader would produce."""
    book = open_workbook(path, data_only=True)
    doc = PdfDoc(path=str(path), sha256=sha)
    try:
        for ordinal, worksheet in enumerate(book.worksheets, start=1):
            if worksheet.sheet_state != "visible":
                continue
            merged = _merged_text(worksheet)
            try:
                blocks = _blocks(_cells(worksheet))
            except Exception as error:     # a sheet that is not a statement
                doc.issues.append(Issue(
                    code="sheet_unreadable", severity="warning", page=ordinal,
                    message=f"Sheet {worksheet.title!r} could not be read: {error}"))
                continue
            seen: dict[str, Statement] = {}
            for block in blocks:
                statement = _statement(ordinal, worksheet.title, block, merged)
                if statement is None:
                    continue
                earlier = seen.get(statement.title)
                if earlier is not None and ([c.header for c in earlier.columns]
                                            == [c.header for c in statement.columns]):
                    # The same statement continued on the next printed page.
                    earlier.rows.extend(statement.rows)
                    continue
                seen[statement.title] = statement
                doc.statements.append(statement)
    finally:
        book.close()

    unit = Counter(s.units for s in doc.statements if s.units).most_common(1)
    doc.units = unit[0][0] if unit else None
    if doc.statements:
        doc.issues.append(Issue(
            code="workbook_filing", severity="info",
            message=f"Read from the workbook {Path(path).name}: {len(doc.statements)} "
                    f"statement(s). Every figure is the filing's own cell value; no "
                    f"page was read or verified."))
    else:
        doc.issues.append(Issue(
            code="no_statements", severity="error",
            message=f"{Path(path).name} holds no sheet that reads as a financial statement."))
    return doc
