"""The lean mapping: its vocabulary, its compilation, and what it refuses.

The mapping is the only file a human owns, so the tests that matter are the ones
about not losing or misreading a human decision.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from openpyxl import Workbook

from finscan.mapping.compile import compile_mapping, unmapped_inputs
from finscan.mapping.convert import from_model_map
from finscan.mapping.schema import Mapping, MappingRow
from finscan.model.discover import RowLayout, SheetLayout
from finscan.model.schema import ModelMap, RowKey, RowSpec


def _layout(rows=None):
    rows = rows or [(7, "Revenue", "income_statement", "input", False),
                    (9, "Cost of Sales", "income_statement", "input", False),
                    (10, "Gross Profit", "income_statement", "formula", True),
                    (21, "Zakat and Income Tax", "income_statement", "input", False)]
    return SheetLayout(
        sheet="Model", label_col=2, header_row=2, first_data_row=5,
        period_dates={3: "2026-03-31"}, period_date_row=2, reference_col=3,
        write_col=4, units="millions", cadence_months=3,
        rows=[RowLayout(row=r, label=label, section=section, role=role,
                        has_formula=formula, formula="=C7+C9" if formula else None)
              for r, label, section, role, formula in rows])


# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

def test_pdf_field_expresses_every_case():
    row = MappingRow
    assert row(label="a", pdf="Revenue").instruction() == "pdf:Revenue"
    assert row(label="a", pdf=["Zakat", "Income Tax"]).instruction() == \
        "sum:pdf:Zakat|pdf:Income Tax"
    assert row(label="a", pdf=["Debt", "-Cash"]).instruction() == \
        "sum:pdf:Debt|-pdf:Cash"
    assert row(label="a", value=0.26645).instruction() == "const:0.26645"
    assert row(label="a", pdf=None, note="inside Other").instruction() == \
        "absent:inside Other"
    assert row(label="a", carry_forward=True, note="an FX peg").instruction() == \
        "carry:an FX peg"


def test_basis_and_statement_are_derived_from_the_section():
    """Kept out of the file: they followed from `section` on every row."""
    assert MappingRow(label="a", section="balance_sheet").resolved_basis() == "point_in_time"
    assert MappingRow(label="a", section="cash_flow").resolved_basis() == "quarter"
    assert MappingRow(label="a", section="cash_flow").resolved_statement() == "cash_flow"
    override = MappingRow(label="a", section="income_statement", statement="cash_flow")
    assert override.resolved_statement() == "cash_flow"


def test_the_file_stays_lean_on_disk(tmp_path):
    """One row per physical line, and no field carrying a derivable default —
    otherwise a git diff of one decision touches every row."""
    mapping = Mapping(company="x", sheet="Model", rows=[
        MappingRow(label="Revenue", section="income_statement", pdf="Revenue")])
    path = mapping.save(tmp_path / "x.json")
    text = path.read_text(encoding="utf-8")
    assert '{"label": "Revenue", "section": "income_statement", "pdf": "Revenue"}' in text
    assert "occurrence" not in text and "basis" not in text and "sign" not in text


def test_a_carry_forward_row_round_trips_without_a_pdf_field(tmp_path):
    """`carry_forward` and `pdf` are mutually exclusive: on disk, a carried row
    has no `pdf` key at all, and loading it back must not mistake that for absent."""
    mapping = Mapping(company="x", sheet="Model", rows=[
        MappingRow(label="FX rate", section="other", carry_forward=True, note="a peg")])
    path = mapping.save(tmp_path / "x.json")
    text = path.read_text(encoding="utf-8")
    assert '"carry_forward": true' in text and '"pdf"' not in text

    loaded = Mapping.load(path)
    assert loaded.rows[0].carry_forward is True
    assert loaded.rows[0].instruction() == "carry:a peg"


@pytest.mark.parametrize("scale", [0.01, "percent"])
def test_a_scale_round_trips_and_reaches_the_compiled_row(tmp_path, scale):
    """A percentage printed as 1.7 for a model that holds 0.017."""
    mapping = Mapping(company="x", sheet="Model", rows=[
        MappingRow(label="Revenue", section="income_statement", pdf="Revenue", scale=scale)])
    path = mapping.save(tmp_path / "x.json")
    assert Mapping.load(path).rows[0].scale == scale

    model, _ = compile_mapping(Mapping.load(path), _layout())
    assert [r for r in model.rows if r.key.label == "Revenue"][0].scale == scale
    assert RowSpec.from_dict(model.rows[0].to_dict()).scale == model.rows[0].scale


# --------------------------------------------------------------------------- #
# Compilation
# --------------------------------------------------------------------------- #

def test_row_numbers_come_from_the_sheet_not_the_mapping():
    """The mapping has no row numbers at all, so an inserted Excel row changes
    nothing in the reviewed file."""
    mapping = Mapping(company="x", sheet="Model", rows=[
        MappingRow(label="Revenue", section="income_statement", pdf="Revenue")])
    model, issues = compile_mapping(mapping, _layout())
    revenue = [r for r in model.rows if r.key.label == "Revenue"][0]
    assert revenue.row_hint == 7 and revenue.kind == "input"
    assert revenue.resolve == "pdf:Revenue"
    assert not issues

    shifted = _layout([(107, "Revenue", "income_statement", "input", False)])
    model, issues = compile_mapping(mapping, shifted)
    assert [r for r in model.rows if r.key.label == "Revenue"][0].row_hint == 107
    assert not issues


def test_a_mapping_row_that_binds_to_nothing_is_an_error_with_suggestions():
    """A caption renamed in Excel would otherwise stop being written silently."""
    mapping = Mapping(company="x", sheet="Model", rows=[
        MappingRow(label="Revenues", section="income_statement", pdf="Revenue")])
    _, issues = compile_mapping(mapping, _layout())
    assert [i.code for i in issues] == ["mapping_row_unbound"]
    assert issues[0].severity == "error"
    assert "revenue" in issues[0].message.lower()


def test_formula_rows_are_taken_from_the_sheet_and_never_from_the_mapping():
    model, _ = compile_mapping(Mapping(company="x", sheet="Model"), _layout())
    gross = [r for r in model.rows if r.key.label == "Gross Profit"][0]
    assert gross.kind == "formula"


def test_a_row_absent_from_the_mapping_is_skipped_not_guessed():
    model, issues = compile_mapping(Mapping(company="x", sheet="Model"), _layout())
    assert {r.kind for r in model.rows} == {"formula", "skip"}
    assert not issues


def test_unmapped_writable_rows_are_reported():
    """Not an error, but a row silently absent from the mapping is
    indistinguishable from one deliberately left out."""
    mapping = Mapping(company="x", sheet="Model", rows=[
        MappingRow(label="Revenue", section="income_statement", pdf="Revenue")])
    left = [r.label for r in unmapped_inputs(mapping, _layout())]
    assert left == ["Cost of Sales", "Zakat and Income Tax"]


def test_only_an_asserted_sign_reaches_the_compiled_map():
    """Sign inference wrote two wrong rows this week; nothing infers one now."""
    mapping = Mapping(company="x", sheet="Model", rows=[
        MappingRow(label="Revenue", section="income_statement", pdf="Revenue"),
        MappingRow(label="Cost of Sales", section="income_statement",
                   pdf="Cost of Sales", sign="negative")])
    model, _ = compile_mapping(mapping, _layout())
    by_label = {r.key.label: r for r in model.rows}
    assert by_label["Revenue"].sign is None
    assert by_label["Cost of Sales"].sign == "negative"


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #

def test_converting_an_old_map_keeps_the_hand_work():
    old = ModelMap(company="almarai", sheet="Model", rows=[
        RowSpec(key=RowKey(label="Zakat and Income Tax", section="income_statement"),
                row_hint=21, kind="input", resolve="sum:pdf:Zakat|pdf:Income Tax"),
        RowSpec(key=RowKey(label="Other Expenses, net", section="income_statement"),
                row_hint=14, kind="input", resolve="pdf:Other Expenses, net",
                pdf_caption="Other (Expenses) / Income, net"),
        RowSpec(key=RowKey(label="Gross Profit", section="income_statement"),
                row_hint=10, kind="formula"),
    ])
    mapping = from_model_map(old)
    by_label = {r.label: r for r in mapping.rows}
    assert by_label["Zakat and Income Tax"].pdf == ["Zakat", "Income Tax"]
    # The filing's own wording wins over the workbook caption that was searched with.
    assert by_label["Other Expenses, net"].pdf == "Other (Expenses) / Income, net"
    # Formula rows are derived, so they do not belong in the human file.
    assert "Gross Profit" not in by_label


def test_conversion_drops_an_inferred_sign_but_keeps_an_asserted_one():
    """A sign in the old maps was usually inferred from one prior column — the
    inference that wrote -11,991 for a quarter that earned +11,991."""
    old = ModelMap(company="x", sheet="Model", rows=[
        RowSpec(key=RowKey(label="Inferred", section="income_statement"), row_hint=1,
                kind="input", resolve="pdf:Inferred", sign="negative"),
        RowSpec(key=RowKey(label="Asserted", section="income_statement"), row_hint=2,
                kind="input", resolve="pdf:Asserted", sign="negative",
                pdf_caption="Asserted"),
    ])
    by_label = {r.label: r for r in from_model_map(old).rows}
    assert by_label["Inferred"].sign is None
    assert by_label["Asserted"].sign == "negative"


def test_a_canonical_field_row_with_no_recorded_caption_asks_for_one():
    """The mapping stores filing captions, so a bare `field:` id cannot carry over —
    and guessing one is what matched a cost row to revenue."""
    old = ModelMap(company="x", sheet="Model", rows=[
        RowSpec(key=RowKey(label="Cost of Sales", section="income_statement"),
                row_hint=9, kind="input", resolve="field:cost_of_revenue")])
    row = from_model_map(old).rows[0]
    assert row.pdf is None and "needs a filing caption" in row.note


def test_conversion_does_not_dress_an_unmatched_row_as_a_decision():
    """`learn` wrote `pdf:<workbook caption>` as the text to SEARCH with. Carrying
    that across would turn "never found" into "this is the line" — which is how
    `Exchange Gain, net` would enter the mapping looking decided while the filing
    has no such row."""
    old = ModelMap(company="almarai", sheet="Model", rows=[
        RowSpec(key=RowKey(label="Exchange Gain, net", section="income_statement"),
                row_hint=17, kind="input", resolve="pdf:Exchange Gain, net"),
        RowSpec(key=RowKey(label="Other Expenses, net", section="income_statement"),
                row_hint=14, kind="input", resolve="pdf:Other (Expenses) / Income, net"),
    ])
    by_label = {r.label: r for r in from_model_map(old).rows}
    assert by_label["Exchange Gain, net"].pdf is None
    assert "never recorded" in by_label["Exchange Gain, net"].note or \
        "no filing caption" in by_label["Exchange Gain, net"].note
    # A caption a human typed that differs from the workbook's IS a decision.
    assert by_label["Other Expenses, net"].pdf == "Other (Expenses) / Income, net"


def test_appending_rows_leaves_every_existing_line_untouched(tmp_path):
    """A new workbook row is added without rewriting the human file: keys this
    class does not model (CCU's `extra_statements`) and hand formatting survive."""
    path = tmp_path / "ccu.json"
    original = (
        '{\n  "mapping_version": "2.0",\n  "company": "ccu",\n  "sheet": "Model",\n'
        '  "extra_statements": [\n    {"title": "Nature of cost and expense"}\n  ],\n'
        '  "rows": [\n'
        '    {"label": "Revenue", "section": "income_statement", "pdf": "Net sales"}\n'
        '  ]\n}\n')
    path.write_text(original, encoding="utf-8")

    Mapping.append_rows(path, [MappingRow(label="Royalties", section="income_statement",
                                          pdf="Royalty income", note="added")])

    text = path.read_text(encoding="utf-8")
    assert text.startswith(original.split('"Net sales"}')[0] + '"Net sales"},\n')
    assert '"extra_statements"' in text
    assert [r.label for r in Mapping.load(path).rows] == ["Revenue", "Royalties"]


def test_appending_to_a_file_in_another_layout_still_keeps_unknown_keys(tmp_path):
    path = tmp_path / "x.json"
    path.write_text('{"company": "x", "sheet": "Model", "custom": 1, "rows": []}',
                    encoding="utf-8")

    Mapping.append_rows(path, [MappingRow(label="Revenue", pdf="Revenue")])

    import json
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["custom"] == 1
    assert [r["label"] for r in payload["rows"]] == ["Revenue"]


def test_a_single_optional_caption_compiles_to_a_sum_instruction():
    """Optional terms are read on the sum path, so one "caption?" is routed there."""
    from finscan.mapping.schema import MappingRow

    row = MappingRow(label="Inventory", section="balance_sheet", pdf="Inventories?")
    assert row.instruction() == "sum:pdf:Inventories?"


def test_optional_markers_survive_in_a_list_of_captions():
    from finscan.mapping.schema import MappingRow

    row = MappingRow(label="Acq", section="cash_flow",
                     pdf=["Sale of subsidiaries?", "-Investment in joint venture?"])
    assert row.instruction() == "sum:pdf:Sale of subsidiaries?|-pdf:Investment in joint venture?"


def test_copy_formula_round_trips_and_compiles_to_a_formula_row(tmp_path):
    """Tencent's average FX rate: typed in 2Q, =AVERAGE(...) in other quarters."""
    path = Mapping(company="x", sheet="Model", rows=[
        MappingRow(label="FX rate average", section="other", copy_formula=True)]).save(
        tmp_path / "x.json")
    assert '"copy_formula": true' in path.read_text(encoding="utf-8")
    assert '"pdf"' not in path.read_text(encoding="utf-8")

    row = Mapping.load(path).rows[0]
    assert row.copy_formula and row.instruction() == "formula:latest"

    layout = SheetLayout(sheet="Model", rows=[
        RowLayout(row=9, label="FX rate average", section="other", role="input",
                  has_formula=False)])
    model, issues = compile_mapping(Mapping.load(path), layout)
    assert not issues
    assert model.rows[0].kind == "formula" and model.rows[0].resolve == "formula:latest"
