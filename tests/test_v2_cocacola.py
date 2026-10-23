"""BMV mappings learned from Q1 and applied to standalone Q2 / cumulative cash flow."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from finscan2.mapping.compile import compile_mapping
from finscan2.mapping.schema import Mapping
from finscan2.match.resolve import resolve_values
from finscan2.model.discover import RowLayout, SheetLayout
from finscan2.model.load import resolve as bind_map
from finscan2.pdf import statements
from finscan2.pdf.llm_read import doc_from_tables
from finscan2.schema import Column, PdfDoc, Statement, StatementRow


@pytest.mark.parametrize(("code", "kind"), [
    ("210000", "balance_sheet"), ("310000", "income_statement"),
    ("410000", "comprehensive_income"), ("520000", "cash_flow"),
    ("610000", "equity"), ("800100", "balance_sheet"),
    ("800200", "income_statement"),
])
def test_bmv_roles_classify_spanish_titles(code, kind):
    assert statements.classify(f"[{code}] Titulo en castellano") == kind
    assert statements.classify("[105000] Comentarios") == "other"


def test_a_bmv_table_of_contents_is_not_a_statement():
    text = "\n".join([
        "[210000] Estado de situacion financiera..............15",
        "[310000] Estado de resultados.......................17",
        "[410000] Estado del resultado integral..............18",
        "[520000] Estado de flujos de efectivo...............20",
    ])
    assert statements.extract_statement(1, text) is None


def test_a_cash_flow_continuation_keeps_leading_zero_rows(monkeypatch):
    headers = ["2026-01-01 - 2026-06-30", "2025-01-01 - 2025-06-30"]
    first_caption = "Dividendos recibidos"
    layout = SimpleNamespace(headers=headers, ranges=[],
                             rows=[(first_caption, ["0", "0"])])
    monkeypatch.setattr(statements, "table_layout", lambda page: layout)
    previous = Statement(
        page=20, kind="cash_flow", title="[520000] Estado de flujos de efectivo",
        heading="", columns=[
            Column(0, headers[0], months=6, end="2026-06-30"),
            Column(1, headers[1], months=6, end="2025-06-30"),
        ])
    text = ("\n".join(headers) + "\nDividendos recibidos 0 0\n"
            "Intereses pagados 0 0\nIntereses cobrados 1,197,622,000 1,214,133,000\n")

    rows = statements.extract_continuation(text, object(), previous)

    assert [r.caption for r in rows] == [
        "Dividendos recibidos", "Intereses pagados", "Intereses cobrados"]
    assert rows[0].values == [0, 0]
    assert statements.extract_continuation(
        "[800001] Otra tabla\n" + text, object(), previous) == []
    layout.headers = ["2026-01-01 - 2026-03-31", "2025-01-01 - 2025-03-31"]
    assert statements.extract_continuation(text, object(), previous) == []


def test_vision_bmv_continuations_use_the_same_classification():
    headers = ["2026-01-01 - 2026-06-30", "2025-01-01 - 2025-06-30"]
    pages = {
        20: {"tables": [{"title": "[520000] Estado de flujos de efectivo",
                         "columns": headers, "rows": [
                             {"caption": "Intereses pagados", "values": ["1,000", "900"]}]}]},
        21: {"tables": [{"title": "", "columns": headers, "rows": [
            {"caption": "Intereses cobrados", "values": ["200", "100"]}]}]},
    }
    doc = doc_from_tables("bmv.pdf", "x", pages, {}, "stub")
    assert len(doc.statements) == 1
    assert doc.statements[0].kind == "cash_flow"
    assert doc.statements[0].units == "units"
    assert [r.caption for r in doc.statements[0].rows] == [
        "Intereses pagados", "Intereses cobrados"]


@pytest.mark.parametrize("quarter", [1, 2])
def test_reviewed_mapping_reproduces_q1_and_resolves_q2(tmp_path, quarter):
    mapping = Mapping.load(Path(__file__).resolve().parents[1] / "mappings" / "cocacola.json")
    assert mapping.reader == "llm"
    expected = {
        "Revenues": (70925.377, 76318.440),
        "COGS": (-37670.059, -40380.499),
        "Finance Income": (514.509, 683.864),
        "Finance costs": (-2086.963, -2272.873),
        "Foreign exchange (loss) gain, net": (-116.543, 95.831),
        "Market value gain on financial instruments": (-167.403, 88.236),
        "Income tax benefit/(expense)": (-2687.887, -2741.069),
        "Cash interest paid": (-1230.956, -1500.877),
    }
    mapping.rows = [r for r in mapping.rows if r.label in expected]
    layout = SheetLayout(
        sheet="Model", reference_col=3, write_col=4, units="millions",
        period_dates={3: "2025-12-31" if quarter == 1 else "2026-03-31"},
        rows=[RowLayout(i, r.label, r.section, "input", False)
              for i, r in enumerate(mapping.rows, start=5)])
    model, issues = compile_mapping(mapping, layout)
    assert not issues
    book = Workbook()
    book.active.title = "Model"
    cash_row = next(r.row for r in layout.rows if r.label == "Cash interest paid")
    book.active.cell(cash_row, 3, -1230.956)
    path = tmp_path / "model.xlsx"
    book.save(path)
    book.close()

    end = "2026-03-31" if quarter == 1 else "2026-06-30"
    columns = [Column(0, f"Quarter {end}", months=3, end=end)]
    if quarter == 2:
        columns.append(Column(1, "Accumulated", months=6, end=end))
    income = {
        "Ingresos": (70925377000, 76318440000),
        "Costo de ventas": (37670059000, 40380499000),
        "Impuestos a la utilidad": (2687887000, 2741069000),
    }
    finance = {
        "Intereses ganados": (514509000, 683864000),
        "Intereses devengados a cargo": (2086963000, 2272873000),
        "Utilidad por fluctuación cambiaria": (0, 95831000),
        "Pérdida por fluctuación cambiaria": (116543000, 0),
        "Utilidad por cambios en el valor razonable de derivados": (0, 88236000),
        "Utilidad por cambios en valor razonable de instrumentos financieros": (0, 0),
        "Pérdidas por cambio en el valor razonable de derivados": (167403000, 0),
        "Pérdida por cambios en valor razonable de instrumentos financieros": (0, 0),
    }

    def rows(figures):
        return [StatementRow(c, [v[quarter - 1]] + ([999999999999] if quarter == 2 else []), "")
                for c, v in figures.items()]

    cash = 1230956000 if quarter == 1 else 2731833000
    doc = PdfDoc(path="bmv.pdf", sha256="x", units="units", statements=[
        Statement(16, "income_statement", "[310000] Estado de resultados", "",
                  columns=columns, rows=rows(income)),
        Statement(47, "income_statement", "[800200] Analisis de ingresos y gastos", "",
                  columns=columns, rows=rows(finance)),
        Statement(19, "cash_flow", "[520000] Estado de flujos de efectivo", "",
                  columns=[Column(0, "Accumulated", months=quarter * 3, end=end)],
                  rows=[StatementRow("Intereses pagados", [-4353095000], ""),
                        StatementRow("Intereses pagados", [0], ""),
                        StatementRow("Intereses pagados", [cash], "")]),
    ])
    values = resolve_values(doc, bind_map(model, layout), str(path), end)
    for value in values.values:
        assert value.ok, value.unresolved
        assert value.value == pytest.approx(expected[value.label][quarter - 1])
    assert len(values.resolved) == len(expected)
