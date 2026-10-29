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

import re

from datetime import date
from pathlib import Path

from openpyxl.utils import get_column_letter

from finscan.match import basis as basis_mod
from finscan.match.schema import Adjustment, ResolvedValue, Source, Values
from finscan.match.select import field_aliases, find_row, select_column
from finscan.model.learn import _is_bidirectional
from finscan.model.load import ResolvedMap
from finscan.model.schema import normalize_label
from finscan.model.discover import sheet_named
from finscan.model.workbook import open_workbook
from finscan.schema import Issue, PdfDoc

UNIT_MULTIPLIER: dict[str, float] = {
    "units": 1.0, "thousands": 1e3, "lakhs": 1e5,
    "millions": 1e6, "crores": 1e7, "billions": 1e9,
}

#: Sections whose figures are flows, and so may be de-cumulated.
_FLOW_SECTIONS = {"income_statement", "cash_flow"}


def _is_point_in_time(spec) -> bool:
    """A balance is never de-cumulated. A row's section decides, unless the mapping says
    `basis: quarter` — a flow that sits under a heading the sheet reads as 'other'
    (Quiport's cash items below its 'Net Debt' block)."""
    if spec.basis == "point_in_time":
        return True
    if spec.basis == "quarter":
        return False
    return spec.key.section not in _FLOW_SECTIONS


#: A unit printed on the row itself, as its caption's last words: Airtel's IR Pack
#: summary prints "Market Capitalization  Rs Bn  11,559" beside rows in "Rs Mn".
_ROW_UNIT = re.compile(r"\b(?:rs\.?|inr|₹|us\$|usd)\s*(mn|million|bn|billion|cr|crore)s?\s*$",
                       re.IGNORECASE)
_ROW_UNIT_SCALE = {"mn": "millions", "million": "millions", "bn": "billions",
                   "billion": "billions", "cr": "crores", "crore": "crores"}


def _row_units(row) -> str | None:
    """The scale a row states for itself, which outranks its table's."""
    m = _ROW_UNIT.search(getattr(row, "caption", "") or "")
    return _ROW_UNIT_SCALE[m.group(1).lower()] if m else None


def _carry_source(model) -> int | None:
    """The previous period's column: the one right before the column written.

    Usually that is the reference column too, but not when the previous period
    is derived — Airtel's 4Q column is "=FY − 9M", so its reference is the 3Q
    column, while the rate to carry forward is 4Q's.
    """
    before = (model.write_col or 0) - 1
    if before in (model.period_dates or {}):
        return before
    return model.reference_col


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
    book = open_workbook(workbook_path, data_only=True)
    try:
        worksheet = sheet_named(book, sheet)
        out: dict[tuple[int, int], float | None] = {}
        for row, col in cells:
            value = worksheet.cell(row, col).value
            out[(row, col)] = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    finally:
        book.close()

    missing = [cell for cell, value in out.items() if value is None]
    if missing:
        # A typed expression ("=(-15482581-255436)/1000000") references no cell, so
        # it has exactly one value even without Excel's cache. Anything that
        # references a cell is still refused, as above.
        formulas = open_workbook(workbook_path, data_only=False)
        try:
            worksheet = sheet_named(formulas, sheet)
            for row, col in missing:
                out[(row, col)] = literal_value(worksheet.cell(row, col).value)
        finally:
            formulas.close()
    return out


def literal_value(formula) -> float | None:
    """The value of a formula made only of numbers and + - * / ^ ( ), else None.

    Parsed, never `eval`-ed: any name, call or cell reference returns None.
    """
    import ast
    import operator

    if not (isinstance(formula, str) and formula.startswith("=")):
        return None
    ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.Pow: operator.pow,
           ast.USub: operator.neg, ast.UAdd: operator.pos}

    def walk(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            return ops[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in ops:
            return ops[type(node.op)](walk(node.operand))
        raise ValueError("not a literal expression")

    try:
        return walk(ast.parse(formula[1:].replace("^", "**"), mode="eval").body)
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError, TypeError):
        return None


class _SummedRow:
    """A synthetic printed row, so a sum rejoins the ordinary value path."""

    def __init__(self, caption: str, index: int, total: float):
        self.caption, self.raw = caption, caption
        self.values = [None] * index + [total]


#: Separator between alternative titles of a `title:` statement.
_TITLE_OR = " || "


def _wanted_captions(spec) -> list[tuple[str, int | None]]:
    """The captions a row reads from its own statement, with any `#n` occurrence.

    Terms that name another statement (`@ title:...`), optional terms, constants
    and fields are not required of the row's statement.
    """
    instruction = getattr(spec, "resolve", "") or ""
    if instruction.startswith("pdf:"):
        terms = [(1.0, instruction)]
    elif instruction.startswith("sum:"):
        terms = _sum_terms(instruction)
    else:
        return []
    captions = []
    for _, term in terms:
        if not term.startswith("pdf:") or _TERM_STATEMENT in term or term.endswith(_OPTIONAL):
            continue
        wanted = term.split(":", 1)[1]
        occurrence = _OCCURRENCE.search(wanted)
        caption = _OCCURRENCE.sub("", wanted).strip()
        if caption and _BLOCK not in caption:
            captions.append((caption, int(occurrence.group(1)) if occurrence else None))
    return captions


def _prints(statement, caption: str, occurrence: int | None) -> bool:
    """True when the statement can supply this caption (the nth exact line, if asked)."""
    if _CAPTION_OR in caption:
        return _first_printed(statement, [a.strip() for a in caption.split(_CAPTION_OR)])[0] is not None
    if occurrence is None:
        return find_row(statement, caption)[0] is not None
    target = normalize_label(caption)
    return sum(1 for r in statement.rows if normalize_label(r.caption) == target) >= occurrence


def _holding_captions(candidates: list, captions: list[tuple[str, int | None]]):
    """The first statement carrying every wanted caption, else the first readable one.

    Several tables may share a title ("Consolidated" over a note's table and over
    the loans table). The first is kept whenever it holds the lines — which is every
    mapping that worked before this — and a later one is used only when the first
    could not have supplied them.
    """
    if len(candidates) > 1 and captions:
        for candidate in candidates:
            if all(_prints(candidate, caption, occurrence) for caption, occurrence in captions):
                return candidate
    return candidates[0]


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

    if wanted.startswith("title:"):
        text = wanted.split(":", 1)[1]
        # "title:A || B": the alternatives are tried in order, so a row can survive a
        # filer renumbering or rewording the title. Among the statements they name,
        # the first holding the row's captions is read (see _holding_captions).
        alternatives = [part.strip() for part in text.split(_TITLE_OR) if part.strip()]
        candidates: list = []
        for alternative in alternatives:
            candidates.extend(s for s in doc.titled_all(alternative)
                              if not any(s is seen for seen in candidates))
        if not candidates:
            return None, f"no statement's title contains '{text}'"
        return _holding_captions(candidates, _wanted_captions(spec)), ""

    statement = doc.statement(wanted)
    if statement is None:
        kinds = ", ".join(sorted({s.kind for s in doc.statements})) or "none"
        return None, f"the filing has no {wanted} (found: {kinds})"
    return statement, ""


#: Separator between the terms of a `sum:`. Not a comma: filing captions contain
#: commas ("Other Expenses, net"), and splitting on one would shred them. A doubled
#: "||" is a caption alternative inside one term, never a separator.
_TERM_SEPARATOR = "|"


def _sum_terms(instruction: str) -> list[tuple[float, str]]:
    """Parse "sum:pdf:Zakat|-field:income_tax" into [(+1, "pdf:Zakat"), (-1, ...)]."""
    body = instruction.split(":", 1)[1]
    terms: list[tuple[float, str]] = []
    for raw in re.split(r"(?<!\|)\|(?!\|)", body):
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


#: Marks a sum term optional: "pdf:Inventories?". See _lookup_sum.
_OPTIONAL = "?"


def _not_printed(why: str) -> bool:
    """True only when the caption is absent from the statement altogether.

    Deliberately narrow. "matches 2 rows" or "no line after the block heading" are
    NOT absence — they are a caption that is there but cannot be pinned down, and
    turning those into 0 would hide exactly the ambiguity the matcher refused.
    """
    return why.startswith("no row captioned like") or " printed 0 time(s)" in why


def _lookup_optional(instruction: str, statement):
    """An optional term is matched on its exact caption only.

    A line that may legitimately be missing must never fall back to containment
    or word matching: the quarter it is missing is exactly when a near match is
    wrong. CCU's report prints each page footer "F-8" as a row captioned "F", and
    containment found that "F" inside an absent "Loan from related entities", so
    the absent line read as -8. Exact on the normalized caption; "#n" and block
    syntax keep their own rules. Printed more than once without "#n" is refused.
    """
    wanted = instruction.split(":", 1)[1] if ":" in instruction else instruction
    if _BLOCK in wanted:
        anchor, caption = (part.strip() for part in wanted.split(_BLOCK, 1))
        row, why = _in_block(statement, anchor, caption)
        # Missing from the block AND from the whole statement is plain absence (Vesta
        # prints "Costs of debt issuance" in its financing section only in quarters
        # that raise debt). Printed elsewhere in the statement is not: that is the
        # line in the wrong block, and stays an error.
        if (row is None and why.startswith("no '")
                and not any(_plain(r.caption) == _plain(caption) for r in statement.rows)):
            return None, f"no row captioned like '{caption}' in the statement"
        return row, why
    if m := _OCCURRENCE.search(wanted):
        return _nth_exact(statement, wanted[:m.start()], int(m.group(1)))
    target = normalize_label(wanted)
    hits = [r for r in statement.rows if normalize_label(r.caption) == target]
    if not hits:
        # Not printed exactly. Before reading that as "not printed at all", ask the
        # ordinary matcher whether a real line comes close — a renamed caption must
        # stop the row, not quietly become 0. Page furniture ("F" from "F-8") is not
        # a line, so it is left out of this check.
        from dataclasses import replace

        lines = [r for r in statement.rows if re.search(r"[^\W\d_]{3,}", r.caption or "")]
        near, why = find_row(replace(statement, rows=lines), wanted)
        if near is not None:
            return None, (f"'{wanted.strip()}' is not printed exactly, but "
                          f"'{near.caption}' is close — confirm the caption and update "
                          f"the mapping")
        if not why.startswith("no row captioned like"):
            return None, f"'{wanted.strip()}' is not printed exactly, and {why}"
        return None, f"no row captioned like '{wanted.strip()}'"
    if len(hits) > 1:
        return None, (f"caption '{wanted.strip()}' is printed {len(hits)} times; "
                      f"name the one wanted with '#n'")
    return hits[0], "exact caption match"


class _Term:
    """One resolved term of a sum: enough to explain the arithmetic afterwards."""

    def __init__(self, sign: float, caption: str, printed: float, assumed_zero: bool = False,
                 not_printed: bool = False):
        self.sign, self.caption, self.printed = sign, caption, printed
        self.assumed_zero = assumed_zero
        #: An optional term whose caption the filing does not print at all.
        self.not_printed = not_printed

    def signed(self) -> float:
        return self.sign * self.printed

    def describe(self) -> str:
        lead = "-" if self.sign < 0 else "+"
        if self.not_printed:
            note = " (optional, not printed in this filing, treated as 0)"
        elif self.assumed_zero:
            note = " (no figure printed, treated as 0)"
        else:
            note = ""
        return f"{lead} {self.caption} {self.printed:,.2f}{note}"


#: Separates a sum term's caption from the statement that term alone is read in:
#: "-Gasto por Depreciación @ title:23. PÉRDIDAS POR DETERIORO". A figure the
#: filing splits over two statements (D&A in the income statement, the impairment
#: that belongs with it in a note) is one row in the model.
_TERM_STATEMENT = " @ "

#: Separates a sum term's caption from the header of the column that term alone is
#: read in: "Total banks ## Domestic currency Until 2 years". Grupo Bimbo's debt
#: annex prints one TOTAL row across twelve currency/maturity columns, and the
#: model's local-currency debt is the six domestic columns added together.
_TERM_COLUMN = " ## "


def _lookup_sum(spec, statement, column, scope=None):
    """Every term of a sum, or nothing.

    A term whose caption cannot be found at all fails the whole sum: that is a
    plausible number, smaller than the truth by exactly one missing component,
    and nothing about it looks wrong. But a term whose caption IS found, printing
    a dash in the wanted column, has already told us its value — the filing is
    saying "nothing happened here this period" — so it contributes 0 rather than
    failing the row.

    The exception is a term the map marks optional with a trailing "?": a line the
    filing prints only in quarters where it has something to report (Airtel's cash
    flow drops "Sale of subsidiaries" entirely when nil). Such a term contributes 0
    when its caption is absent from the statement — and only then. A caption that
    is present but ambiguous still fails, and every optional zero is reported.

    A term may name its own statement (see `_TERM_STATEMENT`). Its column must cover
    the same span as the row's, and is brought to the row's units, so a quarter is
    never added to a year to date.
    """
    terms: list[_Term] = []
    for sign, instruction in _sum_terms(spec.resolve or ""):
        instruction, _, override = instruction.partition(_TERM_STATEMENT)
        instruction, override = instruction.strip(), override.strip()
        instruction, _, term_header = instruction.partition(_TERM_COLUMN)
        instruction, term_header = instruction.strip(), term_header.strip()
        optional = instruction.endswith(_OPTIONAL)
        if optional:
            instruction = instruction[:-len(_OPTIONAL)].rstrip()
        if instruction.startswith("const:"):
            try:
                terms.append(_Term(sign, f"const {instruction.split(':', 1)[1]}",
                                   float(instruction.split(":", 1)[1])))
            except ValueError:
                return None, f"'{instruction}' in the sum is not a number"
            continue

        term_statement, term_column, factor = statement, column, 1.0
        if override:
            if scope is None:
                return None, f"the sum term '{instruction}' names a statement, which this lookup cannot resolve"

            class _Where:
                statement = override
                key = spec.key
                # So that, among tables sharing the title, the one printing this term is read.
                resolve = f"pdf:{instruction}"

            term_statement, why = _statement_for(_Where, scope["doc"], scope["target"])
            if term_statement is None:
                return None, f"the sum term '{instruction}' is read in '{override}': {why}"
            term_column, why = select_column(term_statement, scope["target"], scope["cadence"],
                                             scope["point_in_time"])
            if term_column is None:
                return None, f"the sum term '{instruction}' in '{override}': {why}"
            if (term_column.months or None) != (column.months or None):
                return None, (f"the sum term '{instruction}' in '{override}' covers "
                              f"{term_column.months or 'a point in'} month(s), but the row's "
                              f"column covers {column.months or 'a point in'} — they cannot be added")
            row_units = statement.units or scope["doc"].units or scope["units"]
            term_units = term_statement.units or scope["doc"].units or scope["units"]
            factor = (UNIT_MULTIPLIER.get(term_units, 1.0) / UNIT_MULTIPLIER.get(row_units, 1.0))

        if term_header:
            if scope is None:
                return None, f"the sum term '{instruction}' names a column, which this lookup cannot resolve"
            term_column, why = select_column(term_statement, scope["target"], scope["cadence"],
                                             scope["point_in_time"], header_contains=term_header)
            if term_column is None:
                return None, f"the sum term '{instruction}' column '{term_header}': {why}"

        class _Part:
            resolve = instruction
            pdf_caption = None
            key = spec.key

        if optional:
            row, why = _lookup_optional(instruction, term_statement)
        else:
            row, why = _lookup(_Part, term_statement)
        if row is None:
            if optional and _not_printed(why):
                caption = instruction.split(":", 1)[1].strip()
                terms.append(_Term(sign, caption, 0.0, assumed_zero=True, not_printed=True))
                continue
            return None, f"the sum term '{instruction}' did not resolve: {why}"
        if term_column.index >= len(row.values):
            return None, (f"the sum term '{instruction}' matched '{row.caption}', which "
                          f"has only {len(row.values)} value(s) — the wanted column is "
                          f"number {term_column.index + 1}")
        if term_column.index in getattr(row, "unverified", []):
            return None, (f"the sum term '{instruction}' ('{row.caption}') was read by "
                          f"the vision model but not confirmed (see pdf/verify.py)")
        if row.values[term_column.index] is None:
            terms.append(_Term(sign, row.caption, 0.0, assumed_zero=True))
            continue
        terms.append(_Term(sign, row.caption, row.values[term_column.index] * factor))
    if not terms:
        return None, "the sum has no terms"
    return terms, ""


#: "Interest paid #2": the Nth row printing exactly this caption. Gruma's cash
#: flow prints "Interest paid" under operating AND financing activities; the
#: model wants the financing one, which no caption alone can pick out.
_OCCURRENCE = re.compile(r"\s*#(\d+)\s*$")


#: "GIMSA > EBITDA": a line inside a block of the table. Gruma's "Financial
#: Highlights by Subsidiary" repeats Net Sales / Gross Profit / EBITDA under each
#: subsidiary, and the subsidiary is named once, on the block's first row.
_BLOCK = " > "

#: "Income taxes || Income tax (expense) benefit": the same line under the wordings
#: a filer has used in different quarters. Each alternative must match EXACTLY.
_CAPTION_OR = " || "


def _first_printed(statement, alternatives: list[str]):
    """The row printed under the first of these exact captions that appears.

    Exact only: an alternative that is merely close would be the guess the mapping
    exists to avoid. A caption printed twice under one wording is ambiguous and is
    refused, and none being printed reads as absence ("no row captioned like").
    """
    for alternative in alternatives:
        target = normalize_label(alternative)
        hits = [r for r in statement.rows if normalize_label(r.caption) == target]
        if len(hits) == 1:
            return hits[0], f"exact caption match on '{alternative}'"
        if len(hits) > 1:
            return None, (f"caption '{alternative}' is printed {len(hits)} times; "
                          f"add #n to say which")
    return None, "no row captioned like any of: " + ", ".join(
        repr(a) for a in alternatives)


def _plain(text: str) -> str:
    """normalize_label without accents: a heading typed "GRUMA CENTROAMERICA"
    must find the printed "GRUMA CENTROAMÉRICA"."""
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", text or "")
    return normalize_label("".join(c for c in decomposed if not unicodedata.combining(c)))


def _in_block(statement, anchor: str, caption: str):
    """The first row at or after the block's heading whose caption is, or ends
    with, `caption`. The heading must name exactly one row, and the caption must
    match as whole words, so neither can drift onto a neighbouring block."""
    key, target = _plain(anchor), _plain(caption)
    rows = [_plain(r.caption) for r in statement.rows]
    # The heading may be the row's own caption ("GRUMA USA Sales Volume") or a
    # line printed above it ("OTHER SUBSIDIARIES &" over "Sales Volume").
    named = [_plain(f"{getattr(r, 'heading', '')} {r.caption}") for r in statement.rows]
    heads = [i for i, text in enumerate(named) if f" {key} " in f" {text} "]
    if len(heads) != 1:
        return None, (f"block heading '{anchor}' names {len(heads)} rows; it must "
                      f"name exactly one")
    for i in range(heads[0], len(rows)):
        if rows[i] == target or rows[i].endswith(f" {target}"):
            return statement.rows[i], f"'{caption}' in the '{anchor}' block"
    return None, f"no '{caption}' line after the '{anchor}' block heading"


def _nth_exact(statement, caption: str, n: int):
    """The Nth row whose caption matches exactly — never a looser tier, because
    counting fuzzy matches would make the number mean something different each
    time the filing rewords a neighbouring line."""
    target = normalize_label(caption)
    hits = [r for r in statement.rows if normalize_label(r.caption) == target]
    if len(hits) < n:
        return None, (f"caption '{caption}' is printed {len(hits)} time(s); "
                      f"occurrence #{n} does not exist")
    return hits[n - 1], f"occurrence #{n} of an exact caption match"


def _is_part_of_label(caption: str, label: str) -> bool:
    """True when the printed caption is a strictly shorter piece of the label."""
    printed, wanted = normalize_label(caption), normalize_label(label)
    return bool(printed) and printed != wanted and f" {printed} " in f" {wanted} "


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
        if _CAPTION_OR in wanted:
            return _first_printed(statement, [a.strip() for a in wanted.split(_CAPTION_OR)])
        if _BLOCK in wanted:
            anchor, caption = (part.strip() for part in wanted.split(_BLOCK, 1))
            return _in_block(statement, anchor, caption)
        if m := _OCCURRENCE.search(wanted):
            return _nth_exact(statement, wanted[:m.start()], int(m.group(1)))
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
            if source := _carry_source(model):
                needed.add((bound.row, source))
            continue
        if (spec.resolve or "").startswith("absent:"):
            # Planned anyway, so the declaration can be checked against the filing.
            statement, why = _statement_for(spec, doc)
            plans[bound.row] = (statement, None, None, None)
            continue
        point_in_time = _is_point_in_time(spec)
        statement, why = _statement_for(spec, doc, target)
        if statement is None:
            plans[bound.row] = (None, None, None, why)
            continue
        column, reason = select_column(statement, target, model.cadence_months, point_in_time,
                                       getattr(spec, "column", None))
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
            source = _carry_source(model)
            if not source:
                value.unresolved = "the map has no previous period column to carry forward from"
            else:
                prior = cells.get((bound.row, source))
                if prior is None:
                    value.unresolved = "the previous period has no numeric value to carry forward"
                else:
                    value.value = prior
                    ref_letter = get_column_letter(source)
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
                if found is not None and not _is_part_of_label(found.caption, spec.key.label):
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
            terms, why = _lookup_sum(spec, statement, column, {
                "doc": doc, "target": target, "cadence": model.cadence_months,
                "point_in_time": _is_point_in_time(spec),
                "units": model.units})
            if terms is None:
                value.unresolved = why
                out.values.append(value)
                out.issues.append(Issue(
                    code="unresolved_row", severity="error",
                    message=f"row {bound.row} '{spec.key.label}': {why}"))
                continue

            absent = [t.caption for t in terms if t.not_printed]
            if absent:
                out.issues.append(Issue(
                    code="optional_term_not_printed", severity="warning",
                    message=f"row {bound.row} '{spec.key.label}': this filing does not "
                            f"print {', '.join(repr(c) for c in absent)}; counted as 0. "
                            f"If the filing renamed the line, update the mapping."))
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

        if column.index in getattr(printed_row, "unverified", []):
            why = (f"'{printed_row.caption}' was read from a scanned page by the vision "
                   f"model ({printed_row.values[column.index]:,}), but neither the "
                   f"scan's text layer, a second read, nor the table's arithmetic "
                   f"confirms it — enter it by hand after checking the filing")
            value.unresolved = why
            out.values.append(value)
            out.issues.append(Issue(code="unverified_figure", severity="error",
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
        row_scale = 0.01 if spec.scale == "percent" else spec.scale
        if columns_to_subtract:
            letters = {c: get_column_letter(c) for c in columns_to_subtract}
            priors = {c: cells.get((bound.row, c)) for c in columns_to_subtract}
            # The workbook's own figures are in the sheet's units; the filing's are
            # in the filing's, so both are brought to the sheet's scale first.
            scaled_printed = (printed * row_scale if row_scale is not None else
                              _scale(printed, _row_units(printed_row) or statement.units
                                     or doc.units or model.units, model.units))
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
        elif row_scale is not None:
            # Not money: the map's own multiplier stands in for the unit conversion.
            figure = printed * row_scale
            scale_detail = ("percent to fraction" if spec.scale == "percent"
                            else "the map's multiplier")
            value.adjustments.append(Adjustment(
                kind="scale",
                detail=f"printed {printed:,} x {row_scale:g} ({scale_detail}) "
                       f"= {figure:,.6g}; no unit conversion applied"))
        else:
            source_units = (_row_units(printed_row) or statement.units or doc.units
                            or model.units)
            figure = _scale(printed, source_units, model.units)
            value.units_from = source_units
            value.units_to = model.units

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

    from finscan.model.load import load as load_map

    doc = PdfDoc.from_dict(json.loads(Path(pdf_json).read_text(encoding="utf-8")))
    resolved_map = load_map(map_path, workbook)
    return resolve_values(doc, resolved_map, str(workbook), period_end)
