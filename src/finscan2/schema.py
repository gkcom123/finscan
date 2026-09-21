"""The pdf.json contract — stage 1's only output.

Everything here is a plain dataclass with a `to_dict`, because pdf.json is a file
other stages read, not an object they import. Stage 3 must be runnable from a
pdf.json produced weeks earlier by a different version of this code, so the JSON
shape is the interface and these classes are only a convenience for producing it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SCHEMA_VERSION = "1.0"

#: Bump whenever parsing changes what a given PDF produces — new header forms,
#: different row splitting, a changed statement test. Cached pdf.json files
#: carrying an older value are ignored and re-read.
#:
#: This exists because the cache is keyed by the PDF's SHA-256 alone, which is
#: the right key for "same document" but says nothing about "same parser". A
#: fixed parser silently served pre-fix output until the cache was deleted by
#: hand, which looks exactly like the fix not working.
PARSER_VERSION = "3"        # 3: note tables, with severed-digit repair

#: How a page's text was recovered. Recorded per page because it changes how much
#: the figures on it can be trusted: a vision transcription is not reproducible,
#: so a value read from such a page deserves a second look.
TextSource = Literal["text", "text_respaced", "vision", "empty"]

StatementKind = Literal[
    "income_statement",
    "comprehensive_income",
    "balance_sheet",
    "cash_flow",
    "equity",
    "other",
]

#: A column either covers a span of months (an income or cash-flow column) or
#: states a position on one date (a balance-sheet column). The distinction is the
#: whole point of parsing headers: only a period column can ever be de-cumulated.
ColumnKind = Literal["period", "point_in_time"]


@dataclass
class Column:
    """One numeric column of a statement, as its printed header describes it."""

    index: int
    header: str
    kind: ColumnKind = "period"
    #: Months the column accumulates. 3 = standalone quarter, 6/9/12 = cumulative.
    #: None for a point-in-time column, or when the header could not be read.
    months: int | None = None
    #: ISO date the column ends on, when the header states or implies one.
    end: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StatementRow:
    """A captioned line of a statement, with one value per column where possible."""

    caption: str
    values: list[float | None]
    raw: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Statement:
    page: int
    kind: StatementKind
    title: str
    heading: str
    #: The note number, for a table extracted from the notes rather than from one
    #: of the primary statements. Addressed from a mapping as "note:10".
    note: int | None = None
    columns: list[Column] = field(default_factory=list)
    rows: list[StatementRow] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "kind": self.kind,
            "note": self.note,
            "title": self.title,
            "heading": self.heading,
            "columns": [c.to_dict() for c in self.columns],
            "rows": [r.to_dict() for r in self.rows],
        }


@dataclass
class Page:
    page: int
    source: TextSource
    chars: int
    text: str
    tables: int = 0

    def to_dict(self, include_text: bool = True) -> dict[str, Any]:
        d = {"page": self.page, "source": self.source,
             "chars": self.chars, "tables": self.tables}
        if include_text:
            d["text"] = self.text
        return d


@dataclass
class Issue:
    """A problem stage 1 found. `blocking` means no later stage can compensate."""

    code: str
    severity: Literal["error", "warning", "info"]
    message: str
    page: int | None = None

    @property
    def blocking(self) -> bool:
        return self.severity == "error"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PdfDoc:
    path: str
    sha256: str
    pages: list[Page] = field(default_factory=list)
    statements: list[Statement] = field(default_factory=list)
    units: str | None = None
    currency: str | None = None
    ocr_pages: list[int] = field(default_factory=list)
    ocr_engine: str | None = None
    issues: list[Issue] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION
    parser_version: str = PARSER_VERSION

    def statement(self, kind: StatementKind) -> Statement | None:
        """The first statement of a kind, which is what stage 3 asks for by name."""
        return next((s for s in self.statements if s.kind == kind), None)

    def note(self, number: int, end: str | None = None) -> Statement | None:
        """A note table, optionally the one for a particular period end.

        A note block yields one statement per period it reports, so the end date is
        what distinguishes "Total Assets as at 30 June 2026" from the same caption
        at 31 December 2025 — captions that are identical and figures that are not.
        """
        for statement in self.statements:
            if statement.note != number:
                continue
            if end is None or statement.heading == end:
                return statement
        return None

    def spans_by_end(self) -> dict[str, int]:
        """end date -> the longest span any primary statement reports to that date.

        A note reports "the period then ended", and the primary statements already
        say how long that period is. Longest wins: an interim note's figures are
        cumulative, matching the six-month column rather than the quarter.
        """
        spans: dict[str, int] = {}
        for statement in self.statements:
            if statement.note is not None:
                continue
            for column in statement.columns:
                if column.end and column.months:
                    spans[column.end] = max(spans.get(column.end, 0), column.months)
        return spans

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    def to_dict(self, include_page_text: bool = True) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "parser_version": self.parser_version,
            "path": self.path,
            "sha256": self.sha256,
            "units": self.units,
            "currency": self.currency,
            "ocr": {"pages": self.ocr_pages, "engine": self.ocr_engine},
            "issues": [i.to_dict() for i in self.issues],
            "statements": [s.to_dict() for s in self.statements],
            "pages": [p.to_dict(include_page_text) for p in self.pages],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PdfDoc":
        ocr = d.get("ocr") or {}
        return cls(
            path=d["path"],
            sha256=d["sha256"],
            units=d.get("units"),
            currency=d.get("currency"),
            ocr_pages=ocr.get("pages") or [],
            ocr_engine=ocr.get("engine"),
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            parser_version=d.get("parser_version", ""),
            issues=[Issue(**i) for i in d.get("issues", [])],
            pages=[Page(**p) for p in d.get("pages", [])],
            statements=[
                Statement(
                    page=s["page"], kind=s["kind"], title=s["title"],
                    heading=s.get("heading", ""), note=s.get("note"),
                    columns=[Column(**c) for c in s.get("columns", [])],
                    rows=[StatementRow(**r) for r in s.get("rows", [])],
                )
                for s in d.get("statements", [])
            ],
        )
