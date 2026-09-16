"""LLM extraction of the canonical P&L from raw results text."""
from __future__ import annotations

import re
from itertools import combinations

from pydantic import BaseModel, Field

from finscan.llm.factory import structured
from finscan.schemas import (
    CUMULATIVE_CORRECTION_INELIGIBLE_FIELDS,
    FIELD_LABELS,
    Extraction,
    Field_,
    LineItem,
    NON_SCALED_FIELDS,
)

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
    STRICT RULE for mixed six-month/quarter tables, in either language — Spanish,
    titled like "Por los periodos de seis y tres meses ..." with columns
    "Transacciones del [primer/segundo/tercer/cuarto] trimestre <year>" (e.g.
    "Transacciones del segundo trimestre 2026") vs cumulative "Seis meses"; or
    English, titled like "For the six and three-months periods ended ..." with
    columns "6 months as of <date>", "[Ordinal]-quarter <year> transactions"
    (e.g. "Second-quarter 2026 transactions"), "3 months as of <date>":
    for a quarterly extraction you MUST read every value from the quarter/
    trimestre transactions column — e.g. "Second-quarter 2026 transactions" —
    and NEVER from the cumulative six-month/"seis meses"/"6 months as of"
    column, even though the cumulative column is printed first (leftmost) and
    even if its header shows the same end date. If you cannot find a quarter-
    only column, say so in a note rather than silently reporting the cumulative
    figure.
2. If both Standalone and Consolidated statements are present, extract the
   CONSOLIDATED one and set meta.consolidated = true. If only standalone exists,
   use it and set consolidated = false.
3. Report values exactly as printed; do NOT convert units. Record the printed
   scale in meta.units ("lakhs", "crores", "millions", ...). Read it from the
   header line such as "(Rs. in lakhs)".
4. Numbers in parentheses or with a trailing minus are negative. Strip thousands
   separators. "-", "–", "NA", "Nil" mean the line is absent: omit it. On an
   OCR'd/scanned page one paren of a bracketed negative is sometimes dropped
   (e.g. "9,532)" with no opening "("); still treat that as negative — a
   trailing ")" with no other explanation is not a typo, it is a lost "(".
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
_MIXED_QUARTER_TABLE = re.compile(
    r"seis\s+y\s+tres\s+meses"
    r"|transacciones\s+del\s+(?:primer|segundo|tercer|cuarto)\s+trimestre"
    r"|six\s+and\s+three[\s-]months?\s+periods?\s+ended"
    r"|(?:first|second|third|fourth)-quarter\s+\d{4}\s+transactions",
    re.IGNORECASE,
)


def _parse_locale_number(text: str) -> float | None:
    token = (text or "").strip()
    if not token:
        return None
    token = token.replace("$", "").replace(" ", "")
    # OCR on a scanned page routinely drops one paren of a bracketed negative
    # (e.g. "(9,532)" -> "9,532)"), so a lone leading/trailing paren still counts.
    neg = token.startswith("(") or token.endswith(")")
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


def _maybe_realign_quarter_values(
    result: Extraction, document_text: str
) -> tuple[str | None, set[int]]:
    """Prefer quarter transaction values in mixed 6M/3M statements (Spanish or English).

    Some filings present rows with multiple numeric columns in this order:
    6M current, Q current, 6M prior, Q prior. The model can choose the first
    value (6M current) for quarterly runs. When we can detect that pattern, we
    swap to the second value (Q current) if the extracted value matches the
    first one.

    Returns (note, changed_ids): changed_ids holds id() of every LineItem this
    function rewrote in place, so _detect_months_covered() below can skip them —
    a row already realigned to its standalone quarter value must not also be
    treated as a lone cumulative figure and corrected a second time.
    """
    if result.meta.period_type != "quarter":
        return None, set()
    if not _MIXED_QUARTER_TABLE.search(document_text or ""):
        return None, set()

    changed_ids: set[int] = set()
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
            changed_ids.add(id(item))

    if changed_ids:
        return (
            "Detected a mixed six-month/quarter table and switched "
            f"{len(changed_ids)} line item(s) from cumulative 6M values to quarter "
            "transaction values (second numeric column) for quarterly extraction."
        ), changed_ids
    return None, changed_ids


_CUMULATIVE_PERIOD_PHRASE = re.compile(
    r"\b(three|six|nine|twelve|tres|seis|nueve|doce|3|6|9|12)[\s-]"
    r"(?:month|months|mes|meses)\b(?:[\s-]periods?)?\s*"
    r"(?:ended|ending|terminad[oa]s?|al)\b",
    re.IGNORECASE,
)
_MONTHS_WORDS = {
    "three": 3, "3": 3, "tres": 3,
    "six": 6, "6": 6, "seis": 6,
    "nine": 9, "9": 9, "nueve": 9,
    "twelve": 12, "12": 12, "doce": 12,
}
#: How far back (characters) to look for a table's own accumulation-period header
#: above a data row that carries no such phrase itself. Approximate by nature —
#: every hit is logged as an info note so real filings can tune this over time.
_CUMULATIVE_PROXIMITY_WINDOW = 600


def _months_from_phrase(text: str) -> int | None:
    m = _CUMULATIVE_PERIOD_PHRASE.search(text or "")
    return _MONTHS_WORDS.get(m.group(1).lower()) if m else None


#: A statement whose heading announces two accumulation bases at once — e.g.
#: "FOR THE THREE-MONTH AND NINE-MONTH PERIODS ENDED 30 SEPTEMBER 2025", the standard
#: IAS 34 interim heading. Such a statement prints BOTH a standalone-quarter column and
#: a year-to-date column, so a row's basis is decided by which column the value came
#: from, never by the heading — which says both. Proximity matching must not fire here.
_MULTI_BASIS_HEADING = re.compile(
    r"\b(?:three|six|nine|twelve|3|6|9|12)[\s-](?:month|months)\b[^.\n]{0,40}?"
    r"\b(?:and|y|&)\b[^.\n]{0,40}?"
    r"\b(?:three|six|nine|twelve|3|6|9|12)[\s-](?:month|months)\b",
    re.IGNORECASE,
)

_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11,
    "diciembre": 12,
}

#: A period column header written as a month range, e.g. "July - September 2025" or
#: "January -\nSeptember\n2025" (statement headers are routinely stacked across lines,
#: so newlines are treated as ordinary whitespace). The span between the two months,
#: inclusive, is how many months that column accumulates: Jul-Sep = 3, Jan-Sep = 9.
_COLUMN_MONTH_RANGE = re.compile(
    r"\b(" + "|".join(sorted(_MONTH_NAMES, key=len, reverse=True)) + r")\b"
    r"\s*[-–—]\s*"
    r"\b(" + "|".join(sorted(_MONTH_NAMES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

#: How far above a data row to look for that table's own column-header block. Wider than
#: _CUMULATIVE_PROXIMITY_WINDOW because the header sits above every row of the table, so
#: the last rows of a long statement are a long way from it.
_COLUMN_HEADER_WINDOW = 2500


#: Column headers of one statement are printed together in a single header block, so
#: consecutive month-range labels sit close together. A larger gap than this means the
#: earlier label belongs to a different statement further up the document.
_HEADER_BLOCK_GAP = 200


def _column_months(window: str) -> list[int] | None:
    """Months accumulated by each period column, left to right, from a header block.

    Only the header block nearest the row is used. A window wide enough to reach a long
    statement's header also reaches the *previous* statement's header, and those columns
    describe a different table: on a document where a four-column profit or loss is
    followed by a two-column cash flow, taking every match would place the cash flow's
    first column at index 4 of a six-entry list and read the profit or loss's basis for
    it. Returns None when no month-range columns are found, leaving the caller with no
    column-level signal.
    """
    spans: list[tuple[int, int]] = []
    for m in _COLUMN_MONTH_RANGE.finditer(window or ""):
        start, end = _MONTH_NAMES[m.group(1).lower()], _MONTH_NAMES[m.group(2).lower()]
        spans.append((m.start(), (end - start) % 12 + 1))
    if not spans:
        return None

    block = [spans[-1]]
    for pos, months in reversed(spans[:-1]):
        if block[0][0] - pos > _HEADER_BLOCK_GAP:
            break
        block.insert(0, (pos, months))
    return [months for _, months in block]


def _value_column_index(item: LineItem) -> int | None:
    """Which numeric slot of its own printed row the extracted value sits in.

    Matched on magnitude, not signed value: an expense row prints its figures in
    parentheses ("Cost of Sales | (4,044,312) | ...") which parse as negative, while the
    extractor reports the same figure positive and lets enforce_sign_rules() settle the
    convention later. Comparing signed values there finds no column at all, which sends
    every bracketed row — most of the cost base — down the "basis undetectable" path.

    Ambiguous when the same magnitude is printed more than once in the row (e.g. a flat
    year on year line); returning None there keeps the caller from picking a basis off
    a coin flip.
    """
    nums = _row_numbers(item.source_row_text or "")
    hits = [i for i, v in enumerate(nums) if _close(abs(v), abs(item.value))]
    return hits[0] if len(hits) == 1 else None


def _nearest_preceding_months(window: str) -> int | None:
    """Closest accumulation-period phrase to the END of window (i.e. just above the row)."""
    matches = list(_CUMULATIVE_PERIOD_PHRASE.finditer(window))
    return _MONTHS_WORDS.get(matches[-1].group(1).lower()) if matches else None


def _detect_months_covered(
    result: Extraction, document_text: str, skip_ids: set[int]
) -> str | None:
    """Tag each line item with how many months its value actually covers.

    Some fields are only disclosed as a year-to-date cumulative figure even in an
    otherwise quarterly filing (e.g. Depreciation & Amortisation stated "for the
    six-month period ended 30 June" with no adjoining quarter-only column at all).
    Writing that raw cumulative number into a quarterly model column would silently
    corrupt it and everything derived from it. This function only *tags* the value
    with how many months it covers; excel/cumulative.py (run later, once the target
    workbook's own prior-quarter columns are available) does the actual correction.

    Deterministic and regex-only, like _maybe_realign_quarter_values above — the
    model is never asked to self-report this (see LineItem.months_covered's
    SkipJsonSchema annotation in schemas.py, which keeps the field out of the
    function-calling schema entirely).

    Three passes, most precise first:

    1. Column position. An IAS 34 interim statement routinely prints a standalone
       quarter column and a year-to-date column side by side under one heading that
       names both bases ("FOR THE THREE-MONTH AND NINE-MONTH PERIODS ENDED ..."). The
       basis is then a property of the column the value was read from, not of the page:
       on Almarai's 3Q25 profit or loss the same heading covers Revenue read from
       "July - September 2025" (3 months, standalone) and the very same row's
       "January - September 2025" figure (9 months, cumulative). Month-range column
       headers give this exactly, so it is tried first and trusted over any phrase.
    2. The row's own text ("... for the six months ended 30 June ...").
    3. The nearest accumulation phrase above the row — but ONLY when the surrounding
       heading names a single basis. Under a heading naming two, proximity would tag
       every row of a mixed table with the cumulative figure's basis and the correction
       pass would then subtract prior quarters from figures that are already standalone.
       Refusing to guess there is the whole point of this ordering.
    """
    if result.meta.period_type != "quarter":
        return None
    text = document_text or ""
    tagged: list[str] = []
    skipped_ambiguous: list[str] = []
    for item in result.line_items:
        if id(item) in skip_ids or item.field.value in CUMULATIVE_CORRECTION_INELIGIBLE_FIELDS:
            continue

        row = (item.source_row_text or "").strip()
        pos = text.find(row) if row else -1
        header_window = text[max(0, pos - _COLUMN_HEADER_WINDOW):pos] if pos > 0 else ""

        months = None
        # 1. column position within this table's own month-range headers
        columns = _column_months(header_window)
        if columns:
            idx = _value_column_index(item)
            if idx is not None and idx < len(columns):
                months = columns[idx]

        # 2. the row's own inline phrase
        if months is None:
            months = _months_from_phrase(row)

        # 3. nearest phrase above the row, only under a single-basis heading
        if months is None and pos > 0:
            proximity_window = text[max(0, pos - _CUMULATIVE_PROXIMITY_WINDOW):pos]
            if _MULTI_BASIS_HEADING.search(header_window):
                if _nearest_preceding_months(proximity_window) is not None:
                    skipped_ambiguous.append(item.field.value)
            else:
                months = _nearest_preceding_months(proximity_window)

        # A three-month figure in a quarterly filing is already standalone; tagging it
        # would claim a cumulative disclosure in the review note for a value that needs
        # no correction at all.
        if months is not None and months > 3:
            item.months_covered = months
            tagged.append(f"{item.field.value} ({months} months)")

    notes: list[str] = []
    if tagged:
        notes.append(
            f"Detected a cumulative (year-to-date) disclosure for {len(tagged)} line "
            f"item(s), flagged for standalone-quarter correction against the workbook's "
            f"own prior-period columns: {', '.join(tagged)}."
        )
    if skipped_ambiguous:
        notes.append(
            f"{len(skipped_ambiguous)} line item(s) sit under a heading naming two "
            f"accumulation periods at once (e.g. 'three-month and nine-month periods "
            f"ended') and their printed row gave no column position, so their basis "
            f"could not be established from the text: "
            f"{', '.join(sorted(set(skipped_ambiguous)))}. Left as extracted rather than "
            f"corrected on a guess — verify these against the source PDF."
        )
    return "\n".join(notes) or None


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


#: Anchor for locating the cash flow statement's operating section, standard IFRS
#: wording. Used (not a generic document-wide caption search) so a segment note's own
#: "Depreciation and Amortisation" breakdown table — a different, unrelated table that
#: can appear elsewhere in the same filing — is never mistaken for the cash-flow line.
_CASH_FLOW_OPERATING_HEADING = re.compile(
    r"cash\s+flows?\s+from\s+operating\s+activities", re.IGNORECASE
)
#: How far past that heading to look — enough to cover "Profit for the period" plus
#: the non-cash-adjustments block, not so far it reaches "Changes in working capital".
_DA_CASH_FLOW_WINDOW = 1500
_DEPRECIATION_ROW = re.compile(
    r"(?im)^[^\n|]*\b(?:depreciation\s+and\s+amortisation|depreciation\s+and\s+amortization|"
    r"depreciation,?\s*amortisation\s+and\s+impairment|depreciation,?\s*depletion\s*(?:&|and)\s*amortization)"
    r"\b[^\n]*\d[^\n]*$"
)


def _rescue_depreciation_from_cash_flow(result: Extraction, statements_text: str) -> str | None:
    """Recover depreciation_amortisation when the P&L has no line for it at all.

    Some filings (e.g. Almarai) fold D&A into Cost of Sales/opex on the P&L with no
    caption of its own, disclosing it only as the first non-cash adjustment in the
    Statement of Cash Flows — the main pass then has nothing to extract it from. Reads
    the raw row text directly (rather than asking the model) and appends it as a real
    LineItem with genuine source_row_text, so it is found by _detect_months_covered same
    as any other line — this filing's cash flow statement may print only a cumulative
    (e.g. six-month) column, and standalone-quarter correction must still catch that.
    """
    if any(li.field.value == "depreciation_amortisation" for li in result.line_items):
        return None
    heading = _CASH_FLOW_OPERATING_HEADING.search(statements_text or "")
    if not heading:
        return None
    window = statements_text[heading.end(): heading.end() + _DA_CASH_FLOW_WINDOW]
    m = _DEPRECIATION_ROW.search(window)
    if not m:
        return None
    row = m.group(0).strip()
    nums = _row_numbers(row)
    if not nums:
        return None

    result.line_items.append(LineItem(
        field=Field_.depreciation_amortisation,
        label_in_pdf=row.split("|")[0].strip(),
        value=nums[0],
        confidence=0.7,
        source_row_text=row,
    ))
    return (
        f"depreciation_amortisation was not printed as its own P&L line; recovered "
        f"{nums[0]:,.2f} from the cash flow statement's non-cash-adjustments block "
        f"('{row}'). Review against the source PDF."
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
- STRICT RULE: you MUST use a column whose header explicitly names a quarter —
  "quarter" / "3 months" / a specific quarter number, e.g. "Second-quarter 2026
  transactions" or "Transacciones del segundo trimestre 2026" — and NEVER a
  column labelled only with a date, or "six months" / "9 months" / "year to
  date" / "seis meses", even if it is printed first (leftmost) or shares the
  same end-date header as the quarter column.
- The two figures you need sit in the same table, a few lines apart — once you
  have identified the correct quarter column for one, use that exact same
  column for the other.

Numbers in parentheses or with a trailing minus are negative. Return null for a
figure only if you genuinely cannot locate it in the statement — do not guess.
Report which column header you used in column_used, verbatim.

Report each figure EXACTLY as printed in the table — do not convert, rescale, or
divide/multiply by 1,000 or any other factor, even if you believe a different
scale would be more standard. Both figures must be read at the same scale as
they are printed in the statement.
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
        return {}, None, ""
    column_used = getattr(result, "column_used", "")
    note = (
        f"Recovered {', '.join(found)} via a focused follow-up extraction scoped to just "
        f"these figures (the main pass did not return {'them' if len(found) > 1 else 'it'})"
        + (f"; column used: {column_used}" if column_used else "")
        + "."
    )
    return found, note, column_used


_CF_RESCUE_FIELDS = ("total_before_working_capital_changes", "net_cash_from_operating_activities")


def _reconcile_rescued_scale(result: Extraction, fids: set[str]) -> str | None:
    """Guard against the focused rescue call returning X/Y at the wrong scale.

    Unlike the main extraction pass, the focused call has no source row text to
    cross-check against (`_maybe_rescale_items_to_printed_units` can't see it),
    and the model has occasionally pre-divided the figure by 1000 despite being
    told not to. Compare the newly-added items' magnitude against the median of
    the rest of the extraction's already-scaled line items, which came from the
    main pass and are trustworthy — if a rescued figure is ~1000x off from that
    crowd, it was almost certainly returned at the wrong scale.
    """
    others = [abs(li.value) for li in result.line_items
              if li.field.value not in fids and li.value and li.field.value not in NON_SCALED_FIELDS]
    if len(others) < 3:
        return None
    others.sort()
    median = others[len(others) // 2]
    if median < 1e-9:
        return None

    fixed = []
    for li in result.line_items:
        if li.field.value in fids and li.value:
            ratio = median / abs(li.value)
            if 300 <= ratio <= 3000:
                li.value *= 1000.0
                fixed.append(li.field.value)
    if not fixed:
        return None
    return (
        f"Rescaled {', '.join(fixed)} by 1000x: the focused follow-up extraction returned "
        f"{'it' if len(fixed) == 1 else 'them'} at a different scale than the rest of this "
        "extraction (compared against the extraction's own median line-item size). Review "
        "against the source PDF before relying on this figure."
    )


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
    missing = set(_CF_RESCUE_FIELDS) - have
    if missing:
        # Tolerate a 2-tuple: test stubs (and any older monkeypatch) predate column_used.
        rescued = _rescue_via_focused_llm_call(
            statements_text, result.meta.period_label, result.meta.period_end_date
        )
        found, note = rescued[0], rescued[1]
        column_used = rescued[2] if len(rescued) > 2 else ""

        # The rescue reports which column it read, which is the only basis signal these
        # items will ever have: they are appended after _detect_months_covered has run,
        # and their placeholder source_row_text cannot be located in the document, so
        # neither the column-position nor the proximity pass can reach them. A cash flow
        # statement routinely prints only a year-to-date column (Almarai's 2Q26 interims
        # state "January - June 2026" and no quarter at all), so leaving these untagged
        # writes a six- or nine-month figure into a quarterly column as though it were
        # standalone — the exact corruption the cumulative correction exists to prevent.
        rescued_months = None
        if result.meta.period_type == "quarter":
            spans = _column_months(column_used)
            if spans and spans[0] > 3:
                rescued_months = spans[0]

        recovered = set()
        for fid in missing:
            if fid in found:
                result.line_items.append(LineItem(
                    field=Field_(fid), label_in_pdf=FIELD_LABELS[fid],
                    value=found[fid], confidence=0.7,
                    source_row_text="(recovered via focused follow-up extraction)",
                    months_covered=rescued_months,
                ))
                recovered.add(fid)
        if rescued_months and recovered:
            notes.append(
                f"The focused rescue read {', '.join(sorted(recovered))} from a "
                f"{rescued_months}-month column ({column_used.strip()}); tagged for "
                f"standalone-quarter correction against the workbook's prior columns."
            )
        if note:
            notes.append(note)
        if recovered:
            scale_note = _reconcile_rescued_scale(result, recovered)
            if scale_note:
                notes.append(scale_note)

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
        text = statements_text or document_text
        quarter_note, realigned_ids = _maybe_realign_quarter_values(result, text)
        if quarter_note:
            notes.append(quarter_note)
        da_note = _rescue_depreciation_from_cash_flow(result, text)
        if da_note:
            notes.append(da_note)
        months_note = _detect_months_covered(result, text, realigned_ids)
        if months_note:
            notes.append(months_note)
        scale_note = _maybe_rescale_items_to_printed_units(result)
        if scale_note:
            notes.append(scale_note)
        wc_note = _ensure_working_capital_components(result, text)
        if wc_note:
            notes.append(wc_note)
        return result, notes

    notes.append("First extraction pass returned no line items; retried against the "
                 "financial statement pages alone.")
    focused = statements_text or document_text
    retry = _invoke(focused, (hint + " " + RETRY_HINT).strip())
    if retry.line_items:
        quarter_note, realigned_ids = _maybe_realign_quarter_values(retry, focused)
        if quarter_note:
            notes.append(quarter_note)
        da_note = _rescue_depreciation_from_cash_flow(retry, focused)
        if da_note:
            notes.append(da_note)
        months_note = _detect_months_covered(retry, focused, realigned_ids)
        if months_note:
            notes.append(months_note)
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
    source_row_text: str | None = None


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

CASH FLOW STATEMENT — a label naming an actual CASH movement (e.g. "Cash interest
paid", "Cash interest income", "Cash tax paid") is NOT the same line as the P&L
figure of the same name, and is usually NOT the reconciliation line either:
  * An indirect-method cash flow statement starts from profit and, in its
    OPERATING ACTIVITIES section, adds non-cash/reclassified P&L items back
    (e.g. "Interest expense" printed as a positive addition) purely to reverse
    them out of profit — that add-back is a bookkeeping step, not cash received.
  * The real cash paid for interest is a separate line, normally further down
    in the FINANCING ACTIVITIES section (often captioned "Interest paid" /
    "Intereses pagados"), and is a cash outflow.
  * When the label explicitly says "cash" (e.g. "Cash interest paid"), search
    for that distinct financing-section line — do not settle for the
    operating-section add-back just because it shares the word "interest".

SUBTOTAL AVOIDANCE — you are given the FULL list of Excel row labels together, not
one at a time. Use that: several labels in the list are often one analyst's
breakdown of a single PDF total into its components (e.g. "Management fees,
expenses", "Administrative expenses", "Property taxes", "Insurance" sitting
alongside a vaguer "Operating expenses" label).
  * Do NOT match a label to a PDF TOTAL/subtotal caption (e.g. "Total operating
    expenses", or an "Operating expenses" line that is itself the sum of
    several narrower captions below it) if that total's value already equals
    the sum of other lines you are matching to OTHER labels in this same
    batch — that silently double-counts everything the other labels already
    captured.
  * Instead, look in the PDF for the one specific, narrower caption in that
    same expense/income breakdown that is NOT already claimed by any other
    label in the batch (e.g. a "Maintenance" or "Maintenance expenses" line)
    and use its value instead.
  * If you cannot find a distinct, unclaimed specific line for that label,
    return null rather than a subtotal — a null is reviewed by a human, a
    wrong subtotal silently corrupts every sibling row's total.
  * A subtotal row is often printed with NO caption at all — just a row of
    numbers directly below the last named line in that section. A line with
    no name printed next to it is never the right match for a label asking
    for one specific, named category — keep looking for the actual named
    line (it may be one you have not been asked about at all), or return null.

EXTRACTION RULES
1. Use the CURRENT REPORTING PERIOD column only (the most recent period end date).
    STRICT RULE for mixed six-month/quarter tables — Spanish ("seis y tres
    meses" / "Transacciones del ... trimestre") or English ("six and
    three-months periods ended" / "[Quarter] transactions", e.g.
    "Second-quarter 2026 transactions"): quarterly mode means you MUST read
    the quarter transactions column ONLY — never the cumulative "Seis meses" /
    "6 months as of" column, even if it is printed first or shares the same
    end-date header.
2. Report the value exactly as printed — do NOT convert units or scale.
3. Numbers in parentheses or with a trailing minus are negative.
4. Return null only when you genuinely cannot find any semantically related line
   in the document. Do not return null just because the wording differs.
5. Do not compute or infer values that are not explicitly printed in the document.
6. Every numbered label must appear in the output, even if the value is null.
7. source_row_text must be the full raw line the value came from, verbatim,
   including every other number printed on that same line — this lets a
   deterministic check confirm you used the quarter column and not a
   neighbouring cumulative column, so do not omit it or paraphrase it.
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


# Cash actually paid/received is a direction the label itself asserts (money out
# vs. money in), independent of how the source line happened to print its sign
# — e.g. an indirect cash flow statement's operating-section "Interest expense"
# add-back is printed positive even though it is a cost, because it is reversing
# a non-cash item out of profit, not recording cash received. Trusting the
# source sign for these labels carries that bookkeeping artefact into the model.
_CASH_OUTFLOW_LABEL = re.compile(
    r"\b(?:cash\s+)?interest\s+paid\b|\btax(?:es)?\s+paid\b|\bdividends?\s+paid\b",
    re.IGNORECASE,
)
_CASH_INFLOW_LABEL = re.compile(
    r"\b(?:cash\s+)?interest\s+(?:income|received)\b",
    re.IGNORECASE,
)


def _enforce_label_direction(label: str, value: float) -> float:
    """Force sign by what the label itself says (money out = negative, money in =
    positive), rather than carrying over whatever sign the matched source line
    happened to print."""
    if _CASH_OUTFLOW_LABEL.search(label):
        return -abs(value)
    if _CASH_INFLOW_LABEL.search(label):
        return abs(value)
    return value


def _maybe_realign_label_quarter_values(
    matches: list[_LabeledValue], document_text: str
) -> str | None:
    """Same fix as _maybe_realign_quarter_values, for the label-fallback path.

    extract_for_labels asks the LLM to match a raw Excel caption directly
    against the PDF rather than a canonical field, and has none of the main
    pass's deterministic cross-checks — the prompt's "use the quarter column"
    rule is the only thing stopping it from reading the cumulative six-month
    figure. Back that instruction with the same positional check: if a row
    prints [6M current, Q current, ...] and the matched value equals the first
    number but not the second, it picked the cumulative column — swap it.
    """
    if not _MIXED_QUARTER_TABLE.search(document_text or ""):
        return None

    changed = 0
    for m in matches:
        row = m.source_row_text or ""
        if not row or m.value is None:
            continue
        nums = [v for v in (_parse_locale_number(t) for t in _ALL_NUMBERS.findall(row)) if v is not None]
        if len(nums) < 2:
            continue
        first, second = nums[0], nums[1]
        if _close(m.value, first) and not _close(m.value, second):
            m.value = second
            changed += 1

    if changed:
        return (
            f"Detected a mixed six-month/quarter table and switched {changed} "
            "label-matched row(s) from cumulative 6M values to quarter transaction "
            "values (second numeric column) for quarterly extraction."
        )
    return None


_MAX_SUBTOTAL_COMBO = 4
_MAX_SUBTOTAL_BATCH = 40
_SUBTOTAL_PROXIMITY_LINES = 15


def _line_number(document_text: str, row_text: str) -> int | None:
    """Approximate line position of a match's own source line in the document."""
    row_text = (row_text or "").strip()
    if not row_text:
        return None
    idx = document_text.find(row_text)
    return document_text.count("\n", 0, idx) if idx >= 0 else None


def _maybe_reject_subtotal_matches(matches: list[_LabeledValue], document_text: str) -> str | None:
    """Null out a match whose value is really the sum of OTHER matches in this
    same batch — the prompt's subtotal-avoidance rule is not a guarantee, so
    back it with a deterministic check: several sibling Excel rows already
    breaking a PDF total into components (e.g. "Management fees, expenses",
    "Administrative expenses", "Property taxes", "Insurance"), plus one vaguer
    label (e.g. "Operating expenses") that the LLM matched to the PDF's TOTAL
    line instead of the one remaining specific caption, double the other rows
    into the model. A label whose own wording says "total" is exempt — it is
    supposed to hold a subtotal.

    BUG (JD 2026-09-07): the sum-of-siblings pool used to be every OTHER
    matched value in the whole batch, regardless of which statement it came
    from. With 30-40 unrelated matches spanning the income statement, balance
    sheet and cash flow statement all in one label-fallback call, some 2-4
    term combination coincidentally lands within the 0.1% tolerance of almost
    any given value by sheer combinatorics — e.g. "Marketing" (income
    statement) got rejected because it happened to equal the sum of some
    unrelated combination of "Free cash flow"/"Inventory"/"Equity" (balance
    sheet/cash-flow rows), which are obviously not its siblings. A genuine
    subtotal's components always sit in the SAME PDF table, a few lines away
    at most — so the pool is now restricted to other matches whose own source
    line is within `_SUBTOTAL_PROXIMITY_LINES` lines of the candidate's.
    """
    candidates = [
        m for m in matches
        if m.value is not None and "total" not in (m.label or "").lower()
    ]
    if not candidates or len(matches) > _MAX_SUBTOTAL_BATCH:
        return None

    lines = {id(m): _line_number(document_text, m.source_row_text or "") for m in matches}

    rejected: list[str] = []
    for m in candidates:
        my_line = lines[id(m)]
        if my_line is None:
            continue
        others = [
            other.value for other in matches
            if other is not m and other.value is not None and lines[id(other)] is not None
            and abs(lines[id(other)] - my_line) <= _SUBTOTAL_PROXIMITY_LINES
        ]
        if others and _sums_to(m.value, others, _MAX_SUBTOTAL_COMBO):
            rejected.append(m.label)
            m.value = None

    if not rejected:
        return None
    return (
        f"Rejected {len(rejected)} label-matched value(s) that equalled the sum of "
        f"other matched rows in this same batch — likely a PDF subtotal picked up "
        f"instead of the one specific caption still needed: {', '.join(rejected)}. "
        "Left null for human review rather than double-counting sibling rows."
    )


def _sums_to(target: float, pool: list[float], max_terms: int) -> bool:
    for k in range(2, min(max_terms, len(pool)) + 1):
        for combo in combinations(pool, k):
            if _close(target, sum(combo)):
                return True
    return False


_HAS_LETTER = re.compile(r"[A-Za-z\u00C0-\u024F]")


def _maybe_reject_uncaptioned_matches(matches: list[_LabeledValue]) -> str | None:
    """Null out a match whose source_row_text has no caption at all.

    The sum-of-siblings check in _maybe_reject_subtotal_matches only catches a
    subtotal if every one of its components was ALSO matched to some other
    label in this batch. That fails when the true narrower line (e.g. "Maintenance
    expenses") has no corresponding Excel row of its own to match against, so
    the total never equals a sum the check can see, and the label silently
    keeps the PDF's bare TOTAL line. That bare total line is recognisable on
    its own though: in these tables an unlabeled subtotal row is rendered as
    pure numbers with no caption before them (e.g. "(4,100,289) (2,018,911) ..."),
    whereas every genuinely named line has the caption's words in it. A label
    matched to a line with zero letters in source_row_text has no textual
    justification at all — reject it rather than trust it.

    A "derive by subtracting the other matched sibling lines" recovery was
    tried here instead of a flat reject, but proved unreliable in practice:
    which sibling values are even available in `matches` depends on an
    unrelated part of the pipeline (whether e.g. "Administrative expenses"
    was already claimed by the main taxonomy pass via alias matching before
    this label-fallback batch ran), so the same PDF input produced a
    correct residual on one run and a silently wrong one on another. Null +
    flag for human review is safer than a plausible-looking but occasionally
    wrong guess — do not resurrect the subtraction approach without also
    threading in full visibility of every value claimed elsewhere in the
    pipeline, not just this batch's `matches`.
    """
    rejected: list[str] = []
    for m in matches:
        row = (m.source_row_text or "").strip()
        if m.value is not None and row and not _HAS_LETTER.search(row):
            rejected.append(m.label)
            m.value = None

    if not rejected:
        return None
    return (
        f"Rejected {len(rejected)} label-matched value(s) whose source line had no "
        f"caption at all (a bare-numbers PDF subtotal row, not the named line the "
        f"label refers to): {', '.join(rejected)}. Left null for human review."
    )


_DECIMAL_NUMBER = re.compile(r"\d\.\d")


def _match_is_already_absolute(m: _LabeledValue) -> bool:
    """A label match sometimes comes from a raw-absolute-currency note table
    (e.g. a statement-of-cash-flows XBRL note stated to the nearest peso/rupee)
    sitting in the SAME document as a primary statement stated "in millions" —
    the filing's one declared `source_units` does not apply to that table, so
    blindly multiplying every match by it silently inflates these values by
    the scale factor (e.g. 1,000,000x too big). Genuine millions-scale figures
    routinely print a fractional part (e.g. "21,906.321") since one peso is a
    non-trivial fraction of a million; a raw absolute-currency table's numbers
    are whole, and rounded to a coarse absolute precision (e.g. nearest 1,000),
    so they print as clean integers instead. Use the row's own raw text (not
    just the parsed float, which loses this information) to tell them apart.
    """
    if m.value is None or m.value != int(m.value) or m.value % 1000 != 0:
        return False
    row = m.source_row_text or ""
    return bool(row) and not _DECIMAL_NUMBER.search(row)


def _maybe_reject_duplicate_canonical_matches(
    values: dict[str, float], claimed_values: dict[str, float]
) -> tuple[dict[str, float], str | None]:
    """Null a label match that exactly repeats an already-extracted canonical field's
    value — the same PDF line claimed twice under a different caption (e.g. an Excel
    row semantically close to an existing field, like "Non-operating income /
    expenses, (net)" next to a canonical "other_income"), which silently double-counts
    or, if one of the two is later subtracted, cancels that figure wherever both appear
    in the model's own formulas. Zero is excluded on both sides: many unrelated rows are
    legitimately (and coincidentally) blank/zero, which is not evidence of a shared
    source line.
    """
    rejected = []
    out = dict(values)
    for label, val in values.items():
        if val is None or abs(val) < 1e-9:
            continue
        if any(claimed and _close(val, claimed) for claimed in claimed_values.values()):
            rejected.append(label)
            del out[label]
    if not rejected:
        return values, None
    return out, (
        f"Rejected {len(rejected)} label-matched value(s) that exactly repeated an "
        f"already-extracted canonical field's value — likely the same PDF line claimed "
        f"twice under a different caption, which would double-count or cancel it in the "
        f"model's own formulas: {', '.join(rejected)}. Left null for human review."
    )


def extract_for_labels(
    labels: list[str],
    document_text: str,
    source_units: str = "units",
    claimed_values: dict[str, float] | None = None,
) -> tuple[dict[str, float], str | None]:
    """Match raw Excel row labels against the PDF and return label -> value in BASE units."""
    if not labels:
        return {}, None
    from finscan.extract.normalize import UNIT_MULTIPLIER
    llm = structured(_LabelExtraction)
    numbered = "\n".join(f"{i + 1}. {lbl}" for i, lbl in enumerate(labels))
    result: _LabelExtraction = llm.invoke([
        {"role": "system", "content": _LABEL_SYSTEM},
        {"role": "user", "content": _LABEL_USER.format(
            document=document_text, labels=numbered)},
    ])
    notes = [n for n in (
        _maybe_realign_label_quarter_values(result.matches, document_text),
        _maybe_reject_subtotal_matches(result.matches, document_text),
        _maybe_reject_uncaptioned_matches(result.matches),
    ) if n]
    multiplier = UNIT_MULTIPLIER.get(source_units, 1.0)
    already_absolute = [m.label for m in result.matches if _match_is_already_absolute(m)]
    if already_absolute:
        notes.append(
            f"{len(already_absolute)} label-matched value(s) looked like they came from a "
            f"raw-absolute-currency table rather than the filing's declared '{source_units}' "
            f"scale (whole numbers, no fractional part): {', '.join(already_absolute)}. "
            "Used them as-is instead of applying the filing-wide unit multiplier."
        )
    already_absolute_set = set(already_absolute)
    values = {
        m.label: _enforce_label_direction(
            m.label, m.value * (1.0 if m.label in already_absolute_set else multiplier)
        )
        for m in result.matches
        if m.value is not None and m.confidence >= 0.5
    }
    if claimed_values:
        values, dup_note = _maybe_reject_duplicate_canonical_matches(values, claimed_values)
        if dup_note:
            notes.append(dup_note)
    return values, (" ".join(notes) if notes else None)


def extract_for_labels_reliably(
    labels: list[str],
    document_text: str,
    source_units: str = "units",
    claimed_values: dict[str, float] | None = None,
) -> tuple[dict[str, float], str | None]:
    """Resolve each label in its own focused pass to avoid batch interference."""
    values: dict[str, float] = {}
    notes: list[str] = []
    for label in labels:
        matched, note = extract_for_labels([label], document_text, source_units, claimed_values)
        values.update(matched)
        if note:
            notes.append(f"{label}: {note}")
    return values, (" ".join(notes) if notes else None)

