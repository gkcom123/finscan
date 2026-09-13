from __future__ import annotations

from conftest import Q1_FY27_LAKHS, sample_extraction

from finscan.extract.normalize import convert, derive_missing, sniff_units, to_target_units
from finscan.validate import check_arithmetic, check_plausibility, has_blocking_errors


def test_sniff_units():
    assert sniff_units("(Rs. in Lakhs, except per share data)") == "lakhs"
    assert sniff_units("Amounts in Rs. Crores") == "crores"
    assert sniff_units("US$ in millions") == "millions"
    assert sniff_units("Particulars") is None


def test_convert_lakhs_to_crores():
    assert convert(128_450.0, "lakhs", "crores") == 1284.5


def test_eps_is_not_rescaled():
    values, _, _ = to_target_units(sample_extraction(), "crores")
    assert values["revenue_from_operations"] == 1284.50
    assert values["eps_basic"] == 31.31          # per-share stays per-share
    assert values["eps_diluted"] == 31.14


def test_units_conversion_is_reported():
    _, _, issues = to_target_units(sample_extraction(), "crores")
    assert any(i.code == "units_converted" for i in issues)


def test_derive_only_fills_absent_subtotals():
    values = {"revenue_from_operations": 100.0, "other_income": 5.0,
              "profit_before_tax": 20.0, "tax_expense": 5.0,
              "finance_costs": 3.0, "depreciation_amortisation": 7.0}
    out, issues = derive_missing(values)
    assert out["total_income"] == 105.0
    assert out["profit_after_tax"] == 15.0
    assert out["ebitda"] == 20.0 + 3.0 + 7.0 - 5.0
    assert {i.field for i in issues} == {"total_income", "profit_after_tax", "ebitda"}


def test_clean_extraction_passes_arithmetic():
    values, _, _ = to_target_units(sample_extraction(), "crores")
    values, _ = derive_missing(values)
    issues = check_arithmetic(values) + check_plausibility(values)
    assert not has_blocking_errors(issues), [i.message for i in issues if i.severity == "error"]


def test_wrong_column_is_caught():
    """Simulate reading PAT from last year's column: identities stop matching."""
    bad = dict(Q1_FY27_LAKHS)
    bad["profit_after_tax"] = 10_120.00           # Q1 FY26 figure
    values, _, _ = to_target_units(sample_extraction(bad), "crores")
    issues = check_arithmetic(values)
    assert has_blocking_errors(issues)
    assert any(i.code == "identity_pat" for i in issues)


def test_missing_component_breaks_total_expenses():
    bad = dict(Q1_FY27_LAKHS)
    bad["employee_benefit_expense"] = 2_176.00     # decimal slip
    values, _, _ = to_target_units(sample_extraction(bad), "crores")
    issues = check_arithmetic(values)
    assert any(i.code == "identity_total_expenses" for i in issues)


def test_negative_revenue_flagged():
    issues = check_plausibility({"revenue_from_operations": -10.0})
    assert has_blocking_errors(issues)


def test_change_in_working_capital_is_y_minus_x():
    """Change in WC = net cash from operating activities (Y) - total before
    working capital changes (X)."""
    values = {"net_cash_from_operating_activities": 4751.585,
              "total_before_working_capital_changes": 6778.834}
    out, issues = derive_missing(values)
    assert out["change_in_working_capital"] == 4751.585 - 6778.834
    assert any(i.code == "derived" and i.field == "change_in_working_capital" for i in issues)


def test_change_in_working_capital_definition_overrides_printed_value():
    values = {"net_cash_from_operating_activities": 100.0,
              "total_before_working_capital_changes": 130.0,
              "change_in_working_capital": -12.0}   # filing's own (different) figure
    out, issues = derive_missing(values)
    assert out["change_in_working_capital"] == -30.0
    assert any(i.code == "derived_override" for i in issues)


def test_change_in_working_capital_not_derived_without_both_components():
    out, _ = derive_missing({"net_cash_from_operating_activities": 100.0})
    assert "change_in_working_capital" not in out


def test_implausible_working_capital_swing_is_flagged():
    """A movement several times operating cash flow itself is the signature of a
    wrong-column read (e.g. cumulative vs standalone quarter), not a real swing."""
    values = {"net_cash_from_operating_activities": 100.0,
              "total_before_working_capital_changes": 1000.0}   # |Y-X| = 900, 9x Y
    _, issues = derive_missing(values)
    assert any(i.code == "implausible_working_capital_swing" for i in issues)


def test_plausible_working_capital_swing_is_not_flagged():
    values = {"net_cash_from_operating_activities": 4751.585,
              "total_before_working_capital_changes": 6778.834}
    _, issues = derive_missing(values)
    assert not any(i.code == "implausible_working_capital_swing" for i in issues)
