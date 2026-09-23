"""The model.json contract — what a company's Excel model means.

Written once per company by `learn`, reviewed by an analyst, then frozen. The
quarterly run reads it and does no guessing of its own.

Rows are keyed by (label, section, occurrence), never by row number. An analyst
inserting a line moves every row below it; the caption and the statement section
it sits in do not move. `row_hint` is kept only to break a tie and to notice that
a row has shifted.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

MAP_VERSION = "1.0"

#: What the run does with a row.
#:   input   — receives a value from the filing
#:   formula — its formula is copied forward; never written
#:   skip    — a heading, spacer or ratio; neither written nor copied
RowKind = Literal["input", "formula", "skip"]

#: Which statement a row's value comes from. Scoping the lookup is what stops a
#: cash-flow caption matching a P&L line of the same name, and vice versa.
Statement = Literal[
    "income_statement", "comprehensive_income", "balance_sheet", "cash_flow", "equity"
]

#: How the row's figure relates to a period.
#:   quarter       — a standalone period figure
#:   cumulative    — year-to-date; prior columns are subtracted at write time
#:   point_in_time — a balance on a date. NEVER de-cumulated: subtracting last
#:                   quarter's cash balance from this one is wrong and looks fine.
Basis = Literal["quarter", "cumulative", "point_in_time"]

Section = Literal["income_statement", "balance_sheet", "cash_flow", "other"]


@dataclass
class RowKey:
    label: str
    section: Section = "other"
    #: 1-based, for a caption that repeats within one section ("Other", "Total").
    occurrence: int = 1

    def normalized(self) -> tuple[str, str, int]:
        return (normalize_label(self.label), self.section, self.occurrence)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RowSpec:
    key: RowKey
    row_hint: int
    kind: RowKind = "input"
    #: What the row's value comes from:
    #:   field:<canonical>   a canonical field, looked up in the filing by its aliases
    #:   pdf:<caption>       a caption to look for in the filing
    #:   const:<number>      a fixed number; never read from the filing
    #:   absent:<reason>     this filing has no such line — left blank, and reported
    #:                       every quarter, with an error if the line later appears
    #:   carry:<reason>      this filing never reports the line at all; its figure is
    #:                       whatever the analyst last typed into the reference column
    #:                       (an FX peg, say) — carried forward unchanged, never read
    #:                       from the filing
    #:   sum:<a>|<b>         several filing lines added into one row; each term is
    #:                       itself an instruction (pdf:/field:/const:), may carry a
    #:                       leading "-", and "|" separates them because captions
    #:                       contain commas. One unresolvable term fails the row.
    #:
    #: A `pdf:` caption is the caption to LOOK FOR in the filing, not one taken
    #: from it: `learn` reads the workbook and never opens a PDF, so the only text
    #: it has is the workbook's own. The prefix names where to search, not where
    #: the words came from.
    resolve: str | None = None
    #: The caption the filing actually printed, recorded by `bind` after a run
    #: matched this row. Tried first next quarter, so a match found once by
    #: direction-reading or word inclusion becomes an exact match afterwards —
    #: and so the map shows an analyst what each row really bound to.
    pdf_caption: str | None = None
    statement: Statement | None = None
    basis: Basis | None = None
    sign: Literal["positive", "negative"] | None = None
    #: Set by `learn` when a human should look at this row before it is trusted.
    review: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"key": self.key.to_dict(), "row_hint": self.row_hint,
                               "kind": self.kind}
        for name in ("resolve", "pdf_caption", "statement", "basis", "sign", "review"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RowSpec":
        return cls(
            key=RowKey(**d["key"]),
            row_hint=d.get("row_hint", 0),
            kind=d.get("kind", "input"),
            resolve=d.get("resolve"),
            pdf_caption=d.get("pdf_caption"),
            statement=d.get("statement"),
            basis=d.get("basis"),
            sign=d.get("sign"),
            review=d.get("review"),
        )


@dataclass
class ModelMap:
    company: str
    sheet: str
    fingerprint: str = ""
    units: str = "units"
    cadence_months: int = 3
    fiscal_year_start_month: int = 1
    label_col: int = 2
    header_row: int = 1
    first_data_row: int = 1
    reference_col: int | None = None
    write_col: int | None = None
    write_mode: str = "append"
    #: column index -> ISO date of the period it holds, as discovered.
    period_dates: dict[int, str] = field(default_factory=dict)
    period_date_row: int | None = None
    rows: list[RowSpec] = field(default_factory=list)
    confirmed_by: str = ""
    confirmed_at: str = ""
    map_version: str = MAP_VERSION

    @property
    def confirmed(self) -> bool:
        return bool(self.confirmed_by)

    @property
    def inputs(self) -> list[RowSpec]:
        return [r for r in self.rows if r.kind == "input"]

    @property
    def needs_review(self) -> list[RowSpec]:
        return [r for r in self.rows if r.review]

    def to_dict(self) -> dict[str, Any]:
        return {
            "map_version": self.map_version,
            "company": self.company,
            "sheet": self.sheet,
            "fingerprint": self.fingerprint,
            "units": self.units,
            "cadence_months": self.cadence_months,
            "fiscal_year_start_month": self.fiscal_year_start_month,
            "layout": {
                "label_col": self.label_col,
                "header_row": self.header_row,
                "first_data_row": self.first_data_row,
                "reference_col": self.reference_col,
                "write_col": self.write_col,
                "write_mode": self.write_mode,
                "period_dates": {str(k): v for k, v in sorted(self.period_dates.items())},
                "period_date_row": self.period_date_row,
            },
            "confirmed_by": self.confirmed_by,
            "confirmed_at": self.confirmed_at,
            "rows": [r.to_dict() for r in self.rows],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ModelMap":
        layout = d.get("layout") or {}
        return cls(
            company=d["company"],
            sheet=d["sheet"],
            fingerprint=d.get("fingerprint", ""),
            units=d.get("units", "units"),
            cadence_months=d.get("cadence_months", 3),
            fiscal_year_start_month=d.get("fiscal_year_start_month", 1),
            label_col=layout.get("label_col", 2),
            header_row=layout.get("header_row", 1),
            first_data_row=layout.get("first_data_row", 1),
            reference_col=layout.get("reference_col"),
            write_col=layout.get("write_col"),
            write_mode=layout.get("write_mode", "append"),
            period_dates={int(k): v for k, v in (layout.get("period_dates") or {}).items()},
            period_date_row=layout.get("period_date_row"),
            rows=[RowSpec.from_dict(r) for r in d.get("rows", [])],
            confirmed_by=d.get("confirmed_by", ""),
            confirmed_at=d.get("confirmed_at", ""),
            map_version=d.get("map_version", MAP_VERSION),
        )

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
                        encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "ModelMap":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def normalize_label(label: str) -> str:
    """Caption to its comparison form: case, spacing and punctuation removed.

    Keeps letters, digits and single spaces, so "General and Administration
    Expenses " and "General & Administration  Expenses" are the same key.
    """
    import re
    import unicodedata

    text = unicodedata.normalize("NFKD", label or "")
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip().lower()
