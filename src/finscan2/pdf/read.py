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
from finscan2.pdf.statements import column_alignment, extract_statement
from finscan2.pdf.text import extract_page_text, render_tables
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


def _document_units(doc: PdfDoc) -> tuple[str | None, str | None]:
    """Scale and currency, preferred from statement pages over narrative pages."""
    statement_pages = {s.page for s in doc.statements}
    ordered = ([p for p in doc.pages if p.page in statement_pages]
               + [p for p in doc.pages if p.page not in statement_pages])
    units = currency = None
    for page in ordered:
        units = units or _sniff(page.text, _UNIT_PATTERNS)
        currency = currency or _sniff(page.text, _CURRENCIES)
        if units and currency:
            break
    return units, currency


def cache_path(cache_dir: str | Path, sha: str) -> Path:
    return Path(cache_dir) / f"{sha}.json"


def read_pdf(
    path: str | Path,
    *,
    cache_dir: str | Path | None = None,
    use_cache: bool = True,
    ocr_enabled: bool = True,
) -> PdfDoc:
    """Read a filing into a PdfDoc, reusing a cached pdf.json when one matches."""
    import pdfplumber

    path = str(path)
    sha = sha256_of(path)

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

    with pdfplumber.open(path) as pdf:
        for index, page in enumerate(pdf.pages, start=1):
            text, respaced = extract_page_text(page)
            tables = page.extract_tables() or []
            if tables:
                text = f"{text}\n{render_tables(tables)}".strip()

            source = "text_respaced" if respaced else "text"
            if not text.strip():
                if index in transcribed:
                    # Kept verbatim from the earlier cache; see `transcribed` above.
                    text = transcribed[index]
                elif ocr_enabled:
                    text = ocr.transcribe(path, index)
                if text.strip():
                    source, _ = "vision", ocr_pages.append(index)
                else:
                    source = "empty"

            pages.append(Page(page=index, source=source, chars=len(text),
                              text=text, tables=len(tables)))
            # Keep the pdfplumber page for the layout pass; it is only valid
            # inside this `with` block, so statements are built here too.
            statement = extract_statement(index, text, page=page)
            if statement is not None:
                statements.append(statement)
            else:
                # Not a primary statement. It may still hold note tables whose
                # figures no primary statement prints — Almarai's D&A for the
                # period appears only in the segment note.
                note_pages.append((index, text))

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
    doc.units, doc.currency = _document_units(doc)

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
            message="No printed scale marker (thousands, millions, ...) was found. "
                    "Every figure would be written at the wrong magnitude."))

    if cache_dir:
        target = cache_path(cache_dir, sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(doc.to_dict(), indent=2, ensure_ascii=False),
                          encoding="utf-8")
    return doc
