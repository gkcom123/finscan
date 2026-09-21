"""Stage 1 (finscan2.pdf) — offline tests. No PDF, no LLM, no network.

Header shapes here are taken verbatim from real filings in inbox/: Almarai's
interim accounts, Tencent's quarterly release and Koc Holding's convenience
translation. v1 is untouched by all of this.
"""
from __future__ import annotations

from finscan2.pdf.columns import parse_columns
from finscan2.pdf.statements import (
    classify,
    column_alignment,
    extract_statement,
    looks_like_data_row,
    parse_number,
    parse_row,
)
from finscan2.schema import Column, Statement, StatementRow


# --------------------------------------------------------------------------- #
# Column headers — the whole point of stage 1
# --------------------------------------------------------------------------- #

def test_month_range_headers_give_span_and_end():
    """Almarai's profit or loss: a standalone quarter and a YTD column side by
    side under one heading naming both bases."""
    cols = parse_columns(
        "FOR THE THREE-MONTH AND SIX-MONTH PERIODS ENDED 30 JUNE 2026\n"
        "April - June 2026 | April - June 2025 | January - June 2026 | January - June 2025"
    )
    assert [c.months for c in cols] == [3, 3, 6, 6]
    assert [c.end for c in cols] == ["2026-06-30", "2025-06-30", "2026-06-30", "2025-06-30"]
    assert all(c.kind == "period" for c in cols)


def test_nine_month_range_spans_nine():
    cols = parse_columns("January - September 2025 | January - September 2024")
    assert [c.months for c in cols] == [9, 9]


def test_stacked_header_lines_are_flattened():
    """Headers are routinely printed one word per line."""
    cols = parse_columns("April -\nJune\n2026\nApril -\nJune\n2025")
    assert [c.months for c in cols] == [3, 3]


def test_months_phrase_above_dated_columns():
    """Tencent-style: the span is stated once, the dates name the columns."""
    cols = parse_columns("Three months ended\n30 September 2025 30 September 2024")
    assert [c.months for c in cols] == [3, 3]
    assert [c.end for c in cols] == ["2025-09-30", "2024-09-30"]


def test_two_phrases_split_the_dates_between_them():
    cols = parse_columns(
        "Three months ended ... Nine months ended\n"
        "30 September 2025 30 September 2024 30 September 2025 30 September 2024"
    )
    assert [c.months for c in cols] == [3, 3, 9, 9]


def test_quarter_labels():
    cols = parse_columns("Unaudited Unaudited\n3Q2025 3Q2024 3Q2025 2Q2025")
    assert [c.months for c in cols] == [3, 3, 3, 3]
    assert [c.end for c in cols] == ["2025-09-30", "2024-09-30", "2025-09-30", "2025-06-30"]


def test_bare_dates_are_point_in_time_not_periods():
    """A balance sheet states a position on a date; it has no span, and must never
    be de-cumulated downstream."""
    cols = parse_columns("As at As at\n30 September, 2025 31 December, 2024")
    assert [c.kind for c in cols] == ["point_in_time", "point_in_time"]
    assert [c.months for c in cols] == [None, None]
    assert [c.end for c in cols] == ["2025-09-30", "2024-12-31"]


def test_comma_between_month_and_year_is_tolerated():
    assert parse_columns("30 September, 2025")[0].end == "2025-09-30"


def test_fiscal_range_wrapping_december():
    cols = parse_columns("October - March 2026")
    assert cols[0].months == 6


def test_no_recognisable_header_returns_nothing():
    assert parse_columns("Unaudited\n(Amounts expressed in million)") == []


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #

def test_parse_number_handles_parentheses_and_separators():
    assert parse_number("5,868,205") == 5868205.0
    assert parse_number("(4,044,312)") == -4044312.0
    assert parse_number("0.64") == 0.64
    assert parse_number("-") is None


def test_row_keeps_printed_order_and_signs():
    row = parse_row("Cost of Sales | (4,044,312) | (3,575,223) | (8,338,456) | (7,573,658)")
    assert row.caption == "Cost of Sales"
    assert row.values == [-4044312.0, -3575223.0, -8338456.0, -7573658.0]


def test_note_reference_column_is_dropped():
    """"Revenue 10 5,868,205 ..." — the 10 is a note reference, not a figure."""
    row = parse_row("Revenue 10 5,868,205 5,288,402 12,028,258 11,055,492")
    assert row.values == [5868205.0, 5288402.0, 12028258.0, 11055492.0]


def test_heading_rows_are_not_rows():
    assert parse_row("CASH FLOWS FROM OPERATING ACTIVITIES") is None
    assert parse_row("The accompanying notes form an integral part 2.6") is None


def test_single_figure_boilerplate_is_not_a_data_row():
    """Koc's translation notice carries "(NOTE 2.6)" above the real title; reading
    it as the start of the body hides the statement entirely."""
    assert not looks_like_data_row("STATEMENTS ORIGINALLY ISSUED IN TURKISH (NOTE 2.6)")
    assert looks_like_data_row("Revenue 10 5,868,205 5,288,402")


# --------------------------------------------------------------------------- #
# Statement detection
# --------------------------------------------------------------------------- #

def test_classify_titles_seen_in_real_filings():
    assert classify("CONDENSED CONSOLIDATED STATEMENT OF PROFIT OR LOSS") == "income_statement"
    assert classify("CONSOLIDATED STATEMENTS OF INCOME") == "income_statement"
    assert classify("CONDENSED CONSOLIDATED INCOME STATEMENT") == "income_statement"
    assert classify("CONSOLIDATED BALANCE SHEETS") == "balance_sheet"
    assert classify("CONDENSED CONSOLIDATED STATEMENT OF FINANCIAL POSITION") == "balance_sheet"
    assert classify("CONDENSED CONSOLIDATED STATEMENT OF CASHFLOWS") == "cash_flow"
    assert classify("STATEMENT OF COMPREHENSIVE INCOME") == "comprehensive_income"
    assert classify("NOTES TO THE FINANCIAL STATEMENTS") == "other"


def test_comprehensive_income_beats_income():
    """Order matters: the more specific title must not fall through to the P&L."""
    assert classify("CONDENSED CONSOLIDATED STATEMENT OF COMPREHENSIVE INCOME") == \
        "comprehensive_income"


_PAGE = """ALMARAI COMPANY
CONDENSED CONSOLIDATED STATEMENT OF PROFIT OR LOSS
FOR THE THREE-MONTH AND SIX-MONTH PERIODS ENDED 30 JUNE 2026
April - June 2026 | April - June 2025 | January - June 2026 | January - June 2025
(Unaudited) | (Unaudited) | (Unaudited) | (Unaudited)
Revenue 10 5,868,205 5,288,402 12,028,258 11,055,492
Cost of Sales (4,044,312) (3,575,223) (8,338,456) (7,573,658)
Gross Profit 1,823,893 1,713,179 3,689,802 3,481,834
"""


def test_extract_statement_end_to_end():
    statement = extract_statement(6, _PAGE)
    assert statement.kind == "income_statement"
    assert [c.months for c in statement.columns] == [3, 3, 6, 6]
    assert [r.caption for r in statement.rows] == ["Revenue", "Cost of Sales", "Gross Profit"]
    assert statement.rows[0].values[0] == 5868205.0
    assert column_alignment(statement) == {"columns": 4, "rows": 3, "aligned": 3}


def test_a_notes_page_is_not_a_statement():
    assert extract_statement(11, "NOTES TO THE CONDENSED INTERIM STATEMENTS\n"
                                 "1. General 12,345 6,789") is None


def test_column_alignment_flags_a_misread_header():
    """The guard that catches a column-major header parsed as one column."""
    statement = Statement(
        page=5, kind="income_statement", title="t", heading="h",
        columns=[Column(index=0, header="30 September 2025", months=9)],
        rows=[StatementRow(caption="Revenue", values=[1.0, 2.0, 3.0], raw="")] * 4,
    )
    align = column_alignment(statement)
    assert align["aligned"] == 0 and align["rows"] == 4


# --------------------------------------------------------------------------- #
# Column-major headers and per-column parsing
#
# Fibra Uno's income statement is the case that defeats every text-based parse:
# six columns, printed label-over-date, and the current quarter is the SECOND
# column, not the first.
#
#   6 months as of   Second-quarter 2026   3 months as of   6 months as of  ...
#   30/06/2026       transactions          31/03/2026       30/06/2025
# --------------------------------------------------------------------------- #

from finscan2.pdf.columns import (  # noqa: E402
    parse_columns_from_headers,
    parse_numeric_date,
    parse_single_column,
)

_FUNO_HEADERS = [
    "6 months as of 30/06/2026",
    "Second-quarter 2026 transactions",
    "3 months as of 31/03/2026",
    "6 months as of 30/06/2025",
    "Second-quarter 2025 transactions",
    "3 months as of 31/03/2025",
]


def test_funo_six_columns():
    cols = parse_columns_from_headers(_FUNO_HEADERS, statement_kind="income_statement")
    assert [c.months for c in cols] == [6, 3, 3, 6, 3, 3]
    assert [c.end for c in cols] == [
        "2026-06-30", "2026-06-30", "2026-03-31",
        "2025-06-30", "2025-06-30", "2025-03-31",
    ]


def test_the_current_quarter_is_not_the_first_column():
    """Selection must be by (months, end), never by position: here the standalone
    Q2 2026 column sits second, between two cumulative columns."""
    cols = parse_columns_from_headers(_FUNO_HEADERS, statement_kind="income_statement")
    match = [c for c in cols if c.months == 3 and c.end == "2026-06-30"]
    assert len(match) == 1
    assert match[0].index == 1


def test_q1_column_is_rejected_by_its_end_date():
    """A 3-month column that is not the current quarter must not be selected."""
    cols = parse_columns_from_headers(_FUNO_HEADERS, statement_kind="income_statement")
    q1 = [c for c in cols if c.end == "2026-03-31"]
    assert q1 and q1[0].months == 3 and q1[0].index == 2


def test_date_only_column_on_a_flow_statement_is_year_to_date():
    """Fibra Uno's cash flow states the span only in the page heading; a column
    headed "As of 30/06/2026" is six months from a January year start."""
    cols = parse_columns_from_headers(
        ["As of 30/06/2026", "Second-quarter 2026 flows", "As of 31/03/2026"],
        statement_kind="cash_flow",
    )
    assert [c.months for c in cols] == [6, 3, 3]


def test_fiscal_year_is_not_inferred_from_a_quarter_column():
    """A three-month column starts at the quarter, not the year. Reading a fiscal
    year off it made "As of 30/06" three months instead of six."""
    cols = parse_columns_from_headers(
        ["Second-quarter 2026 flows", "As of 30/06/2026"], statement_kind="cash_flow"
    )
    assert [c.months for c in cols] == [3, 6]


def test_fiscal_year_start_is_taken_from_a_cumulative_sibling():
    """An April year start: "6 months as of 30/09/2026" fixes it, and the
    date-only column then reads as three months, not six."""
    cols = parse_columns_from_headers(
        ["6 months as of 30/09/2026", "As of 30/06/2026"], statement_kind="income_statement"
    )
    assert [c.months for c in cols] == [6, 3]


def test_date_only_column_on_a_balance_sheet_stays_point_in_time():
    cols = parse_columns_from_headers(
        ["30/06/2026", "31/12/2025"], statement_kind="balance_sheet"
    )
    assert [c.kind for c in cols] == ["point_in_time", "point_in_time"]
    assert [c.months for c in cols] == [None, None]


def test_numeric_date_order_is_inferred_from_the_values():
    assert parse_numeric_date("30/06/2026") == "2026-06-30"
    assert parse_numeric_date("06/30/2026") == "2026-06-30"
    assert parse_numeric_date("2026-06-30") == "2026-06-30"


def test_ambiguous_numeric_date_follows_the_statement_convention():
    """03/06/2026 is either 3 June or 6 March; the sibling column decides."""
    cols = parse_columns_from_headers(["30/06/2026", "03/06/2026"],
                                      statement_kind="balance_sheet")
    assert [c.end for c in cols] == ["2026-06-30", "2026-06-03"]


def test_ordinal_quarter_column():
    col = parse_single_column("Second-quarter 2026 transactions")
    assert col.months == 3 and col.end == "2026-06-30"
    assert parse_single_column("Fourth quarter 2025").end == "2025-12-31"


def test_a_table_of_contents_is_not_a_statement():
    """Fibra Uno's contents page lists "Interim Consolidated Condensed Statements of
    Financial Position 2", which matches the title pattern but has no body."""
    assert extract_statement(2, "Fibra Uno Trust and subsidiaries\n"
                                "Table of Contents Page\n"
                                "Interim Consolidated Condensed Statements of "
                                "Financial Position 2\n"
                                "Interim Consolidated Condensed Statement of Cash Flow 5\n") is None


def test_cache_is_ignored_when_the_parser_changed(tmp_path):
    """A cache keyed by the PDF's hash alone serves pre-fix output after a parser
    change, which is indistinguishable from the fix not working."""
    import json

    from finscan2.pdf.read import cache_path
    from finscan2.schema import PARSER_VERSION, PdfDoc

    stale = PdfDoc(path="x.pdf", sha256="abc", parser_version="0")
    target = cache_path(tmp_path, "abc")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(stale.to_dict()), encoding="utf-8")

    payload = json.loads(target.read_text())
    assert payload["parser_version"] != PARSER_VERSION

    fresh = PdfDoc(path="x.pdf", sha256="abc")
    assert fresh.parser_version == PARSER_VERSION
