"""Stage 1 by LLM: every table page read by a vision model, as a company opts in.

Chosen per company with `"reader": "llm"` in its mapping. It replaces the rule-based
reader entirely — titles, geometry, column parsing, OCR — and produces the same
pdf.json, so stages 2 and 3 run unchanged.

The model is good at structure, which is where rules are brittle: captions that
wrap over several lines (sometimes above and below their own figures), headers
stacked over several lines or printed once over a group of columns, bullets and
indentation. Its weakness is the one that matters most — a misread digit looks
exactly like a right one — so two rules stay non-negotiable:

* every figure must be printed on its page — checked against the PDF's own text
  layer, and marked unverified (never written by stage 3) when it is not;
* each page is read once and cached by the PDF's hash, so the same PDF always
  yields the same pdf.json, however often the pipeline runs.
"""
from __future__ import annotations

import base64
import io
import json
import re
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

#: Bump when the prompt changes what a page read returns; older caches are kept
#: (re-reading costs money and moves figures) but are reported as older.
PROMPT_VERSION = "2"

#: Kept free of any one filer's wording: it describes layouts, not companies.
PROMPT = """You extract financial tables from one page of a company filing.
Return ONLY a JSON object, no prose, no code fences:
{"tables": [{"title": "<the table's title as printed, including any section or note number>",
             "units": "<the currency and scale as printed for the table>",
             "columns": ["<the complete header of each value column, left to right>"],
             "rows": [{"caption": "<the complete caption of the line>",
                       "values": [<one entry per column: the number exactly as printed, as a string, keeping parentheses, signs and separators; null if the cell is blank or a dash>]}]}]}
Rules:
- Columns: build each header from all the header text above that column, top to bottom, including a label printed once across a group of columns (such as a period or "year to date" label) — repeat it for every column it spans.
- Captions: a caption can wrap over several lines, including lines printed above and below its own figures; join all its parts into one caption. Keep a leading bullet or dash out of the caption.
- One row per printed line item, in printed order. Include subtotal and total lines, using the printed caption or "" when the line has none.
- Never compute, round, re-sign or reorder numbers. Omit headings that carry no numbers. Omit note-reference columns.
- If the page has no table of figures, return {"tables": []}."""

#: The role code that opens every BMV XBRL statement title: "[310000] Statement of ...".
_XBRL_TITLE = re.compile(r"\s*\[\d{6}\]")

#: Primary statements whose later pages may print no title of their own.
_CONTINUED_KINDS = frozenset({"balance_sheet", "cash_flow", "comprehensive_income",
                              "income_statement"})

#: A page is sent when its text layer carries at least this many figures, or has
#: no text at all (a scan). Narrative pages are not worth a model call.
_MIN_FIGURES = 20

_FIGURE = re.compile(r"\(?-?\d[\d,]*(?:\.\d{3})*(?:\.\d+)?\)?")
_BULLET = re.compile(r"^\s*[-–•·]+\s*")
_QUARTER = re.compile(r"\b(?:Q([1-4])\s*(\d{2,4})|([1-4])Q\s*(\d{2,4}))\b", re.IGNORECASE)
_GROUP_PERIOD = re.compile(r"\b([1-4])\s*([QH])\s*(\d{2,4})\b", re.IGNORECASE)
_BARE_YEAR = re.compile(r"^\s*((?:19|20)\d{2}|\d{2})\*?\s*$")
_TL_OR_TRY = re.compile(r"\b(?:tl|try)\b", re.IGNORECASE)
_DOTTED_THOUSANDS = re.compile(r"^-?\d{1,3}(?:\.\d{3})+$")
#: Chilean/European grouping that no other reading can explain: two or more
#: dot-separated groups ("1.512.033.764"), or dotted groups with a decimal comma
#: ("1.234,56"). Safe in any column; a single group ("1.234") is not.
_DOTTED_GROUPS = re.compile(r"\d{1,3}(?:\.\d{3}){2,}")
_DOTTED_DECIMAL = re.compile(r"\d{1,3}(?:\.\d{3})+,\d+")
#: A decimal comma ("177,67"). A US thousands comma is always followed by three
#: digits, so one or two digits can only be a decimal.
_DECIMAL_COMMA = re.compile(r"\d+,\d{1,2}")
#: Chilean-peso scale markers ("M$", "MM$"). A table in them prints whole units of
#: the scale, so a lone "748.160" is 748,160 and not 748.16.
_PESO_SCALE = re.compile(r"(?<!\w)MM?\$|\bCLP\b|\bCh\$", re.IGNORECASE)
_NON_MONETARY_COLUMN = re.compile(
    r"%|rate|margin|multiple|leverage|y/y|q/q|variance|var\b|change",
    re.IGNORECASE,
)


def cache_file(cache_dir: str | Path, sha: str) -> Path:
    return Path(cache_dir) / f"{sha}.llm.json"


def _page_text(page) -> str:
    """Both readings of the text layer: by position and in stream order."""
    try:
        return "\n".join([page.extract_text() or "",
                          page.extract_text(x_tolerance=3, use_text_flow=True) or ""])
    except Exception:       # pragma: no cover - malformed page
        return ""


def _printed(text: str) -> set[float]:
    from finscan2.pdf.statements import parse_number

    tokens = _FIGURE.findall(text or "")
    # A decimal comma is only read on a page that also prints dotted thousands: in a
    # US-format page "56,51" is a misread "5,651", never 56.51.
    european = any(_DOTTED_GROUPS.fullmatch(t.strip().strip("()").lstrip("-")) for t in tokens)
    out = set()
    for token in tokens:
        value = parse_number(token)
        if value is not None:
            out.add(value)
            out.add(-value)          # a sign the layout shows by a column, not a bracket
        compact = token.strip().strip("()").replace("−", "-")
        if _DOTTED_THOUSANDS.fullmatch(compact):
            value = parse_number(token.replace(".", ""))
            if value is not None:
                out.add(value)
                out.add(-value)
        if european and _DECIMAL_COMMA.fullmatch(compact.lstrip("-")):
            value = parse_number(compact.replace(",", "."))
            if value is not None:
                out.add(value)
                out.add(-value)
    return out


def _is_european_table(table: dict) -> bool:
    """True when a table prints dotted thousands ("1.512.033.764").

    The marker that licenses the Chilean/European readings below: two or more dot
    groups cannot be anything else, whereas a lone "1.234" or "56,51" can.
    """
    for row in table.get("rows") or []:
        for cell in row.get("values") or []:
            body = str(cell or "").replace(" ", "").replace("−", "-").strip().strip("()").lstrip("-")
            if _DOTTED_GROUPS.fullmatch(body) or _DOTTED_DECIMAL.fullmatch(body):
                return True
    return False


def _european(body: str, header: str, context: str, european: bool = False) -> str | None:
    """A Chilean/Turkish/European-format figure as plain digits, or None.

    `header` is the column's own header (where TL/TRY mark a dotted-thousands
    table); `context` adds the table's printed units (where "M$" does); `european`
    says the table prints dotted thousands elsewhere. A lone "1.234" is only
    thousands, and "177,67" only a decimal, when one of them says so.
    """
    if _DOTTED_DECIMAL.fullmatch(body):
        return body.replace(".", "").replace(",", ".")
    if _DOTTED_GROUPS.fullmatch(body):
        return body.replace(".", "")
    if _DOTTED_THOUSANDS.fullmatch(body) and (
            european or _TL_OR_TRY.search(header) or _PESO_SCALE.search(context)):
        return body.replace(".", "")
    if european and _DECIMAL_COMMA.fullmatch(body):
        return body.replace(",", ".")
    return None


def _number(cell, header: str, units: str = "", european: bool = False):
    """Parse an LLM cell, preserving dotted thousands and decimal commas."""
    from finscan2.pdf.statements import parse_number

    if cell is None or str(cell).strip() in {"", "-", "–", "—"}:
        return None
    text = str(cell).replace(" ", "").replace("%", "")
    compact = text.strip().strip("()").replace("−", "-")
    plain = _european(compact.lstrip("-"), header, f"{header} {units}", european)
    if plain is not None:
        lead = "-" if compact.startswith("-") else ""
        text = f"({lead}{plain})" if text.startswith("(") and text.endswith(")") else f"{lead}{plain}"
    return parse_number(text)


def _render(pdf, page_no: int) -> str:
    """The page as a base64 PNG. Done one page at a time: the PDF renderer
    (pypdfium2) is not thread-safe, and rendering in parallel aborts the process."""
    image = pdf.pages[page_no - 1].to_image(resolution=200).original
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _read_page(page_no: int, encoded: str) -> dict:
    """One model call: the page image in, the page's tables as JSON out."""
    from finscan2.llm import chat_model

    try:
        response = chat_model().invoke([
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": f"Page {page_no}."},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{encoded}", "detail": "high"}}]},
        ])
        content = re.sub(r"^```(?:json)?|```$", "", (response.content or "").strip()).strip()
        return json.loads(content)
    except Exception as error:      # reported per page; the rest of the PDF still reads
        return {"tables": [], "error": f"{type(error).__name__}: {error}"[:300]}


def _cover_period_end(page_text: str) -> str | None:
    """The period a presentation's cover unambiguously declares, if any."""
    ends: set[str] = set()
    for match in _QUARTER.finditer(page_text or ""):
        quarter = int(match.group(1) or match.group(3))
        raw_year = match.group(2) or match.group(4)
        year = int(raw_year) if len(raw_year) == 4 else 2000 + int(raw_year)
        month = quarter * 3
        if month == 12:
            ends.add(f"{year}-12-31")
        else:
            ends.add(f"{year}-{month:02d}-30")
    return next(iter(ends)) if len(ends) == 1 else None


def _inherit_cover_period(statements, cover_end: str) -> list[int]:
    """Date undated monetary presentation tables without inventing comparison dates."""
    inherited: list[int] = []
    for statement in statements:
        if not statement.columns or any(column.end for column in statement.columns):
            continue
        changed = False
        for column in statement.columns:
            if _NON_MONETARY_COLUMN.search(column.header):
                continue
            column.end = cover_end
            changed = True
        if changed:
            inherited.append(statement.page)
    return inherited


def _inherit_group_period_headers(columns, page_text: str) -> bool:
    """Restore omitted Q/H group labels when later headers anchor their position."""
    labels = []
    for match in _GROUP_PERIOD.finditer(page_text or ""):
        number, kind, raw_year = int(match.group(1)), match.group(2).upper(), match.group(3)
        year = int(raw_year) if len(raw_year) == 4 else 2000 + int(raw_year)
        months = 3 if kind == "Q" else 6
        month = number * months
        day = monthrange(year, month)[1]
        labels.append((match.group(0).replace(" ", ""), year, months,
                       f"{year}-{month:02d}-{day:02d}"))

    changed = False
    for index, column in enumerate(columns):
        if not column.end or not column.months:
            continue
        known = next((position for position, (_, year, months, end) in enumerate(labels)
                      if year == int(column.end[:4]) and months == column.months
                      and end == column.end), None)
        if known is None:
            continue
        missing = []
        for prior in range(index - 1, -1, -1):
            if columns[prior].end:
                break
            if _BARE_YEAR.fullmatch(columns[prior].header):
                missing.append(prior)
            elif not _NON_MONETARY_COLUMN.search(columns[prior].header):
                break
        missing.reverse()
        source = labels[known - len(missing):known]
        if len(source) != len(missing):
            continue
        years = [int(_BARE_YEAR.fullmatch(columns[position].header).group(1))
                 for position in missing]
        years = [year if year > 99 else 2000 + year for year in years]
        if years != [year for _, year, _, _ in source]:
            continue
        for position, (label, _, months, end) in zip(missing, source):
            restored = columns[position]
            restored.header = f"{label} {restored.header}"
            restored.kind, restored.months, restored.end = "period", months, end
            changed = True
    return changed


def _inherit_flow_note_periods(statements) -> list[int]:
    """Date undated flow notes from the matching year in a primary statement."""
    from finscan2.pdf.statements import _FLOW_NOTE_TITLE

    primary_by_year = {}
    for statement in statements:
        if statement.kind != "income_statement":
            continue
        for column in statement.columns:
            if column.end and column.months:
                primary_by_year.setdefault(column.end[:4], (column.end, column.months))
    inherited: list[int] = []
    for statement in statements:
        if (not _FLOW_NOTE_TITLE.search(statement.title or "") or not statement.columns
                or any(column.end for column in statement.columns)):
            continue
        changed = False
        for column in statement.columns:
            year = re.search(r"(?:19|20)\d{2}", column.header)
            period = primary_by_year.get(year.group(0)) if year else None
            if period is None:
                continue
            column.end, column.months = period
            column.kind = "period"
            changed = True
        if changed:
            inherited.append(statement.page)
    return inherited


def _printed_line(caption: str, text: str, count: int):
    """The figures the page's text layer prints for a caption, or None.

    Only when exactly one printed line starts with the caption's first words, and
    it holds at least `count` figures; the last `count` are returned, so a note
    reference printed before the figures falls away.
    """
    from finscan2.model.schema import normalize_label

    words = normalize_label(caption).split()[:5]
    if not words or not count:
        return None
    lead = " ".join(words)
    candidates = set()
    for line in text.split("\n"):
        if not normalize_label(line).startswith(lead):
            continue
        tokens = _FIGURE.findall(line)
        if len(tokens) >= count:
            candidates.add(tuple(tokens[-count:]))
    return next(iter(candidates)) if len(candidates) == 1 else None


def _signs_from_text(caption: str, values: list, text: str, headers: list[str],
                     units: str = "", european: bool = False):
    """Restore the sign the page prints where the model read the right digits.

    Every printed figure is accepted in either sign (a layout may show the sign by
    a column, not a bracket), so a dropped parenthesis passes verification and
    writes an expense as income. When the line's figures all agree with the row in
    size, the line is the right one and its own signs are authoritative.
    """
    filled = [j for j, v in enumerate(values) if v is not None]
    tokens = _printed_line(caption, text, len(filled))
    if tokens is None:
        return values, []
    parsed = {j: _number(token, headers[j] if j < len(headers) else "", units, european)
              for j, token in zip(filled, tokens)}
    if any(parsed[j] is None or abs(parsed[j]) != abs(values[j]) for j in filled):
        return values, []
    fixed, changes = list(values), []
    for j in filled:
        if parsed[j] != values[j]:
            fixed[j] = parsed[j]
            changes.append(f"'{caption}' sign {values[j]:,.0f} -> {parsed[j]:,.0f}")
    return fixed, changes


def _repair_from_text(caption: str, values: list, unverified: list[int],
                      text: str, headers: list[str], units: str = "",
                      european: bool = False):
    """Replace a misread figure with the one the page's text layer prints for the line.

    Only when one printed line starts with the caption, every figure the model got
    right agrees with that line, and each replaced figure keeps its digit count: a
    one-digit slip, not a different number.
    """
    filled = [j for j, v in enumerate(values) if v is not None]
    if not unverified or not filled:
        return values, unverified, []
    tokens = _printed_line(caption, text, len(filled))
    if tokens is None:
        return values, unverified, []

    parsed = {j: _number(token, headers[j] if j < len(headers) else "", units, european)
              for j, token in zip(filled, tokens)}
    if any(parsed[j] is None for j in filled):
        return values, unverified, []
    if any(j not in unverified and abs(parsed[j]) != abs(values[j]) for j in filled):
        return values, unverified, []

    fixed = list(values)
    changes = []
    # A digit-count change is a different number, not a slip — unless the row's
    # alignment with the printed line is proven: a figure the model got right
    # already agreed with it, or every column holds a figure so the right-aligned
    # tokens cannot have shifted. Dropped digits ("909.333.911" read as "99.333.931")
    # are the commonest vision error on dense dotted-thousands tables. Only trusted
    # there: elsewhere, text lines carry fragments and footnote markers that make a
    # right-aligned token match wrong ("12,203" split into "12" and "203").
    aligned = european and (any(j not in unverified for j in filled)
                            or len(filled) == len(values))
    for j in unverified:
        if j not in parsed or parsed[j] == values[j]:
            continue
        if not aligned and len(str(int(abs(parsed[j])))) != len(str(int(abs(values[j])))):
            continue
        changes.append(f"'{caption}' {values[j]:,.0f} -> {parsed[j]:,.0f}")
        fixed[j] = parsed[j]
    remaining = [j for j in unverified if fixed[j] not in _printed(text)]
    return fixed, remaining, changes


def doc_from_tables(path: str, sha: str, pages_json: dict[int, dict],
                    page_texts: dict[int, str], model_name: str):
    """The LLM's tables as a PdfDoc — the same contract the rule-based reader keeps."""
    from finscan2.pdf.columns import parse_columns_from_headers
    from finscan2.pdf.read import _CURRENCIES, _UNIT_PATTERNS, _sniff
    from finscan2.pdf.statements import _inherit_title_period, _stated_span, classify
    from finscan2.schema import Issue, Page, PdfDoc, Statement, StatementRow

    issues: list[Issue] = []
    statements: list[Statement] = []
    unprinted: dict[int, int] = {}
    unchecked: list[int] = []
    inherited_group_headers: list[int] = []
    corrected: list[str] = []
    previous_last_page = 0

    for page_no in sorted(pages_json):
        payload = pages_json[page_no] or {}
        if payload.get("error"):
            issues.append(Issue(code="llm_page_failed", severity="warning", page=page_no,
                                message=f"Page {page_no} could not be read: {payload['error']}"))
        text = page_texts.get(page_no, "")
        printed = _printed(text) if text.strip() else None
        if printed is None and payload.get("tables"):
            unchecked.append(page_no)

        for table in payload.get("tables") or []:
            title = (table.get("title") or "").strip()
            headers = [str(h or "").strip() for h in table.get("columns") or []]
            kind = classify(title)
            columns = parse_columns_from_headers(
                headers, statement_kind=kind, default_months=_stated_span(" ".join(headers)))
            _inherit_title_period(columns, title, kind)
            if _inherit_group_period_headers(columns, text):
                inherited_group_headers.append(page_no)
            rows: list[StatementRow] = []
            table_units = str(table.get("units") or "")
            european = _is_european_table(table)
            for row in table.get("rows") or []:
                values = [_number(v, headers[index] if index < len(headers) else "",
                                  table_units, european)
                          for index, v in enumerate(row.get("values") or [])]
                if not any(v is not None for v in values):
                    continue
                caption = _BULLET.sub("", str(row.get("caption") or "")).strip()
                unverified = ([] if printed is None else
                              [j for j, v in enumerate(values) if v is not None and v not in printed])
                if unverified:
                    values, unverified, changes = _repair_from_text(
                        caption, values, unverified, text, headers, table_units, european)
                    corrected.extend(f"page {page_no}: {c}" for c in changes)
                if european and printed is not None:
                    values, changes = _signs_from_text(caption, values, text, headers,
                                                       table_units, european)
                    corrected.extend(f"page {page_no}: {c}" for c in changes)
                if unverified:
                    unprinted[page_no] = unprinted.get(page_no, 0) + len(unverified)
                rows.append(StatementRow(caption=caption, values=values,
                                         raw=json.dumps(row, ensure_ascii=False),
                                         unverified=unverified))
            if not rows:
                continue
            units = _sniff(f"{table.get('units') or ''}\n{title}\n{' '.join(headers)}",
                           _UNIT_PATTERNS)
            if units is None and _XBRL_TITLE.match(title):
                # A BMV XBRL statement ("[310000] Statement of ...") states only its
                # currency and prints every figure in full. Left unset it would
                # inherit the press release's scale from the same PDF.
                units = "units"
            previous = statements[-1] if statements else None
            same_columns = (previous is not None
                            and [(c.months, c.end) for c in previous.columns]
                            == [(c.months, c.end) for c in columns])
            same = same_columns and previous.title == title
            # A continuation page often prints no title of its own (BMV XBRL reports:
            # the balance sheet's liabilities and the cash flow's investing and
            # financing sections). It belongs to the primary statement on the page
            # before when its columns are the same.
            untitled_continuation = (
                same_columns and not title and previous.kind in _CONTINUED_KINDS
                and page_no == previous_last_page + 1)
            if same or untitled_continuation:
                # A statement continued over pages under the same title and columns.
                previous.rows.extend(rows)
                previous_last_page = page_no
                continue
            statements.append(Statement(page=page_no, kind=kind, title=title, heading="",
                                        columns=columns, rows=rows, units=units))
            previous_last_page = page_no

    pages = [Page(page=n, source="vision" if n in pages_json else "text",
                  chars=len(page_texts.get(n, "")), text=page_texts.get(n, ""))
             for n in sorted(set(page_texts) | set(pages_json))]
    doc = PdfDoc(path=path, sha256=sha, pages=pages, statements=statements, issues=issues,
                 ocr_pages=sorted(pages_json), ocr_engine=f"llm:{model_name}")

    primary = [s for s in statements if s.units and s.columns]
    doc.units = primary[0].units if primary else None
    if doc.units is None:
        # A BMV/XBRL report prints no scale on its tables; it declares a rounding
        # level once ("Grado de redondeo ...: 0"). Left unknown, its full-peso figures
        # would be scaled as the merged release's millions - a million times too big.
        from finscan2.pdf.read import _xbrl_rounding

        if rounded := _xbrl_rounding(doc, statements):
            doc.units = "units"
            for statement in statements:
                statement.units = statement.units or "units"
            issues.append(Issue(
                code="units_from_rounding", severity="info",
                message=f"No table states a scale; the report declares its rounding level "
                        f"as {rounded}, and its figures are multiples of it, so figures "
                        f"are printed in full (units)."))
    doc.currency = next((c for s in statements
                         if (c := _sniff(" ".join([s.title] + [x.header for x in s.columns]),
                                         _CURRENCIES))), None) \
        or next((c for t in page_texts.values() if (c := _sniff(t, _CURRENCIES))), None)

    cover_end = _cover_period_end(page_texts.get(1, ""))
    if cover_end and (inherited_pages := _inherit_cover_period(statements, cover_end)):
        issues.append(Issue(
            code="llm_inherited_cover_period", severity="info",
            message=f"Inferred {cover_end} from the presentation cover for undated monetary "
                    f"table(s) on page(s) {inherited_pages}; rate and variance columns were "
                    "left undated."))

    if inherited_pages := _inherit_flow_note_periods(statements):
        issues.append(Issue(
            code="llm_inherited_flow_note_period",
            severity="info",
            message="Inferred period dates for undated flow note table(s) on page(s) "
                    f"{inherited_pages} from the primary statements' matching year columns."))

    if corrected:
        issues.append(Issue(
            code="llm_figure_corrected_from_text", severity="info",
            message="Figures the model misread were replaced by the page's printed text: "
                    + "; ".join(corrected)))

    if inherited_group_headers:
        issues.append(Issue(
            code="llm_inherited_group_period_headers", severity="info",
            message="Recovered omitted Q/H group labels for table column headers on page(s) "
                    f"{sorted(set(inherited_group_headers))} from their page text."))

    issues.append(Issue(
        code="llm_read", severity="info",
        message=f"Read by {model_name}: {len(pages_json)} page(s), {len(statements)} "
                f"table(s). Each figure is checked against the page's text layer; the "
                f"read is cached by the PDF's hash, so later runs return the same figures."))
    if unprinted:
        issues.append(Issue(
            code="llm_figure_not_printed", severity="warning",
            message="Figures the model returned that the page's text layer does not "
                    "print, which stage 3 will not write: "
                    + ", ".join(f"page {p}: {n}" for p, n in sorted(unprinted.items()))))
    if unchecked:
        issues.append(Issue(
            code="llm_unchecked", severity="warning",
            message=f"Page(s) {unchecked} have no text layer (scans), so their figures "
                    f"could not be checked against the PDF; spot-check them."))
    if not statements:
        issues.append(Issue(code="no_statements", severity="error",
                            message="The model found no table of figures in this document."))
    if doc.units is None:
        issues.append(Issue(code="units_unknown", severity="error",
                            message="No table states a scale (thousands, millions, ...)."))
    return doc


def _model_name() -> str:
    """Return the actual model target used by the configured provider."""
    from finscan2.config import settings

    return settings.model_name


def read_pdf_llm(path: str | Path, sha: str, cache_dir: str | Path | None = None,
                 enabled: bool = True):
    """Read a filing with the configured vision model, reusing a cached read."""
    import pdfplumber

    path = str(path)
    model_name = _model_name()
    cached = cache_file(cache_dir, sha) if cache_dir else None
    store: dict = {}
    if cached and cached.exists():
        store = json.loads(cached.read_text(encoding="utf-8"))
        if store.get("model") != model_name:
            store = {}
    pages_json = {int(k): v for k, v in (store.get("pages") or {}).items()}

    page_texts: dict[int, str] = {}
    wanted: list[int] = []
    images: dict[int, str] = {}
    with pdfplumber.open(path) as pdf:
        for index, page in enumerate(pdf.pages, start=1):
            text = _page_text(page)
            page_texts[index] = text
            if not text.strip() or len(_FIGURE.findall(text)) >= _MIN_FIGURES:
                wanted.append(index)
                if enabled and index not in pages_json:
                    images[index] = _render(pdf, index)

    missing = [n for n in wanted if n not in pages_json]
    if missing and enabled:
        # Only the network calls run in parallel; rendering happened above.
        with ThreadPoolExecutor(max_workers=6) as pool:
            for n, result in zip(missing, pool.map(lambda n: _read_page(n, images[n]), missing)):
                pages_json[n] = result
        if cached:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps({
                "sha256": sha, "prompt_version": PROMPT_VERSION,
                "model": model_name,
                "pages": {str(k): v for k, v in sorted(pages_json.items())},
            }, indent=1, ensure_ascii=False), encoding="utf-8")

    older = store.get("prompt_version") not in (None, PROMPT_VERSION) and not missing
    doc = doc_from_tables(path, sha, {n: pages_json[n] for n in wanted if n in pages_json},
                          page_texts, model_name)
    if older:
        from finscan2.schema import Issue

        doc.issues.append(Issue(
            code="llm_read_older_prompt", severity="info",
            message=f"This filing's cached read used prompt version "
                    f"{store.get('prompt_version')} (current {PROMPT_VERSION}); it is kept "
                    f"so figures already reviewed do not move. Delete "
                    f"{cached.name if cached else 'the cached read'} to read it again."))
    return doc
