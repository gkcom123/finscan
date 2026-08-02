# FinScan extraction report

**Status:** `ok`

**Company:** Tencent Holdings Limited  
**Period:** 3Q2025 (quarter)  
**Basis:** Consolidated · Unaudited  
**PDF units:** RMB millions

**Profile:** `tencent` · fingerprint `361d6a0362d19961` · reused · confirmed

## Sheets

| Sheet | In scope | Enabled | Units | Write | Input rows | Formula rows |
|---|---|---|---|---|---:|---:|
| Capitalisation | no — only 0 recognisable line item(s) | no | units | append  | 0 | 0 |
| Model | yes | yes | millions | append AH | 11 | 27 |
| PB_CACHE | no — only 0 recognisable line item(s) | no | units | append  | 0 | 0 |
| KPIs | no — only 2 recognisable line item(s) | no | units | append  | 0 | 0 |

**Written:** Model!AH (10v/27f)  
**File:** `inbox/Tencent_test_updated.xlsx`

## Mapping

| Sheet!row | Label | Canonical field | Value | Source | Match | Conf |
|---|---|---|---:|---|---|---:|
| Model!6 | Revenue | revenue_from_operations | 192,869.00 | Revenues | alias | 0.97 |
| Model!8 | Cost of revenues | cost_of_revenue | -84,071.00 | Cost of revenues | alias | 0.97 |
| Model!9 | Gross Profit | gross_profit | — formula — | formula from AG9 | alias/formula | 0.97 |
| Model!12 | Interest income | interest_income | 4,256.00 | Interest income | alias | 0.97 |
| Model!13 | Other gains, net | other_income | 3,303.00 | Other gains/(losses), net | alias | 0.97 |
| Model!14 | Selling and marketing expenses | selling_marketing_expense | -11,468.00 | Selling and marketing expenses | alias | 0.97 |
| Model!15 | General and administrative expenses | general_admin_expense | -34,259.00 | General and administrative expenses | alias | 0.97 |
| Model!17 | Finance costs, net | finance_costs | -3,756.00 | Finance costs | fuzzy | 0.87 |
| Model!18 | Share of (loss)/profit of associates and joint ventures | share_of_associates | 7,854.00 | Share of profit/(loss) of associates and joint ventures, net | alias | 0.97 |
| Model!19 | Profit before income tax | profit_before_tax | — formula — | formula from AG19 | alias/formula | 0.97 |
| Model!20 | Income tax expenses | tax_expense | -9,785.00 | Income tax expense | alias | 0.97 |
| Model!21 | Net profit/loss | profit_after_tax | — formula — | formula from AG21 | alias/formula | 0.97 |
| Model!23 | Non-controlling interest | non_controlling_interest | 1,810.00 | Non-controlling interests | alias | 0.97 |
| Model!37 | EBITDA | ebitda | — formula — | formula from AG37 | alias/formula | 0.85 |
| Model!39 | Depreciation, depletion & amortization | depreciation_amortisation | **NOT FOUND** | — | alias | 0.85 |

## Input rows left empty — the filing yielded no value

- Model!39 Depreciation, depletion & amortization (depreciation_amortisation)

## Derived (not printed in the PDF)

- Total income

## Warnings

- **unmapped_extraction**: Extracted from the PDF but no matching row in the workbook: Earnings per share - Basic, Earnings per share - Diluted, Net gains/(losses) from investments and others, Operating profit (EBIT), Profit attributable to equity holders, Total income
- **constant_formula_not_carried** (depreciation_amortisation): Model!AH39 ('Depreciation, depletion & amortization') left empty. The previous column holds `=6076+1575+8134` — typed-in arithmetic, not a reference — so copying it would repeat last period's figure, and the filing gave no value for this row.

## Notes

- **statement_pages**: Financial statements located on page(s) [5, 6, 7, 8, 9] of 9; the rest was passed as context only.
- **sheet_scanned**: Capitalisation: skipped (only 0 recognisable line item(s)) — 0 input row(s), 0 formula row(s), units=units, write append at C
- **sheet_scanned**: Model: in scope (16 line items, blue/black convention detected; balance_sheet, cash_flow, other section(s) excluded) — 11 input row(s), 27 formula row(s), units=millions, write append at AH
- **sheet_scanned**: PB_CACHE: skipped (only 0 recognisable line item(s)) — 0 input row(s), 0 formula row(s), units=units, write append at C
- **sheet_scanned**: KPIs: skipped (only 2 recognisable line item(s)) — 0 input row(s), 0 formula row(s), units=units, write append at C
- **profile_reused**: Reusing the confirmed layout profile for 'tencent' (fingerprint 361d6a0362d19961).
- **units_converted**: Filing is in millions; rescaled to base units for checking. Each sheet is then converted to its own scale at write time.
- **composite_applied** (other_income): Other income written as 3,303,000,000 = Other income 483,000,000 + Net gains/(losses) from investments and others 2,820,000,000, per this company's saved profile.
- **derived** (total_income): total_income derived = revenue + other income
- **row_ruled_out** (operating_profit): Model!16 ('Operating profit') is excluded by this company's profile — a reviewer determined it is not the filing's operating_profit.
- **period_continuous**: Model: Filing period 2025-09-30 follows the last column (2025-06-30).
- **crosscheck_clean**: The model's own formulas reproduce the filing's subtotals.
- **sign_convention**: Costs are printed in brackets (negative); identities were checked against that convention.
- **rows_unmatched**: 26 workbook row(s) left blank (headings/unknown captions): r7:Model!% of growth, r10:Model!Gross Profit margin, r11:Model!Operating expenses:, r16:Model!Operating profit, r22:Model!% margin, r24:Model!Net profit/loss attributable to equity holders of the Company, r26:Model!% of sales or stated otherwise, r27:Model!=B12 ...
- **constant_formula_replaced** (other_income): Model!AH13 ('Other gains, net'): the previous column held `=2638-3578` — hardcoded arithmetic rather than a reference. Written as this period's value instead of copying it forward.

_Generated 2026-08-02 from inbox/Tencent_q325.pdf_