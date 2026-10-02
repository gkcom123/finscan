"""Find the statements on a page and read them into captions and numbers.

Deliberately conservative: a row is recorded with whatever numbers its printed line
carries, in printed order, and nothing is inferred. Stage 3 decides which column it
wants; stage 1's job is to preserve the page faithfully, including rows whose value
count does not match the header count, because that mismatch is itself evidence.
"""
from __future__ import annotations

import re
from collections import Counter

from finscan2.pdf.columns import (
    dedupe_stacked,
    parse_columns,
    parse_columns_from_headers,
)
from finscan2.pdf.layout import TableLayout, table_layout
from finscan2.model.schema import normalize_label
from finscan2.schema import Column, Statement, StatementKind, StatementRow

#: Title -> statement kind. Ordered: the first match wins, so the more specific
#: phrases must come first ("comprehensive income" before "income").
_TITLE_PATTERNS: list[tuple[str, StatementKind]] = [
    (r"comprehensive\s+income", "comprehensive_income"),
    (r"cash\s*flows?", "cash_flow"),
    (r"changes\s+in\s+equity|statement\s+of\s+equity"
     # A trust/REIT reports the same roll-forward under its own capital account
     # name instead of "equity" — Fibra Uno's is "Changes in Trustors'/
     # Beneficiaries' Capital". Gated safely: this whole table is only checked
     # against a line already matching `_TITLE_LINE` ("statement(s) of ...").
     r"|changes\s+in\s+.*\bcapital\b", "equity"),
    (r"financial\s+position|balance\s+sheet", "balance_sheet"),
    (r"profit\s+or\s+loss|income\s+statement|statements?\s+of\s+(?:income|operations)"
     r"|profit\s+and\s+loss|results\s+of\s+operations"
     # India's SEBI format: "Statement of Audited Consolidated Financial Results for
     # the quarter ended ..." is the profit-and-loss table of a results release.
     r"|financial\s+results", "income_statement"),
]

_TITLE_LINE = re.compile(
    r"^[^\n]*(?:\b(?:statement|statements)\s+of\b"
    r"|\b(?:income\s+statements?|balance\s+sheets?)\b)[^\n]*$",
    re.IGNORECASE,
)

#: The accumulation heading that usually sits directly under the title.
_HEADING_LINE = re.compile(
    r"^[^\n]*\b(?:for\s+the|as\s+at|as\s+of|period|periods|year|quarter)\b[^\n]*$",
    re.IGNORECASE,
)

#: A printed number: optional parentheses (negative), thousands separators,
#: optional decimals. A leading currency symbol is tolerated and discarded.
_NUMBER = re.compile(r"\(\s*-?[\d][\d,]*(?:\.\d+)?\s*\)|-?\d[\d,]*(?:\.\d+)?")

_SIGN_MARKER = re.compile(r"^\s*[+-]\s*(?:\(\s*[-+]\s*\)\s*)?(?=[A-Za-z])")

_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\s*\d{2}\b")

_NUMBER_OR_DASH = re.compile(_NUMBER.pattern + r"|(?<!\S)-(?!\S)")

#: A statement has a body. Fewer captioned rows than this and the page is a table
#: of contents, an index, or a stray mention of a statement's name — all of which
#: match a title pattern and would otherwise be recorded as empty statements.
MIN_STATEMENT_ROWS = 3

#: Rows whose caption is really a section heading carry no figures of their own.
_NOISE_CAPTION = re.compile(
    r"^\s*(?:note|notes|page|the\s+accompanying|these\s+condensed)\b", re.IGNORECASE
)


def classify(title: str) -> StatementKind:
    low = (title or "").lower()
    for pattern, kind in _TITLE_PATTERNS:
        if re.search(pattern, low):
            return kind
    return "other"


def parse_number(token: str) -> float | None:
    """Printed token to float. Parentheses mean negative, as does a trailing minus."""
    t = token.strip()
    if not t:
        return None
    negative = t.startswith("(") and t.endswith(")")
    t = t.strip("()").strip()
    if t.endswith("-"):
        negative, t = True, t[:-1].strip()
    t = t.replace(",", "").replace("−", "-")
    if t.startswith("-"):
        negative, t = True, t[1:]
    if not t or not re.fullmatch(r"\d+(?:\.\d+)?", t):
        return None
    value = float(t)
    return -value if negative else value


def parse_row(line: str, keep_first: bool = False) -> StatementRow | None:
    """Split a printed line into its caption and its numbers, in printed order."""
    raw = line.rstrip()
    # "+ (-) Adjustments for ...", "- Purchase of property ...": the BMV report
    # marks each line's sign convention in front of its caption. A leading "-"
    # would otherwise read as a dash figure and leave the caption empty.
    body = _SIGN_MARKER.sub("", raw).replace("|", " ")
    # A line printing ISO dates is a column header ("Concept | Quarter Current Year
    # | 2026-04-01 - 2026-06-30"), whose date parts would otherwise read as figures.
    if _ISO_DATE.search(body):
        return None
    matches = list(_NUMBER_OR_DASH.finditer(body))
    if not matches:
        return None

    caption = body[: matches[0].start()].strip(" .·-\t")
    # A note-reference column ("Revenue 10 5,868,205 ...") puts a small integer
    # between the caption and the figures. Treat a bare 1-2 digit first number as
    # the note reference when more numbers follow it.
    values = [None if m.group(0) == "-" else parse_number(m.group(0)) for m in matches]
    if (not keep_first and len(values) > 1 and values[0] is not None
            and values[0].is_integer() and 0 < values[0] < 100
            and "." not in matches[0].group(0) and "," not in matches[0].group(0)):
        values = values[1:]

    if not caption or _NOISE_CAPTION.match(caption):
        return None
    if not any(v is not None for v in values):
        return None
    return StatementRow(caption=caption, values=values, raw=raw.strip())


def _title_and_heading(text: str) -> tuple[str, str, int]:
    """The statement's title line, its accumulation heading, and where the body starts."""
    lines = text.splitlines()
    title, heading, body_start = "", "", 0
    for i, line in enumerate(lines[:12]):
        # Stop at the first real data row: everything above it is heading matter,
        # everything below is the statement body. Without this, a caption like
        # "Profit for the period 64,943 53,983" matches the heading pattern and
        # the column headers above it are never read.
        # Above the title a figure-bearing line is letterhead ("T.: +91-124-4222222",
        # a registered address), not the body.
        if title and looks_like_data_row(line):
            break
        if not title and _TITLE_LINE.match(line) and classify(line) != "other":
            title, body_start = line.strip(), i + 1
            continue
        if title and not heading and _HEADING_LINE.match(line):
            heading, body_start = line.strip(), i + 1
    return title, heading, body_start


#: A number that is an amount rather than a label: it carries a separator, or is
#: long enough that no year or quarter label could be confused with it.
_AMOUNT = re.compile(r"\d[\d,]*\.\d+|\d{1,3}(?:,\d{3})+|\b\d{5,}\b")


def looks_like_data_row(line: str) -> bool:
    """A captioned line carrying real amounts, as opposed to a column-header row.

    The distinction matters because header rows are often numeric themselves —
    "3Q2025 3Q2024 3Q2025 2Q2025" or "30 September, 2025 31 December, 2024" —
    so counting numbers alone ends the header block one row too early and the
    column labels are never seen.
    """
    body = line.replace("|", " ")
    numbers = list(_NUMBER.finditer(body))
    # Two numbers minimum: a comparative statement always prints at least a current
    # and a prior column, while boilerplate carrying a single figure ("(NOTE 2.6)",
    # a page number, a year) must not be mistaken for the start of the body.
    if len(numbers) < 2 or not _AMOUNT.search(body):
        return False
    caption = body[: numbers[0].start()]
    return len(re.findall(r"[A-Za-zÀ-ɏ]", caption)) >= 3


def _header_block(lines: list[str], body_start: int) -> tuple[str, int]:
    """Lines between the heading and the first data row — where columns are named."""
    for i in range(body_start, min(len(lines), body_start + 16)):
        if looks_like_data_row(lines[i]):
            return "\n".join(lines[body_start:i]), i
    return "\n".join(lines[body_start:body_start + 10]), min(len(lines), body_start + 10)


def _parse_rows(lines: list[str]) -> list[StatementRow]:
    """Body lines to rows, joining a caption that wrapped onto the next line.

    A page's text is its text layer followed by its tables rendered as pipe-
    delimited rows (see read.py), so a table the text layer already read comes
    twice. A rendered row is kept only when the text layer did not print the same
    caption and figures — counted, so a line the filing genuinely prints twice
    stays twice. Without this, "the second 'Interest paid'" is the operating line
    again rather than the financing one.
    """
    rows: list[StatementRow] = []
    pending_caption: str | None = None
    for line in lines:
        row = parse_row(line)
        if pending_caption is not None:
            if row is None:
                merged = parse_row(f"{pending_caption} {line.strip()}")
                if merged is not None:
                    rows.append(merged)
                    pending_caption = None
                    continue
            # Not a wrapped caption but a heading over this row: kept beside the
            # caption, never merged into it, so matching is unchanged.
            if row is not None:
                row.heading = pending_caption
            pending_caption = None

        if row is not None:
            rows.append(row)
            continue

        stripped = line.strip(" .·-\t")
        if (stripped and not _NUMBER_OR_DASH.search(line)
                and len(re.findall(r"[A-Za-z]", stripped)) >= 3
                and not _NOISE_CAPTION.match(stripped)):
            pending_caption = stripped

    seen = Counter((normalize_label(r.caption), tuple(r.values))
                   for r in rows if "|" not in r.raw)
    kept: list[StatementRow] = []
    for row in rows:
        key = (normalize_label(row.caption), tuple(row.values))
        if "|" in row.raw and seen[key] > 0:
            seen[key] -= 1
            continue
        kept.append(row)
    return kept


def _pipe_header_columns(lines: list[str], kind: str) -> list[Column]:
    """Columns from a pipe-delimited header row, one cell per column.

    A vision transcription renders each table with " | " between columns and the
    column headers as the table's first row(s), e.g. Airtel's
    "Particulars | Quarter ended June 30, 2026 Audited | ... | Previous year ended
    March 31, 2026 Audited". The cells are already separated, so each is parsed
    on its own; stacked header rows with the same number of cells are joined
    cell by cell.
    """
    rows = [[c.strip() for c in line.split("|")] for line in lines if "|" in line]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    stacked = [r for r in rows if len(r) == width]
    headers = [" ".join(r[i] for r in stacked if r[i]).strip() for i in range(1, width)]
    headers = [h for h in headers if h]
    columns = parse_columns_from_headers(headers, statement_kind=kind)
    dated = [c for c in columns if c.end]
    return columns if len(dated) >= 2 else []


def extract_statement(page_no: int, text: str, page=None) -> Statement | None:
    """Read one page into a Statement, or None when it is not a statement page.

    `page` is the pdfplumber page, when available. Its word positions give the
    column layout directly, which is the only way to read a column-major header
    (label on one line, date on the next). Without it, or when the geometry is
    unclear, the flattened-text parser is used instead.
    """
    title, heading, body_start = _title_and_heading(text)
    if not title:
        return None
    kind = classify(title)
    if kind == "other":
        return None

    lines = text.splitlines()
    header_text, first_data = _header_block(lines, body_start)
    columns: list = []
    layout = table_layout(page) if page is not None else None
    if layout is not None:
        columns = parse_columns_from_headers(layout.headers, statement_kind=kind)
    if not columns:
        layout = None
        columns = dedupe_stacked(parse_columns(f"{heading}\n{header_text}"))
    if not columns:
        columns = _pipe_header_columns(lines[:first_data], kind)

    # A first row of small figures only ("GRUMA USA Sales Volume 380 391 (12)")
    # carries no amount, so the text test reads it as header. The geometry pass
    # found the body's real first row; start there when it is earlier.
    if layout is not None and layout.rows and layout.rows[0][0]:
        first_caption = layout.rows[0][0]
        for i in range(body_start, first_data):
            if lines[i].strip().startswith(first_caption):
                first_data = i
                break

    rows = _parse_rows(lines[first_data:])

    if len(rows) < MIN_STATEMENT_ROWS:
        return None
    # A small first figure was taken for a note reference ("Revenue 10 5,868,205").
    # When keeping it gives the row exactly one figure per column, it is a figure:
    # Airtel's "Net gain / (loss) on net investment hedge | 53 | (289) | 357 | 47".
    # Only in a pipe-delimited table (a transcription), where each figure has its
    # own cell, and never where the header names a Notes column — KOC's text-layer
    # rows print "Investment properties 51 59 ..." with 51 the note.
    notes_column = any(re.fullmatch(r"notes?|nota?s?", cell.strip(), re.IGNORECASE)
                       for line in lines[:first_data] if "|" in line
                       for cell in line.split("|"))
    if columns and not notes_column:
        for i, row in enumerate(rows):
            if "|" in row.raw and len(row.values) == len(columns) - 1:
                full = parse_row(row.raw, keep_first=True)
                if full is not None and len(full.values) == len(columns):
                    full.heading = row.heading
                    rows[i] = full
    if layout is not None and len(layout.ranges) == len(columns):
        rows = [_slot_by_position(row, layout) for row in rows]

    return Statement(page=page_no, kind=kind, title=title, heading=heading,
                     columns=columns, rows=rows)


def _signature(columns: list[Column]) -> list[tuple]:
    return [(c.kind, c.months, c.end) for c in columns]


def extract_continuation(text: str, page, previous: Statement) -> list[StatementRow]:
    """Rows of a statement carried over onto an untitled page, or none.

    Gruma's BMV report prints its balance sheet across pages 19-20 and its cash
    flow across 24-25; the second page repeats the column header but not the
    title, so it is not a statement of its own and its rows — the liabilities,
    the financing lines — were lost. A page continues the previous statement only
    when its columns read as exactly the same periods, in the same order.
    """
    if page is None or not previous.columns:
        return []
    layout = table_layout(page)
    if layout is None:
        return []
    columns = parse_columns_from_headers(layout.headers, statement_kind=previous.kind)
    if _signature(columns) != _signature(previous.columns):
        return []
    lines = text.splitlines()
    first_data = next((i for i, line in enumerate(lines) if looks_like_data_row(line)),
                      len(lines))
    rows = _parse_rows(lines[first_data:])
    if len(rows) < MIN_STATEMENT_ROWS:
        return []
    if len(layout.ranges) == len(columns):
        rows = [_slot_by_position(row, layout) for row in rows]
    return rows


#: Units printed after a figure, stripped before a layout cell is read as a number.
_CELL_UNITS = re.compile(r"%|\b(?:bps?|pp|pts?)\b|\s+", re.IGNORECASE)


def _cell_value(text: str | None) -> float | None:
    return None if text is None else parse_number(_CELL_UNITS.sub("", text))


def _slot_by_position(row: StatementRow, layout: TableLayout) -> StatementRow:
    """A short row's values placed under the columns they are printed in.

    A row printing fewer figures than the table has columns (Gruma's D&A leaves its
    VAR cells blank) reads by position as if the blanks were at the end, putting
    the year-to-date figure under the prior-quarter column. The layout pass knows
    which column each figure sits in, so the row takes its values from there — but
    only when exactly one layout row prints the same figures in the same order, so
    nothing is ever guessed.
    """
    if len(row.values) == len(layout.ranges):
        return row
    matches = [
        cells for caption, cells in layout.rows
        if [_cell_value(c) for c in cells if c is not None] == row.values
        and any(c is not None for c in cells)
    ]
    if len(matches) > 1:
        # The same figures on two lines: the caption decides, or nothing does.
        matches = [cells for caption, cells in layout.rows
                   if cells in matches and caption and row.caption.startswith(
                       caption.rstrip("0123456789").strip())]
    if len(matches) != 1:
        return row
    return StatementRow(caption=row.caption,
                        values=[_cell_value(c) for c in matches[0]], raw=row.raw,
                        heading=row.heading, unverified=row.unverified)


def column_alignment(statement: Statement) -> dict[str, int]:
    """How many rows carry exactly as many numbers as there are columns.

    A low ratio means the header parse and the body disagree — usually a stacked
    header read as too many columns, or a transcription that dropped a column.
    Reported rather than repaired: stage 1 does not invent structure.
    """
    n = len(statement.columns)
    if not n:
        return {"columns": 0, "rows": len(statement.rows), "aligned": 0}
    aligned = sum(1 for r in statement.rows if len(r.values) == n)
    return {"columns": n, "rows": len(statement.rows), "aligned": aligned}
