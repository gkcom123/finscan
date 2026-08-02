"""PDF -> text. Tables are rendered as pipe-delimited rows so column alignment
survives into the prompt, which is what stops the model from grabbing the
prior-year figure out of a four-column results table.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from finscan.config import settings


@dataclass
class PageText:
    page: int
    text: str
    tables: list[list[list[str]]] = field(default_factory=list)
    ocr_used: bool = False


@dataclass
class PdfDoc:
    path: str
    pages: list[PageText]

    @property
    def ocr_pages(self) -> list[int]:
        return [p.page for p in self.pages if p.ocr_used]

    def statement_pages(self, threshold: float = 1.5) -> list[int]:
        """Pages that look like actual financial statements, best first."""
        scored = sorted(
            ((statement_score(p), p.page) for p in self.pages), reverse=True
        )
        hits = [pg for score, pg in scored if score >= threshold]
        return sorted(hits) or [pg for _, pg in scored[:2]]

    def _page_block(self, p: PageText) -> str:
        chunks = [f"\n===== PAGE {p.page} =====\n{p.text.strip()}"]
        for ti, tbl in enumerate(p.tables, start=1):
            rendered = _render_table(tbl)
            if rendered:
                chunks.append(f"\n--- PAGE {p.page} TABLE {ti} ---\n{rendered}")
        return "\n".join(chunks)

    def as_prompt_text(self, max_chars: int | None = None,
                       statements_only: bool = False) -> str:
        """Statements first, narrative second.

        A quarterly release is mostly prose: in a typical 9-page announcement the
        income statement is one page of about 1,400 characters sitting behind
        11,000 characters of commentary about product launches. Handed the pages
        in document order, a model will often return an empty extraction — it
        never reliably finds the table. Leading with the statement pages, clearly
        labelled, fixes that; the narrative is kept afterwards only for context
        such as the period and the reporting basis.
        """
        max_chars = max_chars or settings.finscan_max_pdf_chars
        wanted = set(self.statement_pages())

        statements = [self._page_block(p) for p in self.pages if p.page in wanted]
        narrative = [self._page_block(p) for p in self.pages if p.page not in wanted]

        out = ("<financial_statements>\n" + "\n".join(statements) + "\n</financial_statements>")
        if not statements_only and narrative:
            budget = max(0, max_chars - len(out) - 200)
            body = "\n".join(narrative)[:budget]
            out += ("\n\n<supporting_narrative>\n"
                    "Context only — do not read figures from here.\n"
                    + body + "\n</supporting_narrative>")
        return out[:max_chars]


_STATEMENT_HEADINGS = re.compile(
    r"income statement|statement of profit|statement of operations"
    r"|comprehensive income|financial position|balance sheet|cash flows?"
    r"|financial results|reconciliation",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"\(?\d[\d,]{2,}\)?")


def statement_score(page: PageText) -> float:
    """Numeric density plus a statement heading. Prose scores near zero; a
    results table scores several times higher."""
    text = page.text or ""
    if not text.strip():
        return 0.0
    density = len(_NUMBER.findall(text)) / max(len(text) / 100.0, 1.0)
    heading = 2.0 if _STATEMENT_HEADINGS.search(text[:400]) else 0.0
    table_bonus = 0.5 if page.tables else 0.0
    return density + heading + table_bonus


def _render_table(tbl: list[list[str]]) -> str:
    rows = []
    for row in tbl or []:
        cells = [(c or "").replace("\n", " ").strip() for c in row]
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _ocr_page(pdf_path: str, page_no: int) -> str:
    """OCR a single page. Requires poppler + tesseract on the host."""
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError:  # pragma: no cover - optional dependency
        return ""
    try:
        images = convert_from_path(pdf_path, dpi=300, first_page=page_no, last_page=page_no)
        return "\n".join(pytesseract.image_to_string(im) for im in images)
    except Exception:  # pragma: no cover - host tooling missing
        return ""


def read_pdf(path: str | Path) -> PdfDoc:
    """Extract text + tables from every page, OCR'ing pages with no text layer."""
    import pdfplumber

    path = str(path)
    pages: list[PageText] = []
    with pdfplumber.open(path) as pdf:
        for idx, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            tables = page.extract_tables() or []
            ocr_used = False
            if not text.strip() and not tables and settings.finscan_ocr_fallback:
                text = _ocr_page(path, idx)
                ocr_used = bool(text.strip())
            pages.append(PageText(page=idx, text=text, tables=tables, ocr_used=ocr_used))
    return PdfDoc(path=path, pages=pages)
