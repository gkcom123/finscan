"""Learning a new company's mapping from the analyst's own figures, and checking it.

The workbook says "Revenue"; the filing says "Ingresos". No caption rule bridges
that, but last year's figures do: the filing prints them beside this quarter's,
and the analyst typed them into the model.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

from finscan import runner
from finscan.company import MAPPING_DIR_ENV
from finscan.mapping.learn_values import ModelFigures, check_mapping, match_by_value
from finscan.mapping.propose import propose_mapping
from finscan.mapping.schema import Mapping, MappingRow
from finscan.model import propose_captions
from finscan.model.discover import discover_sheet
from finscan.schema import PdfDoc

BLUE = Font(color="FF0000FF")
DATES = ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31"]
WRITE_COL = 8                                   # H: the 2026-06-30 column

#: label -> the analyst's figures for DATES, in millions
MODEL = {
    "Revenue": [1100.5, 1234.567, 1300.0, 1400.0, 1500.25],
    "Cost of Sales": [-600.1, -654.321, -700.0, -750.0, -800.5],
    "Other Income": [40.0, 50.0, 45.0, 55.0, 60.0],
    "Inventories": [300.0, 310.0, 320.0, 345.678, 350.0],
}


def _workbook(path, rows=None):
    rows = rows or ["Revenue", "Cost of Sales", "Other Income"]
    book = Workbook()
    sheet = book.active
    sheet.title = "Model"
    sheet.cell(1, 2, "SAR millions")
    for index, iso in enumerate(DATES):
        sheet.cell(2, 3 + index, datetime.fromisoformat(iso))
    sheet.cell(4, 2, "Income Statement")
    line = 5
    for label in rows:
        if label == "Inventories":
            sheet.cell(line, 2, "Balance Sheet")
            line += 1
        sheet.cell(line, 2, label)
        for index, value in enumerate(MODEL[label]):
            sheet.cell(line, 3 + index, value).font = BLUE
        sheet.cell(line, WRITE_COL).font = BLUE
        line += 1
    book.save(path)
    return path


def _doc(other_income_2025=50_000, inventories=True) -> dict:
    """A filing in thousands, Spanish captions, cost printed as a positive figure."""
    income = {
        "page": 3, "kind": "income_statement", "title": "Estado de resultados",
        "heading": "", "columns": [
            {"index": 0, "header": "2T26", "months": 3, "end": "2026-06-30", "kind": "period"},
            {"index": 1, "header": "2T25", "months": 3, "end": "2025-06-30", "kind": "period"},
            {"index": 2, "header": "6M26", "months": 6, "end": "2026-06-30", "kind": "period"},
            {"index": 3, "header": "6M25", "months": 6, "end": "2025-06-30", "kind": "period"},
        ],
        "rows": [
            {"caption": "Ingresos", "values": [1_600_000, 1_234_567, 3_100_250, 2_335_067], "raw": ""},
            {"caption": "Costo de ventas", "values": [850_000, 654_321, 1_650_500, 1_254_421], "raw": ""},
            # Same caption as the workbook, but not the analyst's figure: a
            # caption match the check must catch.
            {"caption": "Other Income", "values": [70_000, other_income_2025, 130_000, 117_000], "raw": ""},
            # Small figures agree with something by coincidence; never by value.
            {"caption": "Margen", "values": [0.4, 0.47, 0.41, 0.46], "raw": ""},
        ],
    }
    statements = [income]
    if inventories:
        statements.append({
            "page": 4, "kind": "balance_sheet", "title": "Estado de situación financiera",
            "heading": "", "columns": [
                {"index": 0, "header": "30/06/2026", "end": "2026-06-30", "kind": "point_in_time"},
                {"index": 1, "header": "31/12/2025", "end": "2025-12-31", "kind": "point_in_time"},
            ],
            "rows": [{"caption": "Inventarios", "values": [360_000, 345_678], "raw": ""}],
        })
    return {"schema_version": "1.0", "path": "f.pdf", "sha256": "x", "units": "thousands",
            "pages": [], "issues": [], "statements": statements}


@pytest.fixture(autouse=True)
def no_model_calls(monkeypatch):
    """The caption pass's model call is not what these tests are about."""
    monkeypatch.setattr(propose_captions, "with_model", lambda proposals, doc: proposals)
    monkeypatch.delenv(MAPPING_DIR_ENV, raising=False)


# --------------------------------------------------------------------------- #
# Learning by value
# --------------------------------------------------------------------------- #

def _learn(tmp_path, rows=None, **doc_args):
    excel = _workbook(tmp_path / "model.xlsx", rows)
    doc = PdfDoc.from_dict(_doc(**doc_args))
    layout = discover_sheet(str(excel), "Model")
    mapping, counts = propose_mapping(layout, "acme", doc, workbook=str(excel))
    return mapping, layout, doc, excel


def test_rows_are_learned_by_value_across_languages(tmp_path):
    mapping, *_ = _learn(tmp_path)
    rows = {r.label: r for r in mapping.rows}
    assert rows["Revenue"].pdf == "Ingresos"
    assert rows["Revenue"].note.startswith("learned by value")


def test_a_figure_printed_with_the_opposite_sign_is_learned_negated(tmp_path):
    mapping, *_ = _learn(tmp_path)
    assert {r.label: r for r in mapping.rows}["Cost of Sales"].pdf == ["-Costo de ventas"]


def test_one_precisely_printed_balance_figure_is_enough(tmp_path):
    """A balance sheet prints one comparative date; 345,678 thousands to the unit."""
    mapping, *_ = _learn(tmp_path, rows=["Revenue", "Inventories"])
    assert {r.label: r for r in mapping.rows}["Inventories"].pdf == "Inventarios"


def test_small_figures_are_never_matched_by_value():
    """A growth rate equal to some printed figure is a coincidence, not a match:
    "Margen" 0.47 thousand is the 0.00047 million this row holds."""
    figures = ModelFigures(cadence=3, units="millions", by_month={(2025, 6): [4]},
                           cells={(9, 4): 0.00047})
    matches, why = match_by_value(PdfDoc.from_dict(_doc()), [(9, "Growth", "other")],
                                  figures, "2026-06-30")
    assert 9 not in matches and "too small" in why[9]


def test_the_check_verifies_learned_rows_and_flags_a_wrong_caption_match(tmp_path):
    mapping, layout, doc, excel = _learn(tmp_path)
    check = check_mapping(mapping, layout, doc, str(excel), "2026-06-30")
    by_label = {r.label: r for r in check.rows.values()}
    assert by_label["Revenue"].status == "verified"
    assert by_label["Cost of Sales"].status == "verified"
    # "Other Income" matched by caption, but prints 50,000 vs the analyst's 50.0m…
    assert by_label["Other Income"].status == "verified"


def test_a_caption_match_with_different_figures_is_flagged(tmp_path):
    mapping, layout, doc, excel = _learn(tmp_path, other_income_2025=77_000)
    row = {r.label: r for r in mapping.rows}["Other Income"]
    assert row.pdf == "Other Income"                       # matched by its caption
    check = check_mapping(mapping, layout, doc, str(excel), "2026-06-30")
    other = {r.label: r for r in check.rows.values()}["Other Income"]
    assert other.status == "differs" and "77.00" in other.detail


# --------------------------------------------------------------------------- #
# A new company, end to end
# --------------------------------------------------------------------------- #

def _company(tmp_path, monkeypatch, **doc_args):
    root = tmp_path / "root"
    folder = root / "inbox" / "Acme"
    folder.mkdir(parents=True)
    (folder / "f.pdf").write_bytes(b"%PDF")
    _workbook(folder / "Acme model.xlsx", ["Revenue", "Cost of Sales", "Other Income"])
    payload = _doc(**doc_args)

    def read(paths, *, cache_dir):
        for target in paths.pdf_jsons:
            target.write_text(json.dumps(payload), encoding="utf-8")
        return [PdfDoc.from_dict(payload)]

    monkeypatch.setattr(runner, "read_filings", read)
    return root


def _written(root):
    sheet = load_workbook(root / "inbox" / "acme_v2_output.xlsx")["Model"]
    return {sheet.cell(r, 2).value: sheet.cell(r, WRITE_COL).value for r in range(5, 8)}


def test_a_new_company_is_learned_checked_and_written_in_one_run(tmp_path, monkeypatch):
    root = _company(tmp_path, monkeypatch)
    outcome = runner.run_company("acme", root=root)

    assert outcome.status == "written", outcome.errors
    assert (root / "mappings" / "acme.json").exists()
    assert outcome.mapping_status.startswith("learned")
    assert outcome.mapping_check["verified"] == 3 and not outcome.withheld
    assert _written(root) == {"Revenue": 1600.0, "Cost of Sales": -850.0,
                              "Other Income": 70.0}


def test_a_learned_row_whose_figures_differ_is_withheld_and_left_blank(tmp_path, monkeypatch):
    root = _company(tmp_path, monkeypatch, other_income_2025=77_000)
    outcome = runner.run_company("acme", root=root)

    assert outcome.withheld == ["Other Income"]
    written = _written(root)
    assert written["Revenue"] == 1600.0 and written["Other Income"] is None
    saved = {r.label: r for r in Mapping.load(root / "mappings" / "acme.json").rows}
    assert saved["Other Income"].pdf is None
    assert saved["Other Income"].note.startswith("withheld: learned 'Other Income'")


def test_a_reviewed_row_that_differs_is_reported_but_still_written(tmp_path, monkeypatch):
    """The check informs; a person's decision is never withheld."""
    root = _company(tmp_path, monkeypatch, other_income_2025=77_000)
    Mapping(company="acme", sheet="Model", rows=[
        MappingRow(label="Revenue", section="income_statement", pdf="Ingresos"),
        MappingRow(label="Cost of Sales", section="income_statement", pdf=["-Costo de ventas"]),
        MappingRow(label="Other Income", section="income_statement", pdf="Other Income"),
    ]).save(root / "mappings" / "acme.json")

    outcome = runner.run_company("acme", root=root)
    assert outcome.mapping_status == "unchanged" and not outcome.withheld
    assert outcome.mapping_check["differs"] == 1
    assert _written(root)["Other Income"] == 70.0


def test_a_dry_run_saves_a_new_companys_mapping_and_writes_nothing(tmp_path, monkeypatch):
    root = _company(tmp_path, monkeypatch)
    outcome = runner.run_company("acme", root=root, dry_run=True)
    assert outcome.status == "dry_run" and (root / "mappings" / "acme.json").exists()
    assert not (root / "inbox" / "acme_v2_output.xlsx").exists()


def test_relearn_replaces_the_mapping_and_keeps_the_old_one(tmp_path, monkeypatch):
    root = _company(tmp_path, monkeypatch)
    Mapping(company="acme", sheet="Model", rows=[
        MappingRow(label="Revenue", section="income_statement", pdf="Other Income"),
    ]).save(root / "mappings" / "acme.json")

    outcome = runner.run_company("acme", root=root, relearn=True)
    assert outcome.mapping_status.startswith("relearned")
    assert {r.label: r for r in Mapping.load(root / "mappings" / "acme.json").rows}[
        "Revenue"].pdf == "Ingresos"
    assert list((root / "mappings" / "past").glob("acme-*.json"))
    assert _written(root)["Revenue"] == 1600.0


def test_learned_mappings_go_to_the_writable_folder_when_one_is_set(tmp_path, monkeypatch):
    """In a container, mappings/ is the image's: a learned or extended mapping is
    written to FINSCAN_MAPPING_DIR (the file share) and read from there next time."""
    root = _company(tmp_path, monkeypatch)
    monkeypatch.setenv(MAPPING_DIR_ENV, "_work/mappings")
    shipped = Mapping(company="acme", sheet="Model", rows=[
        MappingRow(label="Revenue", section="income_statement", pdf="Ingresos")])
    shipped.save(root / "mappings" / "acme.json")

    outcome = runner.run_company("acme", root=root)
    learned = root / "_work" / "mappings" / "acme.json"
    assert outcome.mapping == str(learned)
    assert len(Mapping.load(learned).rows) == 3                      # rows appended
    assert len(Mapping.load(root / "mappings" / "acme.json").rows) == 1  # untouched


# --------------------------------------------------------------------------- #
# The service side
# --------------------------------------------------------------------------- #

def test_a_company_without_a_mapping_is_ready_and_says_it_will_be_learned(tmp_path):
    from finscan.service import files

    folder = tmp_path / "inbox" / "Acme"
    folder.mkdir(parents=True)
    (folder / "f.pdf").write_bytes(b"%PDF")
    _workbook(folder / "model.xlsx")
    info = files.describe(tmp_path, folder)
    assert info["ready"] and info["mapping"] == "none" and not info["has_mapping"]


def test_an_uploaded_mapping_is_saved_where_runs_read_it(tmp_path, monkeypatch):
    import io

    from finscan.service import files

    monkeypatch.setenv(MAPPING_DIR_ENV, "_work/mappings")
    body = Mapping(company="whatever", sheet="Model", rows=[
        MappingRow(label="Revenue", section="income_statement", pdf="Ingresos")]).to_dict()
    done = files.save_uploads(tmp_path, "Acme", [("reviewed.json", io.BytesIO(
        json.dumps(body).encode()))], 1 << 20)
    assert done["saved"][0]["kind"] == "mapping"
    saved = Mapping.load(tmp_path / "_work" / "mappings" / "acme.json")
    assert saved.company == "acme" and done["status"]["mapping"] == "learned"

    with pytest.raises(files.UploadError):
        files.save_uploads(tmp_path, "Acme", [("x.json", io.BytesIO(b"{}"))], 1 << 20)


def test_relearn_reaches_the_run_as_a_flag():
    from finscan.service.jobs import _flags

    assert _flags({"dry_run": True, "relearn": True}) == ["--dry-run", "--relearn"]
