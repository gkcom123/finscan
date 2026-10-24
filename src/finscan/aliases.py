"""Filing-side spellings of each canonical financial field.

A data table built from real filings. `learn` uses it to recognise a workbook
caption as a canonical field; `match` uses it to find that field's line in a
filing. The mapping decides whether any match it produces is actually kept.
"""
from __future__ import annotations

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
        "cost of raw materials",
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
    "total_before_working_capital_changes": [
        "total before working capital changes", "operating profit before working capital changes",
        "cash generated from operations before working capital changes",
        "operating cash flows before movements in working capital",
        "profit before working capital changes", "cash flows before working capital changes",
        "funds from operations before working capital changes",
    ],
    "net_cash_from_operating_activities": [
        "net cash flow from operating activities", "net cash flow from operating activity",
        "net cash from operating activities", "net cash generated from operating activities",
        "net cash provided by operating activities", "net cash generated from operations",
        "net cash from operations", "cash flow from operating activities",
        "net cash flows from operating activities",
    ],
    "change_in_working_capital": [
        "change in working capital", "changes in working capital",
        "movement in working capital", "movements in working capital",
        "working capital changes", "net change in working capital",
        "(increase)/decrease in working capital", "increase decrease in working capital",
    ],
}
