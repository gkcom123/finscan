"""Simulate the new column before writing it, and compare against the filing.

Copying a model's formulas forward is safe only if the inputs feeding them mean
what their captions say. They often do not. A row captioned "Other gains, net"
turned out to hold `=2638-3578` — the analyst's own combination of *investment
gains* and *other gains*. Filling it with the filing's "other gains" figure
alone is a perfectly plausible-looking write that leaves every subtotal below it
wrong by the investment gains.

Nothing about the caption reveals this. What reveals it is arithmetic: evaluate
the copied formulas against the values about to be written, and compare the
result with what the filing printed for that same line. If the model's own
calculation of profit before tax disagrees with the filing's profit before tax,
something upstream is not what it claims to be.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from finscan.schemas import FIELD_LABELS, Issue

_REF = re.compile(r"(?<![A-Za-z0-9_$])(\$?)([A-Za-z]{1,3})(\$?)(\d{1,7})(?![0-9(])")
_SUM = re.compile(r"\bSUM\(\s*([A-Za-z]{1,3}\$?\d+)\s*:\s*([A-Za-z]{1,3}\$?\d+)\s*\)", re.I)
_SAFE = re.compile(r"^[0-9+\-*/(). ]*$")
MAX_DEPTH = 24


@dataclass
class CellCheck:
    row: int
    field: str | None
    computed: float | None
    reported: float | None
    label: str = ""


class ColumnModel:
    """A single spreadsheet column, evaluated in isolation.

    Only same-column arithmetic is resolved — that covers the subtotal chain of
    a period column, which is what needs checking. Anything referring to another
    column, another sheet, or a function beyond SUM is simply not evaluated and
    is reported as unchecked rather than guessed at.
    """

    def __init__(self, column: str, cells: dict[int, object]):
        self.column = column.upper()
        self.cells = cells
        self._cache: dict[int, float | None] = {}

    def value(self, row: int, depth: int = 0) -> float | None:
        if row in self._cache:
            return self._cache[row]
        if depth > MAX_DEPTH:
            return None
        self._cache[row] = None                 # cycle guard
        raw = self.cells.get(row)

        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            self._cache[row] = float(raw)
            return self._cache[row]
        if not isinstance(raw, str) or not raw.startswith("="):
            return None

        expr = self._expand(raw[1:], depth)
        if expr is None or not _SAFE.match(expr):
            return None
        try:
            self._cache[row] = float(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307
        except Exception:
            self._cache[row] = None
        return self._cache[row]

    def _expand(self, expr: str, depth: int) -> str | None:
        def sum_range(m: re.Match) -> str:
            c1, r1 = _split(m.group(1))
            c2, r2 = _split(m.group(2))
            if c1 != self.column or c2 != self.column:
                raise ValueError("cross-column SUM")
            total = 0.0
            for r in range(min(r1, r2), max(r1, r2) + 1):
                v = self.value(r, depth + 1)
                if v is not None:
                    total += v
            return repr(total)

        def ref(m: re.Match) -> str:
            col, row = m.group(2).upper(), int(m.group(4))
            if col != self.column:
                raise ValueError("cross-column reference")
            v = self.value(row, depth + 1)
            if v is None:
                raise ValueError("unresolved reference")
            return f"({v!r})"

        try:
            expr = _SUM.sub(sum_range, expr)
            expr = _REF.sub(ref, expr)
        except ValueError:
            return None
        return expr


def _split(ref: str) -> tuple[str, int]:
    m = re.match(r"\$?([A-Za-z]{1,3})\$?(\d+)", ref)
    return (m.group(1).upper(), int(m.group(2))) if m else ("", 0)


def simulate(plan, values: dict[str, float], column: str) -> list[CellCheck]:
    """Build the column as it would be written, then evaluate every formula row."""
    from openpyxl.formula.translate import Translator

    cells: dict[int, object] = {}
    for rp in plan.rows:
        if rp.writable and rp.field and rp.field in values:
            cells[rp.row] = values[rp.field]
        elif rp.carries_formula and rp.formula_template and rp.reference_cell:
            try:
                cells[rp.row] = Translator(
                    rp.formula_template, origin=rp.reference_cell
                ).translate_formula(f"{column}{rp.row}")
            except Exception:
                continue

    model = ColumnModel(column, cells)
    out: list[CellCheck] = []
    for rp in plan.rows:
        if not rp.carries_formula or not rp.field:
            continue
        out.append(CellCheck(
            row=rp.row, field=rp.field, label=rp.label,
            computed=model.value(rp.row),
            reported=values.get(rp.field),
        ))
    return out


def check(plan, values: dict[str, float], column: str,
          tolerance_pct: float = 1.0) -> list[Issue]:
    issues: list[Issue] = []
    for c in simulate(plan, values, column):
        if c.computed is None or c.reported is None:
            continue
        scale = max(abs(c.computed), abs(c.reported), 1e-9)
        diff = c.computed - c.reported
        if abs(diff) <= tolerance_pct / 100.0 * scale:
            continue
        issues.append(Issue(
            severity="error", code="formula_crosscheck", field=c.field,
            message=(
                f"{plan.sheet}!{column}{c.row} ('{c.label}'): the model's own formula computes "
                f"{c.computed:,.2f} for {FIELD_LABELS.get(c.field, c.field)}, but the filing "
                f"reports {c.reported:,.2f} (out by {diff:,.2f}). An input row feeding this "
                f"subtotal does not hold what its caption suggests — check the rows above."
            ),
        ))
    return issues
