"""`apply` — the one-step command, its write gate, and the identity checks.

The gate is the design's sharpest edge: because resolving and writing now happen in
one command, exactly when it refuses and when it writes-with-blanks has to be
pinned down by tests rather than by prose.
"""
from __future__ import annotations

import json
from datetime import datetime

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

from finscan2.apply import check_period, run
from finscan2.mapping.schema import Mapping, MappingRow

BLUE = Font(color="FF0000FF")


def _workbook(path):
    book = Workbook()
    sheet = book.active
    sheet.title = "Model"
    # The sheet declares its units, as a real model does; without this the layout
    # reads "units" and every figure is scaled by a million.
    sheet.cell(1, 2, "SAR millions")
    for index, iso in enumerate(("2025-12-31", "2026-03-31")):
        sheet.cell(2, 3 + index, datetime.fromisoformat(iso))
    for row, label, values in ((5, "Revenue", [100.0, 110.0]),
                               (6, "Cost of Sales", [-60.0, -66.0])):
        sheet.cell(row, 2, label)
        for index, value in enumerate(values):
            cell = sheet.cell(row, 3 + index, value)
            cell.font = BLUE
        sheet.cell(row, 5).font = BLUE
    sheet.cell(7, 2, "Gross Profit")
    sheet.cell(7, 3, "=C5+C6")
    sheet.cell(7, 4, "=D5+D6")
    book.save(path)
    return path


def _pdf_json(path, rows=None, end="2026-06-30"):
    rows = rows or [("Revenue", [120.0]), ("Cost of Sales", [-70.0])]
    doc = {
        "schema_version": "1.0", "path": "f.pdf", "sha256": "x", "units": "millions",
        "pages": [], "issues": [],
        "statements": [{
            "page": 4, "kind": "income_statement", "title": "t", "heading": "h",
            "columns": [{"index": 0, "header": "April - June 2026", "months": 3,
                         "end": end, "kind": "period"}],
            "rows": [{"caption": c, "values": v, "raw": ""} for c, v in rows],
        }],
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _mapping(path, rows=None):
    mapping = Mapping(company="acme", sheet="Model", rows=rows or [
        MappingRow(label="Revenue", section="income_statement", pdf="Revenue"),
        MappingRow(label="Cost of Sales", section="income_statement", pdf="Cost of Sales"),
    ])
    return mapping.save(path)


def _setup(tmp_path, *, mapping_rows=None, pdf_rows=None):
    return (_pdf_json(tmp_path / "f.pdf.json", pdf_rows),
            _mapping(tmp_path / "m.json", mapping_rows),
            _workbook(tmp_path / "model.xlsx"))


# --------------------------------------------------------------------------- #
# The happy path, in one command
# --------------------------------------------------------------------------- #

def test_resolve_and_write_happen_in_one_call(tmp_path):
    """No intermediate file, so nothing between them can go stale — which is what
    produced a workbook from a four-minute-old resolution."""
    pdf, mapping, excel = _setup(tmp_path)
    out = tmp_path / "out.xlsx"
    result = run(pdf, mapping, excel, out)

    assert result.refused == ""
    sheet = load_workbook(out)["Model"]
    assert sheet.cell(5, 5).value == 120.0
    assert sheet.cell(6, 5).value == -70.0
    assert sheet.cell(7, 5).value == "=E5+E6"
    assert sheet.cell(2, 5).value == datetime(2026, 6, 30)
    assert result.exit_code == 0


def test_a_dry_run_resolves_and_writes_nothing(tmp_path):
    pdf, mapping, excel = _setup(tmp_path)
    out = tmp_path / "out.xlsx"
    result = run(pdf, mapping, excel, out, dry_run=True)
    assert result.values and len(result.values.resolved) == 2
    assert result.write is None and not out.exists()


# --------------------------------------------------------------------------- #
# The write gate
# --------------------------------------------------------------------------- #

def test_an_unbindable_mapping_row_refuses_before_the_workbook_is_touched(tmp_path):
    pdf, mapping, excel = _setup(tmp_path, mapping_rows=[
        MappingRow(label="Turnover", section="income_statement", pdf="Revenue")])
    out = tmp_path / "out.xlsx"
    result = run(pdf, mapping, excel, out)
    assert result.refused and not out.exists()
    assert any(i.code == "mapping_row_unbound" for i in result.issues)
    assert result.exit_code == 1


def test_a_row_that_cannot_be_resolved_does_not_withhold_the_others(tmp_path):
    """One unmappable line should not cost the other twenty-eight."""
    pdf, mapping, excel = _setup(tmp_path, pdf_rows=[("Revenue", [120.0])])
    out = tmp_path / "out.xlsx"
    result = run(pdf, mapping, excel, out)
    sheet = load_workbook(out)["Model"]
    assert sheet.cell(5, 5).value == 120.0
    assert sheet.cell(6, 5).value is None
    assert "no value written" in sheet.cell(6, 5).comment.text
    assert result.exit_code == 2          # written, with row-level issues


def test_the_source_workbook_is_untouched(tmp_path):
    pdf, mapping, excel = _setup(tmp_path)
    run(pdf, mapping, excel, tmp_path / "out.xlsx")
    assert load_workbook(excel)["Model"].cell(5, 5).value is None


# --------------------------------------------------------------------------- #
# Period safety
# --------------------------------------------------------------------------- #

def test_an_explicit_period_the_sheet_cannot_support_is_refused():
    """--period-end is checked, not trusted: left unchecked it is the one way to
    write a period the sheet has no history for."""
    target, issues = check_period({3: "2025-06-30"}, 3, 5, "2026-06-30")
    assert target == "2026-06-30"
    assert any(i.code == "period_not_contiguous" and i.severity == "error"
               for i in issues)


def test_annual_and_quarterly_blocks_side_by_side_are_not_a_gap():
    """Almarai's sheet carries annual columns for 2016-2025 AND quarterly columns
    for 2018-2026. A date-ordered walk calls the annual block nine gaps."""
    dates = {3: "2024-12-31", 4: "2025-12-31",
             43: "2025-09-30", 44: "2025-12-31", 45: "2026-03-30"}
    target, issues = check_period(dates, 3, 46)
    assert target == "2026-06-30"
    assert [i.code for i in issues] == []


def test_a_month_end_that_differs_by_a_day_still_counts_as_the_prior_period():
    """Models date a quarter 2026-03-30 as readily as 2026-03-31."""
    target, issues = check_period({45: "2025-12-31", 46: "2026-03-30"}, 3, 47)
    assert target == "2026-06-30" and not issues


def test_writing_over_a_dated_column_is_refused():
    _, issues = check_period({3: "2025-12-31", 4: "2026-03-31"}, 3, 4)
    assert any(i.code == "write_column_occupied" for i in issues)


def test_the_target_period_follows_the_latest_column():
    target, issues = check_period({3: "2025-12-31", 4: "2026-03-31"}, 3, 5)
    assert target == "2026-06-30" and not issues


def test_a_sheet_with_no_dates_cannot_establish_a_period():
    target, issues = check_period({}, 3, 5)
    assert target == "" and issues[0].code == "no_dated_columns"


# --------------------------------------------------------------------------- #
# Identity checks
# --------------------------------------------------------------------------- #

def test_a_failing_identity_is_a_warning_not_a_refusal():
    """An identity fails whenever a component is deliberately blank, which is a
    legitimate state here — so it is something to read, not a reason to withhold."""
    from finscan2.match.schema import ResolvedValue, Values
    from finscan2.validate import check

    values = Values(company="x", sheet="Model", period_end="2026-06-30",
                    write_col=5, units="millions")
    for label, value in (("Profit before Zakat and Income Tax", 100.0),
                         ("Zakat and Income Tax", -10.0),
                         ("Profit for the year", 75.0)):
        values.values.append(ResolvedValue(row=1, label=label,
                                           section="income_statement", value=value,
                                           resolve="pdf:x"))
    issues = check(values)
    assert [i.code for i in issues] == ["identity_failed"]
    assert issues[0].severity == "warning"
    assert "15.00" in issues[0].message


def test_an_identity_that_holds_is_silent():
    from finscan2.match.schema import ResolvedValue, Values
    from finscan2.validate import check

    values = Values(company="x", sheet="Model", period_end="2026-06-30",
                    write_col=5, units="millions")
    for label, value in (("Profit before Zakat and Income Tax", 100.0),
                         ("Zakat and Income Tax", -10.0),
                         ("Profit for the year", 90.0)):
        values.values.append(ResolvedValue(row=1, label=label,
                                           section="income_statement", value=value,
                                           resolve="pdf:x"))
    assert check(values) == []


def test_an_identity_with_a_missing_component_is_skipped():
    """Asserting an identity against rows that are not all there would produce a
    finding about a blank, which the blank already reports itself."""
    from finscan2.match.schema import ResolvedValue, Values
    from finscan2.validate import check

    values = Values(company="x", sheet="Model", period_end="2026-06-30",
                    write_col=5, units="millions")
    values.values.append(ResolvedValue(row=1, label="Profit for the year",
                                       section="income_statement", value=90.0,
                                       resolve="pdf:x"))
    assert check(values) == []


def test_low_coverage_is_reported_as_a_configuration_problem():
    from finscan2.match.schema import ResolvedValue, Values
    from finscan2.validate import coverage

    values = Values(company="x", sheet="Model", period_end="2026-06-30",
                    write_col=5, units="millions")
    values.values.append(ResolvedValue(row=1, label="a", section="income_statement",
                                       value=1.0, resolve="pdf:a"))
    for row in range(2, 6):
        values.values.append(ResolvedValue(row=row, label=f"r{row}",
                                           section="income_statement", value=None,
                                           resolve="pdf:x", unresolved="not found"))
    issue = coverage(values)
    assert issue is not None and issue.code == "low_coverage"
