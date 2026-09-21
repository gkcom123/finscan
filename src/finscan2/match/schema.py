"""The values.json contract — one resolved number per mapped row, with its proof.

Every field here exists so a reviewer can answer "why is this number in my model?"
without rerunning anything: which caption it came from, which column of which
statement, what was printed there, and what was done to it afterwards.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

VALUES_VERSION = "1.0"


@dataclass
class Source:
    """Where the figure was read."""

    statement: str
    page: int
    caption: str
    column_header: str
    column_index: int
    months: int | None
    end: str | None
    printed: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Adjustment:
    """What was done to the printed figure, and why."""

    kind: str                       # decumulate | scale | sign
    detail: str
    subtracted: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResolvedValue:
    row: int
    label: str
    section: str
    value: float | None
    resolve: str
    source: Source | None = None
    adjustments: list[Adjustment] = field(default_factory=list)
    units_from: str | None = None
    units_to: str | None = None
    #: Set when the row could not be resolved; `value` is then None and the cell
    #: is left blank rather than filled with anything plausible.
    unresolved: str | None = None

    @property
    def ok(self) -> bool:
        return self.value is not None and self.unresolved is None

    def comment(self) -> str:
        """The Excel cell comment. Stage 4 writes this verbatim."""
        if self.unresolved:
            return f"FinScan: no value written — {self.unresolved}"
        lines = [f"FinScan: {self.value:,.2f}"]
        if self.source:
            s = self.source
            lines.append(f"from '{s.caption}' · {s.statement} p.{s.page}")
            lines.append(f"column '{s.column_header}' (printed {s.printed:,.2f})")
        for adjustment in self.adjustments:
            lines.append(adjustment.detail)
        if self.units_from and self.units_from != self.units_to:
            lines.append(f"scaled {self.units_from} -> {self.units_to}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "row": self.row, "label": self.label, "section": self.section,
            "value": self.value, "resolve": self.resolve,
        }
        if self.source:
            out["source"] = self.source.to_dict()
        if self.adjustments:
            out["adjustments"] = [a.to_dict() for a in self.adjustments]
        if self.units_from:
            out["units"] = {"from": self.units_from, "to": self.units_to}
        if self.unresolved:
            out["unresolved"] = self.unresolved
        return out


@dataclass
class Values:
    company: str
    sheet: str
    period_end: str
    write_col: int | None
    units: str
    values: list[ResolvedValue] = field(default_factory=list)
    issues: list = field(default_factory=list)
    values_version: str = VALUES_VERSION

    @property
    def resolved(self) -> list[ResolvedValue]:
        return [v for v in self.values if v.ok]

    @property
    def blank(self) -> list[ResolvedValue]:
        return [v for v in self.values if not v.ok]

    @property
    def errors(self) -> list:
        return [i for i in self.issues if i.severity == "error"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "values_version": self.values_version,
            "company": self.company,
            "sheet": self.sheet,
            "period_end": self.period_end,
            "write_col": self.write_col,
            "units": self.units,
            "issues": [i.to_dict() for i in self.issues],
            "values": [v.to_dict() for v in self.values],
        }

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
                        encoding="utf-8")
        return path
