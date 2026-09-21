"""Learn-time binding of a workbook caption to the caption the filing printed.

The workbook owns the label; the filing owns the value; the map is the recorded
correspondence. These tests cover what that correspondence is allowed to be made
of — and, more importantly, what it refuses to be made of.
"""
from __future__ import annotations

from finscan2.model import propose_captions as pc
from finscan2.model.schema import ModelMap, RowKey, RowSpec
from finscan2.schema import Column, PdfDoc, Statement, StatementRow


def _doc(rows, kind="income_statement"):
    return PdfDoc(path="f.pdf", sha256="x", statements=[Statement(
        page=6, kind=kind, title="t", heading="h",
        columns=[Column(index=0, header="April - June 2026", months=3,
                        end="2026-06-30", kind="period")],
        rows=[StatementRow(caption=c, values=[1.0], raw="") for c in rows])])


def _map(labels):
    return ModelMap(company="x", sheet="Model", rows=[
        RowSpec(key=RowKey(label=label, section="income_statement"), row_hint=10 + i,
                kind="input", statement="income_statement", resolve=f"pdf:{label}")
        for i, label in enumerate(labels)])


def test_an_exact_caption_needs_no_model_and_no_review():
    model = _map(["Revenue"])
    proposals = pc.deterministic(model, _doc(["Revenue", "Cost of Sales"]))
    pc.apply(model, proposals)
    assert model.rows[0].pdf_caption == "Revenue"
    assert proposals[0].method == "exact"
    assert model.rows[0].review is None


def test_a_derived_match_is_recorded_but_flagged_for_confirmation():
    """The deterministic tiers found it; a person still confirms it is the same
    line, because 'nearly the same words' is not 'the same line'."""
    model = _map(["Impairment (Loss) / Reversal on Financial Assets"])
    proposals = pc.deterministic(model, _doc(["Impairment Loss on Financial Assets"]))
    pc.apply(model, proposals)
    assert model.rows[0].pdf_caption == "Impairment Loss on Financial Assets"
    assert proposals[0].method == "derived"
    assert "confirm" in model.rows[0].review


def test_an_unmatched_row_carries_the_candidates_for_the_model_to_choose_from():
    model = _map(["Zakat"])
    proposals = pc.deterministic(model, _doc(["Revenue", "Income tax"]))
    assert proposals[0].caption is None
    assert proposals[0].candidates == ["Revenue", "Income tax"]


def test_an_unmatched_row_is_flagged_never_guessed():
    model = _map(["Zakat"])
    pc.apply(model, pc.deterministic(model, _doc(["Revenue"])))
    assert model.rows[0].pdf_caption is None
    assert "no line in the filing" in model.rows[0].review


def test_a_model_answer_outside_the_candidate_list_is_discarded():
    """The model picks from a list so it cannot invent a line. An answer that is
    not in the list is a paraphrase or a hallucination, and changes nothing."""
    proposal = pc.Proposal(row=10, label="Zakat", statement="income_statement",
                           candidates=["Revenue", "Income tax"])
    _run_with_answers([("Zakat", "Zakat and income tax")], [proposal])
    assert proposal.caption is None
    assert "not a caption in this statement" in proposal.why


def test_a_model_answer_inside_the_list_is_accepted_and_marked_for_confirmation():
    proposal = pc.Proposal(row=10, label="Zakat", statement="income_statement",
                           candidates=["Revenue", "Income tax"])
    _run_with_answers([("Zakat", "Income tax")], [proposal])
    assert proposal.caption == "Income tax" and proposal.method == "model"

    model = _map(["Zakat"])
    pc.apply(model, [proposal])
    assert model.rows[0].pdf_caption == "Income tax"
    assert "CONFIRM" in model.rows[0].review


def test_an_empty_model_answer_leaves_the_row_unmatched():
    """"" is the instructed answer when unsure, and must stay unmatched rather
    than falling back to a near-miss."""
    proposal = pc.Proposal(row=10, label="Zakat", statement="income_statement",
                           candidates=["Revenue"])
    _run_with_answers([("Zakat", "")], [proposal])
    assert proposal.caption is None


def _run_with_answers(pairs, proposals):
    """Drive `with_model` against a stub client, so no network is touched."""
    import types

    class _Pick:
        def __init__(self, label, caption):
            self.excel_label, self.pdf_caption, self.reason = label, caption, "because"

    class _Answer:
        picks = None

    answer = _Answer()
    answer.picks = [_Pick(label, caption) for label, caption in pairs]

    class _Client:
        def invoke(self, prompt): return answer

    import finscan2.llm as llm
    original = llm.structured
    llm.structured = lambda cls: _Client()
    try:
        pc.with_model(proposals, _doc(["Revenue"]))
    finally:
        llm.structured = original
    return proposals


def test_a_canonical_field_row_is_searched_with_its_aliases_not_its_id():
    """Searching for the bare identifier read 'cost_of_revenue' as the caption
    'cost of revenue', whose word 'revenue' matched the filing's `Revenue` line by
    containment — so the map recorded a cost row pointing at revenue."""
    model = ModelMap(company="x", sheet="Model", rows=[
        RowSpec(key=RowKey(label="Cost of Sales", section="income_statement"),
                row_hint=9, kind="input", statement="income_statement",
                resolve="field:cost_of_revenue")])
    proposals = pc.deterministic(model, _doc(["Revenue", "Cost of Sales", "Gross Profit"]))
    pc.apply(model, proposals)
    assert model.rows[0].pdf_caption == "Cost of Sales"
    assert proposals[0].method == "exact"


def test_a_model_pick_already_claimed_by_another_row_is_refused():
    """Asked to place "Exchange Gain, net", a model chose "Other (Expenses) /
    Income, net" because exchange gains are "typically included in" it. True, and
    not the question: another row already reads that line, and honouring the pick
    would write one filing figure into two model rows."""
    combined = pc.Proposal(row=14, label="Other Expenses, net",
                           statement="income_statement",
                           caption="Other (Expenses) / Income, net", method="derived")
    component = pc.Proposal(row=17, label="Exchange Gain, net",
                            statement="income_statement",
                            candidates=["Revenue", "Other (Expenses) / Income, net"])
    _run_with_answers([("Exchange Gain, net", "Other (Expenses) / Income, net")],
                      [combined, component])
    assert component.caption is None
    assert "already reads that line" in component.why
    assert combined.caption == "Other (Expenses) / Income, net"   # untouched


def test_two_model_picks_cannot_both_claim_one_filing_line():
    first = pc.Proposal(row=20, label="Staff costs", statement="income_statement",
                        candidates=["Operating expenses"])
    second = pc.Proposal(row=21, label="Other admin costs",
                         statement="income_statement",
                         candidates=["Operating expenses"])
    _run_with_answers([("Staff costs", "Operating expenses"),
                       ("Other admin costs", "Operating expenses")], [first, second])
    assert [bool(first.caption), bool(second.caption)] == [True, False]
    assert "already reads that line" in second.why


def test_two_deterministic_matches_on_one_filing_line_are_flagged():
    """Writing one filing figure into two rows double-counts it in every subtotal
    above. The model pass refuses its own duplicates; this catches the rest."""
    model = ModelMap(company="x", sheet="Model", rows=[
        RowSpec(key=RowKey(label="Revenue", section="income_statement"), row_hint=7,
                kind="input", statement="income_statement", resolve="pdf:Revenue"),
        RowSpec(key=RowKey(label="Revenue from operations", section="income_statement"),
                row_hint=8, kind="input", statement="income_statement",
                resolve="pdf:Revenue from operations")])
    proposals = pc.deterministic(model, _doc(["Revenue"]))
    pc.apply(model, proposals)
    flagged = [r for r in model.rows if r.review and "cannot fill two rows" in r.review]
    assert len(flagged) == 2
    assert "'Revenue'" in flagged[0].review or "'Revenue'" in flagged[1].review


def test_a_note_about_the_workbook_cell_survives_the_caption_pass():
    """"=-26.573-10.578" names the components a `sum:` should read — the most
    useful thing on that row. The caption pass must add to it, not replace it."""
    model = _map(["Zakat and Income Tax"])
    model.rows[0].review = ("the reference cell is a hand-typed expression "
                            "'=-26.573-10.578', not a calculation")
    pc.apply(model, pc.deterministic(model, _doc(["Revenue"])))
    assert "hand-typed expression" in model.rows[0].review
    assert "no line in the filing" in model.rows[0].review
