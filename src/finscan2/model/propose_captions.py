"""Match an Excel caption to the caption the filing printed, at learn time.

The two sources of truth are fixed and different:

    the LABEL is the workbook's        — it is the workbook we write into
    the VALUE is the filing's          — it is the filing that reports the quarter

So the map's job is one correspondence per row: workbook caption -> filing
caption. This module works that correspondence out ONCE, while a human is
watching, and writes it into the map as `pdf_caption`.

Why here and not in the quarterly run: a model call is not reproducible. Asked
the same question twice it may answer differently, and a number whose provenance
is "the model thought so that time" cannot be audited. Doing it at learn time
makes the nondeterminism land in a JSON file an analyst reads and confirms,
after which every quarterly run is a deterministic lookup.

Order of work, cheapest and most certain first:

    1. the deterministic tiers already used at run time (exact, direction
       reading, containment, word inclusion)
    2. a model, ONLY for the rows those left unmatched, and only allowed to
       choose from the captions actually present in the right statement
    3. whatever is still unmatched is flagged, never guessed

Step 2's constraint is what makes it safe: the model picks from a list rather
than writing a caption, so it cannot invent a line the filing does not contain.
Every pick is still marked for review.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from finscan2.match.select import find_row
from finscan2.model.schema import ModelMap, normalize_label
from finscan2.schema import PdfDoc

#: Notes `propose` left on a row that describe the WORKBOOK cell, not the caption
#: match. The caption pass must not overwrite them: the hand-typed expression is
#: the most useful thing an analyst can see on those rows, because it names the
#: components a `sum:` should read.
_KEEP_IN_REVIEW = ("hand-typed expression", "bidirectional")


def _carried(spec) -> str:
    existing = spec.review or ""
    return " · ".join(part.strip() for part in existing.split(" · ")
                      if any(marker in part for marker in _KEEP_IN_REVIEW))


def _note(spec, *parts: str) -> None:
    """Set a row's review note, keeping any note about the workbook cell itself."""
    spec.review = " · ".join(p for p in (_carried(spec), *parts) if p) or None


#: How a row's caption was matched. Recorded so an analyst can read the map and
#: know which rows deserve their attention.
Method = str


@dataclass
class Proposal:
    row: int
    label: str
    statement: str
    caption: str | None = None
    method: Method = "unmatched"
    why: str = ""
    candidates: list[str] = field(default_factory=list)


def _statement_for(spec, doc: PdfDoc):
    wanted = spec.statement or (spec.key.section if spec.key.section != "other" else None)
    if not wanted:
        return None, "the map does not say which statement this row comes from"
    statement = doc.statement(wanted)
    if statement is None:
        kinds = ", ".join(sorted({s.kind for s in doc.statements})) or "none"
        return None, f"the filing has no {wanted} (found: {kinds})"
    return statement, ""


def _wanted_forms(spec) -> tuple[str, list[str]]:
    """What to search the filing with, and the alternative spellings to accept.

    A `field:` row must be searched with the field's ALIASES, not with its
    identifier. Searching for the bare id read "cost_of_revenue" as the caption
    "cost of revenue", which contains the word "revenue" — so containment matched
    Almarai's `Cost of Sales` row to the filing's `Revenue` line, and the map
    recorded a cost row pointing at revenue. Same lookup as the quarterly run, so
    what `learn` binds is what `match` would find.
    """
    from finscan2.match.select import field_aliases

    instruction = spec.resolve or ""
    if instruction.startswith("field:"):
        field_id = instruction.split(":", 1)[1]
        return field_id.replace("_", " "), field_aliases(field_id)
    if instruction.startswith("pdf:"):
        return instruction.split(":", 1)[1], []
    return spec.key.label, []


def _find_elsewhere(doc: PdfDoc, wanted: str, aliases: list[str], skip: str):
    """Another PRIMARY statement printing this caption, when exactly one does.

    Tried before the notes, and for a reason found in Almarai's own model: D&A is
    printed in both the cash flow statement and the segment note, and the two
    differ in SIGN. The cash flow adds it back, so it prints +1,256,307, which is
    the convention every model in this project stores. The segment note prints it as
    an expense, -1,256,307. Reaching for the note first would hand the model a
    figure of the wrong sign and require an override to undo it.
    """
    hits = []
    for statement in doc.statements:
        if statement.note is not None or statement.kind in (skip, "other"):
            continue
        row, _ = find_row(statement, wanted, aliases)
        if row is not None:
            hits.append((row, statement))

    if not hits:
        return None, None, ""
    if len(hits) > 1:
        kinds = ", ".join(sorted(s.kind for _, s in hits))
        return None, None, (f" · it is also printed in {kinds}, so which statement is "
                            f"wanted cannot be decided here")
    return hits[0][0], hits[0][1], ""


def _find_in_notes(doc: PdfDoc, wanted: str, aliases: list[str]):
    """The note table printing this caption, when exactly one note does.

    A note block yields one statement per period, so the same caption legitimately
    appears several times within ONE note — that is not ambiguity. Two different
    notes printing it is, and is refused.
    """
    hits: dict[int, tuple] = {}
    for statement in doc.statements:
        if statement.note is None:
            continue
        row, _ = find_row(statement, wanted, aliases)
        if row is not None:
            # Prefer the statement whose period span is known: a note period with no
            # span cannot be de-cumulated, so it is the less useful of the two.
            known = any(c.months for c in statement.columns)
            if statement.note not in hits or (known and not hits[statement.note][2]):
                hits[statement.note] = (row, statement, known)

    if not hits:
        return None, None, ""
    if len(hits) > 1:
        numbers = ", ".join(f"note {n}" for n in sorted(hits))
        return None, None, (f" · it is printed in {numbers}, so which one is wanted "
                            f"cannot be decided here")
    row, statement, _ = next(iter(hits.values()))
    return row, statement, ""


def deterministic(model: ModelMap, doc: PdfDoc) -> list[Proposal]:
    """Match every input row it can without a model call."""
    proposals: list[Proposal] = []
    for spec in model.rows:
        if spec.kind != "input":
            continue

        statement, problem = _statement_for(spec, doc)
        if statement is None:
            proposals.append(Proposal(row=spec.row_hint, label=spec.key.label,
                                      statement="", why=problem))
            continue

        wanted, aliases = _wanted_forms(spec)
        row, why = find_row(statement, wanted, aliases)

        if row is None:
            # The row's own statement does not print this line. Another primary
            # statement may — D&A is in the cash flow, not the P&L — and a primary
            # statement is preferred over a note, because its sign convention is the
            # one models are built around.
            other_row, other, other_why = _find_elsewhere(
                doc, wanted, aliases, statement.kind)
            if other_row is not None:
                proposals.append(Proposal(
                    row=spec.row_hint, label=spec.key.label, statement=other.kind,
                    caption=other_row.caption, method="restated",
                    why=f"printed in the {other.kind}, not the {statement.kind}"))
                continue

            # Then the notes.
            note_row, note_statement, note_why = _find_in_notes(doc, wanted, aliases)
            if note_row is not None:
                proposals.append(Proposal(
                    row=spec.row_hint, label=spec.key.label,
                    statement=f"note:{note_statement.note}", caption=note_row.caption,
                    method="note",
                    why=f"found in {note_statement.title} "
                        f"(period ending {note_statement.heading}); "
                        f"not printed in the {statement.kind}"))
                continue
            proposals.append(Proposal(
                row=spec.row_hint, label=spec.key.label, statement=statement.kind,
                why=f"{why}{other_why}{note_why}",
                candidates=[r.caption for r in statement.rows]))
            continue

        printed = normalize_label(row.caption)
        forms = {normalize_label(f) for f in (wanted, *aliases)}
        method = "exact" if printed in forms else "derived"
        proposals.append(Proposal(row=spec.row_hint, label=spec.key.label,
                                  statement=statement.kind, caption=row.caption,
                                  method=method, why=why))
    return proposals


class _Pick:
    """Declared lazily so importing this module never needs pydantic."""


def _pick_model():
    from pydantic import BaseModel, Field

    class Pick(BaseModel):
        excel_label: str = Field(description="The workbook caption, copied back verbatim")
        pdf_caption: str = Field(
            default="",
            description="EXACTLY one caption from the candidate list, or empty if "
                        "none of them is the same line. Never write a caption that "
                        "is not in the list.")
        reason: str = Field(default="", description="One short clause")

    class Picks(BaseModel):
        picks: list[Pick]

    return Picks


_PROMPT = """\
You are mapping rows of a financial model to the lines of a company's filing.

For each workbook caption below, choose the ONE caption from its candidate list
that reports the same financial line. The candidates are the captions actually
printed in the {statement} of this filing.

Rules:
- Answer with a caption copied EXACTLY from the candidate list, or "" if none of
  them is the same line.
- "" is the right answer whenever you are unsure. A wrong match writes a wrong
  number into a financial model; an empty answer only asks a human to look.
- Do not match a subtotal to a component, or a component to a subtotal. A line
  that is INCLUDED IN a candidate is not the same line as that candidate: if the
  filing combines it with other items, answer "".
- Judge whether the two captions name the SAME line in THIS filing. Where a figure
  is usually or typically reported is not a reason to match.
- A caption naming both directions ("(Loss) / Reversal", "(Expenses) / Income")
  is the same line as one naming a single direction.
- Do not match a line from a different statement, a different period, or a
  per-share figure to an absolute one.

{rows}
"""


def _format_rows(unmatched: list[Proposal], limit: int) -> str:
    blocks = []
    for proposal in unmatched:
        candidates = "\n".join(f"    - {c}" for c in proposal.candidates[:limit])
        blocks.append(f"  workbook caption: {proposal.label}\n"
                      f"  candidates:\n{candidates}")
    return "\n\n".join(blocks)


def with_model(proposals: list[Proposal], doc: PdfDoc, *, batch: int = 25,
               candidate_limit: int = 120) -> list[Proposal]:
    """Ask a model to place the rows the deterministic tiers could not.

    Two checks stand between the answer and the map, because prompt wording is not
    a guard:

    1. the caption must be one the filing actually prints — so a paraphrase or an
       invention changes nothing;
    2. the caption must not already be claimed by another row — which is what
       catches a COMPONENT being matched to the COMBINED line it sits inside.

    Check 2 exists because of a real answer: asked to place Almarai's "Exchange
    Gain, net", a model chose "Other (Expenses) / Income, net" and explained that
    "exchange gains are typically included in other income or expenses". That is
    true, and it is not the question — the filing has no separate exchange-gain
    line, and another row already reads the combined one. Honouring it would have
    written one filing figure into two model rows.
    """
    from finscan2.llm import structured

    unmatched = [p for p in proposals if p.caption is None and p.candidates]
    if not unmatched:
        return proposals

    # Captions already spoken for by a deterministic match, per statement.
    claimed: dict[str, dict[str, str]] = {}
    for proposal in proposals:
        if proposal.caption:
            claimed.setdefault(proposal.statement, {})[
                normalize_label(proposal.caption)] = proposal.label

    by_statement: dict[str, list[Proposal]] = {}
    for proposal in unmatched:
        by_statement.setdefault(proposal.statement, []).append(proposal)

    picks_model = _pick_model()
    client = structured(picks_model)

    for statement_kind, group in by_statement.items():
        for start in range(0, len(group), batch):
            chunk = group[start:start + batch]
            prompt = _PROMPT.format(statement=statement_kind.replace("_", " "),
                                    rows=_format_rows(chunk, candidate_limit))
            try:
                answer = client.invoke(prompt)
            except Exception as error:                      # pragma: no cover - network
                for proposal in chunk:
                    proposal.why = f"{proposal.why} · model call failed: {error}"
                continue

            by_label = {normalize_label(p.label): p for p in chunk}
            for pick in getattr(answer, "picks", []):
                proposal = by_label.get(normalize_label(pick.excel_label))
                if proposal is None or not pick.pdf_caption:
                    continue
                # Only a caption the filing actually prints is accepted.
                allowed = {normalize_label(c): c for c in proposal.candidates}
                exact = allowed.get(normalize_label(pick.pdf_caption))
                if exact is None:
                    proposal.why = (f"{proposal.why} · the model answered "
                                    f"'{pick.pdf_caption}', which is not a caption in "
                                    f"this statement; ignored")
                    continue

                # One filing line feeds one model row. A second claim on it means
                # the model matched a component to the combined line above it.
                taken = claimed.get(statement_kind, {})
                owner = taken.get(normalize_label(exact))
                if owner is not None:
                    proposal.why = (
                        f"the model chose '{exact}', but '{owner}' already reads that "
                        f"line — this row looks like a component of it, not the same "
                        f"line; left unmatched")
                    continue

                taken[normalize_label(exact)] = proposal.label
                claimed[statement_kind] = taken
                proposal.caption = exact
                proposal.method = "model"
                proposal.why = pick.reason or "chosen by the model from the filing's captions"

    return proposals


def duplicates(proposals: list[Proposal]) -> dict[tuple[str, str], list[Proposal]]:
    """Filing lines claimed by more than one row, whatever matched them.

    The model pass refuses its own duplicates as it goes, but two DETERMINISTIC
    matches can land on one line too — and writing one filing figure into two model
    rows double-counts it in every subtotal above.
    """
    seen: dict[tuple[str, str], list[Proposal]] = {}
    for proposal in proposals:
        if proposal.caption:
            key = (proposal.statement, normalize_label(proposal.caption))
            seen.setdefault(key, []).append(proposal)
    return {key: group for key, group in seen.items() if len(group) > 1}


def apply(model: ModelMap, proposals: list[Proposal]) -> dict[str, int]:
    """Write the proposals into the map and flag what needs a human."""
    by_row = {spec.row_hint: spec for spec in model.rows}
    counts = {"exact": 0, "derived": 0, "restated": 0, "note": 0, "model": 0,
              "unmatched": 0}

    shared: dict[int, list[str]] = {}
    for group in duplicates(proposals).values():
        for proposal in group:
            shared[proposal.row] = [p.label for p in group if p is not proposal]

    for proposal in proposals:
        spec = by_row.get(proposal.row)
        if spec is None:
            continue
        counts[proposal.method] = counts.get(proposal.method, 0) + 1

        if proposal.caption is None:
            _note(spec, f"no line in the filing's {proposal.statement or 'statements'} "
                        f"matched this caption: {proposal.why}")
            continue

        spec.pdf_caption = proposal.caption

        if proposal.row in shared:
            others = ", ".join(repr(label) for label in shared[proposal.row])
            _note(spec, f"'{proposal.caption}' is also read by {others} — one filing "
                        f"line cannot fill two rows; decide which owns it")
            continue

        if proposal.method == "exact":
            # The filing prints this caption verbatim; nothing to look at beyond any
            # note about the workbook cell itself.
            _note(spec)
        elif proposal.method == "derived":
            _note(spec, f"matched '{proposal.caption}' in the filing ({proposal.why}) "
                        f"— confirm it is the same line")
        elif proposal.method == "restated":
            spec.statement = proposal.statement
            _note(spec, f"matched '{proposal.caption}' in the {proposal.statement} "
                        f"({proposal.why}) — confirm the sign convention matches the "
                        f"workbook's, since statements differ on it")
        elif proposal.method == "note":
            spec.statement = proposal.statement
            _note(spec, f"matched '{proposal.caption}' in the notes ({proposal.why}) "
                        f"— confirm it is the same line and the right period")
        else:
            _note(spec, f"a model chose '{proposal.caption}' from the filing's "
                        f"{proposal.statement} ({proposal.why}) — CONFIRM before use")

    return counts
