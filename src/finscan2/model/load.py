"""Load a map and bind its rows to the workbook's actual rows.

Every key must resolve to exactly one row. Ambiguous, missing or moved rows are
reported here, before anything is read from the filing and long before anything
is written — a caption renamed since the map was frozen should stop the run and
name itself, not quietly become a guessed value.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from finscan2.model.discover import SheetLayout, discover_sheet, fingerprint
from finscan2.model.schema import ModelMap, RowSpec, normalize_label
from finscan2.schema import Issue


@dataclass
class BoundRow:
    """A map row and the workbook row it resolved to."""

    spec: RowSpec
    row: int
    moved_from: int | None = None


@dataclass
class ResolvedMap:
    model: ModelMap
    layout: SheetLayout
    bound: list[BoundRow] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def row_for(self, label: str, section: str = "", occurrence: int = 1) -> int | None:
        target = (normalize_label(label), section, occurrence)
        for bound in self.bound:
            if bound.spec.key.normalized() == target:
                return bound.row
        return None


def _closest(normalized: str, candidates: list[str]) -> str | None:
    """The nearest caption in the sheet, so a rename suggests its own fix."""
    from difflib import get_close_matches

    matches = get_close_matches(normalized, candidates, n=1, cutoff=0.6)
    return matches[0] if matches else None


def resolve(model: ModelMap, layout: SheetLayout) -> ResolvedMap:
    """Bind every map row to a workbook row, or say precisely why it could not."""
    resolved = ResolvedMap(model=model, layout=layout)

    # Index the sheet by the same key the map uses.
    index: dict[tuple[str, str, int], int] = {}
    seen: dict[tuple[str, str], int] = {}
    all_labels: list[str] = []
    for row in layout.rows:
        normalized = normalize_label(row.label)
        all_labels.append(normalized)
        occurrence = seen.get((normalized, row.section), 0) + 1
        seen[(normalized, row.section)] = occurrence
        index[(normalized, row.section, occurrence)] = row.row

    current = fingerprint(layout)
    if model.fingerprint and model.fingerprint != current:
        resolved.issues.append(Issue(
            code="fingerprint_drift", severity="warning",
            message=f"The sheet's captions changed since this map was confirmed "
                    f"({model.fingerprint} -> {current}). Rows still resolving are "
                    f"used; check the review report before trusting the run."))

    if not model.confirmed:
        resolved.issues.append(Issue(
            code="map_unconfirmed", severity="warning",
            message=f"The map for '{model.company}' has not been confirmed by a "
                    f"reviewer. Run `finscan2 confirm` once its rows are checked."))

    for spec in model.rows:
        if spec.kind == "skip":
            continue
        key = spec.key.normalized()
        row = index.get(key)

        if row is None:
            # Same caption and section, different occurrence, is a repeat count
            # that changed — worth saying so rather than "missing".
            repeats = [n for (label, section, n) in index
                       if (label, section) == (key[0], key[1])]
            if repeats:
                resolved.issues.append(Issue(
                    code="map_key_ambiguous", severity="error",
                    message=f"'{spec.key.label}' ({spec.key.section}) is mapped at "
                            f"occurrence {spec.key.occurrence}, but the sheet now has "
                            f"{len(repeats)}. Re-confirm which line is meant."))
            else:
                hint = _closest(key[0], all_labels)
                suggestion = f" Closest caption in the sheet: '{hint}'." if hint else ""
                resolved.issues.append(Issue(
                    code="map_key_missing", severity="error",
                    message=f"'{spec.key.label}' ({spec.key.section}) is in the map but "
                            f"not in the sheet.{suggestion}"))
            continue

        moved = row if spec.row_hint and row != spec.row_hint else None
        if moved is not None:
            resolved.issues.append(Issue(
                code="map_row_moved", severity="info",
                message=f"'{spec.key.label}' moved from row {spec.row_hint} to {row}."))
        resolved.bound.append(BoundRow(spec=spec, row=row, moved_from=spec.row_hint if moved else None))

    unmapped = _unmapped_inputs(model, layout)
    if unmapped:
        preview = ", ".join(f"r{r.row}:{r.label}" for r in unmapped[:6])
        resolved.issues.append(Issue(
            code="sheet_row_unmapped", severity="error",
            message=f"{len(unmapped)} writable row(s) in the sheet are not in the map: "
                    f"{preview}{' ...' if len(unmapped) > 6 else ''}. Re-run learn, or "
                    f"add them explicitly as 'skip' if they are not written."))

    return resolved


def _unmapped_inputs(model: ModelMap, layout: SheetLayout) -> list:
    """Writable sheet rows the map says nothing about.

    The map is the contract in both directions: a row it does not mention is not
    a row to guess at, it is a hole in the contract.
    """
    mapped = {(normalize_label(s.key.label), s.key.section) for s in model.rows}
    return [row for row in layout.rows
            if row.role == "input" and not row.has_formula
            and (normalize_label(row.label), row.section) not in mapped]


def load(map_path: str | Path, workbook: str | Path, sheet: str | None = None) -> ResolvedMap:
    """Load a map, discover the workbook, and bind one to the other."""
    model = ModelMap.load(map_path)
    layout = discover_sheet(str(workbook), sheet or model.sheet)
    return resolve(model, layout)
