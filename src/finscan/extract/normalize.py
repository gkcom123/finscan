"""Unit reconciliation and safe derivation of missing subtotals.

Scale mismatch (PDF in lakhs, model in crores) is the single most common way a
"correct" extraction still lands wrong in the spreadsheet, so it is handled
explicitly rather than left to the model.
"""
from __future__ import annotations

import re

from finscan.schemas import FIELD_ALIASES, NON_SCALED_FIELDS, Extraction, Issue

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
_CCY = (
    r"(?:rs\.?|inr|₹|usd|us\$|\$|rmb|cny|eur|€|gbp|£|hk\$|hkd|jpy|¥|sgd|aed|"
    r"mxn|mx\$|mex\$|pesos?|peso)"
)

_UNIT_PATTERNS: list[tuple[str, str]] = [
    (rf"\b(?:in\s+)?{_CCY}?\s*crores?\b|\bcr\.?\b", "crores"),
    (rf"\b(?:in\s+)?{_CCY}?\s*(?:lakhs?|lacs?)\b", "lakhs"),
    (rf"\b(?:in\s+)?{_CCY}?\s*(?:millions?|millones?|mn|mm)\b", "millions"),
    (rf"\b{_CCY}\s*in\s*millions?\b", "millions"),
    (rf"\b(?:in\s+)?{_CCY}?\s*(?:billions?|bn)\b", "billions"),
    (rf"\b(?:in\s+)?{_CCY}?\s*(?:thousands?|miles?|millar(?:es)?|'?000s?|’?000s?)\b", "thousands"),
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
) -> tuple[dict[str, float], dict[str, int], list[Issue]]:
    """Flatten the extraction into {canonical_field: value} in the workbook's scale.

    Also returns months_covered: {canonical_field: months}, carried alongside values
    using the exact same per-field "which duplicate line item wins" precedence as
    chosen_labels below — there is deliberately only one place that decides which
    duplicate line item wins, so the value and its months-covered annotation can
    never drift apart. Only fields the extractor actually flagged (see
    LineItem.months_covered / extractor._detect_months_covered) appear here; a field
    absent from this dict is assumed to already be a standalone-period figure.
    """
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
    chosen_labels: dict[str, str] = {}
    months_covered: dict[str, int] = {}
    for item in extraction.line_items:
        fid = item.field.value
        v = item.value
        if fid not in NON_SCALED_FIELDS:
            v = convert(v, src, target_units)
        if fid in values and abs(values[fid] - v) > 1e-9:
            # A generic taxonomy field (e.g. "other_expenses") can superficially
            # match several distinct PDF captions in the same breakdown (e.g.
            # "Maintenance expenses", "Property taxes", "Insurance" all loosely
            # read as "other operating expenses"); which one the LLM lists first
            # is not stable across runs. Prefer whichever caption is a literal
            # alias of the field over one that only fits by loose semantics —
            # that is a real signal, not just whatever happened to come first.
            if _alias_hit(item.label_in_pdf, fid) and not _alias_hit(chosen_labels.get(fid, ""), fid):
                issues.append(
                    Issue(
                        severity="warning",
                        code="duplicate_field",
                        field=fid,
                        message=f"{fid} extracted twice with different values "
                        f"({values[fid]:,.2f} vs {v:,.2f}); switched to "
                        f"'{item.label_in_pdf}' — its caption is a closer match "
                        f"for this field than '{chosen_labels.get(fid, '')}'.",
                    )
                )
                values[fid] = v
                chosen_labels[fid] = item.label_in_pdf
                if item.months_covered is not None:
                    months_covered[fid] = item.months_covered
                else:
                    months_covered.pop(fid, None)
                continue
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
        chosen_labels[fid] = item.label_in_pdf
        if item.months_covered is not None:
            months_covered[fid] = item.months_covered
        else:
            months_covered.pop(fid, None)
    return values, months_covered, issues


def _alias_hit(label: str, fid: str) -> bool:
    low = (label or "").lower()
    return any(alias in low for alias in FIELD_ALIASES.get(fid, []))


DERIVATIONS: list[tuple[str, tuple[str, ...], str]] = [
    ("total_income", ("revenue_from_operations", "other_income"), "revenue + other income"),
    (
        "ebitda",
        ("profit_before_tax", "finance_costs", "depreciation_amortisation"),
        "PBT + finance costs + D&A",
    ),
    ("profit_after_tax", ("profit_before_tax", "tax_expense"), "PBT - tax"),
    ("tax_expense", ("current_tax", "deferred_tax"), "current + deferred tax"),
    (
        "change_in_working_capital",
        ("net_cash_from_operating_activities", "total_before_working_capital_changes"),
        "net cash from operating activities - total before working capital changes",
    ),
]

#: Relative tolerance for flagging a printed change-in-WC that disagrees with
#: the definitional Y - X computation.
_WC_TOLERANCE = 0.005


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

    # Change in working capital is DEFINITIONAL, not a fallback: whenever the
    # filing prints both the pre-working-capital subtotal (X) and net operating
    # cash flow (Y), the model's line is Y - X. That definition wins over a
    # directly extracted "changes in working capital" caption, because filings
    # break the movement into many signed sub-lines and the printed subtotal
    # (when present at all) routinely excludes taxes/interest the model expects.
    if has("net_cash_from_operating_activities", "total_before_working_capital_changes"):
        y = out["net_cash_from_operating_activities"]
        x = out["total_before_working_capital_changes"]
        computed = y - x
        printed = out.get("change_in_working_capital")
        out["change_in_working_capital"] = computed
        if printed is None:
            issues.append(Issue(
                severity="info", code="derived", field="change_in_working_capital",
                message=(f"change_in_working_capital = net cash from operating activities "
                         f"({y:,.2f}) - total before working capital changes ({x:,.2f}) "
                         f"= {computed:,.2f}"),
            ))
        elif abs(printed - computed) > _WC_TOLERANCE * max(abs(printed), abs(computed), 1e-9):
            issues.append(Issue(
                severity="warning", code="derived_override", field="change_in_working_capital",
                message=(f"Filing prints change in working capital as {printed:,.2f}, but the "
                         f"definitional Y - X gives {y:,.2f} - {x:,.2f} = {computed:,.2f}. "
                         f"Wrote the computed value; review if the gap is unexpected."),
            ))

        # X and Y are meant to come from the same column of the same table; a
        # movement several times the size of operating cash flow itself usually
        # means one of them was misread from a different (e.g. cumulative or
        # prior-period) column rather than a genuinely large swing. This is the
        # signature of a wrong-column pick when either figure was recovered by
        # a best-effort rescue rather than the main extraction pass.
        if abs(y) > 1e-9 and abs(computed) > 3 * abs(y):
            issues.append(Issue(
                severity="warning", code="implausible_working_capital_swing",
                field="change_in_working_capital",
                message=(f"Computed change in working capital ({computed:,.2f}) is more than "
                         f"3x net cash from operating activities ({y:,.2f}). This often means "
                         f"total_before_working_capital_changes or "
                         f"net_cash_from_operating_activities was read from the wrong column "
                         f"(e.g. a cumulative period instead of the standalone quarter). "
                         f"Verify against the source PDF before relying on this figure."),
            ))

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
