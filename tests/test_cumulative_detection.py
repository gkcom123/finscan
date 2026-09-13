"""Unit tests for extractor.py's cumulative-vs-standalone-quarter detection pass.

These are pure, offline tests of the deterministic regex-based detector — no LLM, no
graph, no workbook. See excel/cumulative.py (tested separately in
test_cumulative_correction.py) for the actual value correction that consumes what this
pass tags.
"""
from __future__ import annotations

from finscan.extract.extractor import _detect_months_covered, _maybe_realign_quarter_values
from finscan.schemas import CUMULATIVE_CORRECTION_INELIGIBLE_FIELDS, Extraction, Field_, LineItem, PeriodMeta


def _extraction(items: list[LineItem], period_type: str = "quarter") -> Extraction:
    return Extraction(
        meta=PeriodMeta(period_label="Q2 FY2027", period_end_date="2026-09-30",
                        period_type=period_type, units="lakhs"),
        line_items=items,
    )


def _da_item(source_row_text: str, value: float = 1500.0) -> LineItem:
    return LineItem(field=Field_.depreciation_amortisation,
                     label_in_pdf="Depreciation and amortisation",
                     value=value, confidence=0.9, source_row_text=source_row_text)


def test_inline_phrase_on_the_rows_own_text_sets_months_covered():
    item = _da_item("Depreciation and amortisation for the six months ended 30 June 2026 "
                    "was 1,500.")
    result = _extraction([item])
    note = _detect_months_covered(result, "", set())
    assert item.months_covered == 6
    assert note is not None and "depreciation_amortisation" in note


def test_nine_month_and_spanish_phrasing_are_both_recognised():
    item = _da_item("Depreciacion y amortizacion    1.500", value=1500.0)
    result = _extraction([item])
    document_text = (
        "ESTADO DE FLUJO DE EFECTIVO\n"
        "para los nueve meses terminados al 30 de septiembre de 2026\n"
        "Depreciacion y amortizacion    1.500\n"
    )
    note = _detect_months_covered(result, document_text, set())
    assert item.months_covered == 9
    assert note is not None


def test_proximity_window_finds_a_header_above_the_row_not_just_on_it():
    """The row's own text carries no phrase; the accumulation period is stated once in
    a table header above it, which is how cash-flow-statement notes commonly work."""
    item = _da_item("Depreciation and amortisation    1,500")
    result = _extraction([item])
    document_text = (
        "CONDENSED CASH FLOW STATEMENT\n"
        "For the nine month period ended 31 December 2026\n"
        "Adjustments for:\n"
        "Depreciation and amortisation    1,500\n"
    )
    note = _detect_months_covered(result, document_text, set())
    assert item.months_covered == 9
    assert note is not None


def test_nearest_preceding_phrase_wins_over_a_farther_one():
    item = _da_item("Depreciation and amortisation    1,500")
    result = _extraction([item])
    document_text = (
        "For the twelve months ended 31 December 2025 (prior year context)\n"
        "...\n"
        "For the six months ended 30 June 2026\n"
        "Depreciation and amortisation    1,500\n"
    )
    note = _detect_months_covered(result, document_text, set())
    assert item.months_covered == 6, "should use the nearer six-month header, not the farther twelve-month one"
    assert note is not None


def test_no_phrase_anywhere_leaves_months_covered_none():
    item = LineItem(field=Field_.revenue_from_operations, label_in_pdf="Revenue",
                     value=5000.0, confidence=0.95, source_row_text="Revenue    5,000")
    result = _extraction([item])
    note = _detect_months_covered(result, "Revenue    5,000", set())
    assert item.months_covered is None
    assert note is None


def test_ineligible_fields_are_never_tagged_even_with_a_phrase_right_next_to_them():
    assert "eps_basic" in CUMULATIVE_CORRECTION_INELIGIBLE_FIELDS
    item = LineItem(field=Field_.eps_basic, label_in_pdf="EPS - Basic", value=1.2,
                    confidence=0.9,
                    source_row_text="EPS - Basic for the six months ended 30 June 2026 was 1.2.")
    result = _extraction([item])
    _detect_months_covered(result, "", set())
    assert item.months_covered is None


def test_only_fires_for_quarterly_filings():
    """An annual filing's own '12 months ended' phrasing is not a cumulative-quarter
    problem; there is no smaller period to recover it from."""
    item = _da_item("Depreciation and amortisation for the twelve months ended "
                    "31 December 2026 was 6,000.", value=6000.0)
    result = _extraction([item], period_type="year")
    note = _detect_months_covered(result, "", set())
    assert item.months_covered is None
    assert note is None


def test_a_row_already_realigned_is_not_also_tagged_as_cumulative():
    """The regression case this whole guard exists for: a mixed 6M/3M table's own
    header text ('six and three months periods ended') would also match the
    cumulative-phrase detector. A row _maybe_realign_quarter_values already rewrote
    in place must be skipped, or it would be corrected a second time, wrongly."""
    item = _da_item("Depreciation and amortisation 1,500 780 1,400 700", value=1500.0)
    result = _extraction([item])
    document_text = "Six and three months periods ended 30 June 2026\n" + item.source_row_text

    quarter_note, realigned_ids = _maybe_realign_quarter_values(result, document_text)
    assert quarter_note is not None
    assert item.value == 780.0            # switched to the quarter (second) figure
    assert id(item) in realigned_ids

    months_note = _detect_months_covered(result, document_text, realigned_ids)
    assert item.months_covered is None, "already-realigned row must not be re-tagged"
    assert months_note is None


def test_maybe_realign_returns_empty_set_when_nothing_changes():
    item = LineItem(field=Field_.revenue_from_operations, label_in_pdf="Revenue",
                     value=5000.0, confidence=0.95, source_row_text="Revenue 5,000")
    result = _extraction([item])
    note, changed_ids = _maybe_realign_quarter_values(result, "no mixed table phrase here")
    assert note is None
    assert changed_ids == set()


# --------------------------------------------------------------------------- #
# Mixed-basis statements: one heading, a standalone-quarter column AND a YTD column
#
# Modelled on Almarai's 3Q25 condensed consolidated interims, which is the shape that
# defeats phrase/proximity matching entirely:
#   - profit or loss  : "FOR THE THREE-MONTH AND NINE-MONTH PERIODS ENDED 30 SEPTEMBER
#                        2025", four columns  Jul-Sep 25 | Jul-Sep 24 | Jan-Sep 25 | Jan-Sep 24
#   - cash flows      : "FOR THE NINE-MONTH PERIOD ENDED 30 SEPTEMBER 2025", two columns
#                        Jan-Sep 25 | Jan-Sep 24  — no quarter column exists at all
# So Revenue is standalone and Depreciation is nine-month cumulative in the same filing.
# --------------------------------------------------------------------------- #

_PL_PAGE = (
    "ALMARAI COMPANY\n"
    "CONDENSED CONSOLIDATED STATEMENT OF PROFIT OR LOSS\n"
    "FOR THE THREE-MONTH AND NINE-MONTH PERIODS ENDED 30 SEPTEMBER 2025\n"
    "July - September 2025 | July - September 2024 | "
    "January - September 2025 | January - September 2024\n"
    "Revenue 5,552,599 5,208,921 16,608,091 15,822,183\n"
    "Cost of Sales (3,805,242) (3,539,024) (11,378,900) (10,741,490)\n"
)

_CF_PAGE = (
    "ALMARAI COMPANY\n"
    "CONDENSED CONSOLIDATED STATEMENT OF CASHFLOWS\n"
    "FOR THE NINE-MONTH PERIOD ENDED 30 SEPTEMBER 2025\n"
    "January - September 2025 | January - September 2024\n"
    "Profit for the period 1,992,153 1,883,317\n"
    "Depreciation and Amortisation 1,801,121 1,731,543\n"
)


def test_quarter_column_under_a_dual_basis_heading_is_not_tagged_cumulative():
    """The regression this fix exists for. Revenue's 5,552,599 comes from the
    'July - September 2025' column — a standalone quarter — but the page heading also
    says 'NINE-MONTH PERIODS ENDED', so proximity matching alone tags it 9 months and
    the correction pass then subtracts Q1+Q2 from an already-standalone figure."""
    item = LineItem(field=Field_.revenue_from_operations, label_in_pdf="Revenue",
                    value=5_552_599.0, confidence=0.95,
                    source_row_text="Revenue 5,552,599 5,208,921 16,608,091 15,822,183")
    result = _extraction([item])
    _detect_months_covered(result, _PL_PAGE, set())
    assert item.months_covered is None


def test_ytd_column_of_the_same_row_is_tagged_nine_months():
    """Same row, same heading — but the value was read from the January-September
    column, so it genuinely is a nine-month figure."""
    item = LineItem(field=Field_.revenue_from_operations, label_in_pdf="Revenue",
                    value=16_608_091.0, confidence=0.95,
                    source_row_text="Revenue 5,552,599 5,208,921 16,608,091 15,822,183")
    result = _extraction([item])
    note = _detect_months_covered(result, _PL_PAGE, set())
    assert item.months_covered == 9
    assert note is not None and "revenue_from_operations" in note


def test_cash_flow_with_no_quarter_column_is_tagged_nine_months():
    """D&A is disclosed only on the nine-month cash flow statement; there is no quarter
    column anywhere in the document to fall back to."""
    item = _da_item("Depreciation and Amortisation 1,801,121 1,731,543", value=1_801_121.0)
    result = _extraction([item])
    note = _detect_months_covered(result, _CF_PAGE, set())
    assert item.months_covered == 9
    assert note is not None


def test_both_statements_in_one_document_get_different_bases():
    """End to end on the mixed filing: the P&L row standalone, the cash-flow row
    cumulative, from a single pass over one document."""
    revenue = LineItem(field=Field_.revenue_from_operations, label_in_pdf="Revenue",
                       value=5_552_599.0, confidence=0.95,
                       source_row_text="Revenue 5,552,599 5,208,921 16,608,091 15,822,183")
    da = _da_item("Depreciation and Amortisation 1,801,121 1,731,543", value=1_801_121.0)
    result = _extraction([revenue, da])
    _detect_months_covered(result, _PL_PAGE + _CF_PAGE, set())
    assert revenue.months_covered is None
    assert da.months_covered == 9


def test_dual_basis_heading_without_a_column_position_refuses_to_guess():
    """No month-range column headers to place the value, and the heading names two
    bases. The honest answer is 'unknown', reported as a note, not a coin flip."""
    item = _da_item("Depreciation and Amortisation 1,801,121", value=1_801_121.0)
    document_text = (
        "CONDENSED CONSOLIDATED STATEMENT OF PROFIT OR LOSS\n"
        "FOR THE THREE-MONTH AND NINE-MONTH PERIODS ENDED 30 SEPTEMBER 2025\n"
        "Depreciation and Amortisation 1,801,121\n"
    )
    note = _detect_months_covered(result := _extraction([item]), document_text, set())
    assert item.months_covered is None
    assert note is not None and "two accumulation periods" in note
    assert result.line_items[0].months_covered is None


def test_single_basis_heading_still_uses_proximity():
    """The pre-existing behaviour must survive: one basis named, no column headers,
    proximity is still the right answer."""
    item = _da_item("Depreciation and amortisation    1,500")
    document_text = (
        "CONDENSED CASH FLOW STATEMENT\n"
        "For the nine month period ended 31 December 2026\n"
        "Depreciation and amortisation    1,500\n"
    )
    _detect_months_covered(_extraction([item]), document_text, set())
    assert item.months_covered == 9


def test_value_printed_twice_in_a_row_is_not_placed_by_column():
    """A flat year-on-year row prints the same figure in two columns; the value's
    column is then genuinely ambiguous and must not decide the basis."""
    item = _da_item("Depreciation and Amortisation 1,801,121 1,801,121", value=1_801_121.0)
    document_text = (
        "CONDENSED CONSOLIDATED STATEMENT OF PROFIT OR LOSS\n"
        "FOR THE THREE-MONTH AND NINE-MONTH PERIODS ENDED 30 SEPTEMBER 2025\n"
        "July - September 2025 | January - September 2025\n"
        "Depreciation and Amortisation 1,801,121 1,801,121\n"
    )
    note = _detect_months_covered(_extraction([item]), document_text, set())
    assert item.months_covered is None
    assert note is not None and "two accumulation periods" in note


def test_bracketed_expense_row_is_placed_by_magnitude_not_sign():
    """An expense row prints its figures in parentheses, so they parse negative, while
    the extractor reports the figure positive. Matching on signed value finds no column
    and silently drops the whole cost base into the 'basis undetectable' bucket."""
    row = ("Cost of Sales | (4,044,312) | (3,575,223) | (8,388,456) | (7,573,658)")
    document_text = (
        "CONDENSED CONSOLIDATED STATEMENT OF PROFIT OR LOSS\n"
        "FOR THE THREE-MONTH AND SIX-MONTH PERIODS ENDED 30 JUNE 2026\n"
        "April - June 2026 | April - June 2025 | January - June 2026 | January - June 2025\n"
        + row + "\n"
    )
    quarter = LineItem(field=Field_.cost_of_revenue, label_in_pdf="Cost of Sales",
                       value=4_044_312.0, confidence=0.95, source_row_text=row)
    _detect_months_covered(_extraction([quarter]), document_text, set())
    assert quarter.months_covered is None, "April-June column is a standalone quarter"

    ytd = LineItem(field=Field_.cost_of_revenue, label_in_pdf="Cost of Sales",
                   value=8_388_456.0, confidence=0.95, source_row_text=row)
    _detect_months_covered(_extraction([ytd]), document_text, set())
    assert ytd.months_covered == 6, "January-June column accumulates six months"


def test_focused_rescue_tags_the_column_it_read(monkeypatch):
    """The cash-flow rescue is appended after detection has run and carries a
    placeholder source row, so the column it reports is its only basis signal. A cash
    flow statement printing only 'January - June 2026' is a six-month figure and must
    not reach a quarterly column untagged."""
    from finscan.extract.extractor import _ensure_working_capital_components

    def fake_rescue(statements_text, period_hint, period_end_date=None):
        return (
            {"total_before_working_capital_changes": 3_213_969.0,
             "net_cash_from_operating_activities": 2_372_360.0},
            "Recovered via a focused follow-up extraction; column used: January - June 2026.",
            "January - June 2026 (Unaudited)",
        )

    monkeypatch.setattr("finscan.extract.extractor._rescue_via_focused_llm_call", fake_rescue)
    result = _extraction([
        LineItem(field=Field_.revenue_from_operations, label_in_pdf="Revenue",
                 value=5_868_205.0, confidence=0.95, source_row_text="Revenue | 5,868,205"),
    ])
    note = _ensure_working_capital_components(result, "some statements text")

    rescued = {li.field.value: li.months_covered for li in result.line_items
               if li.field.value in ("total_before_working_capital_changes",
                                     "net_cash_from_operating_activities")}
    assert rescued == {"total_before_working_capital_changes": 6,
                       "net_cash_from_operating_activities": 6}
    assert note and "6-month column" in note


def test_focused_rescue_on_a_quarter_column_is_not_tagged(monkeypatch):
    """A cash flow statement that does print a standalone quarter needs no correction."""
    from finscan.extract.extractor import _ensure_working_capital_components

    def fake_rescue(statements_text, period_hint, period_end_date=None):
        return ({"net_cash_from_operating_activities": 900_000.0},
                "Recovered via focused follow-up.", "April - June 2026")

    monkeypatch.setattr("finscan.extract.extractor._rescue_via_focused_llm_call", fake_rescue)
    result = _extraction([
        LineItem(field=Field_.revenue_from_operations, label_in_pdf="Revenue",
                 value=5_868_205.0, confidence=0.95, source_row_text="Revenue | 5,868,205"),
        LineItem(field=Field_.total_before_working_capital_changes, label_in_pdf="Total",
                 value=800_000.0, confidence=0.9, source_row_text="Total | 800,000"),
    ])
    _ensure_working_capital_components(result, "text")
    y = next(li for li in result.line_items
             if li.field.value == "net_cash_from_operating_activities")
    assert y.months_covered is None
