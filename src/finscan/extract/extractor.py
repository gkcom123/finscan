"""LLM extraction of the canonical P&L from raw results text."""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

from finscan.llm.factory import structured
from finscan.schemas import FIELD_LABELS, Extraction, Field_, LineItem, NON_SCALED_FIELDS

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
    IMPORTANT for Spanish mixed tables titled like "Por los periodos de seis y
    tres meses ...": for quarterly extraction choose the "Transacciones del
    [primer/segundo/tercer/cuarto] trimestre <year>" column, not the cumulative
    "Seis meses" column.
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
9. Cash-flow fields (total_before_working_capital_changes,
   net_cash_from_operating_activities, change_in_working_capital) come from the
   CASH FLOW STATEMENT, not the P&L. Report them for the same period as the rest
   of the extraction; if the cash flow statement only prints a cumulative
   period (e.g. six months), report the printed figures and say so in notes.
   total_before_working_capital_changes is the subtotal struck AFTER the
   non-cash adjustments and BEFORE the working-capital movements — it is often
   captioned just "Total" at the end of the adjustments block. Do not compute
   change_in_working_capital yourself — only report it if printed.
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
_ALL_NUMBERS = re.compile(r"\(?\s*-?\$?\s*[0-9][0-9,\.]*\)?")
_SPANISH_MIXED_QUARTER_TABLE = re.compile(
    r"seis\s+y\s+tres\s+meses|transacciones\s+del\s+(?:primer|segundo|tercer|cuarto)\s+trimestre",
    re.IGNORECASE,
)


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


def _close(a: float, b: float) -> bool:
    denom = max(abs(a), abs(b), 1.0)
    return abs(a - b) / denom <= 1e-3


def _maybe_realign_quarter_values(result: Extraction, document_text: str) -> str | None:
    """Prefer quarter transaction values in Spanish mixed 6M/3M statements.

    Some filings present rows with multiple numeric columns in this order:
    6M current, Q current, 6M prior, Q prior. The model can choose the first
    value (6M current) for quarterly runs. When we can detect that pattern, we
    swap to the second value (Q current) if the extracted value matches the
    first one.
    """
    if result.meta.period_type != "quarter":
        return None
    if not _SPANISH_MIXED_QUARTER_TABLE.search(document_text or ""):
        return None

    changed = 0
    for item in result.line_items:
        row = item.source_row_text or ""
        if not row:
            continue
        nums: list[float] = []
        for tok in _ALL_NUMBERS.findall(row):
            parsed = _parse_locale_number(tok)
            if parsed is not None:
                nums.append(parsed)
        if len(nums) < 2:
            continue

        first, second = nums[0], nums[1]
        # Switch only when the model clearly picked the first numeric slot.
        if _close(item.value, first) and not _close(item.value, second):
            item.value = second
            changed += 1

    if changed:
        return (
            "Detected a Spanish mixed six-month/quarter table and switched "
            f"{changed} line item(s) from cumulative 6M values to quarter "
            "transaction values (second numeric column) for quarterly extraction."
        )
    return None


_TOTAL_ROW = re.compile(r"(?im)^\s*total\b[^\n]*\d[^\n]*$")


def _row_numbers(text: str) -> list[float]:
    out: list[float] = []
    for tok in _ALL_NUMBERS.findall(text or ""):
        v = _parse_locale_number(tok)
        if v is not None:
            out.append(v)
    return out


def _rescue_working_capital_subtotal(result: Extraction, statements_text: str) -> str | None:
    """Recover total_before_working_capital_changes (X) when the model skipped it.

    IFRS cash-flow statements print X as a bare, uncaptioned "Total" line closing
    the non-cash-adjustments block, right before "Changes in working capital:" —
    a caption too generic for the taxonomy pass to reliably single out among the
    many other "Total" rows in a filing (balance sheet, equity statement, segment
    tables, ...), so it is worth an explicit second try rather than leaving the
    derivation permanently short one input.

    net_cash_from_operating_activities (Y) sits in the same table, a few rows
    below X, and the model extracts it reliably because its caption is
    unambiguous. Reuse it as a column anchor: find which numeric slot in Y's own
    printed row holds Y, then read the value at that same slot from the nearest
    "Total" row preceding the working-capital heading — column position is
    consistent across rows of one statement, so this recovers X for the exact
    period Y was already extracted for, without asking the model to guess again.
    """
    if any(li.field.value == "total_before_working_capital_changes" for li in result.line_items):
        return None
    y_item = next(
        (li for li in result.line_items
         if li.field.value == "net_cash_from_operating_activities" and li.source_row_text),
        None,
    )
    if y_item is None:
        return None

    y_nums = _row_numbers(y_item.source_row_text)
    idx = next((i for i, v in enumerate(y_nums) if _close(v, y_item.value)), None)
    if idx is None:
        return None

    m = re.search(r"working capital", statements_text or "", re.IGNORECASE)
    if not m:
        return None
    total_rows = list(_TOTAL_ROW.finditer(statements_text[: m.start()]))
    if not total_rows:
        return None
    total_line = total_rows[-1].group(0)
    x_nums = _row_numbers(total_line)
    if idx >= len(x_nums):
        return None

    result.line_items.append(LineItem(
        field=Field_.total_before_working_capital_changes,
        label_in_pdf="Total",
        value=x_nums[idx],
        confidence=0.7,
        source_row_text=total_line.strip(),
    ))
    return (
        f"total_before_working_capital_changes was not directly extracted (its printed "
        f"caption is a bare 'Total'); recovered {x_nums[idx]:,.2f} positionally, using the "
        f"same column slot as net_cash_from_operating_activities in the adjustments "
        f"subtotal immediately preceding 'Changes in working capital'. Review against "
        f"the source PDF before relying on it."
    )


class _CashFlowSubtotals(BaseModel):
    total_before_working_capital_changes: float | None = Field(
        default=None,
        description="The subtotal struck immediately after the non-cash adjustments and "
                    "immediately before the 'Changes in / Movement in working capital' "
                    "section. Often captioned just 'Total' at the end of that block, with "
                    "no other words on the line.",
    )
    net_cash_from_operating_activities: float | None = Field(
        default=None,
        description="Net cash flow/generated/provided by operating activities — the "
                    "subtotal struck after the working-capital movements, immediately "
                    "before the investing-activities section.",
    )
    column_used: str = Field(
        default="", description="Which printed column header you read the figures from."
    )


_CF_SYSTEM = """You read a company's Statement of Cash Flows (indirect method, operating
section only) and extract exactly two subtotals for ONE specific reporting period.

TARGET PERIOD: a single quarter — {period_hint}{end_date_clause}

The table may print SIX or more period columns side by side, for example:
  [six-months cumulative] [standalone quarter] [prior comparative quarter]
repeated for the current year and the prior year. Critically:
- A cumulative (six-month / nine-month / year-to-date) column and the standalone
  quarter column can share the SAME end date in their header — do not pick a
  column just because its date matches; a cumulative column is LARGER in
  magnitude than the single-quarter column it contains.
- Prefer a column whose header explicitly says "quarter" / "3 months" / a
  specific quarter number over one labelled only with a date or "six months" /
  "9 months" / "year to date".
- The two figures you need sit in the same table, a few lines apart — once you
  have identified the correct column for one, use that exact same column for
  the other.

Numbers in parentheses or with a trailing minus are negative. Return null for a
figure only if you genuinely cannot locate it in the statement — do not guess.
Report which column header you used in column_used, verbatim.
"""


def _rescue_via_focused_llm_call(
    statements_text: str, period_hint: str, period_end_date: str | None = None
) -> tuple[dict[str, float], str | None]:
    """Ask for X and Y in isolation when the full extraction pass dropped either.

    A single freeform pass over 20-30+ fields can lose recall on any one of them,
    especially a subtotal with a generic caption. Narrowing the ask to just these
    two numbers, with nothing else competing for the model's attention, recovers
    cases the main pass missed — the same principle `extract_for_labels` already
    uses elsewhere in this pipeline for low-recall rows.
    """
    llm = structured(_CashFlowSubtotals)
    end_date_clause = f" (period ending {period_end_date})" if period_end_date else ""
    try:
        result: _CashFlowSubtotals = llm.invoke([
            {"role": "system", "content": _CF_SYSTEM.format(
                period_hint=period_hint or "the most recent quarter",
                end_date_clause=end_date_clause,
            )},
            {"role": "user", "content": f"<document>\n{statements_text}\n</document>"},
        ])
    except Exception:
        return {}, None

    # Defensive getattr: guards against a schema mismatch (e.g. a test stub or a
    # future LLM factory change returning the wrong shape) surfacing as an
    # AttributeError deep in a best-effort rescue path instead of degrading.
    found = {
        k: v for k, v in (
            ("total_before_working_capital_changes",
             getattr(result, "total_before_working_capital_changes", None)),
            ("net_cash_from_operating_activities",
             getattr(result, "net_cash_from_operating_activities", None)),
        ) if v is not None
    }
    if not found:
        return {}, None
    column_used = getattr(result, "column_used", "")
    note = (
        f"Recovered {', '.join(found)} via a focused follow-up extraction scoped to just "
        f"these figures (the main pass did not return {'them' if len(found) > 1 else 'it'})"
        + (f"; column used: {column_used}" if column_used else "")
        + "."
    )
    return found, note


def _ensure_working_capital_components(result: Extraction, statements_text: str) -> str | None:
    """Fill in whichever of X (total_before_working_capital_changes) and
    Y (net_cash_from_operating_activities) the main pass missed, so
    normalize.derive_missing can compute change_in_working_capital = Y - X.

    Tries the free, deterministic positional rescue first (works when Y was
    extracted but X's bare "Total" caption was not); falls back to a focused
    LLM call scoped to just these two numbers when that isn't enough — which
    also covers the case where Y itself was dropped by the main pass.
    """
    notes: list[str] = []
    positional_note = _rescue_working_capital_subtotal(result, statements_text)
    if positional_note:
        notes.append(positional_note)

    have = {li.field.value for li in result.line_items}
    missing = {"total_before_working_capital_changes", "net_cash_from_operating_activities"} - have
    if missing:
        found, note = _rescue_via_focused_llm_call(
            statements_text, result.meta.period_label, result.meta.period_end_date
        )
        for fid in missing:
            if fid in found:
                result.line_items.append(LineItem(
                    field=Field_(fid), label_in_pdf=FIELD_LABELS[fid],
                    value=found[fid], confidence=0.7,
                    source_row_text="(recovered via focused follow-up extraction)",
                ))
        if note:
            notes.append(note)

    return " ".join(notes) if notes else None


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
        quarter_note = _maybe_realign_quarter_values(result, statements_text or document_text)
        if quarter_note:
            notes.append(quarter_note)
        scale_note = _maybe_rescale_items_to_printed_units(result)
        if scale_note:
            notes.append(scale_note)
        wc_note = _ensure_working_capital_components(result, statements_text or document_text)
        if wc_note:
            notes.append(wc_note)
        return result, notes

    notes.append("First extraction pass returned no line items; retried against the "
                 "financial statement pages alone.")
    focused = statements_text or document_text
    retry = _invoke(focused, (hint + " " + RETRY_HINT).strip())
    if retry.line_items:
        quarter_note = _maybe_realign_quarter_values(retry, focused)
        if quarter_note:
            notes.append(quarter_note)
        scale_note = _maybe_rescale_items_to_printed_units(retry)
        if scale_note:
            notes.append(scale_note)
        wc_note = _ensure_working_capital_components(retry, focused)
        if wc_note:
            notes.append(wc_note)
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
    For Spanish mixed tables ("seis y tres meses"), quarterly mode means using
    the "Transacciones del ... trimestre" column, not cumulative "Seis meses".
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
