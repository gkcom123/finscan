"""Stage 4 — values.json + workbook -> a new column in a copy of the workbook.

Three invariants, in priority order:

1. **A formula is never overwritten.** Checked against the live target cell at
   write time, not against the map — so a stale map cannot destroy a calculation,
   and a cell an analyst turned into a formula since the map was frozen wins.
2. **Only input cells receive values.** Formula rows have their formula copied
   from the reference column with references translated, so subtotals keep
   calculating the way the company built them.
3. **The source workbook is never modified.** A copy is produced.

Every written cell carries the provenance from values.json as its comment, and a
row left blank carries a comment saying why — an empty cell with no explanation
is indistinguishable from one nobody got to.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.comments import Comment
from openpyxl.formula.translate import Translator
from openpyxl.utils import get_column_letter

from finscan2.match.schema import Values
from finscan2.model.discover import _references_cells
from finscan2.model.load import ResolvedMap
from finscan2.schema import Issue

AUDIT_SHEET = "FinScan_Audit"


@dataclass
class WriteResult:
    output_path: str
    sheet: str
    column: str
    values_written: int = 0
    formulas_copied: int = 0
    blanks_annotated: int = 0
    rows_skipped: int = 0
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]


def _is_merged(worksheet, row: int, col: int) -> bool:
    coordinate = f"{get_column_letter(col)}{row}"
    return any(coordinate in merged for merged in worksheet.merged_cells.ranges)


def _copy_style(source, target) -> None:
    """Carry the reference column's look so the new column reads like the others."""
    if source.has_style:
        target._style = source._style


def write_column(values: Values, resolved_map: ResolvedMap, workbook_path: str,
                 output_path: str, period_end: str | None = None) -> WriteResult:
    """Write the resolved values into a new column of a copy of the workbook."""
    model = resolved_map.model
    write_col = model.write_col
    sheet_name = model.sheet
    letter = get_column_letter(write_col) if write_col else "?"

    result = WriteResult(output_path=output_path, sheet=sheet_name, column=letter)
    if not write_col:
        result.issues.append(Issue(code="no_write_column", severity="error",
                                   message="The map has no write column."))
        return result
    if not model.reference_col:
        result.issues.append(Issue(code="no_reference_column", severity="error",
                                   message="The map has no reference column, so formulas "
                                           "cannot be copied forward."))
        return result

    # Copy first, then edit the copy: the source is never opened for writing.
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(workbook_path, output_path)

    keep_vba = Path(output_path).suffix.lower() in {".xlsm", ".xltm"}
    book = load_workbook(output_path, data_only=False, keep_vba=keep_vba)
    try:
        worksheet = book[sheet_name]
        reference = model.reference_col

        by_row = {v.row: v for v in values.values}
        bound_by_row = {b.row: b.spec for b in resolved_map.bound}

        for row, spec in sorted(bound_by_row.items()):
            target = worksheet.cell(row, write_col)
            source = worksheet.cell(row, reference)

            if _is_merged(worksheet, row, write_col):
                result.rows_skipped += 1
                continue

            # Invariant 1, checked against the live cell rather than the map.
            live = target.value
            if isinstance(live, str) and live.startswith("="):
                result.rows_skipped += 1
                result.issues.append(Issue(
                    code="formula_protected", severity="info",
                    message=f"{letter}{row} ('{spec.key.label}') already holds a formula; "
                            f"left untouched."))
                continue

            if spec.kind == "formula":
                formula = source.value
                # A formula referencing no cell computes nothing the model maintains;
                # copying it forward would carry the reference period's own typed
                # figures into the new column and report them as a formula copy.
                if (isinstance(formula, str) and formula.startswith("=")
                        and not _references_cells(formula)):
                    result.rows_skipped += 1
                    result.issues.append(Issue(
                        code="literal_formula_not_copied", severity="warning",
                        message=f"{letter}{row} ('{spec.key.label}') was left blank: the "
                                f"reference cell holds '{formula}', which references no "
                                f"cell — copying it would carry that period's figures "
                                f"forward. Map this row as an input or a sum."))
                    target.comment = Comment(
                        f"FinScan: no value written. The reference cell holds "
                        f"'{formula}', a typed expression rather than a calculation.",
                        "FinScan")
                    continue
                if isinstance(formula, str) and formula.startswith("="):
                    target.value = Translator(
                        formula, origin=source.coordinate
                    ).translate_formula(target.coordinate)
                    _copy_style(source, target)
                    result.formulas_copied += 1
                else:
                    result.rows_skipped += 1
                continue

            resolved = by_row.get(row)
            if resolved is None:
                result.rows_skipped += 1
                continue

            if not resolved.ok:
                # Blank, but never silently: the reason travels with the cell.
                target.comment = Comment(resolved.comment(), "FinScan")
                _copy_style(source, target)
                result.blanks_annotated += 1
                continue

            target.value = resolved.value
            target.comment = Comment(resolved.comment(), "FinScan")
            _copy_style(source, target)
            result.values_written += 1

        _write_period_header(worksheet, model, write_col, period_end or values.period_end,
                             result)
        _write_audit(book, values, result, model)
        book.save(output_path)
    finally:
        book.close()

    return result


def _write_period_header(worksheet, model, write_col: int, period_end: str,
                         result: WriteResult) -> None:
    """Stamp the new column's date and label.

    The date matters more than it looks: next quarter's run finds this column by
    its date, and both the continuity check and de-cumulation read those dates.
    A column written without one is invisible to the next run.
    """
    if not period_end:
        result.issues.append(Issue(
            code="period_not_stamped", severity="error",
            message="No period end was available, so the new column carries no date. "
                    "The next run will not see this column."))
        return

    try:
        parsed = date.fromisoformat(period_end)
    except ValueError:
        result.issues.append(Issue(code="period_not_stamped", severity="error",
                                   message=f"'{period_end}' is not a date."))
        return

    if model.period_date_row:
        cell = worksheet.cell(model.period_date_row, write_col)
        if not (isinstance(cell.value, str) and cell.value.startswith("=")):
            cell.value = datetime(parsed.year, parsed.month, parsed.day)
            reference = worksheet.cell(model.period_date_row, model.reference_col)
            _copy_style(reference, cell)
    else:
        result.issues.append(Issue(
            code="period_not_stamped", severity="warning",
            message="The map records no row for period dates, so the new column's date "
                    "was not written. Next quarter's run will not see this column."))

    if model.header_row and model.header_row != model.period_date_row:
        header = worksheet.cell(model.header_row, write_col)
        if header.value is None:
            quarter = (parsed.month - 1) // 3 + 1
            header.value = f"Q{quarter} {parsed.year}"
            _copy_style(worksheet.cell(model.header_row, model.reference_col), header)


def _write_audit(book, values: Values, result: WriteResult, model) -> None:
    """A sheet recording what this run did, and what it refused to do.

    Also the fallback for the openpyxl cached-value trap: a formula written here
    has no cached value until Excel opens the file, so next quarter's
    de-cumulation would find nothing. The figures it needs are recorded as plain
    numbers in this sheet.
    """
    if AUDIT_SHEET in book.sheetnames:
        del book[AUDIT_SHEET]
    sheet = book.create_sheet(AUDIT_SHEET)

    sheet.append(["FinScan v2 audit"])
    sheet.append(["written_at", datetime.now().isoformat(timespec="seconds")])
    sheet.append(["company", values.company])
    sheet.append(["sheet", values.sheet])
    sheet.append(["column", result.column])
    sheet.append(["period_end", values.period_end])
    sheet.append(["units", values.units])
    sheet.append(["values_written", result.values_written])
    sheet.append(["formulas_copied", result.formulas_copied])
    sheet.append(["left_blank", result.blanks_annotated])
    sheet.append([])
    sheet.append(["row", "label", "value", "source_caption", "column_header",
                  "printed", "adjustment", "unresolved"])

    for value in values.values:
        source = value.source
        sheet.append([
            value.row, value.label, value.value,
            source.caption if source else "",
            source.column_header if source else "",
            source.printed if source else "",
            "; ".join(a.detail for a in value.adjustments),
            value.unresolved or "",
        ])

    sheet.append([])
    sheet.append(["issues"])
    for issue in list(values.issues) + list(result.issues):
        sheet.append([issue.severity, issue.code, issue.message])


def run(values_json: str | Path, map_path: str | Path, workbook: str | Path,
        output: str | Path | None = None) -> tuple[Values, WriteResult]:
    """Stage 4 from files."""
    import json

    from finscan2.match.schema import Source, Values as ValuesType
    from finscan2.match.schema import Adjustment, ResolvedValue
    from finscan2.model.load import load as load_map

    payload = json.loads(Path(values_json).read_text(encoding="utf-8"))
    values = ValuesType(
        company=payload["company"], sheet=payload["sheet"],
        period_end=payload["period_end"], write_col=payload.get("write_col"),
        units=payload.get("units", "units"),
        issues=[Issue(**i) for i in payload.get("issues", [])],
    )
    for item in payload.get("values", []):
        source = item.get("source")
        units = item.get("units") or {}
        values.values.append(ResolvedValue(
            row=item["row"], label=item["label"], section=item.get("section", ""),
            value=item.get("value"), resolve=item.get("resolve", ""),
            source=Source(**source) if source else None,
            adjustments=[Adjustment(**a) for a in item.get("adjustments", [])],
            units_from=units.get("from"), units_to=units.get("to"),
            unresolved=item.get("unresolved"),
        ))

    resolved_map = load_map(map_path, workbook)
    workbook_path = Path(workbook)
    out = Path(output) if output else workbook_path.with_name(
        f"{workbook_path.stem}_updated{workbook_path.suffix}")
    return values, write_column(values, resolved_map, str(workbook_path), str(out))
