"""Propose a model.json from a workbook, for a human to review.

This is the one place in v2 where guessing is allowed, because a person checks
the result before it is frozen. Every guess is recorded with the reason it was
made, and anything uncertain is marked `review` so the analyst reads that row
rather than the whole sheet.

Two passes. The first reads only the workbook: what each row IS — an input, a
formula, a heading; which statement it belongs to; whether it is a flow or a
balance. The second, when a pdf.json is supplied, binds each input row to the
caption the filing actually printed (`model/propose_captions.py`), deterministically
where possible and with a model only for what is left.

That split follows the two sources of truth: the workbook owns the LABEL, because
it is the workbook being written into, and the filing owns the VALUE. The map is
the one recorded correspondence between them — worked out here, confirmed by a
person, then read deterministically every quarter.
"""
from __future__ import annotations

import re

from finscan2.model.discover import SheetLayout, discover_sheet, fingerprint
from finscan2.model.schema import ModelMap, RowKey, RowSpec, normalize_label

#: Sections whose rows are flows over a period, and those that are balances.
_BASIS_BY_SECTION = {
    "income_statement": "quarter",
    "cash_flow": "quarter",
    "balance_sheet": "point_in_time",
}

_STATEMENT_BY_SECTION = {
    "income_statement": "income_statement",
    "cash_flow": "cash_flow",
    "balance_sheet": "balance_sheet",
}

#: Captions that are headings or derived ratios: no filing prints a value for
#: them, and asking for one invites an invented number.
#:
#: Matched on WHOLE WORDS of the normalized caption, never as substrings. A
#: substring test silently ate "General and Administration Expenses", because
#: "administ-ratio-n" contains "ratio" — a real expense line dropped out of the
#: map with a reason that read as if it had been considered.
_SKIP_WORDS = (
    "margin", "margins", "growth", "ratio", "ratios", "check", "checks",
    "yoy", "y o y", "qoq", "q o q", "per share change",
    "as of net sales", "as percent", "as a percent", "as of revenue",
    # A multiples caption such as "EV/EBITDA (x)" normalizes to "ev ebitda x".
    "x",
)

#: Captions whose figure legitimately changes direction between quarters. A sign
#: convention must NEVER be inferred for these from one prior period: Almarai's
#: "Other (Expenses) / Income, net" printed (30,302) one quarter and 11,991 the
#: next, and a row frozen as "negative" would have flipped the income quarter to
#: -11,991 — a wrong number with a plausible provenance comment.
_BIDIRECTIONAL_WORDS = (
    "fx", "foreign exchange", "exchange", "currency", "valuation", "fair value",
    "remeasurement", "revaluation", "impairment reversal", "hedge", "hedging",
    "derivative", "mark to market", "translation",
)

#: Word pairs that put both directions in one caption ("(Expenses) / Income").
_BIDIRECTIONAL_PAIRS = (
    ("income", "expense"), ("income", "expenses"), ("gain", "loss"),
    ("gains", "losses"), ("profit", "loss"), ("charge", "credit"),
    ("inflow", "outflow"), ("surplus", "deficit"),
    # An impairment reverses: the loss is negative, the reversal positive.
    ("loss", "reversal"), ("losses", "reversals"), ("loss", "recovery"),
    ("loss", "writeback"), ("charge", "reversal"),
)

#: Tested against the RAW caption, because `normalize_label` strips punctuation
#: and these would never survive to be matched as words.
_SKIP_RAW = ("%",)

_SKIP_RE = re.compile(
    "|".join(rf"\b{re.escape(word).replace(chr(92) + ' ', chr(92) + 's+')}\b"
             for word in _SKIP_WORDS)
)


def _alias_table() -> dict[str, str]:
    """Normalized alias -> canonical field, from v1's taxonomy.

    Reused rather than re-typed: it is a data table that took real filings to
    build, and v2's map is what decides whether any given match is kept.
    """
    try:
        from finscan.schemas import FIELD_ALIASES
    except ImportError:      # pragma: no cover - v1 not importable
        return {}
    table: dict[str, str] = {}
    for field_id, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            table.setdefault(normalize_label(alias), field_id)
    return table


def _looks_derived(label: str) -> bool:
    """True for a caption no filing prints a value for.

    Word-boundary matching, so an ordinary expense line is never mistaken for a
    ratio because a ratio word happens to sit inside one of its words.
    """
    if any(token in (label or "") for token in _SKIP_RAW):
        return True
    return bool(_SKIP_RE.search(normalize_label(label)))


def _is_bidirectional(label: str) -> bool:
    """True when the caption itself says the figure can go either way."""
    normalized = normalize_label(label)
    words = set(normalized.split())
    if any(word in normalized for word in _BIDIRECTIONAL_WORDS):
        return True
    return any(a in words and b in words for a, b in _BIDIRECTIONAL_PAIRS)


def propose(layout: SheetLayout, company: str) -> ModelMap:
    """Turn a discovered layout into a draft map."""
    aliases = _alias_table()
    model = ModelMap(
        company=company,
        sheet=layout.sheet,
        fingerprint=fingerprint(layout),
        units=layout.units,
        cadence_months=layout.cadence_months,
        label_col=layout.label_col,
        header_row=layout.header_row,
        first_data_row=layout.first_data_row,
        reference_col=layout.reference_col,
        write_col=layout.write_col,
        write_mode=layout.write_mode,
        period_dates=dict(layout.period_dates),
        period_date_row=layout.period_date_row,
    )

    seen: dict[tuple[str, str], int] = {}
    for row in layout.rows:
        normalized = normalize_label(row.label)
        occurrence = seen.get((normalized, row.section), 0) + 1
        seen[(normalized, row.section)] = occurrence

        spec = RowSpec(
            key=RowKey(label=row.label, section=row.section, occurrence=occurrence),
            row_hint=row.row,
        )

        if row.has_formula:
            spec.kind = "formula"
            model.rows.append(spec)
            continue

        if _looks_derived(row.label):
            spec.kind, spec.review = "skip", "looks like a heading or a derived ratio"
            model.rows.append(spec)
            continue

        if row.role != "input":
            # Green/red linked cells and anything the colour probe could not place.
            spec.kind = "skip"
            spec.review = f"reference cell is '{row.role}', not a writable input"
            model.rows.append(spec)
            continue

        spec.kind = "input"
        spec.statement = _STATEMENT_BY_SECTION.get(row.section)
        spec.basis = _BASIS_BY_SECTION.get(row.section)
        bidirectional = _is_bidirectional(row.label)
        if row.reference_value is not None and row.reference_value < 0 and not bidirectional:
            spec.sign = "negative"

        canonical = aliases.get(normalized)
        if canonical:
            spec.resolve = f"field:{canonical}"
        else:
            spec.resolve = f"pdf:{row.label}"
            spec.review = ("no canonical field matched this caption; it will be looked "
                           "up in the filing by its own text")

        if row.literal_expression:
            # The analyst was hand-summing components into this cell. Say so, with
            # the expression, because it names the parts a `sum:` should read.
            spec.review = ((spec.review or "") +
                           f" · the reference cell is a hand-typed expression "
                           f"'{row.literal_expression}', not a calculation: it is an "
                           f"input whose parts were added in place — consider "
                           f"sum:<a>|<b>").strip(" ·")

        if bidirectional:
            spec.review = ((spec.review or "") +
                           " · caption is bidirectional, so no sign convention was "
                           "inferred; the filing's own sign is kept").strip(" ·")

        if spec.section_is_unmapped():
            spec.review = (spec.review or "") + " · section could not be determined"

        model.rows.append(spec)

    return model


def learn(path: str, sheet: str, company: str, pdf_json: str | None = None,
          use_llm: bool = True) -> tuple[ModelMap, SheetLayout, dict[str, int]]:
    """Propose a map, and — when a pdf.json is supplied — bind each row to the
    caption the filing actually printed.

    The workbook is the source of truth for the LABEL, because it is the workbook
    being written into. The filing is the source of truth for the VALUE. The map
    is the single recorded correspondence between them, worked out once here and
    confirmed by a person, so that every quarterly run is a deterministic lookup.
    """
    layout = discover_sheet(path, sheet)
    model = propose(layout, company)
    if not pdf_json:
        return model, layout, {}

    import json

    from finscan2.model import propose_captions
    from finscan2.schema import PdfDoc

    doc = PdfDoc.from_dict(json.loads(_read(pdf_json)))
    proposals = propose_captions.deterministic(model, doc)
    if use_llm:
        proposals = propose_captions.with_model(proposals, doc)
    return model, layout, propose_captions.apply(model, proposals)


def _read(path: str) -> str:
    from pathlib import Path

    return Path(path).read_text(encoding="utf-8")


def review_rows(model: ModelMap) -> list[RowSpec]:
    """Rows an analyst must look at before the map is frozen."""
    return [r for r in model.rows if r.review]


# Small helper hung off RowSpec so `propose` reads as a sequence of decisions.
def _section_is_unmapped(self: RowSpec) -> bool:
    return self.kind == "input" and self.statement is None


RowSpec.section_is_unmapped = _section_is_unmapped  # type: ignore[attr-defined]
