"""Find the statements on a page and read them into captions and numbers.

Deliberately conservative: a row is recorded with whatever numbers its printed line
carries, in printed order, and nothing is inferred. Stage 3 decides which column it
wants; stage 1's job is to preserve the page faithfully, including rows whose value
count does not match the header count, because that mismatch is itself evidence.
"""
from __future__ import annotations

import re

from finscan2.pdf.columns import (
    dedupe_stacked,
    parse_columns,
    parse_columns_from_headers,
)
from finscan2.pdf.layout import header_columns
from finscan2.schema import Column, Statement, StatementKind, StatementRow

#: Title -> statement kind. Ordered: the first match wins, so the more specific
#: phrases must come first ("comprehensive income" before "income").
_TITLE_PATTERNS: list[tuple[str, StatementKind]] = [
    (r"comprehensive\s+income", "comprehensive_income"),
    (r"cash\s*flows?", "cash_flow"),
    (r"changes\s+in\s+equity|statement\s+of\s+equity", "equity"),
    (r"financial\s+position|balance\s+sheet", "balance_sheet"),
    (r"profit\s+or\s+loss|income\s+statement|statements?\s+of\s+(?:income|operations)"
     r"|profit\s+and\s+loss|results\s+of\s+operations", "income_statement"),
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


def parse_row(line: str) -> StatementRow | None:
    """Split a printed line into its caption and its numbers, in printed order."""
    raw = line.rstrip()
    body = raw.replace("|", " ")
    matches = list(_NUMBER_OR_DASH.finditer(body))
    if not matches:
        return None

    caption = body[: matches[0].start()].strip(" .·-\t")
    # A note-reference column ("Revenue 10 5,868,205 ...") puts a small integer
    # between the caption and the figures. Treat a bare 1-2 digit first number as
    # the note reference when more numbers follow it.
    values = [None if m.group(0) == "-" else parse_number(m.group(0)) for m in matches]
    if (len(values) > 1 and values[0] is not None
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
        if looks_like_data_row(line):
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
    if page is not None:
        headers = header_columns(page)
        if headers:
            columns = parse_columns_from_headers(headers, statement_kind=kind)
    if not columns:
        columns = dedupe_stacked(parse_columns(f"{heading}\n{header_text}"))

    rows: list[StatementRow] = []
    for line in lines[first_data:]:
        row = parse_row(line)
        if row is not None:
            rows.append(row)

    if len(rows) < MIN_STATEMENT_ROWS:
        return None

    return Statement(page=page_no, kind=kind, title=title, heading=heading,
                     columns=columns, rows=rows)


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
