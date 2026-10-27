"""BMV XBRL packages (the exchange's `.zip` of the whole report) read as statements."""
from __future__ import annotations

import json
import zipfile

import pytest

from finscan.company import resolve
from finscan.pdf.read import read_pdf
from finscan.pdf.xbrl import is_package, read_package

_ROLE = "http://cnbv.gob.mx/role/ifrs/ias_7_2017-03-09_role-520000"


def _concept(name, label, abstract=False):
    return {"Nombre": name, "EsAbstracto": abstract,
            "Etiquetas": {"es": {"http://www.xbrl.org/2003/role/label":
                                 {"Valor": label}}}}


def _fact(concept, context, value, unit="HHifrs_mx_Currency"):
    return {"IdConcepto": f"ifrs-full_{concept}", "IdContexto": context,
            "IdUnidad": unit, "ValorNumerico": value, "EsValorNil": False}


def _package(tmp_path):
    data = {
        "Taxonomia": {
            "ConceptosPorId": {
                "ifrs-full_Abstract": _concept("Abstract", "Estado [sinopsis]", True),
                "ifrs-full_Inventories": _concept(
                    "Inventories", "Disminuciones (incrementos) en los inventarios"),
                "ifrs-full_Customers": _concept(
                    "Customers", "Disminución (incremento) de clientes"),
                "ifrs-full_Cash": _concept("Cash", "Efectivo"),
                "ifrs-full_Eps": _concept("Eps", "Utilidad por acción"),
            },
            "RolesPresentacion": [
                {"Nombre": "[105000] Comentarios", "Uri": "x", "Estructuras": []},
                {"Nombre": "[520000] Estado de flujos de efectivo, método indirecto",
                 "Uri": _ROLE,
                 "Estructuras": [{"IdConcepto": "ifrs-full_Abstract", "SubEstructuras": [
                     {"IdConcepto": "ifrs-full_Inventories"},
                     {"IdConcepto": "ifrs-full_Customers"},
                     {"IdConcepto": "ifrs-full_Eps"}]}]},
                {"Nombre": "[210000] Estado de situación financiera", "Uri": "y",
                 "Estructuras": [{"IdConcepto": "ifrs-full_Cash"}]},
            ],
        },
        "UnidadesPorId": {
            "HHifrs_mx_Currency": {"Medidas": [{"EspacioNombres": "http://www.xbrl.org/2003/iso4217"}]},
            "HHifrs_mx_Shares": {"Medidas": [{"EspacioNombres": "http://www.xbrl.org/2003/instance"}]},
        },
        "ContextosPorId": {
            "ytd": {"ContieneInformacionDimensional": False, "Periodo": {
                "FechaInicio": "2026-01-01T00:00:00Z", "FechaFin": "2026-06-30T00:00:00Z"}},
            "prior": {"ContieneInformacionDimensional": False, "Periodo": {
                "FechaInicio": "2025-01-01T00:00:00Z", "FechaFin": "2025-06-30T00:00:00Z"}},
            "end": {"ContieneInformacionDimensional": False, "Periodo": {
                "FechaInstante": "2026-06-30T00:00:00Z"}},
            "member": {"ContieneInformacionDimensional": True, "Periodo": {
                "FechaInicio": "2026-01-01T00:00:00Z", "FechaFin": "2026-06-30T00:00:00Z"}},
        },
        "HechosPorId": {
            "1": _fact("Inventories", "ytd", -19432000),
            "2": _fact("Inventories", "prior", 280596000),
            "3": _fact("Customers", "ytd", -380505000),
            "4": _fact("Customers", "member", 999),
            "5": _fact("Eps", "ytd", 0.39, "HHifrs_mx_Shares"),
            "6": _fact("Cash", "end", 29651600000),
        },
    }
    path = tmp_path / "ifrsxbrl_1_2026-02_1.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ifrsxbrl_1_2026-02_1.json", json.dumps(data))
    return path


def test_a_package_becomes_statements_in_presentation_order(tmp_path):
    doc = read_package(_package(tmp_path), "sha")
    assert doc.units == "units"
    assert [s.kind for s in doc.statements] == ["cash_flow", "balance_sheet"]
    cash = doc.statements[0]
    assert cash.title.startswith("[520000]") and cash.units == "units"
    assert [r.caption for r in cash.rows] == [
        "Disminuciones (incrementos) en los inventarios",
        "Disminución (incremento) de clientes"]


def test_columns_carry_their_span_and_end_date(tmp_path):
    cash = read_package(_package(tmp_path), "sha").statements[0]
    assert [(c.kind, c.months, c.end) for c in cash.columns] == [
        ("period", 6, "2026-06-30"), ("period", 6, "2025-06-30")]
    assert cash.rows[0].values == [-19432000.0, 280596000.0]
    assert cash.rows[1].values == [-380505000.0, None]


def test_a_period_only_a_stray_line_reports_is_not_a_column(tmp_path):
    """The cash flow's net income is also reported for the quarter and twelve
    months, but no other line is. Left as columns they would be chosen as 'the
    quarter' for every row and read blank."""
    names = [f"Line{i}" for i in range(8)]
    data = {
        "Taxonomia": {
            "ConceptosPorId": {f"ifrs-full_{n}": _concept(n, n) for n in names},
            "RolesPresentacion": [{
                "Nombre": "[520000] Estado de flujos de efectivo", "Uri": "x",
                "Estructuras": [{"IdConcepto": f"ifrs-full_{n}"} for n in names]}],
        },
        "UnidadesPorId": {"HHifrs_mx_Currency": {
            "Medidas": [{"EspacioNombres": "http://www.xbrl.org/2003/iso4217"}]}},
        "ContextosPorId": {
            "ytd": {"ContieneInformacionDimensional": False, "Periodo": {
                "FechaInicio": "2026-01-01T00:00:00Z", "FechaFin": "2026-06-30T00:00:00Z"}},
            "quarter": {"ContieneInformacionDimensional": False, "Periodo": {
                "FechaInicio": "2026-04-01T00:00:00Z", "FechaFin": "2026-06-30T00:00:00Z"}},
        },
        "HechosPorId": {
            **{f"y{i}": _fact(n, "ytd", float(i + 1)) for i, n in enumerate(names)},
            "q": _fact(names[0], "quarter", 99.0),
        },
    }
    path = tmp_path / "pkg.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("pkg.json", json.dumps(data))

    cash = read_package(path, "sha").statements[0]
    assert [c.months for c in cash.columns] == [6]
    assert all(len(row.values) == 1 for row in cash.rows)


def test_a_balance_column_is_a_position_on_a_date(tmp_path):
    balance = read_package(_package(tmp_path), "sha").statements[1]
    assert [(c.kind, c.months, c.end) for c in balance.columns] == [
        ("point_in_time", None, "2026-06-30")]
    assert balance.rows[0].values == [29651600000.0]


def test_dimensional_and_non_monetary_facts_are_left_out(tmp_path):
    cash = read_package(_package(tmp_path), "sha").statements[0]
    assert all(r.caption != "Utilidad por acción" for r in cash.rows)
    assert cash.rows[1].values[0] == -380505000.0      # not the member's 999


def test_read_pdf_hands_a_zip_to_the_package_reader(tmp_path):
    doc = read_pdf(_package(tmp_path))
    assert [s.kind for s in doc.statements] == ["cash_flow", "balance_sheet"]
    assert any(i.code == "xbrl_package" for i in doc.issues)


def test_a_zip_that_is_not_a_package_is_refused(tmp_path):
    bad = tmp_path / "other.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("note.txt", "hello")
    with pytest.raises(ValueError):
        read_package(bad, "sha")


def test_a_package_in_the_company_folder_is_a_filing(tmp_path):
    folder = tmp_path / "inbox" / "Televisa"
    folder.mkdir(parents=True)
    (tmp_path / "models").mkdir()
    (folder / "release.pdf").write_text("x", encoding="utf-8")
    (folder / "ifrsxbrl_1_2026-02_1.zip").write_text("x", encoding="utf-8")
    (folder / "Model_1Q26.xlsx").write_text("x", encoding="utf-8")
    paths = resolve("televisa", tmp_path)
    assert sorted(p.name for p in paths.pdfs) == ["ifrsxbrl_1_2026-02_1.zip", "release.pdf"]
    assert is_package(paths.pdfs[0])
