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


# --------------------------------------------------------------------------- #
# BMV / XBRL reports: Spanish rounding declaration, untitled continuation pages
# --------------------------------------------------------------------------- #

_BMV_HEADERS = ["Acumulado Año Actual 2026-01-01 - 2026-03-31",
                "Acumulado Año Anterior 2025-01-01 - 2025-03-31"]
_BMV_TITLE = "[520000] Estado de flujos de efectivo, método indirecto"


def _bmv_pages(second_title=""):
    rows = [{"caption": f"Línea {i}", "values": [f"{i},000,000", f"{i + 1},000,000"]}
            for i in range(1, 12)]
    return {
        1: {"tables": [{"title": _BMV_TITLE, "units": "", "columns": _BMV_HEADERS,
                        "rows": rows}]},
        2: {"tables": [{"title": second_title, "units": "", "columns": _BMV_HEADERS,
                        "rows": [{"caption": "Intereses pagados",
                                  "values": ["1,230,956,000", "1,312,102,000"]}]}]},
    }


def _bmv_text(declared=True):
    lines = [f"Línea {i} {i},000,000 {i + 1},000,000" for i in range(1, 12)]
    cover = "Grado de redondeo utilizado en los estados 0\n" if declared else ""
    return {1: cover + "\n".join(lines), 2: "Intereses pagados 1,230,956,000 1,312,102,000"}


def test_a_spanish_xbrl_rounding_declaration_sets_the_units():
    """KOF's BMV report prints no scale on any table; its cover declares rounding
    "0" (whole pesos). Left unknown, those figures would be scaled as the merged
    release's millions - a million times too large."""
    doc = doc_from_tables("bmv.pdf", "0", _bmv_pages(), _bmv_text(), "m")
    assert doc.units == "units"
    assert all(s.units == "units" for s in doc.statements)
    assert any(i.code == "units_from_rounding" for i in doc.issues)
    assert not any(i.code == "units_unknown" for i in doc.issues)


def test_without_a_declaration_the_units_stay_unknown():
    doc = doc_from_tables("bmv.pdf", "0", _bmv_pages(), _bmv_text(declared=False), "m")
    assert doc.units is None
    assert any(i.code == "units_unknown" and i.severity == "error" for i in doc.issues)


def test_an_untitled_table_on_the_next_page_continues_the_statement():
    doc = doc_from_tables("bmv.pdf", "0", _bmv_pages(), _bmv_text(), "m")
    assert len(doc.statements) == 1
    assert doc.statements[0].rows[-1].caption == "Intereses pagados"


def test_a_titled_table_on_the_next_page_is_its_own_statement():
    doc = doc_from_tables("bmv.pdf", "0", _bmv_pages("[610000] Estado de cambios"),
                          _bmv_text(), "m")
    assert len(doc.statements) == 2
