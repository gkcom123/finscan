"""Embotelladora Andina's 6-K header and layout quirks — offline tests."""
from __future__ import annotations

import pytest

from finscan2.pdf.columns import parse_single_column
from finscan2.pdf.read import _CURRENCIES, _UNIT_PATTERNS, _sniff
from finscan2.schema import Column, PdfDoc, Statement, StatementRow


@pytest.mark.parametrize("header, months, end", [
    ("01.01.2026 06.30.2026 (non-audited) ThCh$", 6, "2026-06-30"),
    ("01.04.2026 06.30.2026 (non-audited) ThCh$", 3, "2026-06-30"),
    ("04.01.2026 06.30.2026 ThCh$", 3, "2026-06-30"),
    ("01.01.2025 06.30.2025 (non-audited)", 6, "2025-06-30"),
])
def test_a_two_date_header_with_no_dash_is_a_span(header, months, end):
    column = parse_single_column(header)
    assert (column.months, column.end) == (months, end)


def test_two_unrelated_dates_are_not_a_span():
    column = parse_single_column("30.06.2026 31.12.2025")
    assert column.months is None


def test_thousands_of_chilean_pesos_is_a_thousands_scale_in_clp():
    assert _sniff("Consolidated Statements\nThCh$ ThCh$", _UNIT_PATTERNS) == "thousands"
    assert _sniff("ASSETS NOTE 06.30.2026 ThCl$", _UNIT_PATTERNS) == "thousands"
    assert _sniff("ThCh$ and a USD exchange rate", _CURRENCIES) == "CLP"


def test_a_statement_split_across_pages_merges_when_one_title_drops_the_company_name():
    def part(page: int, title: str, caption: str) -> Statement:
        return Statement(
            page=page, kind="balance_sheet", title=title, heading="",
            columns=[Column(index=0, header="06.30.2026", months=None,
                            end="2026-06-30", kind="point_in_time")],
            rows=[StatementRow(caption=caption, values=[1.0], raw="")])

    doc = PdfDoc(path="f.pdf", sha256="x", statements=[
        part(7, "EMBOTELLADORA ANDINA S.A. Consolidated Statements of Financial Position",
             "Cash"),
        part(8, "Consolidated Statements of Financial Position", "Total Equity")])

    captions = [row.caption for row in doc.statement("balance_sheet").rows]
    assert captions == ["Cash", "Total Equity"]
