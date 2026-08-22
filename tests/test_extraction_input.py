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


class _EmptyCashFlowSubtotals:
    """Stand-in for extractor._CashFlowSubtotals: no cash-flow content in these
    fixtures, so the working-capital rescue call should find nothing and add no
    notes — it must not interfere with the retry-counting these tests check."""
    total_before_working_capital_changes = None
    net_cash_from_operating_activities = None
    column_used = ""


def test_an_empty_first_pass_is_retried_against_the_statements(monkeypatch):
    seen: list[str] = []

    class _Stub:
        def __init__(self, schema):
            self.schema = schema

        def invoke(self, messages):
            if self.schema is not Extraction:
                return _EmptyCashFlowSubtotals()
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
            if self.schema is not Extraction:
                return _EmptyCashFlowSubtotals()
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


def test_rescue_recovers_bare_total_row_before_working_capital_heading():
    """total_before_working_capital_changes is recovered positionally using
    net_cash_from_operating_activities' own column slot, when the main pass
    extracted Y but not X (X's caption is a bare, ambiguous 'Total')."""
    from finscan.extract.extractor import _rescue_working_capital_subtotal
    from finscan.schemas import Field_, LineItem

    result = Extraction(
        meta=PeriodMeta(period_label="Q2 2026", units="thousands"),
        line_items=[
            LineItem(
                field=Field_.net_cash_from_operating_activities,
                label_in_pdf="Net cash flow provided by operating activities",
                value=5897437.0, confidence=0.95,
                source_row_text=("Net cash flow provided by operating activities "
                                 "10,649,022 5,897,437 4,751,585 9,830,514 4,920,661 4,909,853"),
            ),
        ],
    )
    statements_text = (
        "Total 12,794,972 6,016,138 6,778,834 11,298,353 5,459,194 5,839,159\n"
        "Changes in working capital:\n(Increase) decrease in:\nLease receivables (747,113)\n"
    )
    note = _rescue_working_capital_subtotal(result, statements_text)
    assert note is not None
    x_item = next(li for li in result.line_items
                 if li.field.value == "total_before_working_capital_changes")
    assert x_item.value == 6016138.0


def test_rescue_does_nothing_without_a_y_anchor():
    from finscan.extract.extractor import _rescue_working_capital_subtotal

    result = Extraction(meta=PeriodMeta(period_label="Q2 2026", units="thousands"), line_items=[])
    note = _rescue_working_capital_subtotal(result, "Total 1 2 3\nworking capital\n")
    assert note is None
    assert not result.line_items


def test_reconcile_rescued_scale_fixes_a_1000x_mismatch():
    """Regression test: the focused rescue LLM call has occasionally returned
    net_cash_from_operating_activities already divided by 1000 (e.g. 4751.585
    instead of 4,751,585), while total_before_working_capital_changes came back
    at the correct scale in the same call. Left uncorrected this produces a
    wildly wrong change_in_working_capital. The reconcile step should catch and
    fix the mis-scaled figure by comparing it against the rest of the extraction."""
    from finscan.extract.extractor import _reconcile_rescued_scale

    result = Extraction(
        meta=PeriodMeta(period_label="Q2 2026", units="thousands"),
        line_items=[
            LineItem(field=Field_.revenue_from_operations, label_in_pdf="Revenues",
                      value=6976200.0, confidence=0.95),
            LineItem(field=Field_.profit_after_tax, label_in_pdf="Profit for the period",
                      value=5460180.0, confidence=0.95),
            LineItem(field=Field_.ebitda, label_in_pdf="EBITDA",
                      value=7200000.0, confidence=0.95),
            LineItem(field=Field_.total_before_working_capital_changes, label_in_pdf="Total",
                     value=6778834.0, confidence=0.7,
                     source_row_text="(recovered via focused follow-up extraction)"),
            LineItem(field=Field_.net_cash_from_operating_activities,
                     label_in_pdf="Net cash from operating activities",
                     value=4751.585, confidence=0.7,
                     source_row_text="(recovered via focused follow-up extraction)"),
        ],
    )
    fids = {"total_before_working_capital_changes", "net_cash_from_operating_activities"}
    note = _reconcile_rescued_scale(result, fids)

    assert note is not None and "net_cash_from_operating_activities" in note
    y_item = next(li for li in result.line_items
                 if li.field.value == "net_cash_from_operating_activities")
    assert y_item.value == 4751585.0
    x_item = next(li for li in result.line_items
                 if li.field.value == "total_before_working_capital_changes")
    assert x_item.value == 6778834.0, "an already-correctly-scaled figure must not be touched"


def test_reconcile_rescued_scale_leaves_consistent_values_alone():
    from finscan.extract.extractor import _reconcile_rescued_scale

    result = Extraction(
        meta=PeriodMeta(period_label="Q2 2026", units="thousands"),
        line_items=[
            LineItem(field=Field_.revenue_from_operations, label_in_pdf="Revenues",
                      value=6976200.0, confidence=0.95),
            LineItem(field=Field_.profit_after_tax, label_in_pdf="Profit for the period",
                      value=5460180.0, confidence=0.95),
            LineItem(field=Field_.ebitda, label_in_pdf="EBITDA",
                      value=7200000.0, confidence=0.95),
            LineItem(field=Field_.total_before_working_capital_changes, label_in_pdf="Total",
                     value=6778834.0, confidence=0.7),
            LineItem(field=Field_.net_cash_from_operating_activities,
                     label_in_pdf="Net cash from operating activities",
                     value=4751585.0, confidence=0.7),
        ],
    )
    fids = {"total_before_working_capital_changes", "net_cash_from_operating_activities"}
    note = _reconcile_rescued_scale(result, fids)
    assert note is None


def test_ensure_working_capital_components_corrects_mis_scaled_focused_rescue(monkeypatch):
    """End-to-end: the focused LLM rescue returns Y at the wrong scale; the
    overall helper must still leave both figures internally consistent so
    normalize.derive_missing computes a sane change_in_working_capital."""
    from finscan.extract.extractor import _ensure_working_capital_components

    def fake_rescue(statements_text, period_hint, period_end_date=None):
        return (
            {"total_before_working_capital_changes": 6778834.0,
             "net_cash_from_operating_activities": 4751.585},
            "Recovered total_before_working_capital_changes, net_cash_from_operating_activities "
            "via a focused follow-up extraction.",
        )

    monkeypatch.setattr("finscan.extract.extractor._rescue_via_focused_llm_call", fake_rescue)

    result = Extraction(
        meta=PeriodMeta(period_label="Q2 2026", units="thousands"),
        line_items=[
            LineItem(field=Field_.revenue_from_operations, label_in_pdf="Revenues",
                      value=6976200.0, confidence=0.95),
            LineItem(field=Field_.profit_after_tax, label_in_pdf="Profit for the period",
                      value=5460180.0, confidence=0.95),
            LineItem(field=Field_.ebitda, label_in_pdf="EBITDA",
                      value=7200000.0, confidence=0.95),
        ],
    )
    note = _ensure_working_capital_components(result, "some statements text")
    assert note and "Rescaled" in note

    values = {li.field.value: li.value for li in result.line_items}
    assert values["net_cash_from_operating_activities"] == 4751585.0
    assert values["total_before_working_capital_changes"] == 6778834.0
