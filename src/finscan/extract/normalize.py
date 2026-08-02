"""Unit reconciliation and safe derivation of missing subtotals.

Scale mismatch (PDF in lakhs, model in crores) is the single most common way a
"correct" extraction still lands wrong in the spreadsheet, so it is handled
explicitly rather than left to the model.
"""
from __future__ import annotations

import re

from finscan.schemas import NON_SCALED_FIELDS, Extraction, Issue

UNIT_MULTIPLIER: dict[str, float] = {
    "units": 1.0,
    "thousands": 1e3,
    "lakhs": 1e5,
    "millions": 1e6,
    "crores": 1e7,
    "billions": 1e9,
}

# Currency tokens that may sit either side of the scale word. Written this way
# because real headers say all of: "(Rs. in Lakhs)", "RMB in millions",
# "(RMB million)", "US$ mn", "figures in ’000s".
_CCY = r"(?:rs\.?|inr|₹|usd|us\$|\$|rmb|cny|eur|€|gbp|£|hk\$|hkd|jpy|¥|sgd|aed)"

_UNIT_PATTERNS: list[tuple[str, str]] = [
    (rf"\b(?:in\s+)?{_CCY}?\s*crores?\b|\bcr\.?\b", "crores"),
    (rf"\b(?:in\s+)?{_CCY}?\s*(?:lakhs?|lacs?)\b", "lakhs"),
    (rf"\b(?:in\s+)?{_CCY}?\s*(?:millions?|mn|mm)\b", "millions"),
    (rf"\b{_CCY}\s*in\s*millions?\b", "millions"),
    (rf"\b(?:in\s+)?{_CCY}?\s*(?:billions?|bn)\b", "billions"),
    (rf"\b(?:in\s+)?{_CCY}?\s*(?:thousands?|'?000s?|’?000s?)\b", "thousands"),
]


def sniff_units(text: str) -> str | None:
    """Detect a printed scale marker such as '(Rs. in Lakhs)' in free text."""
    low = (text or "").lower()
    for pattern, unit in _UNIT_PATTERNS:
        if re.search(pattern, low):
            return unit
    return None


def convert(value: float, from_units: str, to_units: str) -> float:
    return value * UNIT_MULTIPLIER[from_units] / UNIT_MULTIPLIER[to_units]


def to_target_units(
    extraction: Extraction, target_units: str
) -> tuple[dict[str, float], list[Issue]]:
    """Flatten the extraction into {canonical_field: value} in the workbook's scale."""
    issues: list[Issue] = []
    src = extraction.meta.units
    if target_units not in UNIT_MULTIPLIER:
        issues.append(
            Issue(
                severity="warning",
                code="unknown_target_units",
                message=f"Workbook units '{target_units}' unrecognised; assuming same scale as PDF ({src}).",
            )
        )
        target_units = src
    if src != target_units:
        where = "base units for checking" if target_units == "units" else f"{target_units} (workbook)"
        issues.append(
            Issue(
                severity="info",
                code="units_converted",
                message=f"Filing is in {src}; rescaled to {where}. "
                        f"Each sheet is then converted to its own scale at write time.",
            )
        )

    values: dict[str, float] = {}
    for item in extraction.line_items:
        fid = item.field.value
        v = item.value
        if fid not in NON_SCALED_FIELDS:
            v = convert(v, src, target_units)
        if fid in values and abs(values[fid] - v) > 1e-9:
            issues.append(
                Issue(
                    severity="warning",
                    code="duplicate_field",
                    field=fid,
                    message=f"{fid} extracted twice with different values "
                    f"({values[fid]:,.2f} vs {v:,.2f}); kept the first.",
                )
            )
            continue
        values[fid] = v
    return values, issues


DERIVATIONS: list[tuple[str, tuple[str, ...], str]] = [
    ("total_income", ("revenue_from_operations", "other_income"), "revenue + other income"),
    (
        "ebitda",
        ("profit_before_tax", "finance_costs", "depreciation_amortisation"),
        "PBT + finance costs + D&A",
    ),
    ("profit_after_tax", ("profit_before_tax", "tax_expense"), "PBT - tax"),
    ("tax_expense", ("current_tax", "deferred_tax"), "current + deferred tax"),
]


def derive_missing(values: dict[str, float]) -> tuple[dict[str, float], list[Issue]]:
    """Fill subtotals that the filing did not print, flagging each one as derived.

    Derived values are marked so the review report can show them separately —
    they are never presented as if they were read off the page.
    """
    issues: list[Issue] = []
    out = dict(values)

    def has(*keys: str) -> bool:
        return all(k in out for k in keys)

    if "total_income" not in out and has("revenue_from_operations", "other_income"):
        out["total_income"] = out["revenue_from_operations"] + out["other_income"]
        issues.append(Issue(severity="info", code="derived", field="total_income",
                            message="total_income derived = revenue + other income"))

    if "tax_expense" not in out and has("current_tax", "deferred_tax"):
        out["tax_expense"] = out["current_tax"] + out["deferred_tax"]
        issues.append(Issue(severity="info", code="derived", field="tax_expense",
                            message="tax_expense derived = current + deferred tax"))

    if "profit_after_tax" not in out and has("profit_before_tax", "tax_expense"):
        out["profit_after_tax"] = out["profit_before_tax"] - out["tax_expense"]
        issues.append(Issue(severity="info", code="derived", field="profit_after_tax",
                            message="profit_after_tax derived = PBT - tax"))

    if "ebitda" not in out and has("profit_before_tax", "finance_costs", "depreciation_amortisation"):
        ebitda = (
            out["profit_before_tax"]
            + out["finance_costs"]
            + out["depreciation_amortisation"]
            - out.get("other_income", 0.0)
            - out.get("exceptional_items", 0.0)
        )
        out["ebitda"] = ebitda
        issues.append(Issue(
            severity="info", code="derived", field="ebitda",
            message="ebitda derived = PBT + finance costs + D&A - other income - exceptional items",
        ))

    return out, issues
