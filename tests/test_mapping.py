from __future__ import annotations

from finscan.excel.discovery import discover
from finscan.excel.mapper import map_rows, normalize_label


def test_normalize_strips_numbering_and_punctuation():
    assert normalize_label("   (d) Employee benefits expense") == "employee benefits expense"
    assert normalize_label("9. Profit for the period (7-8)") == "profit for the period"
    assert normalize_label("Depreciation & Amortization") == "depreciation and amortization"


def test_messy_captions_map_to_canonical_fields(workspace):
    sheet = discover(workspace["northwind"]).sheet("P&L Summary")
    got = {r.label: r.field for r in sheet.rows if r.field}

    expected = {
        "Net Sales": "revenue_from_operations",
        "Other Income": "other_income",
        "Total Income": "total_income",
        "Raw Material Consumed": "cost_of_materials",
        "Staff Cost": "employee_benefit_expense",
        "Interest Cost": "finance_costs",
        "Depreciation & Amortization": "depreciation_amortisation",
        "Other Expenditure": "other_expenses",
        "Total Expenditure": "total_expenses",
        "Operating Profit (EBITDA)": "ebitda",
        "PBT": "profit_before_tax",
        "Net Profit": "profit_after_tax",
        "EPS - Basic (Rs.)": "eps_basic",
        "EPS - Diluted (Rs.)": "eps_diluted",
    }
    for label, field in expected.items():
        assert got.get(label) == field, f"{label!r} mapped to {got.get(label)!r}, want {field!r}"


def test_no_canonical_field_is_claimed_by_two_rows(workspace):
    for model in ("northwind", "acme", "zenith"):
        for sheet in discover(workspace[model]).in_scope:
            fields = [r.field for r in sheet.rows if r.field]
            assert len(fields) == len(set(fields)), f"{model}/{sheet.sheet}"


def test_duplicate_captions_are_deduped_with_a_warning():
    labels = {5: "Net Profit", 6: "Profit after tax"}
    mappings, issues = map_rows(labels, use_llm=False)
    claimed = [m for m in mappings if m.field == "profit_after_tax"]
    assert len(claimed) == 1
    assert any(i.code == "ambiguous_row" for i in issues)


def test_headings_and_junk_stay_unmatched():
    labels = {1: "Expenses", 2: "Segment Revenue - North America", 3: "Notes to accounts"}
    mappings, _ = map_rows(labels, use_llm=False)
    assert all(m.field is None for m in mappings)
