"""Stage 3 (finscan2.match) — offline tests. No PDF, no LLM, no network.

Built around the two selection rules and the de-cumulation arithmetic, using the
column shapes of real filings: Almarai's 3M/3M/6M/6M profit or loss and Fibra
Uno's six-column statement where the standalone quarter is the second column.
"""
from __future__ import annotations

from finscan2.match.basis import decumulate, fiscal_year_start, periods_to_subtract, prior_columns
from finscan2.match.select import find_row, select_column
from finscan2.schema import Column, Statement, StatementRow


def _statement(columns: list[tuple[str, int | None, str, str]],
               rows: list[tuple[str, list[float]]] | None = None,
               kind: str = "income_statement") -> Statement:
    return Statement(
        page=4, kind=kind, title="t", heading="h",
        columns=[Column(index=i, header=h, months=m, end=e, kind=k)
                 for i, (h, m, e, k) in enumerate(columns)],
        rows=[StatementRow(caption=c, values=list(v), raw="") for c, v in (rows or [])],
    )


_ALMARAI = [("April - June 2026", 3, "2026-06-30", "period"),
            ("April - June 2025", 3, "2025-06-30", "period"),
            ("January - June 2026", 6, "2026-06-30", "period"),
            ("January - June 2025", 6, "2025-06-30", "period")]

_FUNO = [("6 months as of 30/06/2026", 6, "2026-06-30", "period"),
         ("Second-quarter 2026 transactions", 3, "2026-06-30", "period"),
         ("3 months as of 31/03/2026", 3, "2026-03-31", "period"),
         ("6 months as of 30/06/2025", 6, "2025-06-30", "period"),
         ("Second-quarter 2025 transactions", 3, "2025-06-30", "period"),
         ("3 months as of 31/03/2025", 3, "2025-03-31", "period")]


# --------------------------------------------------------------------------- #
# Column selection
# --------------------------------------------------------------------------- #

def test_the_standalone_quarter_wins_over_the_cumulative_column():
    column, why = select_column(_statement(_ALMARAI), "2026-06-30", 3, point_in_time=False)
    assert column.index == 0 and column.months == 3
    assert "standalone" in why


def test_selection_is_by_months_and_end_not_by_position():
    """Fibra Uno: the quarter column is second, between two cumulative ones."""
    column, _ = select_column(_statement(_FUNO), "2026-06-30", 3, point_in_time=False)
    assert column.index == 1
    assert column.header == "Second-quarter 2026 transactions"


def test_a_three_month_column_of_the_wrong_quarter_is_rejected():
    """Column 2 is also three months, but it ends 31 March — the prior quarter."""
    column, _ = select_column(_statement(_FUNO), "2026-06-30", 3, point_in_time=False)
    assert column.end == "2026-06-30"


def test_a_cumulative_column_is_used_when_no_quarter_exists():
    """Almarai's cash flow prints only year-to-date columns."""
    cash_flow = [("January - June 2026", 6, "2026-06-30", "period"),
                 ("January - June 2025", 6, "2025-06-30", "period")]
    column, why = select_column(_statement(cash_flow, kind="cash_flow"),
                                "2026-06-30", 3, point_in_time=False)
    assert column.months == 6
    assert "cumulative" in why


def test_the_shortest_cumulative_column_is_preferred():
    """Fewer prior quarters to subtract means less exposure to a missing one."""
    both = [("January - June 2026", 6, "2026-06-30", "period"),
            ("January - December 2026", 12, "2026-06-30", "period")]
    column, _ = select_column(_statement(both), "2026-06-30", 3, point_in_time=False)
    assert column.months == 6


def test_a_balance_column_is_matched_by_date_with_no_months_test():
    balance = [("30/06/2026", None, "2026-06-30", "point_in_time"),
               ("31/12/2025", None, "2025-12-31", "point_in_time")]
    column, why = select_column(_statement(balance, kind="balance_sheet"),
                                "2026-06-30", 3, point_in_time=True)
    assert column.index == 0 and column.months is None
    assert "end date" in why


def test_no_column_for_the_period_is_refused_with_what_was_found():
    column, why = select_column(_statement(_ALMARAI), "2026-09-30", 3, point_in_time=False)
    assert column is None
    assert "2026-06-30" in why


def test_a_statement_with_unreadable_headers_is_refused():
    column, why = select_column(_statement([]), "2026-06-30", 3, point_in_time=False)
    assert column is None and "could not be read" in why


# --------------------------------------------------------------------------- #
# Finding the row
# --------------------------------------------------------------------------- #

def test_exact_caption_match_wins():
    statement = _statement(_ALMARAI, [("Revenue", [1.0]), ("Total revenue", [2.0])])
    row, why = find_row(statement, "Revenue")
    assert row.values == [1.0] and "exact" in why


def test_caption_matching_ignores_case_and_punctuation():
    statement = _statement(_ALMARAI, [("Financial Cost, net", [5.0])])
    row, _ = find_row(statement, "financial cost net")
    assert row.values == [5.0]


def test_an_ambiguous_caption_is_refused_rather_than_guessed():
    statement = _statement(_ALMARAI, [("Other expenses", [1.0]), ("Other expenses, net", [2.0])])
    row, why = find_row(statement, "Other")
    assert row is None and "matches 2 rows" in why


def test_a_caption_that_is_absent_is_refused():
    statement = _statement(_ALMARAI, [("Revenue", [1.0])])
    row, why = find_row(statement, "Zakat")
    assert row is None and "no row captioned" in why


# --------------------------------------------------------------------------- #
# De-cumulation
# --------------------------------------------------------------------------- #

def test_periods_to_subtract():
    assert periods_to_subtract(3, 3) == 0        # Q1: a no-op
    assert periods_to_subtract(6, 3) == 1
    assert periods_to_subtract(9, 3) == 2
    assert periods_to_subtract(12, 3) == 3       # a year into Q4
    assert periods_to_subtract(5, 3) is None     # not a whole number of periods


def test_fiscal_year_start_handles_a_year_crossing_december():
    assert fiscal_year_start("2026-06-30", 6) == __import__("datetime").date(2026, 1, 1)
    assert fiscal_year_start("2026-03-31", 12) == __import__("datetime").date(2025, 4, 1)


def test_prior_columns_are_chosen_by_date():
    dates = {45: "2025-09-30", 46: "2025-12-31", 47: "2026-03-31"}
    columns, problem = prior_columns(dates, "2026-06-30", 3, 1)
    assert columns == [47] and problem is None


def test_two_prior_columns_for_a_nine_month_figure():
    dates = {45: "2025-09-30", 46: "2025-12-31", 47: "2026-03-31", 48: "2026-06-30"}
    columns, problem = prior_columns(dates, "2026-09-30", 3, 2)
    assert columns == [47, 48] and problem is None


def test_not_enough_history_is_refused():
    columns, problem = prior_columns({47: "2026-03-31"}, "2026-09-30", 3, 2)
    assert columns == [] and "only 1 dated column" in problem


def test_a_gap_in_the_model_is_refused():
    """A missing quarter would otherwise be silently absorbed."""
    dates = {45: "2025-06-30", 47: "2026-03-31"}
    columns, problem = prior_columns(dates, "2026-06-30", 3, 2)
    assert columns == [] and problem


def test_decumulation_subtracts_the_prior_columns():
    value, adjustment, issue = decumulate(
        1_801_121.0, {45: 590_412.0, 46: 598_300.0}, [45, 46], row=45,
        column_letters={45: "AS", 46: "AT"})
    assert value == 612_409.0
    assert "AS 590,412" in adjustment.detail and issue is None


def test_a_missing_prior_value_leaves_the_row_blank():
    """Formula cells written by a previous run have no cached value until Excel
    has opened the file; that must refuse, not fall back to the cumulative figure."""
    value, adjustment, issue = decumulate(
        1_801_121.0, {45: 590_412.0, 46: None}, [45, 46], row=45,
        column_letters={45: "AS", 46: "AT"})
    assert value is None and adjustment is None
    assert issue.code == "decumulation_missing_prior" and issue.severity == "error"


def test_an_implausible_correction_is_written_but_flagged():
    value, _, issue = decumulate(100.0, {45: -500.0}, [45], row=9)
    assert value == 600.0
    assert issue.code == "decumulation_implausible" and issue.severity == "warning"


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #

def test_the_cell_comment_records_the_arithmetic():
    from finscan2.match.schema import Adjustment, ResolvedValue, Source

    value = ResolvedValue(
        row=45, label="Depreciation & amortisation", section="income_statement",
        value=612_409.0, resolve="pdf:Depreciation and Amortisation",
        source=Source(statement="cash_flow", page=9, caption="Depreciation and Amortisation",
                      column_header="January - September 2025", column_index=0,
                      months=9, end="2025-09-30", printed=1_801_121.0),
        adjustments=[Adjustment(kind="decumulate",
                                detail="de-cumulated: 1,801,121.00 - AS 590,412.00 = 612,409.00")],
    )
    comment = value.comment()
    assert "cash_flow p.9" in comment
    assert "January - September 2025" in comment
    assert "de-cumulated" in comment


def test_an_unresolved_row_says_why_in_its_comment():
    from finscan2.match.schema import ResolvedValue

    value = ResolvedValue(row=58, label="Taxes, net", section="cash_flow", value=None,
                          resolve="pdf:Taxes", unresolved="no row captioned like 'Taxes'")
    assert not value.ok
    assert "no value written" in value.comment()


# --------------------------------------------------------------------------- #
# Word-set matching (the third tier — not fuzzy)
# --------------------------------------------------------------------------- #

def test_a_two_direction_caption_matches_the_direction_the_filing_printed():
    """The model says "Other Expenses, net"; the filing prints "Other (Expenses) /
    Income, net" because the drafter did not know which way the quarter would go."""
    statement = _statement(_ALMARAI, [("Revenue", [1.0]),
                                      ("Other (Expenses) / Income, net", [-30302.0])])
    row, why = find_row(statement, "Other Expenses, net")
    assert row.values == [-30302.0] and "direction" in why


def test_the_two_direction_caption_can_be_on_the_model_side_instead():
    """The mirror case: the model enumerates both directions and the filing prints
    only the one that happened."""
    statement = _statement(_ALMARAI, [("Impairment Loss on Financial Assets", [-19133.0])])
    row, why = find_row(statement, "Impairment (Loss) / Reversal on Financial Assets")
    assert row.values == [-19133.0] and "direction" in why


def test_direction_reading_refuses_when_both_directions_are_printed():
    """A filing that prints the gain line AND the loss line separately is a real
    ambiguity: which one the model's combined row wants is not knowable here."""
    statement = _statement(_ALMARAI, [("Impairment loss on financial assets", [-1.0]),
                                      ("Impairment reversal on financial assets", [2.0])])
    row, why = find_row(statement, "Impairment Loss / Reversal on Financial Assets")
    assert row is None and "both directions" in why


def test_adjacency_keeps_an_ordinary_caption_from_being_split():
    """"Income tax expense" carries two direction words two apart. Reading it as
    two directions would invent "income tax" and "tax expense" as lookups."""
    from finscan2.match.select import direction_variants

    assert direction_variants("income tax expense") == []
    assert direction_variants("finance cost net") == []
    assert direction_variants("impairment loss reversal on financial assets") == [
        "impairment loss on financial assets",
        "impairment reversal on financial assets"]


def test_word_matching_still_refuses_when_two_rows_qualify():
    statement = _statement(_ALMARAI, [("Staff costs, net of recoveries", [1.0]),
                                      ("Staff costs, net", [2.0])])
    row, why = find_row(statement, "Staff net costs")
    assert row is None and "on words" in why


def test_the_word_tier_needs_at_least_two_words():
    """A one-word caption is handled by containment or not at all; it never reaches
    the word tier, where a single generic word could sweep up a statement."""
    statement = _statement(_ALMARAI, [("Other (Expenses) / Income, net", [1.0])])
    row, why = find_row(statement, "Other")
    assert row is not None and "containment" in why
    assert "every word" not in why


def test_exact_and_containment_still_win_before_word_matching():
    statement = _statement(_ALMARAI, [("Other expenses, net", [1.0]),
                                      ("Other (Expenses) / Income, net", [2.0])])
    row, why = find_row(statement, "Other expenses, net")
    assert row.values == [1.0] and "exact" in why


# --------------------------------------------------------------------------- #
# A constant the analyst put in the map
# --------------------------------------------------------------------------- #

def test_a_const_row_is_written_without_touching_the_filing():
    """Almarai's model has an "Exchange Gain, net" row; the filing has no such
    line — it sits inside "Other (Expenses) / Income, net", which another row
    already claims. The analyst wants a zero, not a blank and not a duplicate."""
    from finscan2.match.resolve import resolve_values

    doc, resolved_map, path = _tiny_model(resolve="const:0")
    values = resolve_values(doc, resolved_map, path, "2026-06-30")
    value = values.values[0]
    assert value.value == 0.0 and value.ok
    assert value.source is None                      # nothing was read from the filing
    assert "constant" in value.adjustments[0].detail
    assert any(i.code == "constant_written" for i in values.issues)


def test_a_constant_says_in_its_comment_that_it_is_not_from_the_filing():
    """A figure that did not come from the filing must not look like one that did."""
    from finscan2.match.resolve import resolve_values

    doc, resolved_map, path = _tiny_model(resolve="const:0")
    comment = resolve_values(doc, resolved_map, path, "2026-06-30").values[0].comment()
    assert "not read from the filing" in comment


def test_a_const_that_is_not_a_number_is_refused():
    from finscan2.match.resolve import resolve_values

    doc, resolved_map, path = _tiny_model(resolve="const:n/a")
    values = resolve_values(doc, resolved_map, path, "2026-06-30")
    assert values.values[0].value is None
    assert any(i.code == "bad_constant" for i in values.issues)


# --------------------------------------------------------------------------- #
# carry: a line the filing never reports, held directly in the workbook
# --------------------------------------------------------------------------- #

def test_a_carried_row_takes_the_reference_columns_own_value():
    """An FX peg: no filing reports it, so its figure is whatever the analyst
    last typed into the workbook, carried forward unchanged."""
    from finscan2.match.resolve import resolve_values

    doc, resolved_map, path = _tiny_model(resolve="carry:a SAR/USD peg",
                                          reference_value=0.2665)
    value = resolve_values(doc, resolved_map, path, "2026-06-30").values[0]
    assert value.value == 0.2665 and value.ok
    assert value.source is None                      # nothing was read from the filing
    assert "carried forward" in value.adjustments[0].detail
    assert any(i.code == "carried_forward" and i.severity == "warning"
              for i in resolve_values(doc, resolved_map, path, "2026-06-30").issues)


def test_a_carried_row_says_in_its_comment_that_it_is_not_from_the_filing():
    from finscan2.match.resolve import resolve_values

    doc, resolved_map, path = _tiny_model(resolve="carry:", reference_value=0.2665)
    comment = resolve_values(doc, resolved_map, path, "2026-06-30").values[0].comment()
    assert "not read from the filing" in comment


def test_a_carried_row_with_nothing_to_carry_is_refused():
    """The reference column is blank/non-numeric: there is nothing to carry, so
    this is left unresolved rather than silently written as 0."""
    from finscan2.match.resolve import resolve_values

    doc, resolved_map, path = _tiny_model(resolve="carry:")
    values = resolve_values(doc, resolved_map, path, "2026-06-30")
    assert values.values[0].value is None
    assert any(i.code == "unresolved_row" for i in values.issues)


def _tiny_model(resolve: str, extra_rows=None, reference_value: float | None = None):
    """One input row in a real one-cell workbook, so resolve_values can run."""
    import tempfile
    from pathlib import Path

    from openpyxl import Workbook

    from finscan2.model.discover import RowLayout, SheetLayout
    from finscan2.model.load import resolve as bind_map
    from finscan2.model.schema import ModelMap, RowKey, RowSpec
    from finscan2.schema import PdfDoc

    book = Workbook()
    sheet = book.active
    sheet.title = "Model"
    sheet.cell(2, 3, __import__("datetime").datetime(2026, 3, 31))
    sheet.cell(5, 2, "Exchange Gain, net")
    if reference_value is not None:
        sheet.cell(5, 3, reference_value)
    path = Path(tempfile.mkdtemp()) / "m.xlsx"
    book.save(path)

    layout = SheetLayout(
        sheet="Model", label_col=2, header_row=2, first_data_row=5,
        period_dates={3: "2026-03-31"}, period_date_row=2, reference_col=3,
        write_col=4, units="millions", cadence_months=3,
        rows=[RowLayout(row=5, label="Exchange Gain, net",
                        section="income_statement", role="input", has_formula=False)])
    model = ModelMap(
        company="almarai", sheet="Model", units="millions", cadence_months=3,
        label_col=2, header_row=2, first_data_row=5, reference_col=3, write_col=4,
        period_dates={3: "2026-03-31"}, period_date_row=2, confirmed_by="test",
        rows=[RowSpec(key=RowKey(label="Exchange Gain, net", section="income_statement"),
                      row_hint=5, kind="input", statement="income_statement",
                      basis="quarter", resolve=resolve)])
    rows = [("Revenue", [1.0, 2.0, 3.0, 4.0]), *(extra_rows or [])]
    doc = PdfDoc(path="f.pdf", sha256="x", units="thousands",
                 statements=[_statement(_ALMARAI, rows)])
    return doc, bind_map(model, layout), str(path)


# --------------------------------------------------------------------------- #
# A line the analyst has determined the filing does not report
# --------------------------------------------------------------------------- #

def test_an_absent_row_is_left_blank_and_reported_every_quarter():
    """Preferred over const:0 for a line that simply is not in the filing: a blank
    that asks again each quarter cannot go quietly stale."""
    from finscan2.match.resolve import resolve_values

    doc, resolved_map, path = _tiny_model(
        resolve="absent:inside 'Other (Expenses) / Income, net' (r14)")
    values = resolve_values(doc, resolved_map, path, "2026-06-30")
    value = values.values[0]
    assert value.value is None and not value.ok
    assert "declared absent" in value.unresolved and "r14" in value.unresolved
    codes = {i.code: i.severity for i in values.issues}
    # A warning, not an error: nothing failed, and a failed run must stay meaningful.
    assert codes.get("absent_by_design") == "warning"


def test_an_absent_declaration_that_has_gone_stale_is_an_error():
    """The filing now prints the line. Quietly honouring a decision made about a
    different filing is how a row stays blank forever."""
    from finscan2.match.resolve import resolve_values

    doc, resolved_map, path = _tiny_model(resolve="absent:not in this filing",
                                          extra_rows=[("Exchange Gain, net", [9.0] * 4)])
    values = resolve_values(doc, resolved_map, path, "2026-06-30")
    assert values.values[0].value is None          # still blank, never auto-filled
    stale = [i for i in values.issues if i.code == "absent_declaration_stale"]
    assert stale and stale[0].severity == "error"
    assert "Exchange Gain, net" in stale[0].message


# --------------------------------------------------------------------------- #
# sum: several printed lines into one model row
# --------------------------------------------------------------------------- #

def test_a_sum_adds_the_printed_terms():
    """Almarai's model hand-typed "=-26.573-10.578" into its tax row: Zakat plus
    income tax, two filing lines feeding one model row."""
    from finscan2.match.resolve import resolve_values

    doc, resolved_map, path = _tiny_model(
        resolve="sum:pdf:Zakat|pdf:Income Tax",
        extra_rows=[("Zakat", [-26.573] * 4), ("Income Tax", [-10.578] * 4)])
    value = resolve_values(doc, resolved_map, path, "2026-06-30").values[0]
    assert value.ok and round(value.value, 3) == round(_scaled(-37.151), 3)
    assert "summed:" in value.adjustments[0].detail
    assert "Zakat" in value.source.caption and "Income Tax" in value.source.caption


def test_a_sum_term_can_be_subtracted():
    from finscan2.match.resolve import resolve_values

    doc, resolved_map, path = _tiny_model(
        resolve="sum:pdf:Zakat|-pdf:Income Tax",
        extra_rows=[("Zakat", [30.0] * 4), ("Income Tax", [10.0] * 4)])
    value = resolve_values(doc, resolved_map, path, "2026-06-30").values[0]
    assert round(value.value, 3) == round(_scaled(20.0), 3)
    assert "- Income Tax" in value.adjustments[0].detail


def test_one_missing_term_fails_the_whole_sum():
    """A caption that cannot be found at all is a mapping problem, not a printed
    zero, so it still fails the whole sum: plausible, and short by exactly one
    component, with nothing about it looking wrong."""
    from finscan2.match.resolve import resolve_values

    doc, resolved_map, path = _tiny_model(
        resolve="sum:pdf:Zakat|pdf:Income Tax",
        extra_rows=[("Zakat", [-26.573] * 4)])
    values = resolve_values(doc, resolved_map, path, "2026-06-30")
    assert values.values[0].value is None
    assert "Income Tax" in values.values[0].unresolved
    assert any(i.code == "unresolved_row" for i in values.issues)


def test_a_term_printed_as_a_dash_contributes_zero_instead_of_failing():
    """The caption IS found, but this column prints "-": the filing itself is
    saying nothing happened here this period, so the sum can trust that as 0."""
    from finscan2.match.resolve import resolve_values

    doc, resolved_map, path = _tiny_model(
        resolve="sum:pdf:Zakat|pdf:Income Tax",
        extra_rows=[("Zakat", [-26.573] * 4), ("Income Tax", [None] * 4)])
    value = resolve_values(doc, resolved_map, path, "2026-06-30").values[0]
    assert value.ok and round(value.value, 3) == round(_scaled(-26.573), 3)
    assert "treated as 0" in value.adjustments[0].detail


def test_a_sum_records_the_arithmetic_in_its_comment():
    from finscan2.match.resolve import resolve_values

    doc, resolved_map, path = _tiny_model(
        resolve="sum:pdf:Zakat|pdf:Income Tax",
        extra_rows=[("Zakat", [-26.573] * 4), ("Income Tax", [-10.578] * 4)])
    comment = resolve_values(doc, resolved_map, path, "2026-06-30").values[0].comment()
    assert "Zakat -26.57" in comment and "Income Tax -10.58" in comment


def test_a_caption_containing_a_comma_survives_term_splitting():
    """Terms are separated by '|' precisely because captions contain commas."""
    from finscan2.match.resolve import resolve_values

    doc, resolved_map, path = _tiny_model(
        resolve="sum:pdf:Other Expenses, net|pdf:Zakat",
        extra_rows=[("Other Expenses, net", [1.0] * 4), ("Zakat", [2.0] * 4)])
    value = resolve_values(doc, resolved_map, path, "2026-06-30").values[0]
    assert round(value.value, 3) == round(_scaled(3.0), 3)


# --------------------------------------------------------------------------- #
# pdf: a single printed line, no sum
# --------------------------------------------------------------------------- #

def test_a_plain_row_printed_as_a_dash_is_treated_as_zero():
    """Same principle as a dash inside a sum: the caption is found, this column
    prints "-", and that is the filing's own answer of 0, not a gap."""
    from finscan2.match.resolve import resolve_values

    doc, resolved_map, path = _tiny_model(
        resolve="pdf:Exchange Gain, net",
        extra_rows=[("Exchange Gain, net", [None, 1.0, 2.0, 3.0])])
    value = resolve_values(doc, resolved_map, path, "2026-06-30").values[0]
    assert value.ok and value.value == 0.0
    assert any(a.kind == "assumed_zero" for a in value.adjustments)


def test_a_plain_row_with_too_few_columns_still_fails():
    """A caption found but with fewer printed columns than the statement's header
    row promises is a real misalignment, not a printed dash — still refused."""
    from finscan2.match.resolve import resolve_values

    doc, resolved_map, path = _tiny_model(
        resolve="pdf:Exchange Gain, net",
        extra_rows=[("Exchange Gain, net", [])])
    values = resolve_values(doc, resolved_map, path, "2026-06-30")
    assert values.values[0].value is None
    assert any(i.code == "unresolved_row" for i in values.issues)


def _scaled(printed: float) -> float:
    """The fixture's filing is in thousands and its model in millions."""
    from finscan2.match.resolve import _scale

    return _scale(printed, "thousands", "millions")


def test_block_caption_finds_the_line_inside_a_named_block():
    """Gruma's by-subsidiary table repeats Net Sales / EBITDA under every
    subsidiary; "GIMSA > EBITDA" is the one in GIMSA's block, and the heading may
    sit on a line of its own ("OTHER SUBSIDIARIES &") or carry an accent."""
    from finscan2.match.resolve import _in_block
    from finscan2.schema import Statement, StatementRow

    rows = [StatementRow("GRUMA USA Sales Volume", [380.0], ""),
            StatementRow("Corn flour and other Net Sales", [850.3], ""),
            StatementRow("EBITDA", [165.0], ""),
            StatementRow("GIMSA Sales Volume", [520.0], ""),
            StatementRow("EBITDA", [50.3], ""),
            StatementRow("GRUMA CENTROAMÉRICA Sales Volume", [62.0], ""),
            StatementRow("EBITDA", [19.7], ""),
            StatementRow("Sales Volume", [-18.0], "", heading="OTHER SUBSIDIARIES &"),
            StatementRow("EBITDA", [7.9], "")]
    st = Statement(page=14, kind="income_statement", title="t", heading="", rows=rows)
    assert _in_block(st, "GIMSA", "EBITDA")[0].values == [50.3]
    assert _in_block(st, "GRUMA USA", "Net Sales")[0].values == [850.3]
    assert _in_block(st, "GRUMA CENTROAMERICA", "EBITDA")[0].values == [19.7]
    assert _in_block(st, "OTHER SUBSIDIARIES", "EBITDA")[0].values == [7.9]
    assert _in_block(st, "GRUMA", "EBITDA")[0] is None      # names several rows


def test_a_unit_printed_on_the_row_outranks_the_tables():
    """Airtel's IR Pack summary prints "Market Capitalization Rs Bn 11,559" among
    rows in Rs Mn; that row is billions, and only that row."""
    from finscan2.match.resolve import _row_units
    from finscan2.schema import StatementRow

    assert _row_units(StatementRow("Market Capitalization Rs Bn", [11559.0], "")) == "billions"
    assert _row_units(StatementRow("Total revenues Rs Mn", [585391.0], "")) == "millions"
    assert _row_units(StatementRow("Market Capitalization US$ Bn", [122.5], "")) == "billions"
    assert _row_units(StatementRow("Total Customer Base", [680889.0], "")) is None
    assert _row_units(StatementRow("Revenue", [585391.0], "")) is None
