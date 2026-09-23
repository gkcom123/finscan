"""mappings/<company>.json — the one file a human owns and git tracks.

Everything else in this pipeline is derived and regenerable: the workbook's layout,
the row numbers, the fingerprint, the units. What cannot be regenerated is the
judgement that a model row named "Zakat and Income Tax" is the filing's `Zakat`
line plus its `Income Tax` line. That judgement lives here, one row per physical
line, so a git diff shows one line per decision.

Four things are deliberately absent, each because keeping them caused a real error:

* **row numbers** — `(label, section, occurrence)` is the real key, so inserting a
  row in Excel produces no diff at all in the reviewed file.
* **statement** on every row — it duplicated `section`; derived, overridable.
* **basis** on every row — flows and balances follow from `section`; overridable.
* **an inferred sign** — inference from one prior column wrote two wrong rows
  (a bidirectional "Other (Expenses) / Income, net" frozen as negative). The
  filing's printed sign is used as-is unless a human asserts otherwise.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from finscan2.model.schema import normalize_label

MAPPING_VERSION = "2.0"

#: Flow sections are de-cumulated when a filing prints only year-to-date columns;
#: a balance never is — subtracting last quarter's cash from this quarter's is
#: wrong and looks entirely plausible.
_BASIS_BY_SECTION = {
    "income_statement": "quarter",
    "cash_flow": "quarter",
    "balance_sheet": "point_in_time",
}


@dataclass
class MappingRow:
    """One workbook row and the filing line(s) it reads."""

    label: str
    section: str = "other"
    occurrence: int = 1
    #: str = one caption · list = a sum · None = the filing has no such line
    pdf: str | list[str] | None = None
    #: A constant, for a row no filing reports. Excludes `pdf`.
    value: float | None = None
    #: True for a row the filing never reports at all, whose figure is instead
    #: whatever the analyst last typed into the workbook (an FX peg, say) — carried
    #: forward unchanged from the reference column rather than looked up or defaulted.
    #: Excludes `pdf` and `value`.
    carry_forward: bool = False
    note: str = ""
    #: Overrides, present only where the derived default is wrong.
    statement: str | None = None
    basis: str | None = None
    sign: str | None = None

    @property
    def key(self) -> tuple[str, str, int]:
        return (normalize_label(self.label), self.section, self.occurrence)

    def resolved_statement(self) -> str | None:
        if self.statement:
            return self.statement
        return self.section if self.section != "other" else None

    def resolved_basis(self) -> str | None:
        return self.basis or _BASIS_BY_SECTION.get(self.section)

    def instruction(self) -> str:
        """The `resolve` instruction stage 3 executes for this row."""
        if self.value is not None:
            return f"const:{self.value}"
        if self.carry_forward:
            return f"carry:{self.note}" if self.note else "carry:"
        if self.pdf is None:
            return f"absent:{self.note}" if self.note else "absent:"
        if isinstance(self.pdf, str):
            return f"pdf:{self.pdf}"

        terms = []
        for caption in self.pdf:
            text = caption.strip()
            lead = ""
            if text.startswith("-"):
                lead, text = "-", text[1:].strip()
            terms.append(f"{lead}pdf:{text}")
        return "sum:" + "|".join(terms)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"label": self.label, "section": self.section}
        if self.occurrence != 1:
            out["occurrence"] = self.occurrence
        if self.value is not None:
            out["value"] = self.value
        elif self.carry_forward:
            out["carry_forward"] = True
        else:
            out["pdf"] = self.pdf
        for name in ("note", "statement", "basis", "sign"):
            if getattr(self, name):
                out[name] = getattr(self, name)
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MappingRow":
        return cls(
            label=d["label"],
            section=d.get("section", "other"),
            occurrence=int(d.get("occurrence", 1)),
            pdf=d.get("pdf"),
            value=d.get("value"),
            carry_forward=bool(d.get("carry_forward", False)),
            note=d.get("note", ""),
            statement=d.get("statement"),
            basis=d.get("basis"),
            sign=d.get("sign"),
        )


@dataclass
class Mapping:
    company: str
    sheet: str
    rows: list[MappingRow] = field(default_factory=list)
    mapping_version: str = MAPPING_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {"mapping_version": self.mapping_version, "company": self.company,
                "sheet": self.sheet, "rows": [r.to_dict() for r in self.rows]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Mapping":
        return cls(company=d["company"], sheet=d["sheet"],
                   rows=[MappingRow.from_dict(r) for r in d.get("rows", [])],
                   mapping_version=d.get("mapping_version", MAPPING_VERSION))

    def save(self, path: str | Path) -> Path:
        """One row per physical line, so a diff is one line per decision."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = ",\n".join("    " + json.dumps(r.to_dict(), ensure_ascii=False)
                          for r in self.rows)
        text = (f'{{\n  "mapping_version": "{self.mapping_version}",\n'
                f'  "company": {json.dumps(self.company)},\n'
                f'  "sheet": {json.dumps(self.sheet)},\n'
                f'  "rows": [\n{rows}\n  ]\n}}\n')
        path.write_text(text, encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Mapping":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def by_key(self) -> dict[tuple[str, str, int], MappingRow]:
        return {row.key: row for row in self.rows}
