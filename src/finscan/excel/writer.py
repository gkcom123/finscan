"""Write the new period column into every in-scope sheet.

Three invariants, in priority order:

1. **A formula is never overwritten.** Checked against the live target cell at
   write time, not just against the plan — belt and braces, because a stale
   profile must not be able to destroy a calculation.
2. **Only blue (input) rows receive extracted values.** Black rows keep being
   black: their formula is copied forward from the reference column and
   re-pointed at the new column, so subtotals keep calculating exactly the way
   the company built them.
3. **The source file is never modified.** A copy is produced.

Every written cell carries a comment recording where the number came from, and
for formula rows the comment also records what the PDF said the subtotal was —
so a reviewer can eyeball the model's own calculation against the filing.
"""
from __future__ import annotations

import re
import shutil
from copy import copy
from math import isfinite, log10
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.comments import Comment
from openpyxl.formula.translate import Translator
from openpyxl.utils import column_index_from_string, get_column_letter

from finscan.config import settings
from finscan.excel.discovery import SheetPlan, WorkbookPlan
from finscan.excel.style_probe import cell_has_formula
from finscan.extract.normalize import UNIT_MULTIPLIER
from finscan.schemas import NON_SCALED_FIELDS, Issue, SheetWriteResult, WriteResult
from finscan.validate import SIGNED_EXPENSE_FIELDS

_COL_REF = re.compile(r"\$?([A-Za-z]{1,3})\$?[0-9]{1,7}")


def _max_referenced_col(formula: str) -> int:
    """Highest column referenced by a formula, or 0 if it references none."""
    cols = [column_index_from_string(m.group(1).upper()) for m in _COL_REF.finditer(formula)]
    return max(cols) if cols else 0


def scale_to_sheet(values: dict[str, float], units: str) -> dict[str, float]:
    """Base-unit values -> the scale this particular sheet is kept in.

    Sheets in one workbook do not always agree: a summary tab in crores next to
    a detail tab in lakhs is common, so the conversion happens per sheet rather
    than once for the workbook.
    """
    m = UNIT_MULTIPLIER.get(units, 1.0)
    return {k: (v if k in NON_SCALED_FIELDS else v / m) for k, v in values.items()}


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


_LABEL_SCALE_STEPS = (1e-9, 1e-6, 1e-3, 1e3, 1e6, 1e9)


def _maybe_rescale_label_value(val: float, ref_val: float | None) -> tuple[float, float | None]:
    """Catch a label-matched value pulled from a differently-scaled PDF table.

    extract_for_labels() matches a row's raw caption against whatever passage
    of the PDF mentions it, and a filing does not always state every line in
    the same units its main statement table uses (e.g. a note table in
    absolute currency next to a summary stated in millions). That value has no
    equivalent of the main taxonomy pass's _maybe_rescale_items_to_printed_units
    cross-check, but the existing reference-period cell for that same row is a
    free anchor: if the new value is a clean power-of-ten away from it, treat
    it as a unit mismatch rather than a genuine period-over-period jump.
    """
    if ref_val is None or abs(ref_val) < 1e-9 or abs(val) < 1e-9:
        return val, None
    ratio = abs(val / ref_val)
    step = min(_LABEL_SCALE_STEPS, key=lambda s: abs(log10(ratio / s)))
    if abs(log10(ratio / step)) <= 0.35:
        return val / step, step
    return val, None


def _maybe_fix_label_sign(val: float, ref_val: float | None) -> tuple[float, bool]:
    """Match a label-matched value's sign to this SAME row's existing convention.

    extract_for_labels() has no canonical field, so it has none of
    validate.py's sheet-wide sign-convention detection either — whatever sign
    the LLM/source printed goes straight through. The existing reference-period
    cell for this exact row is a free, row-specific anchor for what this
    particular line is supposed to look like (e.g. an expense row the model
    has always carried negative): if the signs disagree and both numbers are
    genuinely nonzero, flip the new value to match rather than trust the source.
    """
    if ref_val is None or abs(ref_val) < 1e-9 or abs(val) < 1e-9:
        return val, False
    if (val < 0) != (ref_val < 0):
        return -val, True
    return val, False


_BIDIRECTIONAL_LABEL = re.compile(
    r"gain.*loss|loss.*gain|profit.*loss|loss.*profit"
    r"|valuation\s+effect|fair\s+value\s+adjustment|mark[- ]to[- ]market"
    r"|remeasurement|revaluation",
    re.IGNORECASE,
)


def _label_can_flip_sign(label: str) -> bool:
    """A caption naming both directions (e.g. "Foreign exchange gain (loss)")
    or a market-driven revaluation line (e.g. "Valuation effect on financial
    instruments", "Fair value adjustment...") is telling us the sign is this
    period's actual data, not a fixed per-row convention — these lines
    legitimately swing between a gain and a loss quarter to quarter based on
    real market movement, so _maybe_fix_label_sign must not force them to
    match whatever sign the prior period happened to have."""
    return bool(_BIDIRECTIONAL_LABEL.search(label or ""))


def _infer_units_from_reference(ws, plan: SheetPlan, base_values: dict[str, float]) -> str | None:
    """Infer sheet scale from the reference column's numeric magnitudes.

    Some workbooks carry stale unit labels (e.g. header says millions while the
    actual period columns are in thousands). Pick the unit whose scaled values
    best match the existing reference-column numbers.
    """
    ref = plan.reference_col or plan.last_period_col

    anchors: list[tuple[str, float]] = []
    for rp in plan.rows:
        if not rp.writable or rp.carries_formula or rp.field is None:
            continue
        if rp.field not in base_values:
            continue
        cell = ws.cell(rp.row, ref)
        if cell_has_formula(cell) or not _is_number(cell.value):
            continue
        rv = float(cell.value)
        if abs(rv) < 1e-9:
            continue
        anchors.append((rp.field, rv))

    if len(anchors) < 3:
        return None

    units = [u for u in UNIT_MULTIPLIER if u != "units"] + ["units"]

    def _score(unit: str) -> float:
        scaled = scale_to_sheet(base_values, unit)
        errs: list[float] = []
        for field, ref_val in anchors:
            pred = scaled.get(field)
            if pred is None or abs(pred) < 1e-9:
                continue
            errs.append(abs(log10(abs(pred) / abs(ref_val))))
        if len(errs) < 3:
            return float("inf")
        errs.sort()
        return errs[len(errs) // 2]  # median absolute log-distance

    declared = plan.units if plan.units in UNIT_MULTIPLIER else "units"
    declared_score = _score(declared)
    best = min(units, key=_score)
    best_score = _score(best)

    if not isfinite(best_score) or not isfinite(declared_score):
        return None

    # Require a meaningful win before overriding the declared unit.
    if best != declared and (best_score + 0.35) < declared_score:
        return best
    return None


def _merged_anchor_conflict(ws, row: int, col: int) -> bool:
    """True if the target is inside a merged range but is not its anchor."""
    for rng in ws.merged_cells.ranges:
        if (rng.min_row <= row <= rng.max_row and rng.min_col <= col <= rng.max_col
                and (row, col) != (rng.min_row, rng.min_col)):
            return True
    return False


def _copy_style(src, dst) -> None:
    if src is None:
        return
    dst.number_format = src.number_format
    dst.font = copy(src.font)
    dst.alignment = copy(src.alignment)
    dst.border = copy(src.border)
    dst.fill = copy(src.fill)


def _latest_numeric_left(ws, row: int, from_col: int, min_col: int = 2) -> tuple[float, str] | None:
    """Find the nearest numeric non-formula value to the left in the same row."""
    for c in range(from_col - 1, min_col - 1, -1):
        cell = ws.cell(row, c)
        if cell_has_formula(cell):
            continue
        if _is_number(cell.value):
            return float(cell.value), f"{get_column_letter(c)}{row}"
    return None


def _stacked_header_start_row(ws, header_row: int, ref_col: int, line_count: int) -> int | None:
    """Return the start row for a stacked header block, or None.

    Some templates anchor header_row at the top of the 4-line block; others
    anchor it at the bottom. Detect both using reference-column content.
    """
    if line_count <= 1:
        return None

    def _count_non_empty(start: int, end: int) -> int:
        n = 0
        for r in range(start, end + 1):
            v = ws.cell(r, ref_col).value
            if isinstance(v, str) and v.strip():
                n += 1
        return n

    # Top-anchored block: header_row is first line (e.g., Quarterly).
    top_start = header_row
    top_end = header_row + line_count - 1
    top_hits = _count_non_empty(top_start, top_end)

    # Bottom-anchored block: header_row is last line (e.g., Q2).
    bot_start = max(1, header_row - line_count + 1)
    bot_end = header_row
    bot_hits = _count_non_empty(bot_start, bot_end)

    need = max(2, line_count - 1)
    if top_hits >= need and top_hits >= bot_hits:
        return top_start
    if bot_hits >= need:
        return bot_start
    return None


def write_workbook(
    plan: WorkbookPlan,
    values: dict[str, float],
    header: str,
    header_lines: list[str] | None = None,
    enabled_sheets: set[str] | None = None,
    output_path: str | Path | None = None,
    min_confidence: float | None = None,
    field_confidence: dict[str, float] | None = None,
    pdf_labels: dict[str, str] | None = None,
    label_values: dict[str, float] | None = None,
) -> tuple[WriteResult, list[Issue]]:
    min_confidence = settings.finscan_min_confidence if min_confidence is None else min_confidence
    field_confidence = field_confidence or {}
    pdf_labels = pdf_labels or {}
    issues: list[Issue] = []

    src = Path(plan.path)
    dst = Path(output_path) if output_path else src.with_name(f"{src.stem}_updated{src.suffix}")
    if src.suffix.lower() == ".xlsm" and dst.suffix.lower() != ".xlsm":
        issues.append(Issue(
            severity="warning", code="macros_stripped",
            message=f"Source is macro-enabled ({src.suffix}) but the output path is "
                     f"'{dst.suffix}' — VBA macros will be dropped from the written copy. "
                     f"Use an .xlsm output path to keep them.",
        ))
    if dst.resolve() != src.resolve():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)

    wb = load_workbook(dst, data_only=False, keep_vba=dst.suffix.lower() == ".xlsm")
    # Read-only twin, cached formula results only — used to fall back to a
    # plain value when a formula can't be safely translated into the new column.
    wb_values = load_workbook(dst, data_only=True)

    targets = [s for s in plan.in_scope
               if enabled_sheets is None or s.sheet in enabled_sheets]
    if not targets:
        issues.append(Issue(severity="error", code="no_target_sheets",
                            message="No sheet is both in scope and enabled in the profile."))

    results: list[SheetWriteResult] = []
    for sheet_plan in targets:
        effective_units = sheet_plan.units
        inferred_units = _infer_units_from_reference(wb[sheet_plan.sheet], sheet_plan, values)
        if inferred_units and inferred_units != sheet_plan.units:
            effective_units = inferred_units
            issues.append(Issue(
                severity="warning", code="sheet_units_inferred_from_reference",
                message=f"{sheet_plan.sheet}: detected '{sheet_plan.units}' in headers but "
                        f"reference-column magnitudes match '{inferred_units}'. "
                        f"Used '{inferred_units}' for this run.",
            ))

        res, sheet_issues = _write_sheet(
            wb[sheet_plan.sheet], sheet_plan,
            scale_to_sheet(values, effective_units), header, header_lines,
            min_confidence, field_confidence, pdf_labels,
            scale_to_sheet(label_values, effective_units) if label_values else None,
            wb_values[sheet_plan.sheet] if sheet_plan.sheet in wb_values.sheetnames else None,
        )
        results.append(res)
        issues.extend(sheet_issues)

    # Always re-serialize through openpyxl before closing, even if nothing was
    # written — otherwise the raw byte-for-byte copy made above (which may still
    # carry a source .xlsm's VBA project) is left on disk under the destination's
    # extension, which is exactly what makes Excel warn that a ".xlsx" file
    # "contains macro-enabled content".
    _write_audit(wb, header, plan, results, issues)
    wb.save(dst)
    wb.close()
    wb_values.close()

    return WriteResult(workbook_path=str(dst), header_written=header, sheets=results), issues


def _write_sheet(
    ws, plan: SheetPlan, values: dict[str, float], header: str,
    header_lines: list[str] | None,
    min_confidence: float, field_confidence: dict[str, float],
    pdf_labels: dict[str, str],
    label_values: dict[str, float] | None = None,
    ws_values: Any = None,
) -> tuple[SheetWriteResult, list[Issue]]:
    issues: list[Issue] = []
    col = plan.write_col
    ref = plan.reference_col or plan.last_period_col
    letter = get_column_letter(col)
    written = copied = skipped = 0
    header_rows_written: set[int] = set()

    # --- header -----------------------------------------------------------
    normalized_lines = [x for x in (header_lines or []) if x and x.strip()]
    if not normalized_lines:
        normalized_lines = [x.strip() for x in str(header).split("/") if x.strip()] or [str(header)]

    start = _stacked_header_start_row(ws, plan.header_row, ref, len(normalized_lines))
    if len(normalized_lines) > 1 and start is not None:
        top = start
        for i, line in enumerate(normalized_lines):
            row = top + i
            if _merged_anchor_conflict(ws, row, col):
                continue
            hdr = ws.cell(row, col)
            _copy_style(ws.cell(row, ref), hdr)
            hdr.value = line
            header_rows_written.add(row)
    elif not _merged_anchor_conflict(ws, plan.header_row, col):
        hdr = ws.cell(plan.header_row, col)
        if not cell_has_formula(hdr):
            _copy_style(ws.cell(plan.header_row, ref), hdr)
            hdr.value = header
            header_rows_written.add(plan.header_row)
    try:
        ws.column_dimensions[letter].width = ws.column_dimensions[get_column_letter(ref)].width
    except Exception:
        pass

    # --- rows -------------------------------------------------------------
    for rp in plan.rows:
        if rp.row in header_rows_written:
            continue
        target = ws.cell(rp.row, col)

        if _merged_anchor_conflict(ws, rp.row, col):
            skipped += 1
            issues.append(Issue(severity="warning", code="merged_cell_skipped", field=rp.field,
                                message=f"{plan.sheet}!{letter}{rp.row} sits inside a merged "
                                        f"range; left untouched."))
            continue

        # Invariant 1: never overwrite an existing formula.
        if cell_has_formula(target):
            skipped += 1
            issues.append(Issue(severity="warning", code="formula_protected", field=rp.field,
                                message=f"{plan.sheet}!{letter}{rp.row} already holds a formula; "
                                        f"left untouched."))
            continue

        # Formula rows: carry the company's own calculation forward.
        if rp.carries_formula:
            _copy_style(ws.cell(rp.row, ref), target)
            try:
                translated = Translator(
                    rp.formula_template, origin=rp.reference_cell
                ).translate_formula(f"{letter}{rp.row}")
                # A wide gap between reference and write column (e.g. writing into
                # a quarterly block appended far past an annual/LTM reference
                # column) can shift a relative reference clean off the sheet. A
                # formula pointing at nothing is worse than no formula, so fall
                # back to the reference cell's last known value instead.
                if _max_referenced_col(translated) > ws.max_column and ws_values is not None:
                    ref_cached = ws_values.cell(rp.row, ref).value
                    if _is_number(ref_cached):
                        target.value = ref_cached
                        target.comment = Comment(
                            "FinScan\n"
                            f"Could not safely translate the formula from {rp.reference_cell} "
                            "(it would reference a column past the end of the sheet). "
                            "Carried forward its last computed value instead.",
                            "FinScan",
                        )
                        written += 1
                        issues.append(Issue(
                            severity="warning", code="formula_translation_out_of_range", field=rp.field,
                            message=f"{plan.sheet}!{letter}{rp.row}: translating the formula from "
                                    f"{rp.reference_cell} would reference a column past the sheet's "
                                    f"edge; wrote its last value ({ref_cached}) instead."))
                        continue
                target.value = translated
                copied += 1
                reported = values.get(rp.field) if rp.field else None
                target.comment = Comment(
                    f"FinScan\nformula copied from {rp.reference_cell}\n"
                    + (f"filing reported {reported:,.2f} — compare with the computed value"
                       if reported is not None else "not reported in the filing"),
                    "FinScan",
                )
            except Exception as exc:
                skipped += 1
                issues.append(Issue(severity="warning", code="formula_copy_failed", field=rp.field,
                                    message=f"Could not translate the formula for "
                                            f"{plan.sheet}!{letter}{rp.row}: {exc}"))
            continue

        # Some model blocks use typed arithmetic formulas with no references
        # (e.g. "=1.2-0.7"). For non-mapped rows we still carry these forward
        # so the destination column is structurally complete.
        if rp.constant_formula and rp.formula_template and rp.field is None:
            _copy_style(ws.cell(rp.row, ref), target)
            target.value = rp.formula_template
            target.comment = Comment(
                f"FinScan\nconstant formula carried from {rp.reference_cell}",
                "FinScan",
            )
            copied += 1
            continue

        # Input rows: paste the extracted value.
        # label_only rows (unresolved by taxonomy) use their raw label as the key.
        if rp.label_only and rp.field is None:
            if not rp.writable:
                skipped += 1
                continue
            val = (label_values or {}).get(rp.label)
            if val is not None:
                ref_val = ws.cell(rp.row, ref).value
                ref_num = ref_val if _is_number(ref_val) else None
                rescaled, step = _maybe_rescale_label_value(val, ref_num)
                if step is not None:
                    issues.append(Issue(
                        severity="warning", code="label_value_rescaled", field=rp.field,
                        message=f"{plan.sheet}!{letter}{rp.row} ('{rp.label}'): matched PDF value "
                                f"{val:,.2f} was ~{step:,.0f}x the existing {rp.reference_cell} value "
                                f"({ref_val:,.2f}); divided by {step:,.0f} before writing."))
                val = rescaled
                if not _label_can_flip_sign(rp.label):
                    val, flipped = _maybe_fix_label_sign(val, ref_num)
                    if flipped:
                        issues.append(Issue(
                            severity="warning", code="label_value_sign_flipped", field=rp.field,
                            message=f"{plan.sheet}!{letter}{rp.row} ('{rp.label}'): matched PDF value "
                                    f"had the opposite sign from the existing {rp.reference_cell} value "
                                    f"({ref_val:,.2f}); flipped to match this row's convention."))
                _copy_style(ws.cell(rp.row, ref), target)
                target.value = val
                target.comment = Comment(
                    f"FinScan\nPDF label match: '{rp.label}'\nrow match: label_only",
                    "FinScan",
                )
                written += 1
                continue

            ref_val = ws.cell(rp.row, ref).value
            if _is_number(ref_val):
                _copy_style(ws.cell(rp.row, ref), target)
                target.value = ref_val
                target.comment = Comment(
                    "FinScan\n"
                    "No direct PDF label value was matched for this blue input row. "
                    "Carried forward the previous period value from "
                    f"{rp.reference_cell}.",
                    "FinScan",
                )
                written += 1
                issues.append(Issue(
                    severity="warning", code="input_row_carried_forward", field=rp.field,
                    message=f"{plan.sheet}!{letter}{rp.row} ('{rp.label}') had no matched "
                            f"filing value; copied prior-period input from {rp.reference_cell}.",
                ))
                continue

            prev = _latest_numeric_left(ws, rp.row, ref)
            if prev is not None:
                prev_val, prev_cell = prev
                _copy_style(ws.cell(rp.row, ref), target)
                target.value = prev_val
                target.comment = Comment(
                    "FinScan\n"
                    "No direct PDF label value was matched for this blue input row. "
                    "Carried forward the nearest prior period value from "
                    f"{prev_cell}.",
                    "FinScan",
                )
                written += 1
                issues.append(Issue(
                    severity="warning", code="input_row_carried_forward", field=rp.field,
                    message=f"{plan.sheet}!{letter}{rp.row} ('{rp.label}') had no matched "
                            f"filing value; copied prior-period input from {prev_cell}.",
                ))
                continue

            _copy_style(ws.cell(rp.row, ref), target)
            target.value = 0.0
            target.comment = Comment(
                "FinScan\n"
                "No filing value or prior-period numeric input was found for this blue row. "
                "Set to 0.0 to avoid leaving the input blank.",
                "FinScan",
            )
            written += 1
            issues.append(Issue(
                severity="warning", code="input_row_default_zero", field=rp.field,
                message=f"{plan.sheet}!{letter}{rp.row} ('{rp.label}') had no matched filing "
                        "value and no prior-period numeric input; set to 0.0.",
            ))
            continue

        if not rp.writable:
            skipped += 1
            continue

        # Canonical field missing? Fall back to direct label match if available.
        if rp.field is not None and rp.field not in values:
            val = (label_values or {}).get(rp.label)
            if val is not None:
                ref_val = ws.cell(rp.row, ref).value
                ref_num = ref_val if _is_number(ref_val) else None
                rescaled, step = _maybe_rescale_label_value(val, ref_num)
                if step is not None:
                    issues.append(Issue(
                        severity="warning", code="label_value_rescaled", field=rp.field,
                        message=f"{plan.sheet}!{letter}{rp.row} ('{rp.label}'): matched PDF value "
                                f"{val:,.2f} was ~{step:,.0f}x the existing {rp.reference_cell} value "
                                f"({ref_val:,.2f}); divided by {step:,.0f} before writing."))
                val = rescaled
                if not _label_can_flip_sign(rp.label):
                    val, flipped = _maybe_fix_label_sign(val, ref_num)
                    if flipped:
                        issues.append(Issue(
                            severity="warning", code="label_value_sign_flipped", field=rp.field,
                            message=f"{plan.sheet}!{letter}{rp.row} ('{rp.label}'): matched PDF value "
                                    f"had the opposite sign from the existing {rp.reference_cell} value "
                                    f"({ref_val:,.2f}); flipped to match this row's convention."))
                _copy_style(ws.cell(rp.row, ref), target)
                target.value = val
                target.comment = Comment(
                    f"FinScan\nfield: {rp.field}\nPDF label fallback: '{rp.label}'\n"
                    f"row match: {rp.match_method} + label_fallback",
                    "FinScan",
                )
                written += 1
                continue

        if rp.field is None or rp.field not in values:
            ref_val = ws.cell(rp.row, ref).value
            if _is_number(ref_val):
                _copy_style(ws.cell(rp.row, ref), target)
                target.value = ref_val
                target.comment = Comment(
                    "FinScan\n"
                    "No filing value was matched for this blue input row. "
                    "Carried forward the previous period value from "
                    f"{rp.reference_cell}.",
                    "FinScan",
                )
                written += 1
                issues.append(Issue(
                    severity="warning", code="input_row_carried_forward", field=rp.field,
                    message=f"{plan.sheet}!{letter}{rp.row} ('{rp.label}') had no matched "
                            f"filing value; copied prior-period input from {rp.reference_cell}.",
                ))
                continue

            prev = _latest_numeric_left(ws, rp.row, ref)
            if prev is not None:
                prev_val, prev_cell = prev
                _copy_style(ws.cell(rp.row, ref), target)
                target.value = prev_val
                target.comment = Comment(
                    "FinScan\n"
                    "No filing value was matched for this blue input row. "
                    "Carried forward the nearest prior period value from "
                    f"{prev_cell}.",
                    "FinScan",
                )
                written += 1
                issues.append(Issue(
                    severity="warning", code="input_row_carried_forward", field=rp.field,
                    message=f"{plan.sheet}!{letter}{rp.row} ('{rp.label}') had no matched "
                            f"filing value; copied prior-period input from {prev_cell}.",
                ))
                continue

            _copy_style(ws.cell(rp.row, ref), target)
            target.value = 0.0
            target.comment = Comment(
                "FinScan\n"
                "No filing value or prior-period numeric input was found for this blue row. "
                "Set to 0.0 to avoid leaving the input blank.",
                "FinScan",
            )
            written += 1
            issues.append(Issue(
                severity="warning", code="input_row_default_zero", field=rp.field,
                message=f"{plan.sheet}!{letter}{rp.row} ('{rp.label}') had no matched filing "
                        "value and no prior-period numeric input; set to 0.0.",
            ))
            continue

        conf = min(rp.match_score / 100.0, field_confidence.get(rp.field, 0.85))
        if conf < min_confidence:
            issues.append(Issue(severity="warning", code="low_confidence_skipped", field=rp.field,
                    message=f"{plan.sheet}!{letter}{rp.row} ('{rp.label}') was written "
                        f"despite low confidence {conf:.2f} < {min_confidence:.2f}. "
                        "Review this row."))

        val = values[rp.field]
        if rp.field in SIGNED_EXPENSE_FIELDS:
            ref_val = ws.cell(rp.row, ref).value
            ref_num = ref_val if _is_number(ref_val) else None
            val, flipped = _maybe_fix_label_sign(val, ref_num)
            if flipped:
                issues.append(Issue(
                    severity="warning", code="value_sign_flipped", field=rp.field,
                    message=f"{plan.sheet}!{letter}{rp.row} ('{rp.label}'): extracted value "
                            f"had the opposite sign from the existing {rp.reference_cell} value "
                            f"({ref_val:,.2f}); flipped to match this row's convention — this "
                            "field is always a cost, so its sign should not flip quarter to "
                            "quarter."))

        _copy_style(ws.cell(rp.row, ref), target)
        target.value = val
        kind = ("previous column held typed-in arithmetic "
                f"(`{rp.formula_template}`), replaced with this period's figure"
                if rp.constant_formula else "blue input cell — model formulas untouched")
        target.comment = Comment(
            f"FinScan\nfield: {rp.field}\nPDF caption: {pdf_labels.get(rp.field, 'derived')}\n"
            f"row match: {rp.match_method} ({rp.match_score:.0f})\nconfidence: {conf:.2f}"
            + (" (LOW)" if conf < min_confidence else "")
            + f"\n{kind}",
            "FinScan",
        )
        written += 1
        if rp.constant_formula:
            issues.append(Issue(
                severity="info", code="constant_formula_replaced", field=rp.field,
                message=f"{plan.sheet}!{letter}{rp.row} ('{rp.label}'): the previous column held "
                        f"`{rp.formula_template}` — hardcoded arithmetic rather than a reference. "
                        f"Written as this period's value instead of copying it forward."))

    # Rows the sheet has but the convention says are not ours to fill.
    unwritable = [r for r in plan.rows if r.field and not r.writable and not r.carries_formula]
    if unwritable:
        issues.append(Issue(
            severity="info", code="non_input_rows",
            message=f"{plan.sheet}: {len(unwritable)} matched row(s) are neither blue inputs nor "
                    f"formulas in the reference column, so they were left alone "
                    f"(e.g. {', '.join(r.label for r in unwritable[:4])})."))

    return (
        SheetWriteResult(sheet=plan.sheet, column_letter=letter, column_index=col,
                         write_mode=plan.write_mode, values_written=written,
                         formulas_copied=copied, rows_skipped=skipped),
        issues,
    )


def _write_audit(wb, header: str, plan: WorkbookPlan,
                 results: list[SheetWriteResult], issues: list[Issue]) -> None:
    name = "FinScan_Audit"
    ws = wb[name] if name in wb.sheetnames else wb.create_sheet(name)
    start = (ws.max_row + 2) if ws.max_row > 1 else 1
    lines = [f"--- {header} ---", f"workbook fingerprint: {plan.fingerprint}"]
    for r in results:
        lines.append(
            f"{r.sheet}: column {r.column_letter} ({r.write_mode}) — "
            f"{r.values_written} value(s), {r.formulas_copied} formula(s) copied, "
            f"{r.rows_skipped} row(s) skipped"
        )
    for i in issues:
        lines.append(f"[{i.severity}] {i.code}: {i.message}")
    for offset, line in enumerate(lines):
        ws.cell(start + offset, 1).value = line
    ws.column_dimensions["A"].width = 130
