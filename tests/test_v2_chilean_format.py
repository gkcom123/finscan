"""Chilean-format filings (Entel, Falabella): dotted thousands, decimal commas, "M$".

The readings that are only safe in such a table — a decimal comma, a lone "748.160" —
are gated on the table printing dotted thousands elsewhere, because "56,51" in a
US-format table is a misread "5,651" and must keep its old reading.
"""
from __future__ import annotations

from finscan2.pdf.columns import parse_columns_from_headers
from finscan2.pdf.llm_read import doc_from_tables
from finscan2.pdf.read import _UNIT_PATTERNS, _sniff

_COLUMNS = ["Acumulado 01-01-2026 30-06-2026", "Acumulado 01-01-2025 30-06-2025"]


def _table(rows, units="M$", columns=None):
    return {1: {"tables": [{"title": "ESTADOS DE RESULTADOS", "units": units,
                            "columns": columns or _COLUMNS,
                            "rows": [{"caption": c, "values": v} for c, v in rows]}]}}


def test_dotted_thousands_and_decimal_commas_read_as_numbers():
    pages = _table([("Ingresos de Actividades Ordinarias", ["1.512.033.764", "1.412.720.853"]),
                    ("Gastos por Beneficios", ["(176.866.381)", "(170.273.897)"]),
                    ("Ganancia por Acción Básica", ["177,67", "137,65"]),
                    ("Otros", ["748.160", "-"])])
    text = ("Ingresos de Actividades Ordinarias 22 1.512.033.764 1.412.720.853\n"
            "Gastos por Beneficios 20 (176.866.381) (170.273.897)\n"
            "Ganancia por Acción Básica 177,67 137,65\nOtros 748.160 -")
    rows = {r.caption: r for r in doc_from_tables("x.pdf", "0", pages, {1: text}, "m").statements[0].rows}
    assert rows["Ingresos de Actividades Ordinarias"].values == [1512033764.0, 1412720853.0]
    assert rows["Gastos por Beneficios"].values == [-176866381.0, -170273897.0]
    assert rows["Ganancia por Acción Básica"].values == [177.67, 137.65]
    assert rows["Otros"].values == [748160.0, None]
    assert all(r.unverified == [] for r in rows.values())


def test_a_us_format_table_keeps_the_comma_stripping_reading():
    """The vision model returned "56,51" for a printed "5,651"; only the old
    comma-stripping reading gets that right."""
    pages = _table([("Other Income, net", ["56,51", "(619)"])], units="SAR'000",
                   columns=["January - June 2026", "January - June 2025"])
    doc = doc_from_tables("x.pdf", "0", pages, {1: "Other Income, net 5,651 (619)"}, "m")
    assert doc.statements[0].rows[0].values == [5651.0, -619.0]


def test_dropped_digits_are_repaired_from_the_text_layer_in_a_dotted_table():
    pages = _table([("Ingresos", ["1.512.033.764", "1.412.720.853"]),
                    ("Otros Gastos", ["(99.333.931)", "(843.243.564)"])])
    text = ("Ingresos 22 1.512.033.764 1.412.720.853\n"
            "Otros Gastos 24 (909.333.911) (843.243.564)")
    doc = doc_from_tables("x.pdf", "0", pages, {1: text}, "m")
    row = doc.statements[0].rows[1]
    assert row.values == [-909333911.0, -843243564.0] and row.unverified == []
    assert any("Otros Gastos" in i.message for i in doc.issues
               if i.code == "llm_figure_corrected_from_text")


def test_dropped_digits_are_not_repaired_when_the_table_is_not_dotted():
    """In other formats a text line carries fragments and footnote markers, so a
    digit-count change stays a refusal rather than a right-aligned guess."""
    pages = _table([("Other expenses", ["(99,333,931)", "(843,243,564)"])], units="SAR'000",
                   columns=["January - June 2026", "January - June 2025"])
    doc = doc_from_tables("x.pdf", "0", pages,
                          {1: "Other expenses 24 (909,333,911) (843,243,564)"}, "m")
    assert doc.statements[0].rows[0].unverified == [0]


def test_a_dropped_parenthesis_is_restored_in_a_dotted_table():
    """Either sign is accepted as "printed", so a lost bracket would otherwise write
    an expense as income."""
    pages = _table([("Pérdidas por Deterioro", ["21.828.027", "(22.782.496)"]),
                    ("Ingresos", ["754.737.076", "707.083.926"])])
    text = ("Pérdidas por Deterioro 23 (21.828.027) (22.782.496)\n"
            "Ingresos 22 754.737.076 707.083.926")
    rows = doc_from_tables("x.pdf", "0", pages, {1: text}, "m").statements[0].rows
    assert rows[0].values == [-21828027.0, -22782496.0]
    assert rows[1].values == [754737076.0, 707083926.0]


def test_chilean_peso_scale_markers_are_read_as_units():
    assert _sniff("30-06-2026 M$", _UNIT_PATTERNS) == "thousands"
    assert _sniff("Cifras en MM$", _UNIT_PATTERNS) == "millions"
    assert _sniff("US$ M and 5m$ total", _UNIT_PATTERNS) is None


def test_date_ranges_without_a_dash_or_with_dashed_dates_are_spans():
    """Entel: "Acumulado 01-01-2026 30-06-2026" is six months ending 30 June, not a
    column dated 1 January."""
    cols = parse_columns_from_headers(
        ["Acumulado 01-01-2026 30-06-2026", "Trimestre 01-04-2026 30-06-2026",
         "01-01-2026 30-06-2026 M$"], statement_kind="income_statement")
    assert [(c.months, c.end) for c in cols] == [
        (6, "2026-06-30"), (3, "2026-06-30"), (6, "2026-06-30")]
    balance = parse_columns_from_headers(["30-06-2026 M$", "31-12-2025 M$"],
                                         statement_kind="balance_sheet")
    assert [(c.kind, c.months, c.end) for c in balance] == [
        ("point_in_time", None, "2026-06-30"), ("point_in_time", None, "2025-12-31")]


def test_spanish_month_abbreviation_ranges_are_spans():
    """Falabella: "01-abr-26 30-jun-26" is the quarter, "01-ene-26 30-jun-26" the half."""
    cols = parse_columns_from_headers(
        ["01-ene-26 30-jun-26", "01-abr-26 30-jun-26", "01-ene-25 31-dic-25"],
        statement_kind="other")
    assert [(c.months, c.end) for c in cols] == [
        (6, "2026-06-30"), (3, "2026-06-30"), (12, "2025-12-31")]
