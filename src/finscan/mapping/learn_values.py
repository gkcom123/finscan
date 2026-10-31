"""Learn a mapping from the analyst's own figures, and check one against them.

A results filing prints last year's figures beside this quarter's: 2Q25 next to
2Q26, December's balance sheet next to June's. The model already holds the
analyst's figures for those same periods. So the filing line whose printed
comparatives EQUAL the analyst's figures is the line the analyst read — a match
by value, not by wording. Coca-Cola's model says "Revenues" and "COGS"; its BMV
report prints "Ingresos" and "Costo de ventas". No caption rule bridges that,
but the numbers do.

Two uses, one idea:

    learn   for each model row, find the filing line whose comparative figures
            equal the analyst's (same units, either sign), and write the mapping
            row that addresses it.
    check   run the real resolver for the comparative periods and compare every
            row with the analyst's figure: verified · differs · unchecked. A
            learned row that differs is never written (see runner.sync_mapping).

Agreement on two periods is required where the filing prints two; a single
period is accepted only when exactly one line matches, or the caption shares a
word with the workbook label. Zeros never count — half of any filing is zero.
"""
from __future__ import annotations

import bisect
import itertools
import re
from dataclasses import dataclass, field

from finscan.match.resolve import UNIT_MULTIPLIER, _read_cells, _row_units
from finscan.model.schema import normalize_label
from finscan.schema import PdfDoc, Statement, StatementRow

#: Two figures agree within half the printed rounding step, or this fraction of
#: the figure: an analyst keeping millions to one decimal typed 5,279.4 for a
#: filing's 5,279,412 thousands. Tight on purpose — a balance sheet prints one
#: comparative date only, so one figure may have to identify its line alone.
RELATIVE_TOLERANCE = 0.0005

_PRIMARY = ("income_statement", "balance_sheet", "cash_flow")
_FLOW_SECTIONS = ("income_statement", "cash_flow")

#: Words too common in captions to count as a shared word with a label.
_COMMON = frozenset({
    "total", "net", "of", "and", "the", "other", "for", "in", "on", "to", "from",
    "a", "an", "by", "at", "as", "or", "de", "del", "la", "los", "las", "y", "en",
    "period", "year", "quarter", "income", "expenses", "expense",
})


# --------------------------------------------------------------------------- #
# The analyst's figures, by period
# --------------------------------------------------------------------------- #

def _month(iso: str | None) -> tuple[int, int] | None:
    try:
        return int(iso[:4]), int(iso[5:7])
    except (TypeError, ValueError, IndexError):
        return None


def _shift(month: tuple[int, int], by: int) -> tuple[int, int]:
    year, m = month
    m += by
    while m <= 0:
        year, m = year - 1, m + 12
    while m > 12:
        year, m = year + 1, m - 12
    return year, m


@dataclass
class ModelFigures:
    """The workbook's typed figures for some rows, addressable by period."""

    cadence: int
    units: str
    by_month: dict[tuple[int, int], list[int]]
    cells: dict[tuple[int, int], float | None]

    @classmethod
    def read(cls, workbook: str, sheet: str, period_dates: dict[int, str],
             cadence: int, units: str, rows: list[int]) -> "ModelFigures":
        by_month: dict[tuple[int, int], list[int]] = {}
        for column, iso in period_dates.items():
            if (month := _month(iso)) is not None:
                by_month.setdefault(month, []).append(column)
        wanted = {(row, col) for row in rows for cols in by_month.values() for col in cols}
        return cls(cadence=cadence or 3, units=units or "units", by_month=by_month,
                   cells=_read_cells(workbook, sheet, wanted))

    def at(self, row: int, month: tuple[int, int]) -> list[float]:
        values = (self.cells.get((row, col)) for col in self.by_month.get(month, []))
        return [v for v in values if isinstance(v, (int, float))]

    def equivalents(self, row: int, end: tuple[int, int], months: int | None,
                    flow: bool) -> list[float]:
        """The analyst's figure(s) a filing column ending `end` should equal."""
        if not flow or not months or months <= self.cadence:
            return self.at(row, end)
        if months % self.cadence:
            return []
        parts = [self.at(row, _shift(end, -i * self.cadence))
                 for i in range(months // self.cadence)]
        sums = []
        if all(parts):
            # A December date may carry both the Q4 column and a full-year column;
            # every pairing is tried, and only an exact agreement counts.
            sums = [sum(combo) for combo in itertools.islice(itertools.product(*parts), 16)]
        return sums + (self.at(row, end) if months == 12 else [])


def _step(printed: float, factor: float) -> float:
    """The rounding step of a printed figure, in model units."""
    if printed == int(printed):
        return factor
    decimals = len(f"{abs(printed):.6f}".rstrip("0").split(".")[1])
    return factor * 10 ** -decimals


def _agree(scaled: float, wanted: float, step: float) -> bool:
    return abs(scaled - wanted) <= max(step * 0.51, abs(wanted) * RELATIVE_TOLERANCE)


# --------------------------------------------------------------------------- #
# Learning: the filing line whose comparatives equal the analyst's figures
# --------------------------------------------------------------------------- #

@dataclass
class _Hit:
    statement: Statement
    row: StatementRow
    agreed: dict[int, set[str]] = field(default_factory=lambda: {1: set(), -1: set()})
    flow_match: bool = False
    point_match: bool = False
    #: The largest figure that agreed: tiny ones (growth rates, ratios) agree
    #: with some FX-table line by coincidence and need more evidence.
    magnitude: float = 0.0
    #: Printed rounding relative to the figure: a report in thousands identifies a
    #: line more precisely than a press release in millions.
    precision: float = float("inf")
    #: A column whose figure agreed, for the header of the one to read.
    column: object = None
    #: The largest relative difference from the analyst's figure: 95.831 typed
    #: from a report in thousands is matched exactly by that report and only to
    #: rounding by a press release printing 96.
    error: float = 0.0

    @property
    def sign(self) -> int:
        return 1 if len(self.agreed[1]) >= len(self.agreed[-1]) else -1

    @property
    def score(self) -> int:
        return len(self.agreed[self.sign])


@dataclass
class ValueMatch:
    """A workbook row and the mapping decision that reads its filing line."""

    row: int
    caption: str
    statement: str | None
    #: The filing prints the figure with the opposite sign to the model. Written
    #: as a negated caption ("-X"), which — unlike a `sign` convention — also holds
    #: for a two-direction caption such as "Foreign exchange loss (gain)".
    negate: bool
    basis: str | None
    periods: list[str]
    why: str
    #: Header text naming the column to read, where this quarter prints several
    #: for one date: JD's "June 30, 2026 RMB" beside "June 30, 2026 US$".
    column: str | None = None

    @property
    def pdf(self) -> str | list[str]:
        return [f"-{self.caption}"] if self.negate else self.caption


def _stem(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _words(text: str) -> set[str]:
    return {_stem(w) for w in normalize_label(text).split()
            if len(w) > 2 and w not in _COMMON}


def _columns(statement: Statement):
    for column in statement.columns:
        month = _month(column.end)
        if month is not None:
            yield column, month


def match_by_value(doc: PdfDoc, rows: list[tuple[int, str, str]], figures: ModelFigures,
                   target: str | None = None) -> tuple[dict[int, ValueMatch], dict[int, str]]:
    """For each (row, label, section), the filing line its comparatives equal.

    Returns the matches, and for rows that matched several lines equally well,
    why none was chosen — that note is what an analyst reads.
    """
    current = _month(target) if target else None
    flows = {row: section in _FLOW_SECTIONS for row, _, section in rows}
    kinds = {row: section for row, _, section in rows}

    # Every (row, analyst figure) a column of this shape should equal, sorted by
    # magnitude so each printed figure is compared with its neighbours only.
    targets: dict[tuple, tuple[list[float], list[tuple[float, int]]]] = {}

    def wanted_for(month, months, point):
        key = (month, months, point)
        if key not in targets:
            pairs = []
            for row, _, _ in rows:
                flow = flows[row] and not point
                for value in figures.equivalents(row, month, months, flow):
                    if value:
                        pairs.append((abs(value), value, row))
            pairs.sort()
            targets[key] = ([p[0] for p in pairs], [(p[1], p[2]) for p in pairs])
        return targets[key]

    hits: dict[int, dict[tuple[int, int], _Hit]] = {}
    for s_index, statement in enumerate(doc.statements):
        units = statement.units or doc.units or figures.units
        for r_index, line in enumerate(statement.rows):
            if not normalize_label(line.caption):
                continue
            factor = (UNIT_MULTIPLIER.get(_row_units(line) or units, 1.0)
                      / UNIT_MULTIPLIER.get(figures.units, 1.0))
            for column, month in _columns(statement):
                if month == current or column.index >= len(line.values):
                    continue
                printed = line.values[column.index]
                if printed is None or printed == 0 or column.index in (line.unverified or []):
                    continue
                scaled = printed * factor
                step = _step(printed, factor)
                point = column.kind == "point_in_time" or not column.months
                magnitudes, entries = wanted_for(month, column.months, point)
                if not entries:
                    continue
                slack = max(step * 0.51, abs(scaled) * RELATIVE_TOLERANCE * 1.01)
                lo = bisect.bisect_left(magnitudes, abs(scaled) - slack)
                hi = bisect.bisect_right(magnitudes, abs(scaled) + slack)
                for value, row in entries[lo:hi]:
                    # A flow is never evidenced by a balance, nor a balance by a
                    # flow: Grupo Bimbo's income tax equalled a bank loan's balance.
                    if (point and kinds[row] in _FLOW_SECTIONS) or (
                            not point and kinds[row] == "balance_sheet"):
                        continue
                    if _agree(scaled, value, step):
                        sign = 1
                    elif _agree(-scaled, value, step):
                        sign = -1
                    else:
                        continue
                    hit = hits.setdefault(row, {}).setdefault(
                        (s_index, r_index), _Hit(statement, line))
                    # Each column is its own evidence: the 3-month and the 6-month
                    # 2025 columns both end on 2025-06-30.
                    hit.agreed[sign].add(column.end if point or not column.months
                                         else f"{column.end} ({column.months}m)")
                    hit.magnitude = max(hit.magnitude, abs(value))
                    hit.column = hit.column or column
                    hit.precision = min(hit.precision, step / abs(scaled))
                    hit.error = max(hit.error, abs(sign * scaled - value) / abs(value))
                    if point:
                        hit.point_match = True
                    else:
                        hit.flow_match = True

    matches: dict[int, ValueMatch] = {}
    ambiguous: dict[int, str] = {}
    for row, label, section in rows:
        found = list(hits.get(row, {}).values())
        if not found:
            continue
        chosen, why = _choose(found, label, section, doc)
        if chosen is None:
            ambiguous[row] = why
            continue
        decision = _address(doc, chosen, section)
        if decision is None:
            ambiguous[row] = (f"its figures match '{chosen.row.caption}', but that line "
                              f"cannot be addressed uniquely")
            continue
        selector, caption = decision
        basis = None
        if section in _FLOW_SECTIONS and chosen.point_match and not chosen.flow_match:
            basis = "point_in_time"
        elif section not in _FLOW_SECTIONS and chosen.flow_match and not chosen.point_match:
            basis = "quarter"
        periods = sorted(chosen.agreed[chosen.sign])
        matches[row] = ValueMatch(
            row=row, caption=caption, statement=selector, negate=chosen.sign < 0,
            basis=basis, periods=periods, column=_column_hint(chosen, target),
            why=f"matched by value: the filing's '{chosen.row.caption}' equals the "
                f"model for {', '.join(periods)}" + (" (opposite sign)" if chosen.sign < 0 else ""))
    return matches, ambiguous


def _choose(found: list[_Hit], label: str, section: str,
            doc: PdfDoc) -> tuple[_Hit | None, str]:
    """The one line among equal-valued candidates, or why there is none.

    Ties are broken in order of how much each preference says about the line:
    the row's own statement, a primary statement over a note, the line that
    equals the analyst's figure most exactly, the statement the row reads by
    default (the first of its kind), a shared word with the label, and the more
    precisely printed figure. A same-caption tie across pages of one
    table is no tie at all.
    """
    best = max(h.score for h in found)
    top = [h for h in found if h.score == best]
    label_words = _words(label)

    def narrow(candidates, keep):
        kept = [h for h in candidates if keep(h)]
        return kept or candidates

    top = narrow(top, lambda h: h.statement.kind == section
                 or (section == "income_statement" and h.statement.kind == "comprehensive_income"))
    top = narrow(top, lambda h: h.statement.note is None)
    closest = min(h.error for h in top)
    top = narrow(top, lambda h: h.error <= max(closest * 10, 1e-5))
    if section in _PRIMARY:
        default = doc.statement(section)
        top = narrow(top, lambda h: _contains(default, h.row))
    overlap = {id(h): len(label_words & _words(h.row.caption)) for h in found}
    most = max(overlap[id(h)] for h in top)
    if most:
        top = [h for h in top if overlap[id(h)] == most]
    finest = min(h.precision for h in top)
    top = narrow(top, lambda h: h.precision <= finest * 1.0001)

    distinct = {(normalize_label(h.row.caption), h.statement.kind, h.statement.title)
                for h in top}
    if len(distinct) > 1:
        names = sorted({f"'{h.row.caption}'" for h in top})[:4]
        return None, (f"its figures match several filing lines equally well "
                      f"({', '.join(names)}); none was chosen")
    chosen = top[0]
    shares_word = overlap[id(chosen)] > 0
    if chosen.magnitude < 1 and not (best >= 2 and shares_word):
        # Growth rates and ratios: any small figure agrees with some line.
        return None, (f"its figures are too small to identify a line by value alone "
                      f"('{chosen.row.caption}' agrees)")
    # One figure is enough when nothing else agrees, when the captions share a
    # word, or when it is printed to five significant figures — a balance sheet
    # or a year-to-date cash flow prints only one comparative to go on.
    own_statement = section not in _PRIMARY or chosen.statement.kind == section or (
        section == "income_statement" and chosen.statement.kind == "comprehensive_income")
    if best >= 2 or len(found) == 1 or shares_word or (
            chosen.precision <= 1e-4 and own_statement):
        return chosen, ""
    return None, (f"its figure matches '{chosen.row.caption}' for one period only, "
                  f"the captions share no word and the figure is too coarsely "
                  f"printed to be sure; not taken on one figure")


def _column_hint(hit: _Hit, target: str | None) -> str | None:
    """A header word picking this quarter's column, when the date alone cannot.

    The word comes from the header of the comparative column that agreed — the
    currency, "Reported", "Consolidated" — and must name exactly one of this
    quarter's columns of the same kind.
    """
    column = hit.column
    if column is None or not target:
        return None
    point = column.kind == "point_in_time" or not column.months
    same = [c for c in hit.statement.columns
            if c.end and c.end[:7] == target[:7]
            and (point or c.months == column.months)]
    if len(same) <= 1:
        return None
    words = [w for w in normalize_label(column.header).split()
             if not _TITLE_DATE.fullmatch(w) and not w.isdigit()
             and w not in _COMMON and w not in {"months", "month", "ended", "ending"}]
    for word in reversed(words):
        named = [c for c in same if f" {word} " in f" {normalize_label(c.header)} "]
        if len(named) == 1:
            return word.upper() if len(word) <= 4 else word
    return None


#: Date phrases cut from a statement title so the selector survives next
#: quarter: "for the nine months ended 30 September 2026".
_TITLE_DATE = re.compile(
    r"\b(for the|as at|as of|at|ended|ending|period|three|six|nine|twelve|"
    r"january|february|march|april|may|june|july|august|september|october|november|"
    r"december|enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|"
    r"octubre|noviembre|diciembre|\d{4}|\d{1,2}[/-]\d{1,2})\b", re.IGNORECASE)


def _stable_title(title: str) -> str:
    match = _TITLE_DATE.search(title or "")
    text = (title[:match.start()] if match else title or "").strip(" ,;:-–—(")
    return text if len(text) >= 6 else (title or "").strip()


def _contains(statement: Statement | None, line: StatementRow) -> bool:
    return statement is not None and any(r is line for r in statement.rows)


def _address(doc: PdfDoc, hit: _Hit, section: str) -> tuple[str | None, str] | None:
    """The mapping's `statement` and `pdf` that make the resolver land on this line."""
    statement, line = hit.statement, hit.row
    if statement.note is not None:
        selector = f"note:{statement.note}"
        scope = statement
    else:
        default = section if section in _PRIMARY else None
        if default and _contains(doc.statement(default), line):
            selector, scope = None, doc.statement(default)
        elif statement.kind in _PRIMARY + ("comprehensive_income", "equity", "summary") \
                and _contains(doc.statement(statement.kind), line):
            selector, scope = statement.kind, doc.statement(statement.kind)
        else:
            title = _stable_title(statement.title)
            scope = doc.titled(title) if title else None
            if not _contains(scope, line):
                return None
            selector = f"title:{title}"

    target = normalize_label(line.caption)
    same = [r for r in scope.rows if normalize_label(r.caption) == target]
    position = next((i for i, r in enumerate(same) if r is line), None)
    if position is None:
        return None
    caption = line.caption.strip()
    return selector, caption if position == 0 else f"{caption} #{position + 1}"


# --------------------------------------------------------------------------- #
# Checking: does the mapping reproduce the analyst's figures?
# --------------------------------------------------------------------------- #

@dataclass
class RowCheck:
    row: int
    label: str
    #: verified · differs · unchecked
    status: str
    detail: str = ""
    periods: list[str] = field(default_factory=list)


@dataclass
class MappingCheck:
    periods: list[str] = field(default_factory=list)
    rows: dict[int, RowCheck] = field(default_factory=dict)

    def count(self, status: str) -> int:
        return sum(1 for r in self.rows.values() if r.status == status)

    def summary(self) -> dict:
        return {
            "compared_with": self.periods,
            "verified": self.count("verified"),
            "differs": self.count("differs"),
            "unchecked": self.count("unchecked"),
            "rows": [{"row": r.row, "label": r.label, "status": r.status,
                      "detail": r.detail}
                     for r in sorted(self.rows.values(), key=lambda r: r.row)
                     if r.status != "verified"],
        }


def comparative_periods(doc: PdfDoc, period_dates: dict[int, str], target: str | None,
                        limit: int = 4) -> list[str]:
    """Period ends the filing prints and the model already holds.

    Those the primary statements print come first — last year's quarter, last
    year-end — not whatever a small trailing-twelve-month table happens to date
    most recently.
    """
    held = {_month(iso) for iso in period_dates.values()}
    current = _month(target) if target else None
    weight: dict[str, int] = {}
    for statement in doc.statements:
        for column in statement.columns:
            month = _month(column.end)
            if month is None or month not in held or month == current:
                continue
            weight[column.end] = weight.get(column.end, 0) + (
                10 if statement.note is None and statement.kind in _PRIMARY else 1)
    ranked = sorted(weight, key=lambda end: (-weight[end], end), reverse=False)
    return ranked[:limit]


def check_mapping(mapping, layout, doc: PdfDoc, workbook: str,
                  target: str | None = None) -> MappingCheck:
    """Resolve each comparative period with the real pipeline and compare."""
    from finscan.mapping.compile import compile_mapping
    from finscan.match.resolve import resolve_values
    from finscan.model.load import resolve as bind_map

    model, issues = compile_mapping(mapping, layout)
    check = MappingCheck()
    if any(i.severity == "error" for i in issues):
        return check
    resolved = bind_map(model, layout)
    inputs = {b.row: b.spec for b in resolved.bound if b.spec.kind == "input"}
    checkable = {row: spec for row, spec in inputs.items()
                 if not (spec.resolve or "").startswith(("const:", "carry:", "absent:"))}
    for row, spec in inputs.items():
        check.rows[row] = RowCheck(row=row, label=spec.key.label, status="unchecked",
                                   detail=("not read from the filing"
                                           if row not in checkable else
                                           "the filing prints no comparative the model holds"))
    check.periods = comparative_periods(doc, model.period_dates, target)
    if not check.periods or not checkable:
        return check

    figures = ModelFigures.read(workbook, model.sheet, model.period_dates,
                                model.cadence_months, model.units, list(checkable))
    agreed: dict[int, list[str]] = {}
    differs: dict[int, str] = {}
    for end in check.periods:
        values = resolve_values(doc, resolved, workbook, end)
        for value in values.values:
            if value.row not in checkable or value.value is None:
                continue
            have = figures.at(value.row, _month(end))
            if not have:
                continue
            # Within the printed rounding: a press release in whole millions prints
            # 99 for the 99.44 an analyst typed from the report in thousands.
            step = 0.1
            if value.source is not None and value.source.printed:
                factor = (UNIT_MULTIPLIER.get(value.units_from or "", 1.0)
                          / UNIT_MULTIPLIER.get(value.units_to or "", 1.0))
                step = max(step, _step(value.source.printed, factor))
            if any(_agree(value.value, h, step) for h in have):
                agreed.setdefault(value.row, []).append(end)
            elif value.row not in differs:
                source = value.source.caption if value.source else value.resolve
                differs[value.row] = (f"'{source}' gives {value.value:,.2f} for {end}; "
                                      f"the model has {have[0]:,.2f}")

    for row in checkable:
        result = check.rows[row]
        if row in differs:
            result.status, result.detail = "differs", differs[row]
            result.periods = agreed.get(row, [])
        elif row in agreed:
            result.status, result.periods = "verified", agreed[row]
            result.detail = f"equals the model for {', '.join(agreed[row])}"
    return check
