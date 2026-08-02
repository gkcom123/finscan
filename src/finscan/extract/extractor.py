"""LLM extraction of the canonical P&L from raw results text."""
from __future__ import annotations

from finscan.llm.factory import structured
from finscan.schemas import FIELD_LABELS, Extraction

_TAXONOMY = "\n".join(f"- {fid}: {label}" for fid, label in FIELD_LABELS.items())

SYSTEM = f"""You extract structured financial data from listed-company results filings.

CANONICAL FIELDS (use these ids exactly; omit any field not present in the document):
{_TAXONOMY}

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
        return result, notes

    notes.append("First extraction pass returned no line items; retried against the "
                 "financial statement pages alone.")
    focused = statements_text or document_text
    retry = _invoke(focused, (hint + " " + RETRY_HINT).strip())
    if retry.line_items:
        return retry, notes

    notes.append("The second pass also returned nothing. Check that the PDF's statement "
                 "pages carry a text layer (run `finscan inspect-pdf <file>`).")
    return retry, notes
