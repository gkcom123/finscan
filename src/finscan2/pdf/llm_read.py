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

#: A page is sent when its text layer carries at least this many figures, or has
#: no text at all (a scan). Narrative pages are not worth a model call.
_MIN_FIGURES = 20

_FIGURE = re.compile(r"\(?-?\d[\d,]*\.?\d*\)?")
_BULLET = re.compile(r"^\s*[-–•·]+\s*")


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

    out = set()
    for token in _FIGURE.findall(text or ""):
        value = parse_number(token)
        if value is not None:
            out.add(value)
            out.add(-value)          # a sign the layout shows by a column, not a bracket
    return out


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


def doc_from_tables(path: str, sha: str, pages_json: dict[int, dict],
                    page_texts: dict[int, str], model_name: str):
    """The LLM's tables as a PdfDoc — the same contract the rule-based reader keeps."""
    from finscan2.pdf.columns import parse_columns_from_headers
    from finscan2.pdf.read import _CURRENCIES, _UNIT_PATTERNS, _sniff
    from finscan2.pdf.statements import _stated_span, classify, parse_number
    from finscan2.schema import Issue, Page, PdfDoc, Statement, StatementRow

    def number(cell):
        if cell is None or str(cell).strip() in {"", "-", "–", "—"}:
            return None
        return parse_number(str(cell).replace(" ", "").replace("%", ""))

    issues: list[Issue] = []
    statements: list[Statement] = []
    unprinted: dict[int, int] = {}
    unchecked: list[int] = []

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
            rows: list[StatementRow] = []
            for row in table.get("rows") or []:
                values = [number(v) for v in row.get("values") or []]
                if not any(v is not None for v in values):
                    continue
                caption = _BULLET.sub("", str(row.get("caption") or "")).strip()
                unverified = ([] if printed is None else
                              [j for j, v in enumerate(values) if v is not None and v not in printed])
                if unverified:
                    unprinted[page_no] = unprinted.get(page_no, 0) + len(unverified)
                rows.append(StatementRow(caption=caption, values=values,
                                         raw=json.dumps(row, ensure_ascii=False),
                                         unverified=unverified))
            if not rows:
                continue
            units = _sniff(f"{table.get('units') or ''}\n{title}\n{' '.join(headers)}",
                           _UNIT_PATTERNS)
            previous = statements[-1] if statements else None
            same = (previous is not None and previous.title == title
                    and [(c.months, c.end) for c in previous.columns]
                    == [(c.months, c.end) for c in columns])
            if same:
                # A statement continued over pages under the same title and columns.
                previous.rows.extend(rows)
                continue
            statements.append(Statement(page=page_no, kind=kind, title=title, heading="",
                                        columns=columns, rows=rows, units=units))

    pages = [Page(page=n, source="vision" if n in pages_json else "text",
                  chars=len(page_texts.get(n, "")), text=page_texts.get(n, ""))
             for n in sorted(set(page_texts) | set(pages_json))]
    doc = PdfDoc(path=path, sha256=sha, pages=pages, statements=statements, issues=issues,
                 ocr_pages=sorted(pages_json), ocr_engine=f"llm:{model_name}")

    primary = [s for s in statements if s.units and s.columns]
    doc.units = primary[0].units if primary else None
    doc.currency = next((c for s in statements
                         if (c := _sniff(" ".join([s.title] + [x.header for x in s.columns]),
                                         _CURRENCIES))), None) \
        or next((c for t in page_texts.values() if (c := _sniff(t, _CURRENCIES))), None)

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


def read_pdf_llm(path: str | Path, sha: str, cache_dir: str | Path | None = None,
                 enabled: bool = True):
    """Read a filing with the configured vision model, reusing a cached read."""
    import pdfplumber

    from finscan.llm.factory import settings

    path = str(path)
    cached = cache_file(cache_dir, sha) if cache_dir else None
    store: dict = {}
    if cached and cached.exists():
        store = json.loads(cached.read_text(encoding="utf-8"))
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
                "model": store.get("model") or settings.openai_model,
                "pages": {str(k): v for k, v in sorted(pages_json.items())},
            }, indent=1, ensure_ascii=False), encoding="utf-8")

    model_name = store.get("model") or settings.openai_model
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
