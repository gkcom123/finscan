"""Convert an existing maps/<company>.json into the lean mapping.

Migration, run once per company. The hand-work in the old map — a sum written by
hand, a caption corrected, a sign asserted — is translated, not retyped.

One judgement is made here deliberately: a `sign` in the old map is kept ONLY when
the old row also carried a `pdf_caption` or a hand-written resolve, because most
signs in those files were inferred from a single prior column, and that inference
wrote wrong values. An inferred sign is dropped; the filing's own sign is used.
"""
from __future__ import annotations

from finscan2.mapping.schema import Mapping, MappingRow
from finscan2.model.schema import ModelMap, normalize_label

_SECTION_DEFAULT_BASIS = {"income_statement": "quarter", "cash_flow": "quarter",
                          "balance_sheet": "point_in_time"}


def _pdf_from_instruction(instruction: str, caption: str | None, label: str = ""):
    """(pdf, value, note_prefix) for one old `resolve` string.

    A `pdf:` instruction whose text is just the WORKBOOK caption, with no recorded
    filing caption, was never verified against a filing — `learn` wrote it as the
    text to search with. Carrying it across would turn "we never found this line"
    into "this is the line", which is how `Exchange Gain, net` would arrive in the
    mapping looking decided when the filing has no such row.
    """
    if instruction.startswith("const:"):
        text = instruction.split(":", 1)[1].strip()
        try:
            return None, float(text), ""
        except ValueError:
            return None, None, f"constant '{text}' was not a number"

    if instruction.startswith("absent:"):
        return None, None, instruction.split(":", 1)[1].strip()

    if instruction.startswith("sum:"):
        terms = []
        for raw in instruction.split(":", 1)[1].split("|"):
            text = raw.strip()
            lead = ""
            while text[:1] in "+-":
                if text[0] == "-":
                    lead = "-"
                text = text[1:].strip()
            if text.startswith("pdf:"):
                text = text.split(":", 1)[1]
            elif text.startswith("field:"):
                text = text.split(":", 1)[1].replace("_", " ")
            terms.append(f"{lead}{text}")
        return terms, None, ""

    # A recorded caption is the filing's own wording and beats the workbook's.
    if caption:
        return caption, None, ""
    if instruction.startswith("pdf:"):
        wanted = instruction.split(":", 1)[1]
        if label and normalize_label(wanted) == normalize_label(label):
            return None, None, ("no filing caption was ever recorded for this row; "
                                "name the filing's line, or leave pdf null with a "
                                "reason")
        return wanted, None, ""
    if instruction.startswith("field:"):
        # No caption was ever recorded, so there is nothing certain to carry over.
        return None, None, (f"was mapped to the canonical field "
                            f"'{instruction.split(':', 1)[1]}'; needs a filing caption")
    return None, None, f"unrecognised instruction '{instruction}'"


def from_model_map(model: ModelMap) -> Mapping:
    """The human decisions of an old map, in the lean format."""
    mapping = Mapping(company=model.company, sheet=model.sheet)

    for spec in model.rows:
        if spec.kind != "input":
            continue

        pdf, value, note = _pdf_from_instruction(spec.resolve or "", spec.pdf_caption,
                                                 spec.key.label)
        row = MappingRow(
            label=spec.key.label,
            section=spec.key.section,
            occurrence=spec.key.occurrence,
            pdf=pdf,
            value=value,
            note=note,
        )

        # Only a non-default basis or a different statement is worth carrying.
        if spec.basis and spec.basis != _SECTION_DEFAULT_BASIS.get(spec.key.section):
            row.basis = spec.basis
        if spec.statement and spec.statement != spec.key.section:
            row.statement = spec.statement

        # An asserted sign survives; an inferred one does not (see the module note).
        if spec.sign and (spec.pdf_caption or (spec.resolve or "").startswith(
                ("sum:", "const:"))):
            row.sign = spec.sign

        if not row.note and row.pdf is None and row.value is None:
            carried = (spec.review or "").split(" · ")[0]
            row.note = carried or "no filing caption recorded; needs review"

        mapping.rows.append(row)

    return mapping
