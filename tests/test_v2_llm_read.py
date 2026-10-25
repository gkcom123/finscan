"""The opt-in LLM reader — offline: the model's JSON is stubbed, nothing is called."""
from __future__ import annotations

from types import SimpleNamespace

from finscan.mapping.schema import Mapping
from finscan.pdf.llm_read import doc_from_tables

_HEADERS = ["2nd Quarter Ended 30/06/2026 RM'000", "2nd Quarter Ended 30/06/2025 RM'000",
            "Financial Period Ended 30/06/2026 RM'000", "Financial Period Ended 30/06/2025 RM'000"]
_TITLE = "UNAUDITED CONDENSED CONSOLIDATED STATEMENT OF COMPREHENSIVE INCOME"


def test_vision_request_includes_text_to_check_dense_table_headers(monkeypatch):
    from finscan import llm
    from finscan.pdf.llm_read import _read_page

    calls = []

    class Model:
        def invoke(self, messages):
            calls.append(messages)
            return SimpleNamespace(content='{"tables": []}')

    monkeypatch.setattr(llm, "chat_model", lambda: Model())
    text = "2026 % of Rev. 2025 % of Rev. Reported Comparable"
    assert _read_page(12, "image", text) == {"tables": []}
    content = calls[0][1]["content"]
    assert text in content[0]["text"]
    assert content[1]["image_url"]["url"] == "data:image/png;base64,image"


def _page(rows):
    return {"tables": [{"title": _TITLE, "units": "RM'000", "columns": _HEADERS, "rows": rows}]}


def test_reader_setting_round_trips_through_the_mapping(tmp_path):
    m = Mapping(company="axiata", sheet="Model", reader="llm")
    path = m.save(tmp_path / "axiata.json")
    assert Mapping.load(path).reader == "llm"
    assert '"reader": "llm"' in path.read_text()
    plain = Mapping(company="gruma", sheet="Model_USD").save(tmp_path / "gruma.json")
    assert Mapping.load(plain).reader is None and "reader" not in plain.read_text()


def test_tables_become_statements_with_periods_units_and_checked_figures():
    pages = {1: _page([
        {"caption": "Revenue", "values": ["2,870,302", "2,966,419", "5,670,164", "5,858,276"]},
        {"caption": "- domestic interconnect, international outpayment and other direct costs",
         "values": ["(323,391)", "(331,558)", "(615,268)", "(662,051)"]},
        {"caption": "Staff costs", "values": ["(286,051)", "(999,999)", None, None]},
    ])}
    layer = ("Revenue 2,870,302 2,966,419 5,670,164 5,858,276\n"
             "- domestic interconnect, international outpayment and\n"
             "(323,391) (331,558) (615,268) (662,051)\nother direct costs\n"
             "- staff costs (286,051) (335,825) (569,848) (648,983)\n")
    doc = doc_from_tables("x.pdf", "0", pages, {1: layer}, "gpt-5.4")
    st = doc.statements[0]
    assert st.kind == "comprehensive_income" and st.units == "thousands" and doc.units == "thousands"
    assert [(c.months, c.end) for c in st.columns] == [
        (3, "2026-06-30"), (3, "2025-06-30"), (6, "2026-06-30"), (6, "2025-06-30")]
    rows = {r.caption: r for r in st.rows}
    wrapped = rows["domestic interconnect, international outpayment and other direct costs"]
    assert wrapped.values == [-323391.0, -331558.0, -615268.0, -662051.0]
    assert wrapped.unverified == []
    # 999,999 is not printed on the page: flagged, so stage 3 will not write it.
    assert rows["Staff costs"].unverified == [1]
    assert any(i.code == "llm_figure_not_printed" for i in doc.issues)


def test_a_statement_continued_over_pages_is_one_statement():
    pages = {1: _page([{"caption": "Revenue", "values": ["1", "2", "3", "4"]}]),
             2: _page([{"caption": "Finance income", "values": ["5", "6", "7", "8"]}])}
    doc = doc_from_tables("x.pdf", "0", pages, {1: "1 2 3 4", 2: "5 6 7 8"}, "m")
    assert len(doc.statements) == 1
    assert [r.caption for r in doc.statements[0].rows] == ["Revenue", "Finance income"]


def test_an_untitled_continuation_page_joins_the_statement_before_it():
    """BMV XBRL reports print no title on the page that carries the liabilities, or
    the cash flow's investing and financing sections."""
    cols = ["Close Current Quarter 2026-06-30", "Close Previous Exercise 2025-12-31"]
    pages = {
        1: {"tables": [{"title": "[210000] Statement of financial position", "units": "Currency: MXN",
                        "columns": cols, "rows": [{"caption": "Total assets",
                                                   "values": ["308,185,157,000", "312,735,701,000"]}]}]},
        2: {"tables": [{"title": "", "units": "Currency: MXN", "columns": cols,
                        "rows": [{"caption": "Total equity",
                                  "values": ["180,665,962,000", "179,369,483,000"]}]}]},
    }
    doc = doc_from_tables("x.pdf", "0", pages, {}, "m")
    assert len(doc.statements) == 1
    assert [r.caption for r in doc.statements[0].rows] == ["Total assets", "Total equity"]


def test_a_bmv_xbrl_statement_is_in_full_units_not_the_press_release_scale():
    cols = ["2026-01-01 - 2026-06-30", "2025-01-01 - 2025-06-30"]
    pages = {
        1: {"tables": [{"title": "Estado de Resultados Consolidado",
                        "units": "Cifras expresadas en millones de pesos",
                        "columns": ["ACUMULADO 2026", "ACUMULADO 2025"],
                        "rows": [{"caption": "EBITDA", "values": ["13,665", "14,109"]}]}]},
        2: {"tables": [{"title": "[310000] Statement of comprehensive income", "units": "Currency: MXN",
                        "columns": cols,
                        "rows": [{"caption": "Revenue",
                                  "values": ["102,707,892,000", "101,951,361,000"]}]}]},
    }
    doc = doc_from_tables("x.pdf", "0", pages, {}, "m")
    press, xbrl = doc.statements
    assert press.units == "millions" and xbrl.units == "units"


def test_a_scanned_page_is_reported_as_unchecked_not_silently_trusted():
    doc = doc_from_tables("x.pdf", "0", {1: _page([{"caption": "Revenue",
                                                   "values": ["1", "2", "3", "4"]}])},
                          {1: ""}, "m")
    assert any(i.code == "llm_unchecked" for i in doc.issues)


def test_turkish_lira_columns_keep_dotted_thousands_but_euro_columns_keep_decimals():
    pages = {1: {"tables": [{
        "title": "Balance sheet",
        "units": "Million TL",
        "columns": ["30 September 2025 Million EUR", "30 September 2025 Million TL"],
        "rows": [{"caption": "Cash", "values": ["7.709", "375.008"]}],
    }]}}
    doc = doc_from_tables("x.pdf", "0", pages, {1: "Cash 7.709 375.008"}, "m")
    assert doc.statements[0].rows[0].values == [7.709, 375008.0]
    assert doc.statements[0].rows[0].unverified == []


def test_an_undated_presentation_table_inherits_the_cover_quarter_for_money_only():
    pages = {13: {"tables": [{
        "title": "Debt Currency & Rates Breakdown",
        "units": "TRY mn",
        "columns": ["Effective Interest Rate p.a.", "Original Currency (mn)",
                    "TRY Equivalent (mn)"],
        "rows": [{"caption": "EUR", "values": ["4.6", "2,362", "125,394"]}],
    }]}}
    doc = doc_from_tables("x.pdf", "0", pages,
                          {1: "2Q26 Financial Results", 13: "4.6 2,362 125,394"}, "m")
    assert [column.end for column in doc.statements[0].columns] == [
        None, "2026-06-30", "2026-06-30"]
    assert any(issue.code == "llm_inherited_cover_period" for issue in doc.issues)


def test_year_to_date_columns_get_their_own_year_and_a_span_from_the_cover():
    pages = {5: {"tables": [{
        "title": "Statement of cash flows",
        "units": "R$ thousands",
        "columns": ["Parent YTD 2026", "Parent Reclassified YTD 2025",
                    "Consolidated YTD 2026", "Consolidated Reclassified YTD 2025"],
        "rows": [{"caption": "Depreciation", "values": ["1", "2", "3", "4"]}],
    }]}}
    doc = doc_from_tables("x.pdf", "0", pages,
                          {1: "Interim Financial Information 1Q26", 5: "Depreciation 1 2 3 4"}, "m")
    assert [(c.kind, c.months, c.end) for c in doc.statements[0].columns] == [
        ("period", 3, "2026-03-31"), ("period", 3, "2025-03-31"),
        ("period", 3, "2026-03-31"), ("period", 3, "2025-03-31")]


def test_the_cover_quarter_ends_on_the_last_day_of_its_month():
    from finscan.pdf.llm_read import _cover_period_end

    assert _cover_period_end("1Q26") == "2026-03-31"
    assert _cover_period_end("Q2 2026") == "2026-06-30"
    assert _cover_period_end("3Q26") == "2026-09-30"
    assert _cover_period_end("4Q26") == "2026-12-31"


def _cash_page(caption, values):
    return {1: {"tables": [{"title": "Statement of cash flows", "units": "TRY thousands",
                          "columns": ["June 30, 2026", "June 30, 2025"],
                          "rows": [{"caption": caption, "values": values}]}]}}


def test_a_misread_digit_is_replaced_by_the_printed_line():
    text = "Interest received 3,923,612 1,634,030\nCash outflows from the disposal of equity or debt instruments 5 (4,405) (8,698)"
    doc = doc_from_tables("x.pdf", "0", _cash_page("Interest received", ["3,929,512", "1,634,030"]),
                          {1: text}, "m")
    row = doc.statements[0].rows[0]
    assert row.values == [3923612.0, 1634030.0] and row.unverified == []
    assert any(i.code == "llm_figure_corrected_from_text" for i in doc.issues)

    doc = doc_from_tables("x.pdf", "0", _cash_page(
        "Cash outflows from the disposal of equity or debt instruments", ["(4,605)", "(8,098)"]),
        {1: text}, "m")
    row = doc.statements[0].rows[0]
    assert row.values == [-4405.0, -8698.0] and row.unverified == []


def test_a_misread_is_left_unverified_when_the_digit_count_or_anchor_differs():
    text = "Interest received 3,923,612 1,634,030"
    doc = doc_from_tables("x.pdf", "0", _cash_page("Interest received", ["39,512", "1,634,030"]),
                          {1: text}, "m")
    assert doc.statements[0].rows[0].unverified == [0]
    text = "Interest received 3,923,612 1,700,000\nOther 1,634,030"
    doc = doc_from_tables("x.pdf", "0", _cash_page("Interest received", ["3,929,512", "1,634,030"]),
                          {1: text}, "m")
    assert doc.statements[0].rows[0].unverified == [0]


def test_presentation_group_labels_restore_missing_quarter_headers():
    pages = {12: {"tables": [{
        "title": "Summary Financials",
        "units": "TRY mn",
        "columns": ["2026", "2025*", "y/y", "1Q26*", "q/q", "1H26*", "1H25*", "y/y"],
        "rows": [{"caption": "Adj. EBITDA***",
                  "values": ["7,415", "9,439", "-21", "9,136", "-19", "16,552", "17,540", "-6"]}],
    }]}}
    text = "TRY mn 2Q26 2Q25 y/y 1Q26 q/q 1H26 1H25 y/y Adj. EBITDA 7,415 9,439"
    doc = doc_from_tables("x.pdf", "0", pages, {12: text}, "m")
    columns = doc.statements[0].columns
    assert [(column.months, column.end) for column in columns[:5]] == [
        (3, "2026-06-30"), (3, "2025-06-30"), (None, None),
        (3, "2026-03-31"), (None, None)]
    assert columns[0].header == "2Q26 2026"
    assert any(issue.code == "llm_inherited_group_period_headers" for issue in doc.issues)


def test_a_currency_only_unit_line_means_figures_are_whole_units():
    """Vesta's statements say "(In US dollars)" and nothing else about scale."""
    from finscan.pdf.read import whole_units

    assert whole_units("Statements of Financial Position\n(In US dollars)")
    assert whole_units("For the three-month periods ended March 31, 2026 (In Mexican pesos)")
    assert not whole_units("(In thousands of US dollars)")
    assert not whole_units("(In millions of Mexican pesos)")
    assert not whole_units("Revenue in US dollars grew 12%")


def test_release_headers_with_of_and_first_n_months_are_dated():
    """KOF's release: "For the Second Quarter of: 2026" / "For the first Six Months of: 2026"."""
    from finscan.pdf.columns import parse_columns_from_headers

    cols = parse_columns_from_headers([
        "For the Second Quarter of: 2026", "For the Second Quarter of: 2025",
        "For the Second Quarter of: Δ% Reported", "For the first Six Months of: 2026"])
    assert [(c.months, c.end) for c in cols] == [
        (3, "2026-06-30"), (3, "2025-06-30"), (3, None), (6, "2026-06-30")]


def test_a_share_of_revenue_column_does_not_tie_with_its_amount():
    from finscan.pdf.columns import parse_columns_from_headers

    cols = parse_columns_from_headers(["2Q26", "2Q26 % of Rev.", "2Q25", "2Q25 % of Rev."])
    assert [c.end for c in cols] == ["2026-06-30", None, "2025-06-30", None]
    # A table of percentages alone keeps its dates.
    assert [c.end for c in parse_columns_from_headers(["2Q26 %", "2Q25 %"])] == [
        "2026-06-30", "2025-06-30"]


def test_a_table_titled_with_the_running_page_header_is_a_continuation():
    """BMV reports print "KOF Consolidado" atop every page; the model sometimes
    returns it as the title of a table's second page."""
    from finscan.pdf.llm_read import _is_running_header, _running_headers

    pages = {n: f"KOF Consolidado\nClave de Cotización: KOF\nbody {n}" for n in range(1, 7)}
    running = _running_headers(pages)
    assert _is_running_header("KOF Consolidado", running)
    assert _is_running_header("KOF", running)
    assert not _is_running_header("[800100] Notas - Subclasificaciones", running)


def test_a_month_comma_year_balance_sheet_header_is_dated():
    """Grupo Bimbo's balance sheet: "March, 2026 | Dec, 2025 | % Change"."""
    from finscan.pdf.columns import parse_columns_from_headers

    cols = parse_columns_from_headers(["March, 2026", "Dec, 2025", "% Change"],
                                      statement_kind="balance_sheet")
    assert [c.end for c in cols] == ["2026-03-31", "2025-12-31", None]
    assert parse_columns_from_headers(["May 2026 sales growth"])[0].end is None


def test_bmv_iso_header_lines_give_columns_in_order():
    """Grupo Bimbo's English BMV report wraps its column labels; the ISO dates decide.
    A four-column flow header prints its ranges over two lines."""
    from finscan.pdf.statements import _iso_header_columns

    bs = _iso_header_columns(["Concept Close Current Close Previous", "Quarter Exercise",
                              "2026-03-31 2025-12-31"], "balance_sheet")
    assert [(c.kind, c.end) for c in bs] == [("point_in_time", "2026-03-31"),
                                             ("point_in_time", "2025-12-31")]
    flows = _iso_header_columns([
        "Concept Quarter Current Year Accumulated Current Quarter Previous Year Accumulated Previous",
        "2026-04-01 - 2026-06-30 Year 2025-04-01 - 2025-06-30 Year",
        "2026-01-01 - 2026-06-30 2025-01-01 - 2025-06-30"], "income_statement")
    assert [(c.months, c.end) for c in flows] == [(3, "2026-06-30"), (6, "2026-06-30"),
                                                  (3, "2025-06-30"), (6, "2025-06-30")]


def test_a_rounding_level_of_000_means_whole_units():
    from finscan.pdf.read import _ROUNDING, _ROUNDING_STEP

    m = _ROUNDING.search("Level of rounding used in financial statements: 000")
    assert m and _ROUNDING_STEP[m.group(1).lower()] == 1e3


def test_spanish_dates_and_trimestre_labels_are_dated():
    """BMV commentary: "1T26"; debt note: "31 de marzo de 2026"."""
    from finscan.pdf.columns import parse_columns_from_headers

    cols = parse_columns_from_headers(["Ventas Netas 1T26", "Ventas Netas 1T25", "% Var. (MXN)"])
    assert [(c.months, c.end) for c in cols] == [(3, "2026-03-31"), (3, "2025-03-31"), (None, None)]
    bs = parse_columns_from_headers(["Valor Razonable", "31 de marzo de 2026", "31 de diciembre de 2025"],
                                    statement_kind="balance_sheet")
    assert [c.end for c in bs] == [None, "2026-03-31", "2025-12-31"]


def test_a_supplement_lives_beside_its_pdf():
    from finscan.pdf.read import supplement_path

    assert str(supplement_path("inbox/x/Report.pdf")).endswith("Report.pdf.supplement.json")
