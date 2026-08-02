"""Canonical financial taxonomy + the Pydantic contracts used across the graph.

The taxonomy is the single source of truth. The LLM extracts *into* it, the Excel
mapper maps existing sheet rows *onto* it, and the validator checks arithmetic
*within* it. That is what keeps the mapping correct: PDF and Excel never talk to
each other directly, they only agree through canonical field ids.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Field_(str, Enum):
    """Canonical line items (IND-AS / IFRS style P&L)."""

    revenue_from_operations = "revenue_from_operations"
    other_income = "other_income"
    total_income = "total_income"

    cost_of_revenue = "cost_of_revenue"
    gross_profit = "gross_profit"
    selling_marketing_expense = "selling_marketing_expense"
    general_admin_expense = "general_admin_expense"
    interest_income = "interest_income"
    investment_gains = "investment_gains"
    operating_profit = "operating_profit"
    share_of_associates = "share_of_associates"
    non_controlling_interest = "non_controlling_interest"
    profit_attributable_to_owners = "profit_attributable_to_owners"

    cost_of_materials = "cost_of_materials"
    purchases_of_stock_in_trade = "purchases_of_stock_in_trade"
    changes_in_inventories = "changes_in_inventories"
    employee_benefit_expense = "employee_benefit_expense"
    finance_costs = "finance_costs"
    depreciation_amortisation = "depreciation_amortisation"
    other_expenses = "other_expenses"
    total_expenses = "total_expenses"

    ebitda = "ebitda"
    exceptional_items = "exceptional_items"
    profit_before_tax = "profit_before_tax"
    tax_expense = "tax_expense"
    current_tax = "current_tax"
    deferred_tax = "deferred_tax"
    profit_after_tax = "profit_after_tax"
    other_comprehensive_income = "other_comprehensive_income"
    total_comprehensive_income = "total_comprehensive_income"

    eps_basic = "eps_basic"
    eps_diluted = "eps_diluted"
    paid_up_equity_share_capital = "paid_up_equity_share_capital"


#: Human labels used in prompts and in the review report.
FIELD_LABELS: dict[str, str] = {
    "revenue_from_operations": "Revenue from operations",
    "other_income": "Other income",
    "total_income": "Total income",
    "cost_of_revenue": "Cost of revenues",
    "gross_profit": "Gross profit",
    "selling_marketing_expense": "Selling and marketing expenses",
    "general_admin_expense": "General and administrative expenses",
    "interest_income": "Interest income",
    "investment_gains": "Net gains/(losses) from investments and others",
    "operating_profit": "Operating profit (EBIT)",
    "share_of_associates": "Share of profit/(loss) of associates and joint ventures",
    "non_controlling_interest": "Non-controlling interests",
    "profit_attributable_to_owners": "Profit attributable to equity holders",
    "cost_of_materials": "Cost of materials consumed",
    "purchases_of_stock_in_trade": "Purchases of stock-in-trade",
    "changes_in_inventories": "Changes in inventories of FG, WIP and stock-in-trade",
    "employee_benefit_expense": "Employee benefits expense",
    "finance_costs": "Finance costs",
    "depreciation_amortisation": "Depreciation and amortisation expense",
    "other_expenses": "Other expenses",
    "total_expenses": "Total expenses",
    "ebitda": "EBITDA",
    "exceptional_items": "Exceptional items",
    "profit_before_tax": "Profit before tax",
    "tax_expense": "Total tax expense",
    "current_tax": "Current tax",
    "deferred_tax": "Deferred tax",
    "profit_after_tax": "Profit after tax",
    "other_comprehensive_income": "Other comprehensive income",
    "total_comprehensive_income": "Total comprehensive income",
    "eps_basic": "Earnings per share - Basic",
    "eps_diluted": "Earnings per share - Diluted",
    "paid_up_equity_share_capital": "Paid-up equity share capital",
}

#: Alias vocabulary for matching messy Excel row labels and PDF captions.
FIELD_ALIASES: dict[str, list[str]] = {
    "revenue_from_operations": [
        "revenue from operations", "net sales", "sales", "total revenue from operations",
        "income from operations", "turnover", "net revenue", "revenue", "gross revenue",
        "revenue from contracts with customers", "operating revenue",
    ],
    "other_income": [
        "other income", "other operating income", "non-operating income",
        "other gains net", "other gains losses net", "other gains/(losses), net",
    ],
    "total_income": ["total income", "total revenue", "total income from operations"],
    "cost_of_materials": [
        "cost of materials consumed", "raw material consumed", "material cost",
        "cost of raw materials", "cost of goods sold", "cogs",
    ],
    "purchases_of_stock_in_trade": ["purchases of stock-in-trade", "purchase of traded goods"],
    "changes_in_inventories": [
        "changes in inventories", "change in inventories of finished goods",
        "increase/decrease in inventories", "(increase)/decrease in stock",
    ],
    "employee_benefit_expense": [
        "employee benefits expense", "employee benefit expenses", "staff cost",
        "personnel expenses", "employee cost", "salaries and wages",
    ],
    "finance_costs": ["finance costs", "finance cost", "interest expense", "interest cost", "interest"],
    "depreciation_amortisation": [
        "depreciation and amortisation expense", "depreciation & amortization",
        "depreciation", "d&a", "depreciation, amortisation and impairment",
        "depreciation, depletion & amortization", "depreciation and amortization",
    ],
    "other_expenses": [
        "other expenses", "other expenditure", "other operating expenses",
        "administrative expenses", "misc expenses", "other opex",
    ],
    "total_expenses": ["total expenses", "total expenditure", "total costs", "total cost"],
    # NB: "operating profit" is NOT an EBITDA alias. In an analyst model it is
    # EBIT — struck after depreciation — and models routinely carry both, with
    # EBITDA computed as operating profit + D&A on the next row. Conflating them
    # is a definitional error worth real money.
    "ebitda": [
        "ebitda", "ebidta", "operating ebitda", "adjusted ebitda", "adj ebitda",
        "earnings before interest tax depreciation and amortisation",
        "operating profit before depreciation",
    ],
    "operating_profit": [
        "operating profit", "operating income", "ebit", "operating profit loss",
        "profit from operations", "operating result",
    ],
    "cost_of_revenue": [
        "cost of revenues", "cost of revenue", "cost of sales", "cost of goods sold", "cogs",
    ],
    "gross_profit": ["gross profit", "gross margin amount", "gross profit loss"],
    "selling_marketing_expense": [
        "selling and marketing expenses", "selling and distribution expenses",
        "sales and marketing", "s and m expenses", "selling expenses", "marketing expenses",
    ],
    "general_admin_expense": [
        "general and administrative expenses", "g and a expenses", "administrative expenses",
        "sg and a", "selling general and administrative expenses",
    ],
    "interest_income": ["interest income", "finance income"],
    "investment_gains": [
        "net gains from investments and others", "net gains losses from investments and others",
        "gains on investments", "investment gains", "net investment gains",
        "fair value gains on investments",
    ],
    "share_of_associates": [
        "share of profit of associates", "share of loss profit of associates and joint ventures",
        "share of results of associates", "share of profit of associates and joint ventures",
        "share of net profit of associates",
    ],
    "non_controlling_interest": [
        "non controlling interest", "non controlling interests", "minority interest",
    ],
    "profit_attributable_to_owners": [
        "profit attributable to equity holders", "net profit attributable to owners",
        "profit for the period attributable to equity holders of the company",
        "attributable to equity holders of the company",
        "net profit loss attributable to equity holders",
    ],
    "exceptional_items": ["exceptional items", "exceptional item", "one-off items"],
    "profit_before_tax": [
        "profit before tax", "pbt", "profit/(loss) before tax", "profit before taxation",
        "profit before tax from continuing operations", "profit before income tax",
        "earnings before tax", "ebt", "income before income taxes",
    ],
    "tax_expense": [
        "tax expense", "total tax expense", "total tax", "income tax expense",
        "income tax expenses", "provision for tax", "provision for taxation",
        "taxes", "income tax",
    ],
    "current_tax": ["current tax"],
    "deferred_tax": ["deferred tax", "deferred tax charge/(credit)"],
    "profit_after_tax": [
        "profit after tax", "pat", "net profit", "profit for the period",
        "profit/(loss) for the period", "net profit after tax", "net income",
        "net profit loss", "profit loss for the period", "net earnings",
    ],
    "other_comprehensive_income": ["other comprehensive income", "oci"],
    "total_comprehensive_income": ["total comprehensive income", "tci"],
    "eps_basic": ["basic eps", "earnings per share - basic", "eps (basic)", "basic (rs.)", "basic"],
    "eps_diluted": ["diluted eps", "earnings per share - diluted", "eps (diluted)", "diluted (rs.)", "diluted"],
    "paid_up_equity_share_capital": ["paid-up equity share capital", "equity share capital", "share capital"],
}

#: Fields that are ratios/per-share and must NOT be rescaled by the units multiplier.
NON_SCALED_FIELDS = {"eps_basic", "eps_diluted"}


# --------------------------------------------------------------------------- #
# Extraction contracts
# --------------------------------------------------------------------------- #
class LineItem(BaseModel):
    field: Field_ = Field(description="Canonical field id this value belongs to.")
    label_in_pdf: str = Field(description="Verbatim row caption as printed in the PDF.")
    value: float = Field(description="Numeric value, in the units printed in the PDF.")
    confidence: float = Field(ge=0.0, le=1.0, description="0-1 extraction confidence.")
    page: int | None = Field(default=None, description="1-based PDF page the value came from.")
    source_row_text: str | None = Field(
        default=None, description="Full raw text of the source row, for audit."
    )


class PeriodMeta(BaseModel):
    company_name: str | None = None
    period_label: str = Field(
        description="Human label of the period the numbers belong to, e.g. 'Q1 FY2027' or 'Jun-2026'."
    )
    period_end_date: str | None = Field(default=None, description="ISO date of period end, if printed.")
    period_type: Literal["quarter", "half_year", "nine_months", "year", "unknown"] = "unknown"
    consolidated: bool | None = Field(
        default=None, description="True if consolidated results, False if standalone."
    )
    audited: bool | None = None
    currency: str = "INR"
    units: Literal["units", "thousands", "lakhs", "millions", "crores", "billions"] = "units"


class Extraction(BaseModel):
    """What the LLM returns for one PDF.

    `line_items` is deliberately required rather than defaulted: an optional list
    is the one a model quietly omits, and an empty extraction that validates
    cleanly is worse than one that fails.
    """

    meta: PeriodMeta
    line_items: list[LineItem] = Field(
        description="Every canonical line item found, for the current period only. "
                    "Must not be empty when the document contains a financial statement."
    )
    notes: str | None = Field(
        default=None, description="Anything ambiguous the reviewer should know."
    )


# --------------------------------------------------------------------------- #
# Mapping / validation contracts
# --------------------------------------------------------------------------- #
class RowMapping(BaseModel):
    excel_row: int
    excel_label: str
    field: str | None = None
    value: float | None = None
    match_method: str = "unmatched"   # alias | fuzzy | llm | override | ruled_out | unmatched
                                      # (suffixed "/formula" when carried forward)
    match_score: float = 0.0
    confidence: float = 0.0
    pdf_label: str | None = None


class Issue(BaseModel):
    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    field: str | None = None


class SheetWriteResult(BaseModel):
    sheet: str
    column_letter: str
    column_index: int
    write_mode: str
    values_written: int
    formulas_copied: int
    rows_skipped: int


class WriteResult(BaseModel):
    workbook_path: str
    header_written: str
    sheets: list[SheetWriteResult] = Field(default_factory=list)

    @property
    def values_written(self) -> int:
        return sum(s.values_written for s in self.sheets)

    @property
    def formulas_copied(self) -> int:
        return sum(s.formulas_copied for s in self.sheets)

    def describe(self) -> str:
        if not self.sheets:
            return "nothing written"
        parts = [f"{s.sheet}!{s.column_letter} ({s.values_written}v/{s.formulas_copied}f)"
                 for s in self.sheets]
        return ", ".join(parts)
