"""What actually reaches the model, and what happens when it comes back empty.

A quarterly release is mostly prose. Handing the model 9 pages in document order
buries a 1,400-character income statement behind 11,000 characters of product
commentary, and the extraction comes back empty — which is how this was found.
"""
from __future__ import annotations

from finscan.extract.extractor import extract
from finscan.extract.pdf_reader import PageText, PdfDoc, statement_score
from finscan.schemas import Extraction, Field_, LineItem, PeriodMeta

NARRATIVE = (
    "For Immediate Release. The Company announced significant progress in its AI "
    "capabilities and productivity agents, continuing to grow existing core "
    "businesses through deployment across the platform and its ecosystem. " * 12
)
STATEMENT = """CONDENSED CONSOLIDATED INCOME STATEMENT
RMB in millions, unless specified
1Q2026 1Q2025 4Q2025
Revenues 196,458 180,022 194,371
Cost of revenues (85,193) (79,529) (86,082)
Gross profit 111,265 100,493 108,289
Operating profit 67,375 57,566 60,338
Profit before income tax 73,969 63,442 71,684
Income tax expense (14,577) (13,717) (12,595)
Profit for the period 59,392 49,725 59,089
"""


def _doc() -> PdfDoc:
    return PdfDoc(path="x.pdf", pages=[
        PageText(page=1, text=NARRATIVE),
        PageText(page=2, text=NARRATIVE),
        PageText(page=3, text=NARRATIVE),
        PageText(page=4, text=STATEMENT),
        PageText(page=5, text=NARRATIVE),
    ])


def test_prose_scores_far_below_a_statement():
    doc = _doc()
    prose = statement_score(doc.pages[0])
    table = statement_score(doc.pages[3])
    assert table > prose * 5, f"prose {prose:.2f} vs statement {table:.2f}"


def test_the_statement_page_is_identified():
    assert _doc().statement_pages() == [4]


def test_statements_lead_the_prompt():
    text = _doc().as_prompt_text()
    assert text.index("<financial_statements>") < text.index("<supporting_narrative>")
    assert text.index("Revenues 196,458") < text.index("<supporting_narrative>")


def test_narrative_is_marked_as_context_only():
    assert "do not read figures from here" in _doc().as_prompt_text()


def test_statements_only_drops_the_prose_entirely():
    text = _doc().as_prompt_text(statements_only=True)
    assert "Revenues 196,458" in text
    assert "AI capabilities" not in text


def test_char_budget_is_respected():
    assert len(_doc().as_prompt_text(max_chars=900)) <= 900


# --------------------------------------------------------------------------- #
def _extraction(items: bool) -> Extraction:
    return Extraction(
        meta=PeriodMeta(period_label="1Q2026", units="millions"),
        line_items=(
            [LineItem(field=Field_.revenue_from_operations, label_in_pdf="Revenues",
                      value=196458, confidence=0.95)]
            if items else []
        ),
    )


def test_an_empty_first_pass_is_retried_against_the_statements(monkeypatch):
    seen: list[str] = []

    class _Stub:
        def __init__(self, schema):
            self.schema = schema

        def invoke(self, messages):
            seen.append(messages[-1]["content"])
            return _extraction(items=len(seen) > 1)

    monkeypatch.setattr("finscan.extract.extractor.structured", lambda s: _Stub(s))

    result, notes = extract("full document with prose", statements_text="STATEMENT ONLY")

    assert len(seen) == 2, "an empty result must be retried, not reported as failure"
    assert "STATEMENT ONLY" in seen[1]
    assert "no line items" in seen[1]
    assert result.line_items
    assert notes and "retried" in notes[0]


def test_a_successful_first_pass_is_not_retried(monkeypatch):
    calls = {"n": 0}

    class _Stub:
        def __init__(self, schema):
            self.schema = schema

        def invoke(self, _m):
            calls["n"] += 1
            return _extraction(items=True)

    monkeypatch.setattr("finscan.extract.extractor.structured", lambda s: _Stub(s))
    result, notes = extract("doc")
    assert calls["n"] == 1 and not notes and result.line_items


def test_two_empty_passes_report_an_actionable_message(monkeypatch):
    class _Stub:
        def __init__(self, schema):
            self.schema = schema

        def invoke(self, _m):
            return _extraction(items=False)

    monkeypatch.setattr("finscan.extract.extractor.structured", lambda s: _Stub(s))
    result, notes = extract("doc")
    assert not result.line_items
    assert any("text layer" in n for n in notes)
