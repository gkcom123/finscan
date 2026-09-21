"""Record, in the map, the caption each row actually matched in the filing.

`learn` reads the workbook and never opens a PDF, so a `pdf:` instruction can
only ever carry the WORKBOOK's caption — the text to search the filing for. After
a run has matched a row, the filing's own wording is known, and writing it back
turns next quarter's lookup into an exact match.

Deliberately a separate command, not a side effect of `match`. A stage that
rewrites its own configuration while producing numbers is a stage whose output
cannot be reproduced from its inputs, and every guard in this pipeline rests on
being able to re-run a quarter and get the same answer.

The recorded caption is a hint, not a requirement: `_lookup` tries it first and
falls through to the ordinary tiers when a filing rewords its lines.
"""
from __future__ import annotations

from dataclasses import dataclass

from finscan2.match.schema import Values
from finscan2.model.schema import ModelMap, normalize_label


@dataclass
class Binding:
    row: int
    label: str
    was: str | None
    now: str


def bindings(values: Values, model: ModelMap) -> list[Binding]:
    """Captions worth recording: matched, and not already what the map says."""
    by_row = {spec.row_hint: spec for spec in model.rows}
    out: list[Binding] = []

    for value in values.values:
        if not value.ok or value.source is None:
            continue
        spec = by_row.get(value.row)
        if spec is None:
            continue

        printed = value.source.caption
        if not printed:
            continue

        # Nothing to record when the workbook's own caption already matches
        # exactly — the extra field would be noise in every row of the map.
        wanted = (spec.resolve or "").split(":", 1)[-1] if spec.resolve else ""
        if normalize_label(printed) == normalize_label(wanted):
            continue
        if spec.pdf_caption and normalize_label(spec.pdf_caption) == normalize_label(printed):
            continue

        out.append(Binding(row=value.row, label=spec.key.label,
                           was=spec.pdf_caption, now=printed))
    return out


def apply(values: Values, model: ModelMap) -> list[Binding]:
    """Write the matched captions into the map. Returns what changed."""
    changed = bindings(values, model)
    by_row = {spec.row_hint: spec for spec in model.rows}
    for binding in changed:
        by_row[binding.row].pdf_caption = binding.now
    return changed
