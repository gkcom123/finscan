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


# --------------------------------------------------------------------------- #
# Tiered headers read by geometry
#
# Gruma's 2Q26 summary (p.13) prints a band row over the column labels, and the
# labels share a line with the table's title:
#
#                                          YoY                    YTD
#   Income Statement (USD millions)   2Q26  2Q25  VAR (%)   2026  2025  VAR (%)
#
# The word boxes below are the real ones from that page, trimmed.
# --------------------------------------------------------------------------- #

from finscan2.pdf.layout import table_layout  # noqa: E402


class _Page:
    """Just enough of a pdfplumber page: word boxes."""

    def __init__(self, lines: list[tuple[float, list[tuple[str, float, float]]]]):
        self._words = [{"text": t, "x0": x0, "x1": x1, "top": top, "bottom": top + 8}
                       for top, words in lines for t, x0, x1 in words]

    def extract_words(self, **_):
        return list(self._words)


_GRUMA_P13 = [
    (84.3, [("Gruma,", 76, 100), ("Subsidiaries", 162, 200), ("YoY", 303, 314), ("YTD", 403, 414)]),
    (96.0, [("Income", 76, 98), ("Statement", 100, 130), ("(USD", 131, 146), ("millions)", 148, 172),
            ("2Q26", 267, 283), ("2Q25", 301, 317), ("VAR", 331, 343), ("(%)", 345, 355),
            ("2026", 369, 384), ("2025", 403, 418), ("VAR", 431, 443), ("(%)", 444, 454)]),
    (118.5, [("Net", 76, 87), ("Sales", 88, 103), ("1,649.9", 263, 285), ("1,600.7", 297, 319),
             ("3", 340, 344), ("3,274.7", 364, 386), ("3,149.2", 398, 421), ("4", 440, 443)]),
    (150.8, [("Gross", 85, 102), ("Margin", 104, 125), ("39.2%", 266, 284), ("39.4%", 300, 318),
             ("(20)", 332, 344), ("bp", 346, 353), ("39.0%", 367, 385), ("39.6%", 401, 420),
             ("(60)", 432, 444), ("bp", 446, 453)]),
    (188.5, [("Operating", 76, 105), ("Income", 107, 129), ("214.8", 265, 282), ("229.8", 300, 316),
             ("(7)", 339, 347), ("403.9", 366, 383), ("446.9", 401, 418), ("(10)", 437, 449)]),
    (328.1, [("Depreciation", 76, 113), ("61.8", 267, 280), ("59.9", 301, 315),
             ("123.3", 366, 383), ("118.7", 401, 418)]),
    # The balance sheet summary below: a new table, not more rows of this one.
    (399.7, [("Gruma,", 76, 100), ("YoY", 303, 314), ("QoQ", 387, 400)]),
    (411.4, [("Balance", 76, 99), ("Jun-26", 264, 286), ("Jun-25", 298, 320), ("VAR", 330, 344),
             ("Mar-26", 364, 388), ("VAR", 398, 411)]),
    (431.7, [("Cash", 76, 91), ("484", 268, 280), ("353", 302, 314), ("37", 338, 346),
             ("419", 369, 381), ("15", 405, 414)]),
]


def test_band_row_is_split_between_the_columns_beneath_it():
    layout = table_layout(_Page(_GRUMA_P13))
    assert layout.headers == ["YoY 2Q26", "YoY 2Q25", "YoY VAR (%)",
                              "YTD 2026", "YTD 2025", "YTD VAR (%)"]
    cols = parse_columns_from_headers(layout.headers, statement_kind="income_statement")
    assert [(c.months, c.end) for c in cols] == [
        (3, "2026-06-30"), (3, "2025-06-30"), (None, None),
        (6, "2026-06-30"), (6, "2025-06-30"), (None, None),
    ]


def test_the_next_table_on_the_page_ends_the_body():
    layout = table_layout(_Page(_GRUMA_P13))
    assert [caption for caption, _ in layout.rows] == [
        "Net Sales", "Gross Margin", "Operating Income", "Depreciation"]


def test_blank_cells_keep_their_column():
    """D&A prints no VAR: its YTD figure must stay under YTD, not slide left."""
    layout = table_layout(_Page(_GRUMA_P13))
    assert dict(layout.rows)["Depreciation"] == ["61.8", "59.9", None, "123.3", "118.7", None]
    assert dict(layout.rows)["Gross Margin"][2] == "(20) bp"


def test_a_raised_column_label_is_not_a_band():
    """Fibra Uno prints "Second-quarter" a line above the other labels, in the
    same position as Gruma's "YoY" — but it names one column, not a group."""
    page = _Page([
        (90.0, [("Second-quarter", 300, 350), ("Second-quarter", 450, 500)]),
        (100.0, [("As of 30/06/2026", 240, 290), ("2026 flows", 305, 345),
                 ("As of 31/03/2026", 360, 410), ("2025 flows", 455, 495)]),
        (120.0, [("Revenue", 76, 110), ("1,000.0", 250, 290), ("500.0", 320, 345),
                 ("400.0", 385, 410), ("450.0", 470, 495)]),
        (130.0, [("Costs", 76, 110), ("2,000.0", 250, 290), ("600.0", 320, 345),
                 ("300.0", 385, 410), ("350.0", 470, 495)]),
        (140.0, [("Profit", 76, 110), ("3,000.0", 250, 290), ("700.0", 320, 345),
                 ("200.0", 385, 410), ("250.0", 470, 495)]),
    ])
    layout = table_layout(page)
    assert layout.headers[0] == "As of 30/06/2026"
    assert layout.headers[1] == "Second-quarter 2026 flows"


def test_ytd_year_column_takes_its_end_from_the_quarter_beside_it():
    cols = parse_columns_from_headers(["YoY 3Q25", "YTD 2025", "YTD 2024"],
                                      statement_kind="income_statement")
    assert [(c.months, c.end) for c in cols] == [
        (3, "2025-09-30"), (9, "2025-09-30"), (9, "2024-09-30")]


def test_day_prefixed_month_range():
    """KOC: "1 July - 30 September 2025" is a quarter, not nine months."""
    col = parse_single_column("1 July - 30 September 2025")
    assert (col.months, col.end) == (3, "2025-09-30")


# --------------------------------------------------------------------------- #
# Scale is read from the table, never from narrative
# --------------------------------------------------------------------------- #

from finscan2.pdf.read import _document_units  # noqa: E402
from finscan2.schema import Page, PdfDoc  # noqa: E402


def _doc(pages: dict[int, str], statements: list[Statement]) -> PdfDoc:
    return PdfDoc(path="x.pdf", sha256="0",
                  pages=[Page(page=n, source="text", chars=len(t), text=t)
                         for n, t in pages.items()],
                  statements=statements)


def test_units_come_from_the_table_not_the_narrative():
    """Gruma: page 4's highlights say "US$5.4 billion"; the table is in millions."""
    narrative = _doc({4: "Balance Sheet Highlights\nTotal assets increased by 6% to "
                         "US$5.4 billion when compared to March 2026",
                      13: "Income Statement (USD millions) 2Q26 2Q25\n"
                          "Net Sales 1,649.9 1,600.7"},
                     [Statement(page=4, kind="balance_sheet", title="Balance Sheet Highlights",
                                heading="", rows=[StatementRow(
                                    "Total assets increased by 6% to US$5.4 billion", [5.4],
                                    "Total assets increased by 6% to US$5.4 billion when "
                                    "compared to March 2026")]),
                      Statement(page=13, kind="income_statement",
                                title="Income Statement (USD millions) 2Q26 2Q25", heading="",
                                columns=[Column(index=0, header="2Q26", months=3)],
                                rows=[StatementRow("Net Sales", [1649.9, 1600.7],
                                                   "Net Sales 1,649.9 1,600.7")])])
    units, currency, _ = _document_units(narrative)
    assert (units, currency) == ("millions", "USD")


def test_no_scale_in_any_table_is_unknown_not_guessed():
    """Almarai's notes quote "share capital of 10,000 million" below a table that
    states no scale; that sentence must not decide the scale of every figure."""
    doc = _doc({12: "Statement of Profit or Loss\nRevenue 100 200\n"
                    "The share capital amounted to 10,000 million"},
               [Statement(page=12, kind="income_statement",
                          title="Statement of Profit or Loss", heading="",
                          rows=[StatementRow("Revenue", [100.0, 200.0], "Revenue 100 200")])])
    units, _, _ = _document_units(doc)
    assert units is None


def test_note_table_scale_is_read_from_its_header_block():
    """Almarai's segment note prints "'000" directly above its columns."""
    doc = _doc({15: "for the period then ended, categorised by these business segments, "
                    "is as follows and amounts are in millions elsewhere:\n"
                    "Dairy Bakery Total\n'000 '000 '000\nRevenue 7,937,368 1,408,790 9,346,158"},
               [Statement(page=15, kind="other", note=10, title="Note 10: SEGMENT REPORTING",
                          heading="2026-06-30",
                          columns=[Column(index=i, header=h) for i, h in
                                   enumerate(["Dairy", "Bakery", "Total"])],
                          rows=[StatementRow("Revenue", [7937368.0, 1408790.0, 9346158.0],
                                             "Revenue 7,937,368 1,408,790 9,346,158")])])
    units, _, _ = _document_units(doc)
    assert units == "thousands"


def test_iso_date_range_column():
    """Gruma's BMV report: the span is two ISO dates, the second one the end."""
    cols = parse_columns_from_headers(
        ["Quarter Current Year 2026-04-01 - 2026- 06-30",
         "Accumulated Current Year 2026-01-01 - 2026-06-30"],
        statement_kind="income_statement")
    assert [(c.months, c.end) for c in cols] == [(3, "2026-06-30"), (6, "2026-06-30")]


def test_xbrl_rounding_level_means_figures_in_units():
    """Gruma's BMV report: "Level of rounding ... THOUSAND OF DOLLARS" above
    figures printed in full. Rounding is precision; the scale is units."""
    rows = [StatementRow(f"Line {i}", [1_649_941_000.0 + i * 1000, 1_600_727_000.0],
                         f"Line {i}") for i in range(12)]
    doc = _doc({18: "Level of rounding used in financial statements: THOUSAND OF DOLLARS",
                21: "Statement of comprehensive income\nLine 0 1"},
               [Statement(page=21, kind="income_statement",
                          title="Statement of comprehensive income", heading="",
                          columns=[Column(index=0, header="a"), Column(index=1, header="b")],
                          rows=rows)])
    units, _, issues = _document_units(doc)
    assert units == "units" and issues[0].code == "units_from_rounding"


def test_rounding_level_is_ignored_when_figures_are_not_rounded_to_it():
    rows = [StatementRow(f"Line {i}", [1_649_941.0 + i, 1_600_727.0], f"Line {i}")
            for i in range(12)]
    doc = _doc({18: "Level of rounding used in financial statements: THOUSAND OF DOLLARS",
                21: "Statement of comprehensive income\nLine 0 1"},
               [Statement(page=21, kind="income_statement",
                          title="Statement of comprehensive income", heading="",
                          columns=[Column(index=0, header="a"), Column(index=1, header="b")],
                          rows=rows)])
    assert _document_units(doc)[0] is None


def test_iso_dated_header_line_is_not_a_row():
    assert parse_row("Concept | Quarter Current Year | 2026-04-01 - 2026-06-30") is None


def test_ocr_figures_are_confirmed_only_by_independent_evidence():
    """Airtel's scanned results: gpt-5.4 read 35,929 (twice) where the page
    prints 36,929, and (1,082) where the scan's own OCR layer says "11,082)".
    The text layer and the table's arithmetic settle both; nothing else counts."""
    from finscan2.pdf.verify import verify_rows

    transcription = "\n".join([
        "Income |  |",
        "Revenue from operations | 585,391 |",
        "Other income | 9,066 |",
        " | 594,457 |",
        "Expenses |  |",
        "Network operating expenses | 108,097 |",
        "Access charges | 15,887 |",
        "License fee / Spectrum charges | 41,720 |",
        "Employee benefits expense | 21,776 |",
        "Sales and marketing expenses | 35,929 |",
        "Other expenses | 27,954 |",
        " | 252,363 |",
        "Profit before depreciation, amortisation and tax | 342,094 |",
        "Depreciation and amortisation expenses | 142,350 |",
        "Finance costs | 59,564 |",
        "Share of profit of associates and joint ventures (net) | (1,082) |",
        "Profit before exceptional items and tax | 141,262 |",
        "Gain on investments at fair value through OCI | 465 |",
    ])
    layer = "\n".join([
        "Revenue from opernlions 585,391", "other income 9 066",
        "Network opernting expenses 108,097", "Access charges 15,887",
        "License fee/ Spectrum charges 41,720", "Employee benefits expense 21,n6",
        "Sales and marketing expenses 36,929", "Other expenses 27,954",
        "exceptional items and tax 342,1194", "Depreciation and amortisation expenses 142,350",
        "Flnanceco5ts 59,564", "Share of profit of associates and joint ventures [net) 11,082)",
        "Profit before exceptional items and tax 141,262",
        "Gain on Investments at fair value through o 466",
    ])
    rows = [r for r in (parse_row(line) for line in transcription.splitlines()) if r]
    report = verify_rows(rows, transcription, layer)
    by = {r.caption: r for r in rows}

    assert by["Sales and marketing expenses"].values == [36929.0]       # corrected
    assert report.corrected and report.corrected[0][3] == 36929.0
    assert by["Employee benefits expense"].unverified == []             # by the total
    assert by["Profit before depreciation, amortisation and tax"].unverified == []
    assert by["Share of profit of associates and joint ventures (net)"].unverified == []
    assert by["Gain on investments at fair value through OCI"].unverified == [0]


def test_quarter_ended_is_the_quarter_and_financial_period_ended_is_year_to_date():
    """Axiata: one table prints both. The table-wide "Quarter Ended" must not turn
    the "Financial Period Ended" (January–June) columns into quarters as well."""
    headers = ["2ⁿᵈ Quarter Ended 30/06/2026 RM'000", "2ⁿᵈ Quarter Ended 30/06/2025 RM'000",
               "Financial Period Ended 30/06/2026 RM'000", "Financial Period Ended 30/06/2025 RM'000"]
    cols = parse_columns_from_headers(headers, statement_kind="comprehensive_income",
                                      default_months=3)
    assert [(c.months, c.end) for c in cols] == [
        (3, "2026-06-30"), (3, "2025-06-30"), (6, "2026-06-30"), (6, "2025-06-30")]


def test_quarter_ended_default_still_applies_to_bare_month_columns():
    """Airtel's IR Pack: "Quarter Ended" once over "Jun-26  Jun-25"."""
    cols = parse_columns_from_headers(["Jun-26", "Jun-25", "Y-o-Y Growth"],
                                      statement_kind="income_statement", default_months=3)
    assert [(c.months, c.end) for c in cols[:2]] == [(3, "2026-06-30"), (3, "2025-06-30")]
