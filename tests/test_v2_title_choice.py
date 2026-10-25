"""Choosing between statements that share a title."""
from __future__ import annotations

from types import SimpleNamespace

from finscan.match.resolve import _statement_for
from finscan.schema import Column, PdfDoc, Statement, StatementRow


def _table(page, title, captions, note=None):
    return Statement(
        page, "other", title, "",
        columns=[Column(0, "06/30/2026", kind="point_in_time", end="2026-06-30")],
        rows=[StatementRow(caption, [1.0], "") for caption in captions], note=note)


def _spec(resolve, statement="title:Consolidated"):
    return SimpleNamespace(statement=statement, resolve=resolve,
                           key=SimpleNamespace(section="balance_sheet"))


_DOC = PdfDoc(path="x.pdf", sha256="x", units="thousands", statements=[
    _table(54, "Consolidated", ["Current assets", "Non-current assets"]),
    _table(73, "Consolidated", ["FINAME / FINEP", "Debentures", "CRA"]),
])


def test_a_shared_title_resolves_to_the_table_holding_the_wanted_line():
    statement, _ = _statement_for(_spec("pdf:FINAME / FINEP"), _DOC)
    assert statement.page == 73


def test_every_term_of_a_sum_must_be_in_the_chosen_table():
    statement, _ = _statement_for(_spec("sum:pdf:CRA#1|pdf:Debentures"), _DOC)
    assert statement.page == 73


def test_the_first_table_is_kept_when_it_holds_the_line():
    statement, _ = _statement_for(_spec("pdf:Current assets"), _DOC)
    assert statement.page == 54


def test_no_table_holding_the_line_leaves_the_first_for_the_caption_error():
    statement, _ = _statement_for(_spec("pdf:Something else"), _DOC)
    assert statement.page == 54


def test_alternative_titles_are_searched_in_order_for_the_wanted_line():
    doc = PdfDoc(path="x.pdf", sha256="x", units="thousands", statements=[
        _table(72, "21. LOANS", ["Parent only"]),
        _table(73, "Consolidated", ["FINAME / FINEP"]),
    ])
    statement, _ = _statement_for(
        _spec("pdf:FINAME / FINEP", "title:21. LOANS || Consolidated"), doc)
    assert statement.page == 73
    statement, _ = _statement_for(
        _spec("pdf:Parent only", "title:21. LOANS || Consolidated"), doc)
    assert statement.page == 72


def test_an_occurrence_needs_that_many_exact_lines_in_the_chosen_table():
    doc = PdfDoc(path="x.pdf", sha256="x", units="thousands", statements=[
        _table(54, "Consolidated", ["CRAs issued"]),
        _table(73, "Consolidated", ["CRA", "Debentures", "CRA"]),
    ])
    statement, _ = _statement_for(_spec("pdf:CRA#2"), doc)
    assert statement.page == 73


def test_optional_and_other_statement_terms_do_not_pick_the_table():
    statement, _ = _statement_for(
        _spec("sum:pdf:Current assets|pdf:Debentures?|pdf:Other @ title:Elsewhere"), _DOC)
    assert statement.page == 54
