"""Stage 2 (finscan2.model) — offline tests. No workbook, no LLM, no network.

The property these exist to protect: a map is keyed by caption and section, so
inserting or deleting rows in a model must not break it, while renaming or
removing a line item must stop the run and say so.
"""
from __future__ import annotations

import copy

from finscan2.model.discover import RowLayout, SheetLayout, assign_sections, detect_section
from finscan2.model.load import resolve
from finscan2.model.schema import ModelMap, RowKey, RowSpec, normalize_label


def _layout(rows: list[tuple[int, str, str]], **kwargs) -> SheetLayout:
    return SheetLayout(
        sheet="Model",
        rows=[RowLayout(row=r, label=label, section=section, role="input",
                        has_formula=False)
              for r, label, section in rows],
        **kwargs,
    )


def _map(rows: list[tuple[int, str, str]], **kwargs) -> ModelMap:
    return ModelMap(
        company="acme", sheet="Model",
        rows=[RowSpec(key=RowKey(label=label, section=section), row_hint=r, kind="input",
                      resolve="field:revenue_from_operations")
              for r, label, section in rows],
        **kwargs,
    )


_ROWS = [(7, "Revenue", "income_statement"),
         (9, "Cost of Sales", "income_statement"),
         (58, "Taxes, net", "cash_flow")]


# --------------------------------------------------------------------------- #
# Label normalization and sections
# --------------------------------------------------------------------------- #

def test_normalize_label_ignores_case_spacing_and_punctuation():
    assert normalize_label("General and Administration Expenses ") == \
        normalize_label("general & administration  expenses")
    assert normalize_label("Financial Cost, net") == "financial cost net"


def test_sections_are_inherited_from_the_last_heading():
    sections = assign_sections({1: "Revenue", 5: "CASH FLOW", 6: "Taxes, net",
                                20: "Balance Sheet", 21: "Inventory"})
    assert sections[1] == "income_statement"
    assert sections[6] == "cash_flow"
    assert sections[21] == "balance_sheet"


def test_generic_keywords_flip_the_section_only_on_an_exact_caption():
    """"Segment" alone is a heading; "Segment revenue" is a line item."""
    assert detect_section("Segment") == "other"
    assert detect_section("Segment revenue") is None


# --------------------------------------------------------------------------- #
# Binding a map to a sheet
# --------------------------------------------------------------------------- #

def test_rows_still_bind_after_every_row_number_shifts():
    """The whole point of keying on caption + section: an analyst inserting rows
    at the top moves every line, and the map must survive it."""
    layout = _layout([(r + 3, label, section) for r, label, section in _ROWS])
    resolved = resolve(_map(_ROWS), layout)
    assert len(resolved.bound) == 3
    assert not resolved.errors
    assert resolved.row_for("Revenue", "income_statement") == 10
    assert sum(1 for i in resolved.issues if i.code == "map_row_moved") == 3


def test_a_renamed_caption_is_a_blocking_error_that_suggests_the_fix():
    layout = _layout(_ROWS)
    renamed = copy.deepcopy(_map(_ROWS))
    renamed.rows[0].key.label = "Turnover"
    resolved = resolve(renamed, layout)
    missing = [i for i in resolved.issues if i.code == "map_key_missing"]
    assert missing and "Turnover" in missing[0].message
    assert not resolved.ok


def test_the_same_caption_in_two_sections_is_two_different_rows():
    """"Depreciation" in the P&L and in the cash-flow adjustments are not the
    same line, and a map keyed on the caption alone could not tell them apart."""
    rows = [(20, "Depreciation", "income_statement"), (60, "Depreciation", "cash_flow")]
    resolved = resolve(_map(rows), _layout(rows))
    assert resolved.row_for("Depreciation", "income_statement") == 20
    assert resolved.row_for("Depreciation", "cash_flow") == 60


def test_a_repeated_caption_is_distinguished_by_occurrence():
    rows = [(30, "Other", "cash_flow"), (40, "Other", "cash_flow")]
    model = _map(rows)
    model.rows[1].key.occurrence = 2
    resolved = resolve(model, _layout(rows))
    assert resolved.row_for("Other", "cash_flow", 1) == 30
    assert resolved.row_for("Other", "cash_flow", 2) == 40


def test_a_dropped_repeat_is_reported_as_ambiguous_not_missing():
    """The caption is still there, just fewer times — which is a different fix
    from a rename, so it gets its own message."""
    model = _map([(30, "Other", "cash_flow"), (40, "Other", "cash_flow")])
    model.rows[1].key.occurrence = 2
    resolved = resolve(model, _layout([(30, "Other", "cash_flow")]))
    codes = [i.code for i in resolved.issues]
    assert "map_key_ambiguous" in codes
    assert "map_key_missing" not in codes


def test_a_writable_sheet_row_missing_from_the_map_is_an_error():
    """The map is the contract both ways: a row it never mentions is a hole in
    the contract, not a row to guess at."""
    layout = _layout(_ROWS + [(95, "Inventory", "balance_sheet")])
    resolved = resolve(_map(_ROWS), layout)
    unmapped = [i for i in resolved.issues if i.code == "sheet_row_unmapped"]
    assert unmapped and "Inventory" in unmapped[0].message
    assert not resolved.ok


def test_a_row_mapped_as_skip_is_not_reported_unmapped():
    layout = _layout(_ROWS + [(29, "As % of Net sales", "income_statement")])
    model = _map(_ROWS)
    model.rows.append(RowSpec(key=RowKey(label="As % of Net sales",
                                         section="income_statement"),
                              row_hint=29, kind="skip"))
    resolved = resolve(model, layout)
    assert not resolved.errors


def test_changed_captions_raise_drift_but_still_bind_what_they_can():
    from finscan2.model.discover import fingerprint

    layout = _layout(_ROWS)
    model = _map(_ROWS)
    model.fingerprint = "0000000000000000"
    resolved = resolve(model, layout)
    assert fingerprint(layout) != model.fingerprint
    assert any(i.code == "fingerprint_drift" for i in resolved.issues)
    assert len(resolved.bound) == 3


def test_an_unconfirmed_map_warns_but_does_not_block():
    resolved = resolve(_map(_ROWS), _layout(_ROWS))
    assert any(i.code == "map_unconfirmed" for i in resolved.issues)
    assert resolved.ok


# --------------------------------------------------------------------------- #
# The map file itself
# --------------------------------------------------------------------------- #

def test_map_round_trips_through_json(tmp_path):
    model = _map(_ROWS, units="millions", cadence_months=3, write_col=48,
                 write_mode="fill_blank", period_dates={47: "2026-03-31"})
    model.rows[0].basis = "quarter"
    model.rows[2].basis = "cumulative"
    model.rows[2].statement = "cash_flow"

    path = model.save(tmp_path / "acme.json")
    back = ModelMap.load(path)

    assert back.units == "millions"
    assert back.write_col == 48 and back.write_mode == "fill_blank"
    assert back.period_dates == {47: "2026-03-31"}
    assert [r.key.label for r in back.rows] == [r.key.label for r in model.rows]
    assert back.rows[2].basis == "cumulative"
    assert back.rows[2].statement == "cash_flow"


def test_balance_sheet_rows_are_proposed_as_point_in_time():
    """A cash balance must never be de-cumulated; the map is where that is said."""
    from finscan2.model.learn import propose

    layout = _layout([(7, "Revenue", "income_statement"),
                      (95, "Cash and equivalents", "balance_sheet")])
    model = propose(layout, "acme")
    by_label = {r.key.label: r for r in model.rows}
    assert by_label["Revenue"].basis == "quarter"
    assert by_label["Cash and equivalents"].basis == "point_in_time"


def test_headings_and_ratios_are_proposed_as_skip():
    from finscan2.model.learn import propose

    layout = _layout([(29, "As % of Net sales or stated otherwise", "income_statement"),
                      (41, "D&A / sales", "income_statement"),
                      (7, "Revenue", "income_statement")])
    kinds = {r.key.label: r.kind for r in propose(layout, "acme").rows}
    assert kinds["As % of Net sales or stated otherwise"] == "skip"
    assert kinds["Revenue"] == "input"


def test_a_formula_row_is_never_an_input():
    from finscan2.model.learn import propose

    layout = SheetLayout(sheet="Model", rows=[
        RowLayout(row=10, label="Gross Profit", section="income_statement",
                  role="formula", has_formula=True, formula="=AU7+AU9"),
    ])
    spec = propose(layout, "acme").rows[0]
    assert spec.kind == "formula" and spec.resolve is None


def test_a_caption_that_is_itself_a_formula_is_read_from_its_cached_value():
    """Models mirror blocks of line items by making the caption a formula
    ("=+B121"). Read undisplayed that is one letter, and the row silently drops
    out of the map — eight leverage rows vanished from Almarai's map that way."""
    from finscan2.model.discover import _caption

    class _Cell:
        def __init__(self, value): self.value = value

    class _Sheet:
        def __init__(self, value): self._value = value
        def cell(self, row, col): return _Cell(self._value)

    assert _caption(_Sheet("=+B121"), _Sheet("Total Net Leverage"), 268, 2) == \
        "Total Net Leverage"
    assert _caption(_Sheet("Revenue"), _Sheet("ignored"), 7, 2) == "Revenue"


def test_a_ratio_word_inside_an_expense_caption_does_not_skip_the_row():
    """'General and Administration Expenses' was skipped as a derived ratio,
    because 'administ-ratio-n' contains 'ratio'. A real expense line dropped out
    of the map with a reason that read as though it had been considered."""
    from finscan2.model.learn import propose

    layout = _layout([(13, "General and Administration Expenses", "income_statement"),
                      (14, "Administrative expenses", "income_statement"),
                      (15, "Exploration expenses", "income_statement")])
    kinds = {r.key.label: r.kind for r in propose(layout, "acme").rows}
    assert all(kind == "input" for kind in kinds.values()), kinds


def test_derived_captions_are_still_skipped_on_whole_words():
    from finscan2.model.learn import _looks_derived

    for caption in ("Gross Margin %", "Gross margin", "Operating Margins",
                    "Revenue growth", "Y-o-Y Growth", "Net Debt / EBITDA (x)",
                    "Ratio analysis", "Check"):
        assert _looks_derived(caption), caption
    for caption in ("General and Administration Expenses", "Registration fees",
                    "Zakat", "Finance cost, net", "Depreciation & amortisation"):
        assert not _looks_derived(caption), caption


def test_a_bidirectional_caption_gets_no_inferred_sign_convention():
    """Almarai's "Other (Expenses) / Income, net" printed (30,302) one quarter and
    11,991 the next. A row frozen as 'negative' from the prior column would have
    written -11,991 — wrong, with a plausible provenance comment attached."""
    from finscan2.model.learn import propose

    layout = _layout([(14, "Other (Expenses) / Income, net", "income_statement"),
                      (15, "FX gain / (loss)", "income_statement"),
                      (16, "Finance cost, net", "income_statement")])
    for row in layout.rows:
        row.reference_value = -100.0
    by_label = {r.key.label: r for r in propose(layout, "acme").rows}
    assert by_label["Other (Expenses) / Income, net"].sign is None
    assert by_label["FX gain / (loss)"].sign is None
    # Not bidirectional: a finance cost stays negative every quarter.
    assert by_label["Finance cost, net"].sign == "negative"


def test_bind_records_the_caption_the_filing_actually_printed():
    """`learn` never opens a PDF, so a `pdf:` instruction can only carry the
    WORKBOOK's caption. After a run has matched the row, the filing's own wording
    is known and worth keeping."""
    from finscan2.match.schema import ResolvedValue, Source, Values
    from finscan2.model.bind import apply as apply_bindings
    from finscan2.model.schema import ModelMap, RowKey, RowSpec

    model = ModelMap(company="almarai", sheet="Model", rows=[
        RowSpec(key=RowKey(label="Impairment (Loss) / Reversal on Financial Assets",
                           section="income_statement"),
                row_hint=15, kind="input",
                resolve="pdf:Impairment (Loss) / Reversal on Financial Assets"),
        RowSpec(key=RowKey(label="Revenue", section="income_statement"),
                row_hint=7, kind="input", resolve="pdf:Revenue"),
    ])
    values = Values(company="almarai", sheet="Model", period_end="2026-06-30", write_col=8, units="millions")
    for row, label, caption in (
            (15, "Impairment (Loss) / Reversal on Financial Assets",
             "Impairment Loss on Financial Assets"),
            (7, "Revenue", "Revenue")):
        values.values.append(ResolvedValue(
            row=row, label=label, section="income_statement", value=1.0,
            resolve=f"pdf:{label}",
            source=Source(statement="income_statement", page=6, caption=caption,
                          column_header="April - June 2026", column_index=0,
                          months=3, end="2026-06-30", printed=1.0)))

    changed = apply_bindings(values, model)
    by_row = {spec.row_hint: spec for spec in model.rows}
    assert by_row[15].pdf_caption == "Impairment Loss on Financial Assets"
    # Revenue already matched exactly; recording it would be noise in every row.
    assert by_row[7].pdf_caption is None
    assert [c.row for c in changed] == [15]


def test_a_row_left_blank_records_nothing():
    """A row that did not match has no filing caption to record, and inventing one
    would make the next run look resolved."""
    from finscan2.match.schema import ResolvedValue, Values
    from finscan2.model.bind import apply as apply_bindings
    from finscan2.model.schema import ModelMap, RowKey, RowSpec

    model = ModelMap(company="x", sheet="Model", rows=[
        RowSpec(key=RowKey(label="Zakat", section="income_statement"),
                row_hint=20, kind="input", resolve="pdf:Zakat")])
    values = Values(company="x", sheet="Model", period_end="2026-06-30", write_col=8, units="millions")
    values.values.append(ResolvedValue(row=20, label="Zakat", section="income_statement",
                                       value=None, resolve="pdf:Zakat",
                                       unresolved="no row captioned like 'Zakat'"))
    assert apply_bindings(values, model) == []
    assert model.rows[0].pdf_caption is None
