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

import shutil
from copy import copy
from math import isfinite, log10
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.comments import Comment
from openpyxl.formula.translate import Translator
from openpyxl.utils import get_column_letter

from finscan.config import settings
from finscan.excel.discovery import SheetPlan, WorkbookPlan
from finscan.excel.style_probe import cell_has_formula
from finscan.extract.normalize import UNIT_MULTIPLIER
from finscan.schemas import NON_SCALED_FIELDS, Issue, SheetWriteResult, WriteResult


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


def write_workbook(
    plan: WorkbookPlan,
    values: dict[str, float],
    header: str,
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
            scale_to_sheet(values, effective_units), header,
            min_confidence, field_confidence, pdf_labels,
            scale_to_sheet(label_values, effective_units) if label_values else None,
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

    return WriteResult(workbook_path=str(dst), header_written=header, sheets=results), issues


def _write_sheet(
    ws, plan: SheetPlan, values: dict[str, float], header: str,
    min_confidence: float, field_confidence: dict[str, float],
    pdf_labels: dict[str, str],
    label_values: dict[str, float] | None = None,
) -> tuple[SheetWriteResult, list[Issue]]:
    issues: list[Issue] = []
    col = plan.write_col
    ref = plan.reference_col or plan.last_period_col
    letter = get_column_letter(col)
    written = copied = skipped = 0

    # --- header -----------------------------------------------------------
    if not _merged_anchor_conflict(ws, plan.header_row, col):
        hdr = ws.cell(plan.header_row, col)
        if not cell_has_formula(hdr):
            _copy_style(ws.cell(plan.header_row, ref), hdr)
            hdr.value = header
    try:
        ws.column_dimensions[letter].width = ws.column_dimensions[get_column_letter(ref)].width
    except Exception:
        pass

    # --- rows -------------------------------------------------------------
    for rp in plan.rows:
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
                target.value = Translator(
                    rp.formula_template, origin=rp.reference_cell
                ).translate_formula(f"{letter}{rp.row}")
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

        # Input rows: paste the extracted value.
        # label_only rows (unresolved by taxonomy) use their raw label as the key.
        if rp.label_only and rp.field is None:
            val = (label_values or {}).get(rp.label)
            if val is None or not rp.writable:
                skipped += 1
                continue
            _copy_style(ws.cell(rp.row, ref), target)
            target.value = val
            target.comment = Comment(
                f"FinScan\nPDF label match: '{rp.label}'\nrow match: label_only",
                "FinScan",
            )
            written += 1
            continue

        if not rp.writable or rp.field is None or rp.field not in values:
            skipped += 1
            if rp.constant_formula and rp.field:
                issues.append(Issue(
                    severity="warning", code="constant_formula_not_carried", field=rp.field,
                    message=f"{plan.sheet}!{letter}{rp.row} ('{rp.label}') left empty. "
                            f"The previous column holds `{rp.formula_template}` — typed-in "
                            f"arithmetic, not a reference — so copying it would repeat last "
                            f"period's figure, and the filing gave no value for this row."))
            continue

        conf = min(rp.match_score / 100.0, field_confidence.get(rp.field, 0.85))
        if conf < min_confidence:
            skipped += 1
            issues.append(Issue(severity="warning", code="low_confidence_skipped", field=rp.field,
                                message=f"{plan.sheet}!{letter}{rp.row} ('{rp.label}') left blank: "
                                        f"confidence {conf:.2f} < {min_confidence:.2f}."))
            continue

        _copy_style(ws.cell(rp.row, ref), target)
        target.value = values[rp.field]
        kind = ("previous column held typed-in arithmetic "
                f"(`{rp.formula_template}`), replaced with this period's figure"
                if rp.constant_formula else "blue input cell — model formulas untouched")
        target.comment = Comment(
            f"FinScan\nfield: {rp.field}\nPDF caption: {pdf_labels.get(rp.field, 'derived')}\n"
            f"row match: {rp.match_method} ({rp.match_score:.0f})\nconfidence: {conf:.2f}\n{kind}",
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
