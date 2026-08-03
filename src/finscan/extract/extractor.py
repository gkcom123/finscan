"""LLM extraction of the canonical P&L from raw results text."""
from __future__ import annotations

import re

from finscan.llm.factory import structured
from finscan.schemas import FIELD_LABELS, Extraction, NON_SCALED_FIELDS

_TAXONOMY = "\n".join(f"- {fid}: {label}" for fid, label in FIELD_LABELS.items())

SYSTEM = f"""You extract structured financial data from listed-company results filings.

CANONICAL FIELDS (use these ids exactly; omit any field not present in the document):
{_TAXONOMY}

LANGUAGE
- The filing can be in any language (for example Spanish in Mexico filings).
- Map by meaning, not by exact English wording.
- Keep label_in_pdf and source_row_text in the document's original language.

HARD RULES
1. Column discipline. Results tables carry several periods side by side (current
   quarter, previous quarter, corresponding quarter last year, YTD, full year).
   Identify the CURRENT REPORTING PERIOD column first — normally the leftmost
   numeric column, headed by the most recent period end date — and read every
   value from that one column only. Never mix columns.
2. If both Standalone and Consolidated statements are present, extract the
   CONSOLIDATED one and set meta.consolidated = true. If only standalone exists,
   use it and set consolidated = false.
3. Report values exactly as printed; do NOT convert units. Record the printed
   scale in meta.units ("lakhs", "crores", "millions", ...). Read it from the
   header line such as "(Rs. in lakhs)".
4. Numbers in parentheses or with a trailing minus are negative. Strip thousands
   separators. "-", "–", "NA", "Nil" mean the line is absent: omit it.
5. EPS is per share — never rescale it, report the printed figure.
6. Do not compute or infer values that are not printed. Subtotals like
   total_income / total_expenses / ebitda must only be reported if the document
   actually prints them.
7. confidence: 0.95+ when the caption and column are unambiguous; 0.6-0.8 when
   the caption is non-standard or the column header was inferred; below 0.6 when
   you are guessing. Be honest — low confidence routes the row to human review.
8. label_in_pdf must be the verbatim caption, and source_row_text the full raw
   line, so a reviewer can trace every number.
"""

USER = """Company results document text follows. Extract the current reporting period.

{hint}

<document>
{document}
</document>
"""


RETRY_HINT = (
    "Your previous attempt returned no line items. The document does contain a "
    "financial statement — it is inside <financial_statements>. Work through that "
    "table row by row and return every caption you can map to a canonical field. "
    "Returning an empty list is not an acceptable answer."
)


def _invoke(document_text: str, hint: str) -> Extraction:
    llm = structured(Extraction)
    hint_block = f"Reviewer hint: {hint}" if hint else ""
    return llm.invoke(
        [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER.format(hint=hint_block, document=document_text)},
        ]
    )


_RAW_NUMBER = re.compile(r"\(?\s*\$?\s*(-?[0-9][0-9,\.]*)")


def _parse_locale_number(text: str) -> float | None:
    token = (text or "").strip()
    if not token:
        return None
    token = token.replace("$", "").replace(" ", "")
    neg = token.startswith("(") and token.endswith(")")
    token = token.strip("()")

    if "," in token and "." in token:
        # Treat the last separator as the decimal separator.
        last_comma = token.rfind(",")
        last_dot = token.rfind(".")
        dec = "," if last_comma > last_dot else "."
        thou = "." if dec == "," else ","
        token = token.replace(thou, "")
        token = token.replace(dec, ".")
    else:
        # A lone comma or dot followed by exactly 3 digits is more likely a
        # thousands separator than a decimal point in statement tables.
        for sep in (",", "."):
            if token.count(sep) >= 1:
                parts = token.split(sep)
                if len(parts[-1]) == 3:
                    token = "".join(parts)
                else:
                    token = token.replace(sep, ".")
                break
    try:
        value = float(token)
    except ValueError:
        return None
    return -value if neg else value


def _maybe_rescale_items_to_printed_units(result: Extraction) -> str | None:
    """Repair a common LLM slip: values one 1000x step coarser than meta.units.

    Example: the PDF says '(en miles de pesos)' and prints 14,094,380, but the
    model returns 14094.38. In that case the values have effectively already
    been divided by 1000, so multiply them back to match the printed units.
    """
    candidates = 0
    ratio_hits = 0

    for item in result.line_items:
        if item.field.value in NON_SCALED_FIELDS or not item.source_row_text or not item.value:
            continue
        m = _RAW_NUMBER.search(item.source_row_text)
        if not m:
            continue
        raw = _parse_locale_number(m.group(1))
        if raw is None or abs(raw) < 1e-9:
            continue
        candidates += 1
        ratio = abs(raw / item.value)
        if 900 <= ratio <= 1100:
            ratio_hits += 1

    if candidates >= 3 and ratio_hits * 2 > candidates:
        for item in result.line_items:
            if item.field.value not in NON_SCALED_FIELDS:
                item.value *= 1000.0
        return (
            "The model returned statement values one 1000x step coarser than the printed "
            f"PDF units ({result.meta.units}); values were multiplied by 1000 to match the "
            "source rows before normalization."
        )
    return None


def extract(document_text: str, hint: str = "",
            statements_text: str | None = None) -> tuple[Extraction, list[str]]:
    """Run structured extraction, repairing an empty result once.

    Returns (extraction, notes). An empty first pass is common on press releases
    where the statement is a small table inside a lot of prose; retrying against
    the statement pages alone recovers it, so that is done automatically rather
    than reported as a failure.
    """
    notes: list[str] = []
    result = _invoke(document_text, hint)
    if result.line_items:
        scale_note = _maybe_rescale_items_to_printed_units(result)
        if scale_note:
            notes.append(scale_note)
        return result, notes

    notes.append("First extraction pass returned no line items; retried against the "
                 "financial statement pages alone.")
    focused = statements_text or document_text
    retry = _invoke(focused, (hint + " " + RETRY_HINT).strip())
    if retry.line_items:
        scale_note = _maybe_rescale_items_to_printed_units(retry)
        if scale_note:
            notes.append(scale_note)
        return retry, notes

    notes.append("The second pass also returned nothing. Check that the PDF's statement "
                 "pages carry a text layer (run `finscan inspect-pdf <file>`).")
    return retry, notes


# --------------------------------------------------------------------------- #
# Open-ended label extraction
# --------------------------------------------------------------------------- #
from pydantic import BaseModel as _BaseModel


class _LabeledValue(_BaseModel):
    label: str
    value: float | None = None
    confidence: float = 0.5


class _LabelExtraction(_BaseModel):
    matches: list[_LabeledValue]


_LABEL_SYSTEM = """You extract specific financial values from a company results document.

You will receive a numbered list of row labels exactly as they appear in an analyst's
Excel model. For each label, find the semantically matching value in the CURRENT
REPORTING PERIOD column of the financial statements and return it.

LANGUAGE
- The financial statement can be in any language.
- Match labels to captions by meaning across languages, not by literal wording.

MATCHING RULES — the Excel label and the PDF caption will often differ in wording:
  * "Maintenance revenues"  ↔  "Revenue from maintenance activities"
  * "Finance cost"          ↔  "Financial expenses" / "Interest expense"
  * "Executive bonus"       ↔  "Management incentive payments"
    * "Finance cost"          ↔  "Gastos por intereses"
    * "Finance income"        ↔  "Ingresos por intereses"
    * "Total revenue"         ↔  "Ingresos totales"
Use your best semantic judgement: match on meaning, not exact words.

EXTRACTION RULES
1. Use the CURRENT REPORTING PERIOD column only (the most recent period end date).
2. Report the value exactly as printed — do NOT convert units or scale.
3. Numbers in parentheses or with a trailing minus are negative.
4. Return null only when you genuinely cannot find any semantically related line
   in the document. Do not return null just because the wording differs.
5. Do not compute or infer values that are not explicitly printed in the document.
6. Every numbered label must appear in the output, even if the value is null.
"""

_LABEL_USER = """Document:
<document>
{document}
</document>

Find the current-period value for each of the following Excel row labels.
Match by meaning — the wording in the PDF may differ from the label below.
Return every label, with null for any you genuinely cannot find.

{labels}
"""


def extract_for_labels(
    labels: list[str],
    document_text: str,
    source_units: str = "units",
) -> dict[str, float]:
    """Match raw Excel row labels against the PDF and return label -> value in BASE units."""
    if not labels:
        return {}
    from finscan.extract.normalize import UNIT_MULTIPLIER
    llm = structured(_LabelExtraction)
    numbered = "\n".join(f"{i + 1}. {lbl}" for i, lbl in enumerate(labels))
    result: _LabelExtraction = llm.invoke([
        {"role": "system", "content": _LABEL_SYSTEM},
        {"role": "user", "content": _LABEL_USER.format(
            document=document_text, labels=numbered)},
    ])
    multiplier = UNIT_MULTIPLIER.get(source_units, 1.0)
    return {
        m.label: m.value * multiplier
        for m in result.matches
        if m.value is not None and m.confidence >= 0.5
    }
