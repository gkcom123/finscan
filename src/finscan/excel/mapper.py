"""Map the workbook's line-item rows onto canonical fields.

Three passes, cheapest first:
  1. alias  - normalised exact hit against the curated alias vocabulary
  2. fuzzy  - rapidfuzz token-set ratio above threshold
  3. llm    - only the rows the first two passes could not resolve

Then a global de-duplication pass guarantees a canonical field is written to at
most one row: if two rows both claim `profit_after_tax`, the higher-scoring row
keeps it and the other is reported as ambiguous instead of being filled twice.
"""
from __future__ import annotations

import re
import unicodedata

from finscan.schemas import FIELD_ALIASES, FIELD_LABELS, Issue, RowMapping

try:  # pragma: no cover - optional accelerator
    from rapidfuzz import fuzz

    def _ratio(a: str, b: str) -> float:
        # token_sort, not token_set: token_set scores a subset as a perfect match,
        # so the section heading "Expenses" would score 100 against "Other expenses".
        return float(fuzz.token_sort_ratio(a, b))

except ImportError:  # pragma: no cover
    from difflib import SequenceMatcher

    def _ratio(a: str, b: str) -> float:
        return SequenceMatcher(None, " ".join(sorted(a.split())),
                               " ".join(sorted(b.split()))).ratio() * 100.0


#: Bare section headings. Blocked from *fuzzy* matching only — an exact alias hit
#: (e.g. a sheet whose revenue row is literally captioned "Revenue") still wins.
HEADING_STOPWORDS = {
    "expenses", "expenditure", "income", "total", "particulars", "notes", "note",
    "continuing operations", "discontinued operations",
}


_NOISE = re.compile(r"\(.*?\)|\[.*?\]|[^a-z0-9 ]+")
_NUMBERING = re.compile(r"^\s*(?:[ivxlcdm]+|\d+)\s*[.)\-]\s*", re.IGNORECASE)


def normalize_label(text: str) -> str:
    s = unicodedata.normalize("NFKD", text or "").lower()
    s = _NUMBERING.sub("", s)
    s = s.replace("&", " and ").replace("/", " ")
    s = _NOISE.sub(" ", s)
    s = re.sub(r"\b(rs|inr|usd|expense|expenses|total)\b", r"\1", s)
    return re.sub(r"\s+", " ", s).strip()


_ALIAS_INDEX: dict[str, str] = {}
for _fid, _aliases in FIELD_ALIASES.items():
    for _a in [*_aliases, FIELD_LABELS[_fid]]:
        _ALIAS_INDEX.setdefault(normalize_label(_a), _fid)


_PAREN = re.compile(r"\(([^)]{2,30})\)")


def _alias_match(label: str, allowed_fields: set[str] | None = None) -> str | None:
    """Exact alias hit, with one refinement: when a caption carries a
    parenthetical that is itself a recognised term — "Operating Profit (EBITDA)",
    "Profit (PAT)" — the author is disambiguating on purpose, so that wins over
    the surrounding words. Without this, stripping brackets turns an explicit
    EBITDA row into an EBIT row."""
    def _ok(fid: str | None) -> str | None:
        return fid if fid and (allowed_fields is None or fid in allowed_fields) else None

    for inner in _PAREN.findall(label or ""):
        hit = _ok(_ALIAS_INDEX.get(normalize_label(inner)))
        if hit:
            return hit
    return _ok(_ALIAS_INDEX.get(normalize_label(label)))


def _fuzzy_match(
    label: str, threshold: int, allowed_fields: set[str] | None = None
) -> tuple[str | None, float]:
    norm = normalize_label(label)
    if not norm or norm in HEADING_STOPWORDS:
        return None, 0.0
    best_fid, best_score = None, 0.0
    for alias_norm, fid in _ALIAS_INDEX.items():
        if allowed_fields is not None and fid not in allowed_fields:
            continue
        score = _ratio(norm, alias_norm)
        if score > best_score:
            best_fid, best_score = fid, score
    return (best_fid, best_score) if best_score >= threshold else (None, best_score)


LLM_SYSTEM = """You align spreadsheet row captions to canonical financial fields.

For each numbered caption return the canonical field id it means, or null when it
is a heading, a blank spacer, a segment/geography breakdown, a balance-sheet
line, or anything not in the list. Cash-flow captions may only match the
cash-flow fields (total_before_working_capital_changes,
net_cash_from_operating_activities, change_in_working_capital) — never map a
cash-flow adjustment line (e.g. depreciation added back) to a P&L field.
Never force a match: a wrong
alignment corrupts the client's model, a null is simply reviewed by a human.

Canonical fields:
{taxonomy}
"""


def _llm_match(labels: dict[int, str]) -> dict[int, tuple[str | None, float]]:
    """Resolve leftover captions with the LLM. Returns {row: (field|None, confidence)}."""
    if not labels:
        return {}
    from pydantic import BaseModel, Field

    from finscan.llm.factory import structured

    class _One(BaseModel):
        row: int
        field: str | None = Field(default=None)
        confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    class _Batch(BaseModel):
        matches: list[_One]

    taxonomy = "\n".join(f"- {fid}: {lab}" for fid, lab in FIELD_LABELS.items())
    listing = "\n".join(f"{row}. {text}" for row, text in sorted(labels.items()))
    result = structured(_Batch).invoke(
        [
            {"role": "system", "content": LLM_SYSTEM.format(taxonomy=taxonomy)},
            {"role": "user", "content": f"Captions:\n{listing}"},
        ]
    )
    valid = set(FIELD_LABELS)
    return {
        m.row: ((m.field if m.field in valid else None), m.confidence) for m in result.matches
    }


def llm_match_labels(labels: list[str]) -> dict[str, tuple[str | None, float]]:
    """Resolve a batch of unusual captions by text, independent of row numbers.

    Deduplicated across sheets so the same odd caption is only ever asked once.
    """
    indexed = dict(enumerate(sorted(set(labels))))
    resolved = _llm_match(indexed)
    return {indexed[i]: v for i, v in resolved.items() if i in indexed}


def map_rows(
    row_labels: dict[int, str],
    fuzzy_threshold: int = 86,
    use_llm: bool = True,
    allowed_fields: set[str] | None = None,
) -> tuple[list[RowMapping], list[Issue]]:
    issues: list[Issue] = []
    mappings: list[RowMapping] = []
    leftovers: dict[int, str] = {}

    for row, label in sorted(row_labels.items()):
        fid = _alias_match(label, allowed_fields)
        if fid:
            mappings.append(
                RowMapping(excel_row=row, excel_label=label, field=fid,
                           match_method="alias", match_score=100.0, confidence=1.0)
            )
            continue
        fid, score = _fuzzy_match(label, fuzzy_threshold, allowed_fields)
        if fid:
            mappings.append(
                RowMapping(excel_row=row, excel_label=label, field=fid,
                           match_method="fuzzy", match_score=score, confidence=score / 100.0)
            )
            continue
        leftovers[row] = label

    if use_llm and leftovers:
        try:
            for row, (fid, conf) in _llm_match(leftovers).items():
                if fid and allowed_fields is not None and fid not in allowed_fields:
                    fid = None
                if fid:
                    mappings.append(
                        RowMapping(excel_row=row, excel_label=leftovers.pop(row, ""),
                                   field=fid, match_method="llm",
                                   match_score=conf * 100.0, confidence=conf)
                    )
        except Exception as exc:  # LLM unavailable -> degrade, don't crash
            issues.append(
                Issue(severity="warning", code="llm_mapping_failed",
                      message=f"LLM row matching unavailable ({exc}); alias+fuzzy results kept.")
            )

    for row, label in leftovers.items():
        mappings.append(
            RowMapping(excel_row=row, excel_label=label, field=None, match_method="unmatched")
        )

    mappings.sort(key=lambda m: m.excel_row)
    mappings, dedup_issues = _dedupe(mappings)
    return mappings, issues + dedup_issues


def _dedupe(mappings: list[RowMapping]) -> tuple[list[RowMapping], list[Issue]]:
    issues: list[Issue] = []
    best: dict[str, RowMapping] = {}
    for m in mappings:
        if not m.field:
            continue
        cur = best.get(m.field)
        if cur is None or m.match_score > cur.match_score:
            if cur is not None:
                _demote(cur, m, issues)
            best[m.field] = m
        else:
            _demote(m, cur, issues)
    return mappings, issues


def _demote(loser: RowMapping, winner: RowMapping, issues: list[Issue]) -> None:
    issues.append(
        Issue(
            severity="warning",
            code="ambiguous_row",
            field=loser.field,
            message=(
                f"Row {loser.excel_row} ('{loser.excel_label}') and row {winner.excel_row} "
                f"('{winner.excel_label}') both matched '{loser.field}'. "
                f"Row {winner.excel_row} kept; row {loser.excel_row} left blank for review."
            ),
        )
    )
    loser.field = None
    loser.match_method = "unmatched"
    loser.match_score = 0.0
    loser.confidence = 0.0
