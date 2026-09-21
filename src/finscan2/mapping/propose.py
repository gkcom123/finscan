"""Build a mapping proposal from a workbook and a filing.

This is `learn`'s output: the lean rows of `mappings/<company>.json`, each carrying
how it was matched so a person knows which ones to read. It reuses the matching
already built and tested — deterministic tiers first, a constrained model pass for
the remainder — and converts the result into the human format.

An existing mapping is never overwritten. The proposal is written beside it and the
rows whose decision CHANGED are printed, because that short list is the review.
"""
from __future__ import annotations

from dataclasses import dataclass

from finscan2.mapping.schema import Mapping, MappingRow
from finscan2.model import propose_captions
from finscan2.model.discover import SheetLayout
from finscan2.model.learn import propose as propose_model
from finscan2.model.schema import normalize_label
from finscan2.schema import PdfDoc


@dataclass
class Change:
    label: str
    section: str
    was: object
    now: object

    @property
    def kind(self) -> str:
        if self.was is None and self.now is not None:
            return "added"
        if self.now is None and self.was is not None:
            return "cleared"
        return "changed"


def propose_mapping(layout: SheetLayout, company: str,
                    doc: PdfDoc | None = None,
                    use_llm: bool = True) -> tuple[Mapping, dict[str, int]]:
    """Propose the human mapping for one workbook against one filing."""
    model = propose_model(layout, company)
    counts: dict[str, int] = {}

    captions: dict[int, str | None] = {}
    statements: dict[int, str] = {}
    notes: dict[int, str] = {}
    if doc is not None:
        proposals = propose_captions.deterministic(model, doc)
        if use_llm:
            proposals = propose_captions.with_model(proposals, doc)
        counts = propose_captions.apply(model, proposals)
        for proposal in proposals:
            captions[proposal.row] = proposal.caption
            if proposal.method in ("note", "restated"):
                statements[proposal.row] = proposal.statement
            if proposal.caption is None:
                notes[proposal.row] = _short(proposal.why)

    mapping = Mapping(company=company, sheet=layout.sheet)
    for spec in model.rows:
        if spec.kind != "input":
            continue
        row = MappingRow(
            label=spec.key.label,
            section=spec.key.section,
            occurrence=spec.key.occurrence,
            pdf=captions.get(spec.row_hint),
            statement=statements.get(spec.row_hint),
        )
        # A literal expression in the reference cell names the components a sum
        # should read, so it travels into the note where a human will see it.
        if spec.review and "hand-typed expression" in spec.review:
            row.note = _expression_note(spec.review)
        elif row.pdf is None:
            row.note = notes.get(spec.row_hint, "no matching line found in the filing")
        mapping.rows.append(row)

    return mapping, counts


def _short(why: str, limit: int = 120) -> str:
    text = " ".join((why or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _expression_note(review: str) -> str:
    expression = review.split("'")[1] if "'" in review else ""
    return (f"the reference cell was the hand-typed expression '{expression}' — "
            f"name its components as a list to make this a sum")


def changes(old: Mapping, new: Mapping) -> list[Change]:
    """Decisions that differ. This list is what a person reviews."""
    def index(mapping: Mapping):
        return {(normalize_label(r.label), r.section, r.occurrence): r
                for r in mapping.rows}

    before, after = index(old), index(new)
    out: list[Change] = []
    for key in sorted(set(before) | set(after)):
        was = before.get(key)
        now = after.get(key)
        was_pdf = was.pdf if was else None
        now_pdf = now.pdf if now else None
        if was_pdf == now_pdf:
            continue
        # A proposal that found nothing must never look like a decision to clear a
        # caption a human wrote: those are reported, not applied.
        label = (now or was).label
        section = (now or was).section
        out.append(Change(label=label, section=section, was=was_pdf, now=now_pdf))
    return out
