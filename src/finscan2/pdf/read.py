"""Stage 1 — filing PDF to pdf.json.

Pure function of the file's bytes, cached by SHA-256. No workbook access, no
mapping knowledge, no canonical vocabulary: this stage records what the document
says, and nothing about what anyone intends to do with it.

Caching is not an optimisation here, it is a correctness property. Pages flattened
to images are transcribed by a vision model, which does not return the same text
twice; without a cache the figures a debugging session sees change under it.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from finscan2.pdf import ocr
from finscan2.pdf.notes import extract_notes
from finscan2.pdf.statements import (
    column_alignment,
    extract_continuation,
    extract_embedded,
    extract_statement,
)
from finscan2.pdf.text import extract_page_text, render_tables
from finscan2.pdf.verify import verify_rows
from finscan2.schema import PARSER_VERSION, Issue, Page, PdfDoc, Statement

#: Printed scale markers. Checked in this order so "SAR '000" is read as thousands
#: before a bare "000" elsewhere on the page can confuse it.
_UNIT_PATTERNS: list[tuple[str, str]] = [
    (r"\bin\s+(?:rs\.?\s+)?crores?\b|\bcr\.?\b", "crores"),
    (r"\b(?:in\s+)?(?:lakhs?|lacs?)\b", "lakhs"),
    (r"\b(?:in\s+)?billions?\b|\bbn\b", "billions"),
    (r"\b(?:in\s+)?millions?\b|\bmn\b|\bmm\b", "millions"),
    (r"['’]000s?\b|\bin\s+thousands?\b|\bthousands?\b|\bmiles\b", "thousands"),
]

_CURRENCIES: list[tuple[str, str]] = [
    (r"\bSAR\b|⃂|﷼", "SAR"), (r"\bAED\b", "AED"), (r"\bQAR\b", "QAR"),
    (r"\bINR\b|\bRs\.?\b|₹", "INR"), (r"\bRMB\b|\bCNY\b|¥", "CNY"),
    (r"\bHKD\b|\bHK\$", "HKD"), (r"\bUSD\b|\bUS\$", "USD"),
    (r"\bEUR\b|€", "EUR"), (r"\bGBP\b|£", "GBP"),
    (r"\bMXN\b|\bMX\$", "MXN"), (r"\bTRY\b", "TRY"), (r"\bKRW\b", "KRW"),
]


#: A scanned table: one image covers the page, and the text layer is somebody
#: else's OCR of it — Airtel's results carry "21,n6" and "105,{)44" in theirs.
#: Only pages dense with figures are re-read; a scanned auditor's letter is not.
_SCAN_COVER = 0.9
_SCAN_MIN_FIGURES = 100


def _is_scanned_table(page, text: str) -> bool:
    if not text.strip():
        return False
    try:
        area = float(page.width) * float(page.height)
        cover = max(((float(i["x1"]) - float(i["x0"])) * (float(i["bottom"]) - float(i["top"]))
                     for i in page.images or []), default=0.0)
    except Exception:       # pragma: no cover - malformed page
        return False
    if not area or cover < _SCAN_COVER * area:
        return False
    return len(re.findall(r"\d[\d,]*\.?\d*", text)) >= _SCAN_MIN_FIGURES


def sha256_of(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sniff(text: str, patterns: list[tuple[str, str]]) -> str | None:
    low = (text or "").lower()
    for pattern, value in patterns:
        if re.search(pattern, low, re.IGNORECASE):
            return value
    return None


#: How far above a note table's first figure row its header block can reach, and
#: the word count that marks a line as prose rather than a column heading.
_HEADER_BLOCK_LINES = 8
_PROSE_WORDS = 10


def table_section(statement: Statement, page_text: str) -> str:
    """The text that describes a statement's table: its title line down to its
    first figure row, plus the column headers.

    This is where a filing states the scale its figures are printed in ("Income
    Statement (USD millions)", "(Amounts in SAR '000)", "Million TL"). The rest of
    the page is narrative, and narrative quotes figures at whatever scale reads
    well — Gruma's "total assets of US$5.4 billion" beside a table in millions.
    """
    lines = (page_text or "").splitlines()
    start = next((i for i, line in enumerate(lines)
                  if statement.title and statement.title in line), None)
    section: list[str] = []
    if start is not None:
        first_row = statement.rows[0].raw if statement.rows else None
        for line in lines[start:]:
            if first_row and line.strip() == first_row:
                break
            section.append(line)
    else:
        # A note table's title is synthesised ("Note 10: SEGMENT REPORTING"), so
        # the section is the header block directly above its first figure row —
        # Almarai prints "'000 '000 '000" there — up to the prose that
        # introduces the table.
        first_row = statement.rows[0].raw if statement.rows else None
        end = next((i for i, line in enumerate(lines)
                    if first_row and line.strip() == first_row), None)
        section = [statement.title, statement.heading]
        for line in reversed(lines[max(0, (end or 0) - _HEADER_BLOCK_LINES):end or 0]):
            if len(line.split()) > _PROSE_WORDS:
                break
            section.append(line)
    section.extend(c.header for c in statement.columns)
    return "\n".join(section)


#: An XBRL-rendered report (Mexico's BMV) states its PRECISION, not its scale:
#: "Level of rounding used in financial statements: THOUSAND OF DOLLARS" above
#: figures printed in full — 1,649,941,000.
_ROUNDING = re.compile(
    r"level\s+of\s+rounding[^:\n]*:\s*(thousands?|millions?)\b", re.IGNORECASE)


def _xbrl_rounding(doc: PdfDoc, statements: list[Statement]) -> str | None:
    """The declared rounding level, when the statements' figures prove it is one.

    Figures printed in full and rounded to thousands are all multiples of 1,000;
    figures printed in thousands are not. Per-share amounts and ratios are small
    and skipped. Anything short of 95% of large figures agreeing leaves the scale
    unknown rather than guessed.
    """
    match = next((m for p in doc.pages if (m := _ROUNDING.search(p.text or ""))), None)
    if match is None:
        return None
    step = 1e3 if match.group(1).lower().startswith("thousand") else 1e6
    figures = [abs(v) for s in statements if s.note is None for r in s.rows
               for v in r.values if v is not None and abs(v) >= step]
    # Counts ride along in these reports ("Number of employees 9,237") and are
    # not rounded; money printed in thousands would almost never land on a
    # multiple of 1,000. So a large majority is proof, a near-miss is not.
    if len(figures) < 20 or sum(1 for v in figures if v % step == 0) < 0.95 * len(figures):
        return None
    return match.group(1).lower()


def _document_units(doc: PdfDoc) -> tuple[str | None, str | None, list[Issue]]:
    """Scale and currency, read only from the statements' own table sections.

    Primary statements whose columns were read come first: they are the tables
    stage 3 reads figures from. Scale is never taken from narrative text — a
    wrong scale silently moves every figure by a factor of a thousand, while an
    unknown one stops the run and asks. Currency falls back to page text, since
    it does not change a figure's magnitude.
    """
    text_by_page = {p.page: p.text for p in doc.pages}
    # Only tables whose columns were read: those are the figures stage 3 takes.
    # A page matched as a statement but with no columns is usually prose (Gruma's
    # BMV notes: "The amount of $369 thousand dollars"), and prose quotes figures
    # at whatever scale reads well.
    ordered = sorted((s for s in doc.statements if s.columns),
                     key=lambda s: (s.note is not None, s.page))
    found: list[tuple[Statement, str]] = []
    currency = None
    for statement in ordered:
        section = table_section(statement, text_by_page.get(statement.page, ""))
        if scale := _sniff(section, _UNIT_PATTERNS):
            found.append((statement, scale))
        currency = currency or _sniff(section, _CURRENCIES)

    issues: list[Issue] = []
    units = found[0][1] if found else None
    if units is None and (rounded := _xbrl_rounding(doc, ordered)):
        units = "units"
        issues.append(Issue(
            code="units_from_rounding", severity="info",
            message=f"No table states a scale; the report declares its rounding "
                    f"level as {rounded}, and its statements' figures are multiples "
                    f"of it, so figures are printed in full (units)."))
    if others := sorted({f"{s.kind} p.{s.page}: {scale}" for s, scale in found
                         if scale != units}):
        issues.append(Issue(
            code="units_disagree", severity="warning", page=found[0][0].page,
            message=f"The {found[0][0].kind} on page {found[0][0].page} is printed in "
                    f"{units}, which is used; other tables state a different scale "
                    f"({'; '.join(others)}). Figures read from those tables need "
                    f"checking."))

    if currency is None:
        statement_pages = {s.page for s in doc.statements}
        for page in ([p for p in doc.pages if p.page in statement_pages]
                     + [p for p in doc.pages if p.page not in statement_pages]):
            if currency := _sniff(page.text, _CURRENCIES):
                break
    return units, currency, issues


def cache_path(cache_dir: str | Path, sha: str) -> Path:
    return Path(cache_dir) / f"{sha}.json"


def read_pdf(
    path: str | Path,
    *,
    cache_dir: str | Path | None = None,
    use_cache: bool = True,
    ocr_enabled: bool = True,
    reader: str | None = None,
) -> PdfDoc:
    """Read a filing into a PdfDoc, reusing a cached pdf.json when one matches.

    `reader="llm"` (a company's mapping opts in) hands the whole document to the
    vision model instead; see pdf/llm_read.py. Anything else is this reader.
    """
    import pdfplumber

    path = str(path)
    sha = sha256_of(path)
    if reader == "llm":
        from finscan2.pdf.llm_read import read_pdf_llm

        return read_pdf_llm(path, sha, cache_dir=cache_dir if use_cache else None,
                            enabled=ocr_enabled)

    # Vision transcriptions recovered from a cache written by an EARLIER parser.
    # Parsing is deterministic and cheap to redo; a vision transcription is neither,
    # and asking the model again returns different digits (137,011 one time, 137,001
    # the next). So a parser upgrade re-parses, and never re-transcribes: the
    # figures a reviewer already checked must not move because the parser improved.
    transcribed: dict[int, str] = {}

    if use_cache and cache_dir:
        cached = cache_path(cache_dir, sha)
        if cached.exists():
            payload = json.loads(cached.read_text(encoding="utf-8"))
            if payload.get("parser_version") == PARSER_VERSION:
                doc = PdfDoc.from_dict(payload)
                doc.path = path      # the cache is keyed by content, not location
                return doc
            transcribed = {p["page"]: p.get("text", "")
                           for p in payload.get("pages", [])
                           if p.get("source") == "vision" and p.get("text")}


    pages: list[Page] = []
    issues: list[Issue] = []
    ocr_pages: list[int] = []
    statements: list[Statement] = []
    note_pages: list[tuple[int, str]] = []
    last_page = 0           # the last page a primary statement was read from

    with pdfplumber.open(path) as pdf:
        for index, page in enumerate(pdf.pages, start=1):
            text, respaced = extract_page_text(page)
            tables = page.extract_tables() or []
            if tables:
                text = f"{text}\n{render_tables(tables)}".strip()

            source = "text_respaced" if respaced else "text"
            layer = text
            scanned = _is_scanned_table(page, text)
            if not text.strip() or scanned:
                if index in transcribed:
                    # Kept verbatim from the earlier cache; see `transcribed` above.
                    vision = transcribed[index]
                elif ocr_enabled:
                    vision = ocr.transcribe(path, index)
                else:
                    vision = ""
                if vision.strip():
                    text, source = vision, "vision"
                    ocr_pages.append(index)
                elif not text.strip():
                    source = "empty"

            pages.append(Page(page=index, source=source, chars=len(text),
                              text=text, tables=len(tables)))
            # Keep the pdfplumber page for the layout pass; it is only valid
            # inside this `with` block, so statements are built here too. A
            # transcribed page's geometry belongs to the scan's own text layer,
            # not to the transcription, so it is not offered.
            geometry = None if source == "vision" else page
            statement = extract_statement(index, text, page=geometry)
            if statement is not None and scanned and source == "vision":
                # The scan's own text layer is the independent witness.
                checked = verify_rows(statement.rows, text, layer)
                for caption, _, was, now in checked.corrected:
                    issues.append(Issue(
                        code="ocr_corrected", severity="warning", page=index,
                        message=f"'{caption[:50]}': the vision model read {was:,.0f}; the "
                                f"scan's text layer prints {now:,.0f} on the same line and "
                                f"only that makes the table's subtotal add up, so "
                                f"{now:,.0f} is used."))
                if flagged := [r for r in statement.rows if r.unverified]:
                    issues.append(Issue(
                        code="ocr_unverified", severity="warning", page=index,
                        message=f"{sum(len(r.unverified) for r in flagged)} figure(s) on "
                                f"page {index} were read by the vision model but "
                                f"confirmed neither by the scan's text layer nor the "
                                f"table's arithmetic, and will not be written: "
                                + "; ".join(f"'{r.caption[:40]}'" for r in flagged[:8])
                                + ("…" if len(flagged) > 8 else "")))
            if statement is not None:
                statements.append(statement)
                last_page = index
            elif (statements and last_page == index - 1 and statements[-1].note is None
                  and (carried := extract_continuation(text, geometry, statements[-1]))):
                # An untitled page repeating the previous statement's columns.
                statements[-1].rows.extend(carried)
                last_page = index
            else:
                # Not a primary statement. It may still hold note tables whose
                # figures no primary statement prints — Almarai's D&A for the
                # period appears only in the segment note.
                note_pages.append((index, text))
                if (embedded := extract_embedded(index, text, page=geometry)) is not None:
                    statements.append(embedded)

    # Notes come last: a note reports "the period then ended", and only the primary
    # statements say how long that period is.
    spans: dict[str, int] = {}
    for statement in statements:
        for column in statement.columns:
            if column.end and column.months:
                spans[column.end] = max(spans.get(column.end, 0), column.months)
    for page_no, page_text in note_pages:
        found, note_issues = extract_notes(page_no, page_text, spans)
        statements.extend(found)
        issues.extend(note_issues)

    if respaced_pages := [p.page for p in pages if p.source == "text_respaced"]:
        issues.append(Issue(
            code="text_layer_repaired", severity="info",
            message=f"Letter-spaced text layer repaired on page(s) {respaced_pages}."))

    if reused := sorted(set(transcribed) & {p.page for p in pages
                                            if p.source == "vision"}):
        issues.append(Issue(
            code="vision_transcription_reused", severity="info",
            message=f"Page(s) {reused} kept the transcription from the earlier cache "
                    f"rather than being read again, so their figures are unchanged by "
                    f"this parser upgrade."))

    if ocr_pages:
        issues.append(Issue(
            code="vision_transcription", severity="warning",
            message=f"Page(s) {ocr_pages} had no text layer and were transcribed by "
                    f"the vision model. That transcription is not reproducible — "
                    f"figures from these pages are cached with this document and "
                    f"should be spot-checked against the filing."))

    if unread := [p.page for p in pages if p.source == "empty"]:
        issues.append(Issue(
            code="pages_not_read", severity="error",
            message=f"Page(s) {unread} carry no text layer and OCR recovered nothing. "
                    f"If the financial statements are on those pages, nothing "
                    f"downstream can read them. Check that the model provider is "
                    f"configured, or supply a PDF with a text layer."))

    doc = PdfDoc(path=path, sha256=sha, pages=pages, statements=statements,
                 ocr_pages=ocr_pages, ocr_engine=ocr.ENGINE if ocr_pages else None,
                 issues=issues)
    doc.units, doc.currency, unit_issues = _document_units(doc)
    doc.issues.extend(unit_issues)

    if not statements:
        doc.issues.append(Issue(
            code="no_statements", severity="error",
            message="No financial statement was identified in this document. Stage 3 "
                    "has nothing to read."))
    else:
        for statement in statements:
            if not statement.columns:
                doc.issues.append(Issue(
                    code="columns_unreadable", severity="error", page=statement.page,
                    message=f"The {statement.kind} on page {statement.page} has no "
                            f"readable column headers, so the period basis of its "
                            f"figures cannot be established."))
                continue

            # A header parsed into the wrong number of columns is worse than one
            # not parsed at all: it looks usable and silently mislabels which
            # period each figure belongs to. Column-major headers — where a
            # filing prints "1 January -", the end dates, and the years on three
            # separate lines — read as one column instead of six, and only the
            # body disagrees. So the body is the check.
            align = column_alignment(statement)
            if align["rows"] and align["aligned"] < align["rows"] / 2:
                doc.issues.append(Issue(
                    code="columns_misaligned", severity="error", page=statement.page,
                    message=f"The {statement.kind} on page {statement.page} parsed "
                            f"{align['columns']} column(s), but only "
                            f"{align['aligned']} of {align['rows']} rows carry that "
                            f"many values. The header layout was not understood; "
                            f"figures from this statement cannot be assigned to a "
                            f"period."))

    if doc.units is None:
        doc.issues.append(Issue(
            code="units_unknown", severity="error",
            message="No statement's table section (its title, the lines above its "
                    "first figure row, or its column headers) states a scale "
                    "(thousands, millions, ...). Scale is not taken from narrative "
                    "text, so every figure's magnitude is unknown."))

    if cache_dir:
        target = cache_path(cache_dir, sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(doc.to_dict(), indent=2, ensure_ascii=False),
                          encoding="utf-8")
    return doc
