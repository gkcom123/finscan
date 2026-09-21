"""Page text extraction, with the letter-spacing repair carried over from v1.

Some filings position every glyph individually, so pdfplumber's default tolerance
splits words into single letters and detaches numbers from their captions. Retrying
with a wider x_tolerance and keeping the least-shattered result recovers those pages.
"""
from __future__ import annotations

#: Above this fraction of single-character tokens, a page's text layer is treated
#: as shattered ("w o r k i n g  c a p i t a l") and re-extracted.
SHATTER_THRESHOLD = 0.35


def single_char_ratio(text: str) -> float:
    tokens = (text or "").split()
    if not tokens:
        return 0.0
    return sum(1 for t in tokens if len(t) == 1) / len(tokens)


def extract_page_text(page) -> tuple[str, bool]:
    """Return (text, was_respaced) for one pdfplumber page."""
    text = page.extract_text() or ""
    ratio = single_char_ratio(text)
    if ratio <= SHATTER_THRESHOLD:
        return text, False

    best_text, best_ratio = text, ratio
    for tolerance in (6, 10):
        candidate = page.extract_text(x_tolerance=tolerance) or ""
        r = single_char_ratio(candidate)
        if r < best_ratio:
            best_text, best_ratio = candidate, r
    return best_text, best_text != text


def render_tables(tables) -> str:
    """pdfplumber tables as pipe-delimited rows, so column alignment survives."""
    out: list[str] = []
    for table in tables or []:
        for row in table or []:
            cells = [(c or "").replace("\n", " ").strip() for c in row]
            if any(cells):
                out.append(" | ".join(cells))
    return "\n".join(out)
