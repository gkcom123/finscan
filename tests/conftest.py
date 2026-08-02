"""Test fixtures. The LLM is stubbed so the whole pipeline is testable offline."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "samples"))

from finscan.schemas import Extraction, Field_, LineItem, PeriodMeta  # noqa: E402

# Q1 FY2027 consolidated figures, in lakhs, exactly as printed in the sample PDF.
Q1_FY27_LAKHS: dict[str, float] = {
    "revenue_from_operations": 128_450.00,
    "other_income": 3_120.00,
    "total_income": 131_570.00,
    "cost_of_materials": 52_310.00,
    "purchases_of_stock_in_trade": 6_240.00,
    "changes_in_inventories": -1_180.00,
    "employee_benefit_expense": 21_760.00,
    "finance_costs": 4_310.00,
    "depreciation_amortisation": 8_920.00,
    "other_expenses": 18_470.00,
    "total_expenses": 110_830.00,
    "exceptional_items": 0.00,
    "profit_before_tax": 20_740.00,
    "current_tax": 5_420.00,
    "deferred_tax": -210.00,
    "tax_expense": 5_210.00,
    "profit_after_tax": 15_530.00,
    "other_comprehensive_income": 180.00,
    "total_comprehensive_income": 15_710.00,
    "paid_up_equity_share_capital": 4_960.00,
    "eps_basic": 31.31,
    "eps_diluted": 31.14,
}

#: The same figures in crores, i.e. what a crores-denominated sheet should receive.
Q1_FY27_CRORES = {
    k: (v if k in {"eps_basic", "eps_diluted"} else round(v / 100.0, 6))
    for k, v in Q1_FY27_LAKHS.items()
}


def sample_extraction(values: dict[str, float] | None = None,
                      company: str = "Northwind Industries Limited") -> Extraction:
    values = Q1_FY27_LAKHS if values is None else values
    return Extraction(
        meta=PeriodMeta(
            company_name=company,
            period_label="Q1 FY2027",
            period_end_date="2026-06-30",
            period_type="quarter",
            consolidated=True,
            audited=False,
            currency="INR",
            units="lakhs",
        ),
        line_items=[
            LineItem(field=Field_(k), label_in_pdf=k.replace("_", " ").title(),
                     value=v, confidence=0.96, page=1)
            for k, v in values.items()
        ],
    )


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> dict[str, Path]:
    """PDF + three divergent models + an isolated profile store."""
    from make_samples import make_acme, make_dated, make_northwind, make_pdf, make_zenith

    pdf = tmp_path / "results.pdf"
    make_pdf(pdf)
    paths = {"pdf": pdf, "dir": tmp_path, "profiles": tmp_path / "profiles"}
    for name, fn in (("northwind", make_northwind), ("acme", make_acme),
                     ("zenith", make_zenith), ("dated", make_dated)):
        p = tmp_path / f"{name}.xlsx"
        fn(p)
        paths[name] = p
        paths[f"{name}_out"] = tmp_path / f"{name}_updated.xlsx"

    from finscan.config import settings
    monkeypatch.setattr(settings, "finscan_profile_store", paths["profiles"])
    return paths


@pytest.fixture
def stub_llm(monkeypatch):
    """Replace the Azure call with a deterministic stub."""

    class _Stub:
        def __init__(self, schema):
            self.schema = schema

        def invoke(self, _messages):
            if self.schema is Extraction:
                return sample_extraction()
            return self.schema(matches=[])

    monkeypatch.setattr("finscan.extract.extractor.structured", lambda s: _Stub(s))
    monkeypatch.setattr("finscan.llm.factory.structured", lambda s: _Stub(s))
    return _Stub


@pytest.fixture
def confirmed(workspace):
    """Confirm a company's profile the way a reviewer would, once."""
    from finscan.profiles import ProfileStore

    def _confirm(key: str, sheets: list[str] | None = None):
        store = ProfileStore(workspace["profiles"])
        pr = store.get(key)
        assert pr is not None, f"no profile '{key}' was proposed"
        if sheets is not None:
            for s in pr.sheets:
                s.enabled = s.sheet in set(sheets)
        pr.confirm(by="test")
        store.save(pr)
        return pr

    return _confirm
