"""Work out, for an arbitrary workbook, where a new period column may be written.

Nothing here is company-specific. Every decision is derived from two signals the
workbook itself carries:

  * the blue/black formatting convention (see style_probe), which says which
    cells are hardcodes and which are calculated;
  * the most recent existing period column, which is used as a *template* — for
    each row it tells us whether that row is a hardcode (paste the extracted
    value) or a formula (copy the formula forward so the model keeps computing).

That second point is what makes this safe across 100 different models. FinScan
never decides that a row "should" be a subtotal; it copies whatever the company
already did in the previous quarter's column.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from finscan.excel.mapper import map_rows
from finscan.excel.style_probe import Role, Theme, formula_has_references, probe_cell
from finscan.extract.normalize import sniff_units
from finscan.schemas import CASH_FLOW_FIELDS, FIELD_LABELS

#: P&L fields: everything canonical that is not a cash-flow-statement line.
_PL_FIELDS: set[str] = set(FIELD_LABELS) - CASH_FLOW_FIELDS

MAX_SCAN_ROWS = 500
MAX_SCAN_COLS = 60
MIN_CANONICAL_ROWS = 3      # fewer than this and the tab is not a financial statement

# --------------------------------------------------------------------------- #
# Statement sections.
#
# An analyst model is several statements stacked in one column, and the same
# caption legitimately appears in more than one of them. "Taxes" in the cash
# flow block is cash tax paid; "Income tax expense" in the income statement is
# the P&L charge. They are different numbers. Matching captions without knowing
# which block they sit in will eventually write one into the other's row, and
# nothing downstream would catch it.
# --------------------------------------------------------------------------- #
SECTION_KEYWORDS: list[tuple[str, str]] = [
    ("income statement", "income_statement"),
    ("profit and loss", "income_statement"),
    ("statement of operations", "income_statement"),
    ("p and l", "income_statement"),
    ("cash flow", "cash_flow"),
    ("cashflow", "cash_flow"),
    ("balance sheet", "balance_sheet"),
    ("financial position", "balance_sheet"),
    ("ltm", "other"),
    ("credit ratios", "other"),
    ("credit metrics", "other"),
    ("covenant", "other"),
    ("valuation", "other"),
    ("assumptions", "other"),
    ("segment", "other"),
    ("kpi", "other"),
    ("guidance", "other"),
    ("capitalisation", "other"),
    ("capitalization", "other"),
]

#: Rows before any section marker belong to the statement most models open with.
DEFAULT_SECTION = "income_statement"

# Generic words that are often valid line-item prefixes. Treat these as section
# markers only when the caption is exactly the keyword (or a very small heading
# variant handled elsewhere), never on startswith/endswith matches.
_EXACT_ONLY_SECTION_KEYWORDS = {
    "valuation",
    "assumptions",
    "segment",
    "kpi",
    "guidance",
    "capitalisation",
    "capitalization",
    "covenant",
}


def detect_section(label: str) -> str | None:
    from finscan.excel.mapper import normalize_label

    norm = normalize_label(label)
    if not norm or len(norm) > 60:
        return None
    for keyword, section in SECTION_KEYWORDS:
        if keyword in _EXACT_ONLY_SECTION_KEYWORDS:
            if norm == keyword:
                return section
            continue
        if norm == keyword or norm.startswith(keyword + " ") or norm.endswith(" " + keyword):
            return section
    return None


@dataclass
class RowPlan:
    row: int
    label: str
    field: str | None = None
    match_method: str = "unmatched"
    match_score: float = 0.0
    #: what the same row looks like in the reference period column
    reference_role: str = "unknown"
    formula_template: str | None = None
    reference_cell: str | None = None
    section: str = DEFAULT_SECTION
    #: formula that references no other cell — hardcoded arithmetic, not a formula
    constant_formula: bool = False
    #: captured by the second blue-row pass; matched against PDF by raw label
    label_only: bool = False

    @property
    def writable(self) -> bool:
        """Blue inputs, plus rows whose "formula" is really a typed-in constant:
        those must take this period's value, not last period's arithmetic."""
        if self.field is None and not self.label_only:
            return False
        return self.reference_role == Role.input.value or self.constant_formula

    @property
    def carries_formula(self) -> bool:
        return (self.reference_role == Role.formula.value
                and self.formula_template is not None
                and not self.constant_formula)


@dataclass
class SheetPlan:
    sheet: str
    in_scope: bool
    reason: str = ""
    label_col: int = 1
    header_row: int = 1
    first_data_row: int = 1
    first_period_col: int = 2
    last_period_col: int = 2
    reference_col: int | None = None      # most recent column with input cells
    write_col: int = 3
    write_mode: str = "append"            # append | fill_blank
    units: str = "units"
    colors_found: bool = False
    period_headers: dict[int, str] = field(default_factory=dict)
    period_dates: dict[int, str] = field(default_factory=dict)   # col -> ISO date
    sections: dict[int, str] = field(default_factory=dict)       # start row -> section
    rows: list[RowPlan] = field(default_factory=list)
    score: float = 0.0

    @property
    def write_col_letter(self) -> str:
        return get_column_letter(self.write_col)

    @property
    def writable_rows(self) -> list[RowPlan]:
        return [r for r in self.rows if r.writable]

    @property
    def formula_rows(self) -> list[RowPlan]:
        return [r for r in self.rows if r.carries_formula]

    def summary(self) -> str:
        return (
            f"{self.sheet}: {'in scope' if self.in_scope else 'skipped'} "
            f"({self.reason}) — {len(self.writable_rows)} input row(s), "
            f"{len(self.formula_rows)} formula row(s), units={self.units}, "
            f"write {self.write_mode} at {self.write_col_letter}"
        )


@dataclass
class WorkbookPlan:
    path: str
    fingerprint: str
    sheets: list[SheetPlan]
    colors_found: bool

    @property
    def in_scope(self) -> list[SheetPlan]:
        return [s for s in self.sheets if s.in_scope]

    def sheet(self, name: str) -> SheetPlan | None:
        return next((s for s in self.sheets if s.sheet == name), None)


# --------------------------------------------------------------------------- #
def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _text(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def assign_sections(labels: dict[int, str]) -> tuple[dict[int, str], dict[int, str]]:
    """Walk the caption column top to bottom, tracking which statement we are in.

    Returns (row -> section, section_start_row -> section).
    """
    row_section: dict[int, str] = {}
    starts: dict[int, str] = {}
    current = DEFAULT_SECTION
    for row in sorted(labels):
        found = detect_section(labels[row])
        if found:
            current = found
            starts[row] = found
        row_section[row] = current
    return row_section, starts


def _parse_period_date(v: Any) -> str | None:
    from datetime import date, datetime

    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return None


def _pick_label_col(ws, max_row: int, max_col: int) -> tuple[int, dict[int, str], float]:
    """Label column = the one whose captions best resolve to canonical fields.

    Counting text cells (the naive approach) picks the wrong column whenever a
    model keeps note references or segment names beside the line items.
    """
    best = (1, {}, -1.0)
    for c in range(1, min(max_col, 8) + 1):
        labels = {
            r: _text(ws.cell(r, c).value)
            for r in range(1, max_row + 1)
            if _text(ws.cell(r, c).value)
        }
        if not labels:
            continue
        mapped, _ = map_rows(labels, use_llm=False)
        hits = sum(1 for m in mapped if m.field)
        score = hits + min(len(labels), 40) * 0.01     # ties break toward denser columns
        if score > best[2]:
            best = (c, labels, score)
    return best


def _analyse_sheet(ws, theme: Theme) -> SheetPlan:
    max_row = min(ws.max_row or 1, MAX_SCAN_ROWS)
    max_col = min(ws.max_column or 1, MAX_SCAN_COLS)

    label_col, labels, _ = _pick_label_col(ws, max_row, max_col)

    # Captions are mapped section-by-section: income-statement rows against the
    # P&L fields, cash-flow rows against the cash-flow fields only. The split is
    # load-bearing — the cash flow statement repeats P&L captions (depreciation,
    # tax) as adjustments, and cross-section matches would corrupt both.
    # Balance-sheet / other sections keep their label for the report but are
    # never a write target.
    row_section, section_starts = assign_sections(labels)
    pl_labels = {r: t for r, t in labels.items()
                 if row_section.get(r, DEFAULT_SECTION) == "income_statement"}
    cf_labels = {r: t for r, t in labels.items()
                 if row_section.get(r, DEFAULT_SECTION) == "cash_flow"}

    mapped, _ = map_rows(pl_labels, use_llm=False, allowed_fields=_PL_FIELDS)
    cf_mapped, _ = map_rows(cf_labels, use_llm=False, allowed_fields=CASH_FLOW_FIELDS)
    by_row = {m.excel_row: m for m in [*mapped, *cf_mapped]}
    canonical_hits = sum(1 for m in mapped if m.field)

    plan = SheetPlan(sheet=ws.title, in_scope=False, label_col=label_col)
    plan.sections = section_starts
    if canonical_hits < MIN_CANONICAL_ROWS:
        plan.reason = f"only {canonical_hits} recognisable line item(s)"
        plan.score = canonical_hits
        return plan

    # First row carrying data. Two cells minimum, because header blocks are not
    # inert: a date row is datetimes (not numbers, so already excluded) but an
    # LTM column header is often a formula like "=AG2", and one stray formula
    # must not be mistaken for the start of the data.
    def _data_cells(r: int) -> int:
        n = 0
        for c in range(label_col + 1, max_col + 1):
            v = ws.cell(r, c).value
            if _is_number(v) or (isinstance(v, str) and v.startswith("=")):
                n += 1
        return n

    counts = {r: _data_cells(r) for r in range(1, max_row + 1)}
    data_rows = [r for r, n in counts.items() if n >= 2] or [r for r, n in counts.items() if n]
    if not data_rows:
        plan.reason = "no numeric or formula cells"
        return plan
    plan.first_data_row = min(data_rows)

    header_row, best_fill = max(1, plan.first_data_row - 1), -1
    for r in range(1, plan.first_data_row):
        fill = sum(1 for c in range(label_col + 1, max_col + 1) if _text(ws.cell(r, c).value))
        if fill >= best_fill and fill > 0:
            header_row, best_fill = r, fill
    plan.header_row = header_row

    # --- column census -----------------------------------------------------
    line_rows = [r for r in by_row if r >= plan.first_data_row]
    col_stats: dict[int, dict[str, int]] = {}
    for c in range(label_col + 1, max_col + 1):
        stats = {"input": 0, "formula": 0, "values": 0, "blank": 0, "unknown": 0}
        for r in line_rows:
            st = probe_cell(ws.cell(r, c), theme)
            if st.has_formula:
                stats["formula"] += 1
            elif st.role is Role.input:
                stats["input"] += 1
            elif st.role is Role.unknown:
                stats["unknown"] += 1
            if ws.cell(r, c).value is not None:
                stats["values"] += 1
            else:
                stats["blank"] += 1
        # A column counts as a period slot if it holds data, has a header, or is
        # merely *formatted* — templates routinely pre-style the next quarter's
        # column and leave it empty, and that empty column is where we belong.
        if stats["values"] or stats["input"] or stats["formula"] or _text(ws.cell(header_row, c).value):
            col_stats[c] = stats

    if not col_stats:
        plan.reason = "no period columns found"
        return plan

    period_cols = sorted(col_stats)
    plan.first_period_col, plan.last_period_col = period_cols[0], period_cols[-1]
    plan.period_headers = {
        c: _text(ws.cell(header_row, c).value)
        for c in period_cols if _text(ws.cell(header_row, c).value)
    }
    plan.colors_found = any(s["input"] for s in col_stats.values())

    # Period dates live in whichever of the top rows carries real datetimes —
    # not necessarily the row picked as the header, which is often "1Q"/"FY".
    for r in range(1, plan.first_data_row):
        dates = {c: _parse_period_date(ws.cell(r, c).value) for c in period_cols}
        dates = {c: d for c, d in dates.items() if d}
        if len(dates) > len(plan.period_dates):
            plan.period_dates = dates

    # --- where do we write? ------------------------------------------------
    # A pre-formatted but empty column means the template already reserves the
    # slot for this quarter; filling it beats appending beside it.
    blank_ready = [
        c for c in period_cols
        if col_stats[c]["values"] == 0 and (col_stats[c]["input"] or col_stats[c]["formula"])
    ]
    populated = [c for c in period_cols if col_stats[c]["values"] > 0]

    if blank_ready:
        plan.write_col, plan.write_mode = blank_ready[0], "fill_blank"
    else:
        plan.write_col, plan.write_mode = plan.last_period_col + 1, "append"

    # Reference column: the most recent populated column that best represents
    # the workbook's input template.
    #
    # Some models interleave mostly-formula analysis columns between period
    # columns. Picking the *last* column that has any blue cell can choose a
    # formula-heavy column (e.g. 1 blue row) and make almost every row appear
    # non-writable. Prefer the most recent column with a dense input footprint.
    with_inputs = [c for c in populated if col_stats[c]["input"]]
    if with_inputs:
        max_inputs = max(col_stats[c]["input"] for c in with_inputs)
        # Keep columns that are at least half as dense as the best input column.
        # A small absolute floor avoids over-filtering tiny sheets.
        min_dense = max(3, int(max_inputs * 0.5))
        dense = [c for c in with_inputs if col_stats[c]["input"] >= min_dense]
        if dense:
            plan.reference_col = dense[-1]
        else:
            # Tiny-sheet fallback: choose the most recent among top-scoring
            # input columns.
            best = [c for c in with_inputs if col_stats[c]["input"] == max_inputs]
            plan.reference_col = best[-1]
    else:
        plan.reference_col = (populated or [plan.last_period_col])[-1]

    # --- per-row plan ------------------------------------------------------
    ref = plan.reference_col
    for r in sorted(labels):
        if r < plan.first_data_row:
            continue
        m = by_row.get(r)
        rp = RowPlan(row=r, label=labels[r], field=(m.field if m else None),
                     match_method=(m.match_method if m else "unmatched"),
                     match_score=(m.match_score if m else 0.0),
                     section=row_section.get(r, DEFAULT_SECTION))
        ref_cell = ws.cell(r, ref)
        st = probe_cell(ref_cell, theme)
        rp.reference_role = st.role.value
        rp.reference_cell = f"{get_column_letter(ref)}{r}"
        if st.has_formula:
            rp.formula_template = str(ref_cell.value)
            rp.constant_formula = not formula_has_references(rp.formula_template)
        elif not plan.colors_found:
            # Fallback for models with no colour convention: absent the blue/black
            # signal, a cell that is not a formula is the best available guess at
            # an input. The sheet is flagged so a human always reviews it.
            rp.reference_role = Role.input.value

        # Rows the canonical taxonomy could not resolve are not necessarily a
        # dead end: if the reference cell is a blue input (or, absent a colour
        # convention, any non-formula cell), the row is still a candidate —
        # just matched against the PDF by its own raw label instead of a
        # canonical field id.
        if rp.field is None and not st.has_formula and (
            rp.reference_role == Role.input.value or not plan.colors_found
        ):
            rp.label_only = True

        plan.rows.append(rp)

    plan.in_scope = True
    plan.score = canonical_hits + (2.0 if plan.colors_found else 0.0)
    other_sections = sorted({s for s in section_starts.values()
                             if s not in ("income_statement", "cash_flow")})
    plan.reason = (
        f"{canonical_hits} line items, "
        + ("blue/black convention detected" if plan.colors_found
           else "no colour convention — geometry fallback")
        + (f"; {', '.join(other_sections)} section(s) excluded" if other_sections else "")
    )

    # Unit markers are often printed on the same boundary row where dated
    # period headers begin (e.g. '(MXN millions)'). Include first_data_row
    # itself; excluding it misses the most common placement in some models.
    for r in range(1, plan.first_data_row + 1):
        for c in range(1, max_col + 1):
            hit = sniff_units(_text(ws.cell(r, c).value))
            if hit:
                plan.units = hit
                break
        if plan.units != "units":
            break
    return plan


def fingerprint(plans: list[SheetPlan]) -> str:
    """Stable id for a workbook *layout*, so a stored profile can be reused and
    drift can be spotted. Deliberately ignores values — only structure."""
    h = hashlib.sha256()
    for p in sorted(plans, key=lambda s: s.sheet):
        h.update(p.sheet.encode())
        h.update(str(p.label_col).encode())
        h.update(str(p.header_row).encode())
        for r in p.rows:
            h.update(f"{r.row}:{r.label}".encode())
    return h.hexdigest()[:16]


def discover(path: str | Path) -> WorkbookPlan:
    wb = load_workbook(path, data_only=False)
    theme = Theme.from_workbook(wb)
    plans = [_analyse_sheet(wb[name], theme) for name in wb.sheetnames]
    wb.close()
    return WorkbookPlan(
        path=str(path),
        fingerprint=fingerprint([p for p in plans if p.in_scope]),
        sheets=plans,
        colors_found=any(p.colors_found for p in plans),
    )
