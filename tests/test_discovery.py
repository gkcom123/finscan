"""Discovery must derive everything from the workbook. Three models that agree
on nothing structural are the test."""
from __future__ import annotations

from finscan.excel.discovery import discover


def test_northwind_geometry_and_scope(workspace):
    plan = discover(workspace["northwind"])
    assert plan.colors_found

    scoped = {s.sheet for s in plan.in_scope}
    assert scoped == {"P&L Summary"}, "Cover and Assumptions must not be written to"

    s = plan.sheet("P&L Summary")
    assert s.label_col == 1 and s.header_row == 4
    assert s.units == "crores"
    assert s.write_col_letter == "F" and s.write_mode == "append"
    assert len(s.writable_rows) == 13
    assert len(s.formula_rows) == 5


def test_acme_different_geometry_is_found_without_configuration(workspace):
    """Labels in column B behind a note-number column, headers on row 7,
    theme-coloured inputs — nothing FinScan was told about."""
    plan = discover(workspace["acme"])
    assert plan.colors_found

    s = plan.sheet("Consolidated P&L")
    assert s.label_col == 2, "the note-number column must not be mistaken for the labels"
    assert s.header_row == 7
    assert len(s.writable_rows) == 13


def test_acme_pre_formatted_empty_column_is_filled_not_appended(workspace):
    plan = discover(workspace["acme"])
    s = plan.sheet("Consolidated P&L")
    assert s.write_mode == "fill_blank"
    assert s.write_col_letter == "H"


def test_sheets_in_one_workbook_may_disagree_on_units(workspace):
    plan = discover(workspace["acme"])
    assert plan.sheet("Consolidated P&L").units == "crores"
    assert plan.sheet("Standalone P&L").units == "lakhs"


def test_segment_tab_is_out_of_scope(workspace):
    plan = discover(workspace["acme"])
    seg = plan.sheet("Segments")
    assert not seg.in_scope
    assert "recognisable" in seg.reason


def test_zenith_without_colours_falls_back_to_geometry(workspace):
    plan = discover(workspace["zenith"])
    assert plan.colors_found is False
    s = plan.sheet("Quarterly")
    assert s.in_scope
    assert "geometry fallback" in s.reason
    assert len(s.writable_rows) == 18, "with no convention, non-formula rows are the fallback"


def test_formula_templates_are_captured_from_the_reference_column(workspace):
    s = discover(workspace["northwind"]).sheet("P&L Summary")
    by_field = {r.field: r for r in s.rows}
    assert by_field["total_income"].carries_formula
    assert by_field["total_income"].formula_template == "=E5+E6"
    assert by_field["total_expenses"].formula_template == "=SUM(E8:E14)"
    assert by_field["revenue_from_operations"].writable
    assert not by_field["revenue_from_operations"].carries_formula


def test_fingerprint_is_stable_and_layout_sensitive(workspace, tmp_path):
    from openpyxl import load_workbook

    a = discover(workspace["northwind"]).fingerprint
    assert a == discover(workspace["northwind"]).fingerprint

    moved = tmp_path / "moved.xlsx"
    wb = load_workbook(workspace["northwind"])
    wb["P&L Summary"].insert_rows(5)
    wb.save(moved)
    assert discover(moved).fingerprint != a
