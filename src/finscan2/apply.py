"""`apply` — resolve the filing against the mapping and write the column, in one step.

Match and write used to be separate commands with `values.json` between them, and
nothing checked its age: editing the mapping and running `write` produced a workbook
from a stale resolution, silently, because `write` reads neither the filing nor the
mapping. Joining them removes the possibility. `values.json` is still produced, as an
audit artifact that nothing consumes.

The order is: bind, check the period, resolve, validate, then write — with an explicit
gate between validation and writing. Structural problems refuse before the workbook is
touched; a single row that cannot be resolved is written blank with its reason, because
one unmappable line should not withhold the other twenty-eight.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from finscan2 import validate as validate_mod
from finscan2.mapping.compile import compile_mapping, unmapped_inputs
from finscan2.mapping.schema import Mapping
from finscan2.match.resolve import expected_period_end, resolve_values
from finscan2.match.schema import Values
from finscan2.model.discover import discover_sheet
from finscan2.model.load import resolve as bind_map
from finscan2.schema import Issue, PdfDoc
from finscan2.write.column import WriteResult, write_column


@dataclass
class ApplyResult:
    company: str
    period_end: str = ""
    values: Values | None = None
    write: WriteResult | None = None
    issues: list[Issue] = field(default_factory=list)
    refused: str = ""

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def exit_code(self) -> int:
        if self.refused:
            return 1
        row_errors = [i for i in (self.values.issues if self.values else [])
                      if i.severity == "error"]
        return 2 if row_errors or self.errors else 0


def check_period(period_dates: dict[int, str], cadence_months: int,
                 write_col: int | None,
                 period_end: str | None = None) -> tuple[str, list[Issue]]:
    """The period this run writes, and every reason that slot may be wrong.

    Deliberately NOT a scan for gaps across every dated column. A real model holds
    several blocks side by side — Almarai's sheet carries annual columns for 2016-2025
    AND quarterly columns for 2018-2026 — so a date-ordered walk reports nine
    "gaps" that are simply the annual block. What matters is narrower: the target is
    the next period after the latest, and the slot being written is empty. Whether
    the particular prior columns a cumulative figure needs are present is decided
    per row by `prior_columns`, which already refuses when one is missing.
    """
    issues: list[Issue] = []
    dated: list[tuple[int, date]] = []
    for column, iso in sorted(period_dates.items()):
        try:
            dated.append((column, date.fromisoformat(iso)))
        except (TypeError, ValueError):
            continue

    if not dated:
        issues.append(Issue(code="no_dated_columns", severity="error",
                            message="The sheet has no dated period columns, so the "
                                    "period to write cannot be established."))
        return "", issues

    # An explicit --period-end is checked too, not trusted. Left unchecked it is the
    # one way to write a period the sheet has no history for, which is exactly what
    # this function exists to prevent.
    target = period_end or expected_period_end(period_dates, cadence_months) or ""

    if write_col is not None and write_col in period_dates:
        issues.append(Issue(
            code="write_column_occupied", severity="error",
            message=f"Column {write_col} already carries the date "
                    f"{period_dates[write_col]}. Writing there would overwrite a "
                    f"period that is already in the model."))

    # Is the target genuinely the next period? Compared by month, because models
    # date their columns 2026-03-30 as readily as 2026-03-31.
    if target:
        wanted = date.fromisoformat(target)
        months_back = wanted.month - cadence_months
        year = wanted.year
        while months_back <= 0:
            months_back += 12
            year -= 1
        if not any((d.year, d.month) == (year, months_back) for _, d in dated):
            latest = max(d for _, d in dated)
            issues.append(Issue(
                code="period_not_contiguous", severity="error",
                message=f"The period to write is {target}, but the sheet has no column "
                        f"for the {cadence_months} months before it "
                        f"({year}-{months_back:02d}). Its latest column is "
                        f"{latest.isoformat()}."))

    duplicates = {}
    for column, value in dated:
        duplicates.setdefault(value, []).append(column)
    for value, columns in duplicates.items():
        if len(columns) > 1 and write_col in columns:
            issues.append(Issue(
                code="duplicate_period", severity="error",
                message=f"Columns {columns} all carry {value.isoformat()}."))

    return target, issues


def run(pdf_json: str | Path, mapping_path: str | Path, workbook: str | Path,
        output: str | Path | None = None, period_end: str | None = None,
        dry_run: bool = False) -> ApplyResult:
    """One period, end to end."""
    mapping = Mapping.load(mapping_path)
    result = ApplyResult(company=mapping.company)

    # A1 — layout discovered fresh from the workbook, never from a cached copy.
    layout = discover_sheet(str(workbook), mapping.sheet)
    model, issues = compile_mapping(mapping, layout)
    result.issues.extend(issues)

    # A2 — bind, and refuse if any decision has nowhere to go.
    if result.errors:
        result.refused = ("the mapping does not fit this workbook; nothing was "
                          "written")
        return result

    resolved_map = bind_map(model, layout)
    result.issues.extend(resolved_map.issues)
    if not model.write_col:
        result.issues.append(Issue(code="no_write_column", severity="error",
                                   message="No empty column was found to write into."))
    if not model.reference_col:
        result.issues.append(Issue(
            code="no_reference_column", severity="error",
            message="No reference column was found, so formulas cannot be copied."))

    # A3 — the period.
    target, period_issues = check_period(model.period_dates, model.cadence_months,
                                         model.write_col, period_end)
    result.issues.extend(period_issues)
    result.period_end = period_end or target

    if result.errors:
        result.refused = "the workbook is not ready for this period; nothing was written"
        return result

    # A4 — resolve.
    doc = PdfDoc.from_dict(json.loads(Path(pdf_json).read_text(encoding="utf-8")))
    values = resolve_values(doc, resolved_map, str(workbook), result.period_end)
    result.values = values

    # A5 — validate. Warnings only: an identity fails whenever a component is
    # deliberately blank, which is a legitimate state here.
    values.issues.extend(validate_mod.check(values))
    if (issue := validate_mod.coverage(values)) is not None:
        values.issues.append(issue)

    skipped = unmapped_inputs(mapping, layout)
    if skipped:
        values.issues.append(Issue(
            code="rows_not_in_mapping", severity="info",
            message=f"{len(skipped)} writable row(s) of the sheet are not in the "
                    f"mapping and were left alone: "
                    f"{', '.join(repr(r.label) for r in skipped[:6])}"
                    f"{'…' if len(skipped) > 6 else ''}."))

    if dry_run:
        return result

    # A6 — write.
    workbook_path = Path(workbook)
    out = Path(output) if output else workbook_path.with_name(
        f"{workbook_path.stem}_updated{workbook_path.suffix}")
    try:
        result.write = write_column(values, resolved_map, str(workbook_path), str(out),
                                    result.period_end)
    except PermissionError:
        result.refused = (f"{out} is open in Excel. Close it and run again — nothing "
                          f"was written, and the file on disk is from an earlier run.")
        return result

    return result


def save_artifacts(result: ApplyResult, work_dir: str | Path = "_work") -> list[Path]:
    """The values and the report, named with the period so runs do not overwrite."""
    if result.values is None:
        return []
    directory = Path(work_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{result.company}_{result.period_end or 'unknown'}"
    written = [result.values.save(directory / f"{stem}_values.json")]

    report = directory / f"{stem}_report.md"
    report.write_text(_report(result), encoding="utf-8")
    written.append(report)
    return written


def _report(result: ApplyResult) -> str:
    values = result.values
    lines = [f"# {result.company} — period ending {result.period_end}", ""]
    if result.refused:
        lines += [f"**Refused:** {result.refused}", ""]
    if result.write:
        lines += [f"- output: `{result.write.output_path}`",
                  f"- column: {result.write.sheet}!{result.write.column}",
                  f"- values written: {result.write.values_written}",
                  f"- formulas copied: {result.write.formulas_copied}",
                  f"- left blank: {result.write.blanks_annotated}", ""]

    if values:
        lines += ["## Rows", "", "| row | label | value | source | adjustment |",
                  "|---|---|---|---|---|"]
        for value in values.values:
            source = value.source.caption if value.source else ""
            adjustment = "; ".join(a.kind for a in value.adjustments)
            figure = f"{value.value:,.2f}" if value.value is not None else "_blank_"
            reason = "" if value.ok else f" — {value.unresolved}"
            lines.append(f"| {value.row} | {value.label} | {figure} | {source}{reason} "
                         f"| {adjustment} |")
        lines.append("")

        every = list(values.issues) + list(result.issues) + list(
            result.write.issues if result.write else [])
        if every:
            lines += ["## Issues", ""]
            for issue in every:
                lines.append(f"- **{issue.severity}** `{issue.code}` — {issue.message}")
    return "\n".join(lines) + "\n"
