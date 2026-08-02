# FinScan extraction report

**Status:** `ok`

**Company:** Northwind Industries Limited  
**Period:** Q1 FY2027 (quarter)  
**Basis:** Consolidated · Unaudited  
**PDF units:** INR lakhs

**Profile:** `northwind_industries` · fingerprint `5bcb3d6fb02485ec` · reused · confirmed

## Sheets

| Sheet | In scope | Enabled | Units | Write | Input rows | Formula rows |
|---|---|---|---|---|---:|---:|
| Cover | no — only 0 recognisable line item(s) | no | units | append  | 0 | 0 |
| P&L Summary | yes | yes | crores | append F | 13 | 5 |
| Assumptions | no — only 0 recognisable line item(s) | no | units | append  | 0 | 0 |

**Written:** P&L Summary!F (13v/5f)  
**File:** `samples/northwind_model_updated.xlsx`

## Mapping

| Sheet!row | Label | Canonical field | Value | Source | Match | Conf |
|---|---|---|---:|---|---|---:|
| P&L Summary!5 | Net Sales | revenue_from_operations | 1,284.50 | Revenue From Operations | alias | 0.96 |
| P&L Summary!6 | Other Income | other_income | 31.20 | Other Income | alias | 0.96 |
| P&L Summary!7 | Total Income | total_income | — formula — | formula from E7 | alias/formula | 0.96 |
| P&L Summary!8 | Raw Material Consumed | cost_of_materials | 523.10 | Cost Of Materials | alias | 0.96 |
| P&L Summary!9 | Purchase of Traded Goods | purchases_of_stock_in_trade | 62.40 | Purchases Of Stock In Trade | alias | 0.96 |
| P&L Summary!10 | (Increase)/Decrease in Stock | changes_in_inventories | -11.80 | Changes In Inventories | alias | 0.96 |
| P&L Summary!11 | Staff Cost | employee_benefit_expense | 217.60 | Employee Benefit Expense | alias | 0.96 |
| P&L Summary!12 | Interest Cost | finance_costs | 43.10 | Finance Costs | alias | 0.96 |
| P&L Summary!13 | Depreciation & Amortization | depreciation_amortisation | 89.20 | Depreciation Amortisation | alias | 0.96 |
| P&L Summary!14 | Other Expenditure | other_expenses | 184.70 | Other Expenses | alias | 0.96 |
| P&L Summary!15 | Total Expenditure | total_expenses | — formula — | formula from E15 | alias/formula | 0.96 |
| P&L Summary!16 | Operating Profit (EBITDA) | ebitda | — formula — | formula from E16 | alias/formula | 0.85 |
| P&L Summary!17 | Exceptional Items | exceptional_items | 0.00 | Exceptional Items | alias | 0.96 |
| P&L Summary!18 | PBT | profit_before_tax | — formula — | formula from E18 | alias/formula | 0.96 |
| P&L Summary!19 | Total Tax | tax_expense | 52.10 | Tax Expense | alias | 0.96 |
| P&L Summary!20 | Net Profit | profit_after_tax | — formula — | formula from E20 | alias/formula | 0.96 |
| P&L Summary!21 | EPS - Basic (Rs.) | eps_basic | 31.31 | Eps Basic | fuzzy | 0.96 |
| P&L Summary!22 | EPS - Diluted (Rs.) | eps_diluted | 31.14 | Eps Diluted | fuzzy | 0.96 |

## Derived (not printed in the PDF)

- EBITDA

## Warnings

- **unmapped_extraction**: Extracted from the PDF but no matching row in the workbook: Current tax, Deferred tax, Other comprehensive income, Paid-up equity share capital, Total comprehensive income

## Notes

- **sheet_scanned**: Cover: skipped (only 0 recognisable line item(s)) — 0 input row(s), 0 formula row(s), units=units, write append at C
- **sheet_scanned**: P&L Summary: in scope (18 line items, blue/black convention detected) — 13 input row(s), 5 formula row(s), units=crores, write append at F
- **sheet_scanned**: Assumptions: skipped (only 0 recognisable line item(s)) — 0 input row(s), 0 formula row(s), units=units, write append at C
- **profile_reused**: Reusing the confirmed layout profile for 'northwind_industries' (fingerprint 5bcb3d6fb02485ec).
- **units_converted**: Rescaled values from lakhs (PDF) to units (workbook).
- **derived** (ebitda): ebitda derived = PBT + finance costs + D&A - other income - exceptional items

_Generated 2026-08-01 from samples/northwind_q1_fy27_results.pdf_