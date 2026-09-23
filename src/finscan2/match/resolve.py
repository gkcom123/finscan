"""Stage 3 — pdf.json + model.json -> values.json.

One number per mapped input row, with the column it came from and everything done
to it recorded alongside. No LLM: the filing's captions and columns are already in
pdf.json, and the map already says what each workbook row means, so matching is a
lookup. That is the whole point of the two stages before this one.

A row that cannot be resolved is left blank with a reason. Nothing is carried
forward from a prior period, defaulted to zero, or matched on a near-miss — except
where a mapping explicitly says `carry:`, for a line the filing itself never
reports at all (an FX peg the analyst maintains directly in the workbook).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from finscan2.match import basis as basis_mod
from finscan2.match.schema import Adjustment, ResolvedValue, Source, Values
from finscan2.match.select import field_aliases, find_row, select_column
from finscan2.model.learn import _is_bidirectional
from finscan2.model.load import ResolvedMap
from finscan2.schema import Issue, PdfDoc

UNIT_MULTIPLIER: dict[str, float] = {
    "units": 1.0, "thousands": 1e3, "lakhs": 1e5,
    "millions": 1e6, "crores": 1e7, "billions": 1e9,
}

#: Sections whose figures are flows, and so may be de-cumulated.
_FLOW_SECTIONS = {"income_statement", "cash_flow"}


def expected_period_end(period_dates: dict[int, str], cadence_months: int) -> str | None:
    """The period the write column is for: the latest column plus one cadence."""
    dated = []
    for iso in period_dates.values():
        try:
            dated.append(date.fromisoformat(iso))
        except (ValueError, TypeError):
            continue
    if not dated:
        return None
    last = max(dated)
    year, month = last.year, last.month + cadence_months
    while month > 12:
        year, month = year + 1, month - 12
    from calendar import monthrange
    return date(year, month, monthrange(year, month)[1]).isoformat()


def _scale(value: float, source_units: str, target_units: str) -> float:
    return value * UNIT_MULTIPLIER.get(source_units, 1.0) / UNIT_MULTIPLIER.get(target_units, 1.0)


def _read_cells(workbook_path: str, sheet: str,
                cells: set[tuple[int, int]]) -> dict[tuple[int, int], float | None]:
    """Cached values for particular cells. Formula cells written by a previous run
    have no cached value until Excel has opened and saved the file — that reads
    back as None here, and the caller refuses rather than guessing."""
    if not cells:
        return {}
    book = load_workbook(workbook_path, data_only=True)
    try:
        worksheet = book[sheet]
        out: dict[tuple[int, int], float | None] = {}
        for row, col in cells:
            value = worksheet.cell(row, col).value
            out[(row, col)] = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        return out
    finally:
        book.close()


class _SummedRow:
    """A synthetic printed row, so a sum rejoins the ordinary value path."""

    def __init__(self, caption: str, index: int, total: float):
        self.caption, self.raw = caption, caption
        self.values = [None] * index + [total]


def _statement_for(spec, doc: PdfDoc, period_end: str | None = None):
    """The statement a row reads from: what the map says, else its section.

    "note:10" addresses a table in the notes. A note block yields one statement per
    period it reports, so the period end picks between them — "Total Assets" is
    printed under 30 June 2026 and again under 31 December 2025, identical caption,
    different figure.
    """
    wanted = spec.statement or (spec.key.section if spec.key.section != "other" else None)
    if not wanted:
        return None, "the map does not say which statement this row comes from"

    if wanted.startswith("note:"):
        number = wanted.split(":", 1)[1].strip()
        if not number.isdigit():
            return None, f"'{wanted}' is not a note number"
        statement = doc.note(int(number), period_end)
        if statement is None and period_end:
            # Named without a period: only safe when the note reports just one.
            matches = [s for s in doc.statements if s.note == int(number)]
            if len(matches) == 1:
                statement = matches[0]
            elif matches:
                ends = ", ".join(sorted(s.heading for s in matches))
                return None, (f"note {number} reports {len(matches)} periods ({ends}) "
                              f"and none ends {period_end}")
        if statement is None:
            return None, f"the filing has no note {number}"
        return statement, ""

    statement = doc.statement(wanted)
    if statement is None:
        kinds = ", ".join(sorted({s.kind for s in doc.statements})) or "none"
        return None, f"the filing has no {wanted} (found: {kinds})"
    return statement, ""


#: Separator between the terms of a `sum:`. Not a comma: filing captions contain
#: commas ("Other Expenses, net"), and splitting on one would shred them.
_TERM_SEPARATOR = "|"


def _sum_terms(instruction: str) -> list[tuple[float, str]]:
    """Parse "sum:pdf:Zakat|-field:income_tax" into [(+1, "pdf:Zakat"), (-1, ...)]."""
    body = instruction.split(":", 1)[1]
    terms: list[tuple[float, str]] = []
    for raw in body.split(_TERM_SEPARATOR):
        text = raw.strip()
        if not text:
            continue
        sign = 1.0
        while text[:1] in "+-":
            if text[0] == "-":
                sign = -sign
            text = text[1:].strip()
        terms.append((sign, text))
    return terms


class _Term:
    """One resolved term of a sum: enough to explain the arithmetic afterwards."""

    def __init__(self, sign: float, caption: str, printed: float, assumed_zero: bool = False):
        self.sign, self.caption, self.printed = sign, caption, printed
        self.assumed_zero = assumed_zero

    def signed(self) -> float:
        return self.sign * self.printed

    def describe(self) -> str:
        lead = "-" if self.sign < 0 else "+"
        note = " (no figure printed, treated as 0)" if self.assumed_zero else ""
        return f"{lead} {self.caption} {self.printed:,.2f}{note}"


def _lookup_sum(spec, statement, column):
    """Every term of a sum, or nothing.

    A term whose caption cannot be found at all fails the whole sum: that is a
    plausible number, smaller than the truth by exactly one missing component,
    and nothing about it looks wrong. But a term whose caption IS found, printing
    a dash in the wanted column, has already told us its value — the filing is
    saying "nothing happened here this period" — so it contributes 0 rather than
    failing the row.
    """
    terms: list[_Term] = []
    for sign, instruction in _sum_terms(spec.resolve or ""):
        if instruction.startswith("const:"):
            try:
                terms.append(_Term(sign, f"const {instruction.split(':', 1)[1]}",
                                   float(instruction.split(":", 1)[1])))
            except ValueError:
                return None, f"'{instruction}' in the sum is not a number"
            continue

        class _Part:
            resolve = instruction
            pdf_caption = None
            key = spec.key

        row, why = _lookup(_Part, statement)
        if row is None:
            return None, f"the sum term '{instruction}' did not resolve: {why}"
        if column.index >= len(row.values):
            return None, (f"the sum term '{instruction}' matched '{row.caption}', which "
                          f"has only {len(row.values)} value(s) — the wanted column is "
                          f"number {column.index + 1}")
        if row.values[column.index] is None:
            terms.append(_Term(sign, row.caption, 0.0, assumed_zero=True))
            continue
        terms.append(_Term(sign, row.caption, row.values[column.index]))
    if not terms:
        return None, "the sum has no terms"
    return terms, ""


def _lookup(spec, statement):
    """Find the printed row for a map spec's `resolve` instruction.

    A recorded `pdf_caption` — the wording the filing used when this row last
    matched — is tried first, so a match that once needed direction-reading or
    word inclusion becomes an exact match from then on. It is a hint, never a
    requirement: filings reword their captions, so a miss falls through to the
    ordinary tiers rather than failing the row.
    """
    instruction = spec.resolve or ""
    recorded = [spec.pdf_caption] if spec.pdf_caption else []

    if instruction.startswith("field:"):
        field_id = instruction.split(":", 1)[1]
        aliases = recorded + field_aliases(field_id)
        return find_row(statement, field_id.replace("_", " "), aliases)
    if instruction.startswith("absent:"):
        # Only used for the staleness check: search by the workbook's own caption.
        return find_row(statement, spec.key.label)
    if instruction.startswith("pdf:"):
        wanted = instruction.split(":", 1)[1]
        if recorded:
            row, why = find_row(statement, recorded[0])
            if row is not None:
                return row, "caption matched the wording recorded by `bind`"
        return find_row(statement, wanted)
    return None, f"unsupported resolve instruction '{instruction}'"


def resolve_values(doc: PdfDoc, resolved_map: ResolvedMap, workbook_path: str,
                   period_end: str | None = None) -> Values:
    """Resolve every bound input row of the map against the filing."""
    model = resolved_map.model
    target = period_end or expected_period_end(model.period_dates, model.cadence_months)

    out = Values(company=model.company, sheet=model.sheet, period_end=target or "",
                 write_col=model.write_col, units=model.units)

    if not target:
        out.issues.append(Issue(
            code="period_end_unknown", severity="error",
            message="The model has no dated period columns, so the period being "
                    "written cannot be established. Pass --period-end."))
        return out

    inputs = [b for b in resolved_map.bound if b.spec.kind == "input"]

    # Work out every prior cell needed, then read them all in one pass.
    needed: set[tuple[int, int]] = set()
    plans: dict[int, tuple] = {}
    for bound in inputs:
        spec = bound.spec
        if (spec.resolve or "").startswith("const:"):
            plans[bound.row] = (None, None, None, None)
            continue
        if (spec.resolve or "").startswith("carry:"):
            plans[bound.row] = (None, None, None, None)
            if model.reference_col:
                needed.add((bound.row, model.reference_col))
            continue
        if (spec.resolve or "").startswith("absent:"):
            # Planned anyway, so the declaration can be checked against the filing.
            statement, why = _statement_for(spec, doc)
            plans[bound.row] = (statement, None, None, None)
            continue
        point_in_time = (spec.basis == "point_in_time"
                         or spec.key.section not in _FLOW_SECTIONS)
        statement, why = _statement_for(spec, doc, target)
        if statement is None:
            plans[bound.row] = (None, None, None, why)
            continue
        column, reason = select_column(statement, target, model.cadence_months, point_in_time)
        if column is None:
            plans[bound.row] = (statement, None, None, reason)
            continue

        columns_to_subtract: list[int] = []
        if not point_in_time and column.months and column.months > model.cadence_months:
            count = basis_mod.periods_to_subtract(column.months, model.cadence_months)
            if count is None:
                plans[bound.row] = (statement, column, None,
                                    f"a {column.months}-month figure is not a whole "
                                    f"number of {model.cadence_months}-month periods")
                continue
            columns_to_subtract, problem = basis_mod.prior_columns(
                model.period_dates, target, model.cadence_months, count)
            if problem:
                plans[bound.row] = (statement, column, None, problem)
                continue
            needed.update((bound.row, c) for c in columns_to_subtract)

        plans[bound.row] = (statement, column, columns_to_subtract, None)

    cells = _read_cells(workbook_path, model.sheet, needed)

    for bound in inputs:
        spec = bound.spec
        statement, column, columns_to_subtract, problem = plans[bound.row]
        value = ResolvedValue(row=bound.row, label=spec.key.label,
                              section=spec.key.section, value=None,
                              resolve=spec.resolve or "")

        if problem:
            value.unresolved = problem
            out.values.append(value)
            out.issues.append(Issue(code="unresolved_row", severity="error",
                                    message=f"row {bound.row} '{spec.key.label}': {problem}"))
            continue


        if (spec.resolve or "").startswith("carry:"):
            reason = spec.resolve.split(":", 1)[1].strip()
            if not model.reference_col:
                value.unresolved = "the map has no reference column to carry forward from"
            else:
                prior = cells.get((bound.row, model.reference_col))
                if prior is None:
                    value.unresolved = "the reference column has no numeric value to carry forward"
                else:
                    value.value = prior
                    ref_letter = get_column_letter(model.reference_col)
                    value.adjustments.append(Adjustment(
                        kind="carried_forward",
                        detail=f"carried forward unchanged from {ref_letter}{bound.row} "
                               f"({prior:,.4f}) — not read from the filing"
                               + (f": {reason}" if reason else "")))
            if value.value is None:
                out.issues.append(Issue(
                    code="unresolved_row", severity="error",
                    message=f"row {bound.row} '{spec.key.label}': {value.unresolved}"))
            else:
                out.issues.append(Issue(
                    code="carried_forward", severity="warning",
                    message=f"row {bound.row} '{spec.key.label}' was carried forward "
                            f"from the reference column, not from the filing"
                            + (f" ({reason})" if reason else "") + "."))
            out.values.append(value)
            continue

        # A line the analyst has determined this filing does not report. Left blank
        # and reported EVERY quarter, which is the point: a blank that asks again is
        # safer than a zero that stops asking.
        if (spec.resolve or "").startswith("absent:"):
            reason = spec.resolve.split(":", 1)[1].strip()
            value.unresolved = (f"declared absent from the filing"
                                + (f": {reason}" if reason else ""))
            out.issues.append(Issue(
                code="absent_by_design", severity="warning",
                message=f"row {bound.row} '{spec.key.label}' left blank — the map says "
                        f"this filing has no such line"
                        + (f" ({reason})" if reason else "") + "."))

            # The declaration can go stale. If the filing now prints a line that
            # matches, say so rather than quietly honouring a decision made about a
            # different filing.
            if statement is not None:
                found, _ = _lookup(spec, statement)
                if found is not None:
                    out.issues.append(Issue(
                        code="absent_declaration_stale", severity="error",
                        message=f"row {bound.row} '{spec.key.label}' is declared absent, "
                                f"but this filing's {statement.kind} prints "
                                f"'{found.caption}'. Re-check the declaration."))
            out.values.append(value)
            continue

        # A constant the analyst put in the map: this line is not in the filing, and
        # a number is wanted rather than a blank. Never looked up, never scaled,
        # never de-cumulated — and it says so in the cell comment, because a figure
        # that did not come from the filing must not look like one that did.
        if (spec.resolve or "").startswith("const:"):
            text = spec.resolve.split(":", 1)[1].strip()
            try:
                value.value = float(text)
            except ValueError:
                value.unresolved = f"'{text}' in the map is not a number"
                out.issues.append(Issue(
                    code="bad_constant", severity="error",
                    message=f"row {bound.row} '{spec.key.label}': {value.unresolved}"))
                out.values.append(value)
                continue
            value.adjustments.append(Adjustment(
                kind="constant",
                detail=f"constant {value.value:,.2f} from the map — this line is not "
                       f"read from the filing"))
            out.issues.append(Issue(
                code="constant_written", severity="warning",
                message=f"row {bound.row} '{spec.key.label}' was written as the constant "
                        f"{value.value:,.2f} from the map, not from the filing. Re-check "
                        f"it when the filing changes."))
            out.values.append(value)
            continue

        # A sum joins several printed lines into one model row. Everything after
        # this point — scaling, de-cumulation, sign — is shared with ordinary rows,
        # because a sum of quarter-to-date figures needs de-cumulating exactly as a
        # single one does.
        if (spec.resolve or "").startswith("sum:"):
            terms, why = _lookup_sum(spec, statement, column)
            if terms is None:
                value.unresolved = why
                out.values.append(value)
                out.issues.append(Issue(
                    code="unresolved_row", severity="error",
                    message=f"row {bound.row} '{spec.key.label}': {why}"))
                continue

            total = sum(term.signed() for term in terms)
            value.source = Source(
                statement=statement.kind, page=statement.page,
                caption=" ".join(term.describe() for term in terms).lstrip("+ "),
                column_header=column.header, column_index=column.index,
                months=column.months, end=column.end, printed=total)
            value.adjustments.append(Adjustment(
                kind="sum",
                detail="summed: " + " ".join(t.describe() for t in terms).lstrip("+ ")
                       + f" = {total:,.2f}"))
            printed_row = _SummedRow(value.source.caption, column.index, total)
        else:
            printed_row, why = _lookup(spec, statement)
        if printed_row is None:
            value.unresolved = why
            out.values.append(value)
            out.issues.append(Issue(code="unresolved_row", severity="error",
                                    message=f"row {bound.row} '{spec.key.label}': {why}"))
            continue

        if column.index >= len(printed_row.values):
            why = (f"'{printed_row.caption}' has {len(printed_row.values)} value(s) but "
                   f"the wanted column is number {column.index + 1}")
            value.unresolved = why
            out.values.append(value)
            out.issues.append(Issue(code="unresolved_row", severity="error",
                                    message=f"row {bound.row} '{spec.key.label}': {why}"))
            continue

        printed = printed_row.values[column.index]
        # A dash IS the filing's own answer — "nothing happened here" — not a gap
        # in what could be read, so it is trusted as 0 rather than left unresolved.
        printed_as_dash = printed is None
        if printed_as_dash:
            printed = 0.0
        if value.source is None:
            value.source = Source(statement=statement.kind, page=statement.page,
                                  caption=printed_row.caption, column_header=column.header,
                                  column_index=column.index, months=column.months,
                                  end=column.end, printed=printed)
        if printed_as_dash:
            value.adjustments.append(Adjustment(
                kind="assumed_zero",
                detail=f"'{printed_row.caption}' printed a dash in the wanted column "
                       f"({column.header}) — treated as 0"))

        figure = printed
        if columns_to_subtract:
            letters = {c: get_column_letter(c) for c in columns_to_subtract}
            priors = {c: cells.get((bound.row, c)) for c in columns_to_subtract}
            # The workbook's own figures are in the sheet's units; the filing's are
            # in the filing's, so both are brought to the sheet's scale first.
            scaled_printed = _scale(printed, doc.units or model.units, model.units)
            figure, adjustment, issue = basis_mod.decumulate(
                scaled_printed, priors, columns_to_subtract, bound.row, letters)
            if issue:
                out.issues.append(issue)
            if figure is None:
                value.unresolved = "prior period columns were not usable"
                out.values.append(value)
                continue
            value.adjustments.append(adjustment)
            value.units_from = value.units_to = model.units
        else:
            figure = _scale(printed, doc.units or model.units, model.units)
            value.units_from, value.units_to = (doc.units or model.units), model.units

        # A sign convention is never forced onto a line the FILING presents as
        # two-directional. The map's convention was inferred from one prior column;
        # the caption in front of us is evidence about this quarter. Almarai's
        # "Impairment (Loss) / Reversal on Financial Assets" is negative every
        # quarter until the reversal quarter, when forcing it would write a real
        # gain as a loss and attach a comment explaining the flip.
        bidirectional = _is_bidirectional(printed_row.caption) or _is_bidirectional(spec.key.label)
        if bidirectional and spec.sign:
            value.adjustments.append(Adjustment(
                kind="sign", detail=f"sign kept as the filing printed it "
                                    f"({figure:,.2f}); '{printed_row.caption}' names both "
                                    f"directions, so the row's '{spec.sign}' convention "
                                    f"was not applied"))
        elif spec.sign == "negative" and figure > 0:
            value.adjustments.append(Adjustment(
                kind="sign", detail=f"sign flipped to match the row's convention "
                                    f"({figure:,.2f} -> {-figure:,.2f})"))
            figure = -figure
        elif spec.sign == "positive" and figure < 0:
            value.adjustments.append(Adjustment(
                kind="sign", detail=f"sign flipped to match the row's convention "
                                    f"({figure:,.2f} -> {abs(figure):,.2f})"))
            figure = abs(figure)

        value.value = figure
        out.values.append(value)

    if out.blank:
        out.issues.append(Issue(
            code="rows_left_blank", severity="warning",
            message=f"{len(out.blank)} of {len(inputs)} input row(s) were left blank."))
    return out


def run(pdf_json: str | Path, map_path: str | Path, workbook: str | Path,
        period_end: str | None = None) -> Values:
    """Stage 3 from files: pdf.json + model.json + workbook -> Values."""
    import json

    from finscan2.model.load import load as load_map

    doc = PdfDoc.from_dict(json.loads(Path(pdf_json).read_text(encoding="utf-8")))
    resolved_map = load_map(map_path, workbook)
    return resolve_values(doc, resolved_map, str(workbook), period_end)
