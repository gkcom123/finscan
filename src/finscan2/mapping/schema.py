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
from typing import Any, Literal

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
    #: str = one caption · list = a sum · None = the filing has no such line.
    #: A caption ending in "?" is optional: 0 when the filing does not print it.
    pdf: str | list[str] | None = None
    #: Signed filing captions which may each select a different statement.
    #: Used for a disclosed net figure whose gains and losses live in separate notes.
    sources: list[dict[str, str]] | None = None
    #: A constant, for a row no filing reports. Excludes `pdf`.
    value: float | None = None
    #: True for a row the filing never reports at all, whose figure is instead
    #: whatever the analyst last typed into the workbook (an FX peg, say) — carried
    #: forward unchanged from the reference column rather than looked up or defaulted.
    #: Excludes `pdf` and `value`.
    carry_forward: bool = False
    #: True for a row the analyst fills with a formula in some periods and a typed
    #: figure in others — Tencent's average FX rate is typed in 2Q but is
    #: =AVERAGE(prior end rate, this end rate) in 1Q/3Q/4Q. The row's most recent
    #: formula is copied into the new column, shifted like any formula copy.
    #: Excludes `pdf`, `value` and `carry_forward`.
    copy_formula: bool = False
    note: str = ""
    #: Overrides, present only where the derived default is wrong.
    statement: str | None = None
    basis: str | None = None
    sign: str | None = None
    #: Text the chosen column's header must contain, where one date has several
    #: columns: a borrowings note prints "30 June 2026 Current" and
    #: "30 June 2026 Non-current" side by side.
    column: str | None = None
    #: Multiplier for a figure that is not money — a "1.7%" printed where the model
    #: holds 0.017 takes 0.01. Applied to the printed number INSTEAD of the table's
    #: unit conversion, which would otherwise scale a percentage by the statement's
    #: millions or thousands. "percent" is shorthand for 0.01.
    scale: float | Literal["percent"] | None = None

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
        if self.copy_formula:
            return "formula:latest"
        if self.sources is not None:
            # Signed captions that may each be read in their own statement: the same
            # thing a sum does, so they share its machinery.
            terms = []
            for source in self.sources:
                text, lead = source["caption"].strip(), ""
                if text.startswith("-"):
                    lead, text = "-", text[1:].strip()
                where = f" @ {source['statement']}" if source.get("statement") else ""
                terms.append(f"{lead}pdf:{text}{where}")
            return "sum:" + "|".join(terms)
        if self.pdf is None:
            return f"absent:{self.note}" if self.note else "absent:"
        if isinstance(self.pdf, str):
            # A trailing "?" marks the caption optional (see resolve._lookup_sum):
            # routed through the sum path, which is where optional terms are read.
            if self.pdf.strip().endswith("?"):
                return f"sum:pdf:{self.pdf.strip()}"
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
        elif self.copy_formula:
            out["copy_formula"] = True
        elif self.sources is not None:
            out["sources"] = self.sources
        else:
            out["pdf"] = self.pdf
        for name in ("note", "statement", "basis", "sign", "column", "scale"):
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
            sources=d.get("sources"),
            value=d.get("value"),
            carry_forward=bool(d.get("carry_forward", False)),
            copy_formula=bool(d.get("copy_formula", False)),
            note=d.get("note", ""),
            statement=d.get("statement"),
            basis=d.get("basis"),
            sign=d.get("sign"),
            column=d.get("column"),
            scale=d.get("scale"),
        )


@dataclass
class Mapping:
    company: str
    sheet: str
    rows: list[MappingRow] = field(default_factory=list)
    mapping_version: str = MAPPING_VERSION
    reader: str | None = None
    #: The scale the sheet's figures are kept in, for a sheet whose headers do not say.
    units: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {"mapping_version": self.mapping_version, "company": self.company,
               "sheet": self.sheet}
        if self.units:
            out["units"] = self.units
        if self.reader:
            out["reader"] = self.reader
        out["rows"] = [r.to_dict() for r in self.rows]
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Mapping":
        return cls(company=d["company"], sheet=d["sheet"],
                   rows=[MappingRow.from_dict(r) for r in d.get("rows", [])],
                   mapping_version=d.get("mapping_version", MAPPING_VERSION),
                   reader=d.get("reader"), units=d.get("units"))

    def save(self, path: str | Path) -> Path:
        """One row per physical line, so a diff is one line per decision."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = ",\n".join("    " + json.dumps(r.to_dict(), ensure_ascii=False)
                          for r in self.rows)
        reader = f'  "reader": {json.dumps(self.reader)},\n' if self.reader else ""
        units = f'  "units": {json.dumps(self.units)},\n' if self.units else ""
        text = (f'{{\n  "mapping_version": "{self.mapping_version}",\n'
                f'  "company": {json.dumps(self.company)},\n'
                f'  "sheet": {json.dumps(self.sheet)},\n'
                f'{units}{reader}'
                f'  "rows": [\n{rows}\n  ]\n}}\n')
        path.write_text(text, encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Mapping":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @staticmethod
    def append_rows(path: str | Path, rows: list[MappingRow]) -> Path:
        """Add rows to the end of a mapping file, leaving every existing line as is.

        Not `load` + `save`: `from_dict` keeps only the keys this class knows, so a
        round trip would silently drop any other top-level key a person added.
        The new rows are spliced in before the closing `]` of `rows`; a file not in
        the layout `save` writes is rewritten from its parsed JSON instead.
        """
        import re

        path = Path(path)
        text = path.read_text(encoding="utf-8")
        added = ",\n".join("    " + json.dumps(r.to_dict(), ensure_ascii=False)
                           for r in rows)
        end = re.search(r"\}(\s*)\]\s*\}\s*$", text)
        if end is not None:
            spliced = text[:end.start() + 1] + ",\n" + added + text[end.start() + 1:]
            try:
                json.loads(spliced)
            except ValueError:
                spliced = None
            if spliced is not None:
                path.write_text(spliced, encoding="utf-8")
                return path

        payload = json.loads(text)
        payload.setdefault("rows", []).extend(r.to_dict() for r in rows)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        return path

    def by_key(self) -> dict[tuple[str, str, int], MappingRow]:
        return {row.key: row for row in self.rows}
