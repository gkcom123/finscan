"""Arithmetic and sanity checks. Nothing is written until these have run.

The checks are deliberately about *internal consistency*: if revenue and other
income do not add up to the printed total income, either a value was read off
the wrong column or a caption was mis-mapped. Both are exactly the failure modes
we care about, and both are invisible to a schema check alone.
"""
from __future__ import annotations

from finscan.config import settings
from finscan.schemas import FIELD_LABELS, Issue, RowMapping

EXPENSE_COMPONENTS = [
    "cost_of_revenue",
    "cost_of_materials",
    "purchases_of_stock_in_trade",
    "changes_in_inventories",
    "employee_benefit_expense",
    "selling_marketing_expense",
    "general_admin_expense",
    "finance_costs",
    "depreciation_amortisation",
    "other_expenses",
]

#: Fields printed as costs. Indian filings show them positive and subtract;
#: IFRS releases show them in brackets and add. Both are correct, and the
#: identities only hold once one convention is chosen — so the convention is
#: detected and everything is converted to positive magnitudes for checking.
SIGNED_EXPENSE_FIELDS = [
    "cost_of_revenue", "cost_of_materials", "purchases_of_stock_in_trade",
    "employee_benefit_expense", "selling_marketing_expense", "general_admin_expense",
    "finance_costs", "depreciation_amortisation", "other_expenses", "total_expenses",
    "tax_expense", "current_tax", "deferred_tax",
]


def detect_sign_convention(values: dict[str, float]) -> str:
    """'bracketed' when costs are printed negative, 'positive' otherwise."""
    present = [values[f] for f in SIGNED_EXPENSE_FIELDS if f in values]
    if not present:
        return "positive"
    negatives = sum(1 for v in present if v < 0)
    return "bracketed" if negatives > len(present) / 2 else "positive"


def to_positive_expenses(values: dict[str, float]) -> tuple[dict[str, float], str]:
    convention = detect_sign_convention(values)
    if convention == "positive":
        return dict(values), convention
    out = dict(values)
    for f in SIGNED_EXPENSE_FIELDS:
        if f in out:
            out[f] = -out[f]
    return out, convention


def _close(a: float, b: float, tol_pct: float) -> bool:
    scale = max(abs(a), abs(b), 1e-9)
    return abs(a - b) <= (tol_pct / 100.0) * scale


def check_arithmetic(values: dict[str, float], tol_pct: float | None = None) -> list[Issue]:
    tol = settings.finscan_tolerance_pct if tol_pct is None else tol_pct
    values, convention = to_positive_expenses(values)
    issues: list[Issue] = []
    if convention == "bracketed":
        issues.append(Issue(
            severity="info", code="sign_convention",
            message="Costs are printed in brackets (negative); identities were checked "
                    "against that convention."))

    def has(*keys: str) -> bool:
        return all(k in values for k in keys)

    def cmp(name: str, lhs: float, rhs: float, expr: str, field: str) -> None:
        if not _close(lhs, rhs, tol):
            diff = lhs - rhs
            issues.append(
                Issue(
                    severity="error",
                    code=f"identity_{name}",
                    field=field,
                    message=f"{expr}: reported {lhs:,.2f} vs computed {rhs:,.2f} "
                            f"(diff {diff:,.2f}, {abs(diff) / max(abs(lhs), 1e-9) * 100:.2f}%).",
                )
            )

    if has("gross_profit", "revenue_from_operations", "cost_of_revenue"):
        cmp("gross_profit", values["gross_profit"],
            values["revenue_from_operations"] - values["cost_of_revenue"],
            "Gross profit = revenue - cost of revenue", "gross_profit")

    if has("profit_attributable_to_owners", "profit_after_tax", "non_controlling_interest"):
        cmp("attributable", values["profit_attributable_to_owners"],
            values["profit_after_tax"] - values["non_controlling_interest"],
            "Attributable profit = PAT - non-controlling interests",
            "profit_attributable_to_owners")

    if has("total_income", "revenue_from_operations", "other_income"):
        cmp("total_income", values["total_income"],
            values["revenue_from_operations"] + values["other_income"],
            "Total income = revenue + other income", "total_income")

    present = [k for k in EXPENSE_COMPONENTS if k in values]
    if "total_expenses" in values and len(present) >= 3:
        cmp("total_expenses", values["total_expenses"], sum(values[k] for k in present),
            f"Total expenses = sum of {len(present)} components", "total_expenses")

    if has("profit_before_tax", "total_income", "total_expenses"):
        expected = values["total_income"] - values["total_expenses"] + values.get("exceptional_items", 0.0)
        cmp("pbt", values["profit_before_tax"], expected,
            "PBT = total income - total expenses (+ exceptional)", "profit_before_tax")

    if has("profit_after_tax", "profit_before_tax", "tax_expense"):
        cmp("pat", values["profit_after_tax"],
            values["profit_before_tax"] - values["tax_expense"],
            "PAT = PBT - tax", "profit_after_tax")

    if has("tax_expense", "current_tax", "deferred_tax"):
        cmp("tax", values["tax_expense"], values["current_tax"] + values["deferred_tax"],
            "Tax = current + deferred", "tax_expense")

    if has("ebitda", "profit_before_tax", "finance_costs", "depreciation_amortisation"):
        expected = (
            values["profit_before_tax"] + values["finance_costs"]
            + values["depreciation_amortisation"] - values.get("other_income", 0.0)
            - values.get("exceptional_items", 0.0)
        )
        if not _close(values["ebitda"], expected, max(tol, 2.0)):
            issues.append(
                Issue(severity="warning", code="identity_ebitda", field="ebitda",
                      message=f"EBITDA {values['ebitda']:,.2f} differs from PBT+finance+D&A-other "
                              f"income ({expected:,.2f}). Definitions vary between filers — confirm.")
            )

    return issues


def check_plausibility(values: dict[str, float]) -> list[Issue]:
    values, _ = to_positive_expenses(values)
    issues: list[Issue] = []
    if values.get("revenue_from_operations", 1) < 0:
        issues.append(Issue(severity="error", code="negative_revenue",
                            field="revenue_from_operations",
                            message="Revenue from operations is negative — likely a sign or column error."))
    for f in ("finance_costs", "depreciation_amortisation", "employee_benefit_expense", "total_expenses"):
        if values.get(f, 0) < 0:
            issues.append(Issue(severity="warning", code="negative_expense", field=f,
                                message=f"{FIELD_LABELS[f]} is negative; check for a parenthesis sign flip."))
    if "profit_after_tax" in values and "revenue_from_operations" in values:
        rev = values["revenue_from_operations"]
        if rev and abs(values["profit_after_tax"]) > abs(rev) * 3:
            issues.append(Issue(severity="warning", code="margin_outlier", field="profit_after_tax",
                                message="PAT exceeds 3x revenue — possible unit mismatch between line items."))
    return issues


def check_coverage(mappings: list[RowMapping], values: dict[str, float]) -> list[Issue]:
    """Flag extracted fields with nowhere to go, and key rows with nothing to fill them."""
    issues: list[Issue] = []
    mapped_fields = {m.field for m in mappings if m.field}

    orphans = sorted(set(values) - mapped_fields)
    if orphans:
        issues.append(
            Issue(severity="warning", code="unmapped_extraction",
                  message="Extracted from the PDF but no matching row in the workbook: "
                          + ", ".join(FIELD_LABELS.get(f, f) for f in orphans))
        )

    core = {"revenue_from_operations", "profit_after_tax"}
    for f in sorted(core & mapped_fields - set(values)):
        issues.append(
            Issue(severity="error", code="core_field_missing", field=f,
                  message=f"The workbook has a '{FIELD_LABELS[f]}' row but the PDF yielded no value.")
        )

    unmatched = [m for m in mappings if m.field is None]
    if unmatched:
        preview = ", ".join(f"r{m.excel_row}:{m.excel_label}" for m in unmatched[:8])
        issues.append(
            Issue(severity="info", code="rows_unmatched",
                  message=f"{len(unmatched)} workbook row(s) left blank (headings/unknown captions): {preview}"
                          + (" ..." if len(unmatched) > 8 else ""))
        )
    return issues


def validate(values: dict[str, float], mappings: list[RowMapping]) -> list[Issue]:
    return check_arithmetic(values) + check_plausibility(values) + check_coverage(mappings, values)


def has_blocking_errors(issues: list[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)
