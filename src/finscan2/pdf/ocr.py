"""OCR for pages with no text layer.

Vision-model transcription only, for now: this deployment has no Azure AI Document
Intelligence. The engine is chosen behind `transcribe()` so adding Document
Intelligence later is a new branch here and no change anywhere else.

The caller is expected to cache the result — a vision transcription is not
reproducible, and re-running it per debugging pass both costs money and quietly
changes the figures. In testing the same expense line came back as 194,879 on one
run and 140,879 (the printed value) on the next.
"""
from __future__ import annotations

import base64
import io
import re

ENGINE = "vision"

SYSTEM = """You transcribe a scanned/flattened financial statement page into plain
text for an automated extraction pipeline.

Rules:
- Reproduce every heading, label and number exactly as printed. Do not translate,
  summarise, round, or omit anything, including signature blocks and footnotes.
- Render each table as one row per line, columns separated by " | ", using the
  printed column headers (e.g. period dates) verbatim as the first row(s) of that
  table. Keep columns in the exact left-to-right order they are printed in.
- Keep every column header on its own cell even when it is printed stacked over
  several lines: join those lines into one cell ("April - June 2026").
- Keep negative numbers exactly as printed (parentheses or a minus sign).
- Reproduce the units line exactly ("SAR '000", "in thousands", "RMB million").
- Output plain text only: no markdown, no commentary, no code fences.
"""

_FENCE = re.compile(r"^```[a-zA-Z]*\n?|```$")


def transcribe(pdf_path: str, page_no: int, dpi: int = 300) -> str:
    """Transcribe one image-only page. Returns "" when unavailable or on failure.

    Renders with pdfplumber's own renderer (pypdfium2, a pure-Python wheel), so no
    poppler binary is required.
    """
    try:
        import pdfplumber

        from finscan2.llm import chat_model
    except ImportError:
        return ""

    try:
        with pdfplumber.open(pdf_path) as pdf:
            image = pdf.pages[page_no - 1].to_image(resolution=dpi).original
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")

        response = chat_model().invoke([
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": f"Transcribe page {page_no} of this filing."},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{encoded}"}},
            ]},
        ])
        content = (getattr(response, "content", "") or "").strip()
        return _FENCE.sub("", content).strip()
    except Exception:
        # Best-effort by design: an unreadable page becomes a named issue in
        # read.py rather than an exception that loses the rest of the document.
        return ""
