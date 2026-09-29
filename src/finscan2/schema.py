"""The pdf.json contract — stage 1's only output.

Everything here is a plain dataclass with a `to_dict`, because pdf.json is a file
other stages read, not an object they import. Stage 3 must be runnable from a
pdf.json produced weeks earlier by a different version of this code, so the JSON
shape is the interface and these classes are only a convenience for producing it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
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
PARSER_VERSION = "16"        # 4: a lone "-" is kept as a None placeholder, at its
                            #    printed column position, instead of being dropped
                            #    (which silently shifted every later column left)
                            # 5: tiered headers (band row over label row) read by
                            #    x-overlap; short rows slotted by cell position
                            # 6: units read only from statements' table sections
                            # 7: ISO date-range headers (BMV/XBRL reports)
                            # 8: scale only from columned tables; XBRL rounding
                            # 9: lines printing ISO dates are headers, not rows
                            # 10: rounding proof tolerates counts (95%)
                            # 11: continuation pages; BMV +/- caption markers
                            # 12: leading sign marker is caption in the layout pass
                            # 13: rendered-table rows the text layer already read are dropped
                            # 14: (reverted) image-page OCR
                            # 15: first body row from geometry; row headings kept
                            # 16: headings survive blank-cell slotting

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
    #: A caption-only line printed directly above this row ("OTHER SUBSIDIARIES
    #: &" over "Sales Volume (18) (21)"): the name of the block the row opens.
    heading: str = ""

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
    #: The scale THIS table's figures are printed in, when it differs from the
    #: document's. Filled when documents are merged: Gruma's BMV report prints in
    #: whole dollars and its press release in millions, and one quarter reads both.
    units: str | None = None
    #: The file the table came from, when a quarter reads more than one.
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "kind": self.kind,
            "note": self.note,
            "units": self.units,
            "source": self.source,
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
        """The first statement of a kind, which is what stage 3 asks for by name.

        A combined "Statement of Income and Other Comprehensive Income" is filed
        under `comprehensive_income` (statements.py checks that title pattern
        first). It is a superset — the income statement's own captions, then OCI
        appended below, never the reverse — so an `income_statement` request
        accepts it when no page was classified as `income_statement` outright.
        """
        # A page matched by title but with no readable columns — a table of
        # contents, a one-line informative block — can give no value, so it never
        # stands in for the real statement (Gruma's BMV report opens with one).
        for wanted in [kind] + (["comprehensive_income"] if kind == "income_statement" else []):
            of_kind = [s for s in self.statements if s.kind == wanted]
            found = next((s for s in of_kind if s.columns), None) or next(iter(of_kind), None)
            if found is not None:
                return found
        return None

    def titled(self, text: str) -> Statement | None:
        """The first primary statement whose title contains `text`.

        For filings that print several tables of one kind, where "the first
        income statement" is the wrong one: Gruma's BMV report tags each with
        an XBRL code ("[310000] Statement of comprehensive income"), which is
        stable from quarter to quarter where page numbers are not.
        """
        needle = text.strip().lower()
        return next((s for s in self.statements
                     if s.note is None and needle in (s.title or "").lower()), None)

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

    @classmethod
    def merge(cls, docs: list["PdfDoc"]) -> "PdfDoc":
        """Several filings for one quarter, read as one.

        Each table keeps the scale of the document it came from, and records that
        document, so a figure is always scaled — and traced — by its own source.
        The document with the most readable primary statements leads: it is the
        one a row naming only its section ("balance_sheet") is meant to read, and
        the order must not depend on file names.
        """
        if len(docs) == 1:
            return docs[0]

        def weight(doc: "PdfDoc") -> tuple:
            return (sum(1 for s in doc.statements if s.note is None and s.columns),
                    doc.path)

        ordered = sorted(docs, key=weight, reverse=True)
        lead = ordered[0]
        merged = cls(path=" + ".join(d.path for d in ordered),
                     sha256=" + ".join(d.sha256 for d in ordered),
                     units=lead.units, currency=lead.currency)
        for doc in ordered:
            name = Path(doc.path).name
            for statement in doc.statements:
                statement.units = statement.units or doc.units
                statement.source = statement.source or name
                merged.statements.append(statement)
            merged.pages.extend(doc.pages)
            merged.ocr_pages.extend(doc.ocr_pages)
            merged.issues.extend(
                Issue(code=i.code, severity=i.severity, page=i.page,
                      message=f"[{name}] {i.message}") for i in doc.issues)
        return merged

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
                    units=s.get("units"), source=s.get("source"),
                    columns=[Column(**c) for c in s.get("columns", [])],
                    rows=[StatementRow(**r) for r in s.get("rows", [])],
                )
                for s in d.get("statements", [])
            ],
        )
