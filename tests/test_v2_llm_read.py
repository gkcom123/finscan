"""The opt-in LLM reader — offline: the model's JSON is stubbed, nothing is called."""
from __future__ import annotations

from finscan2.mapping.schema import Mapping
from finscan2.pdf.llm_read import doc_from_tables

_HEADERS = ["2nd Quarter Ended 30/06/2026 RM'000", "2nd Quarter Ended 30/06/2025 RM'000",
            "Financial Period Ended 30/06/2026 RM'000", "Financial Period Ended 30/06/2025 RM'000"]
_TITLE = "UNAUDITED CONDENSED CONSOLIDATED STATEMENT OF COMPREHENSIVE INCOME"


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
