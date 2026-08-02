# FinScan extraction report

**Status:** `awaiting_confirmation`

> This company's layout profile has not been confirmed yet. Nothing was written. Review the sheet list and mapping below, then confirm the profile once — subsequent uploads run without asking.

**Company:** n/a  
**Period:** 3Q2025 (unknown)  
**Basis:** Consolidated · Unaudited  
**PDF units:** RMB millions

**Profile:** `unknown` · fingerprint `361d6a0362d19961` · reused · UNCONFIRMED

## Sheets

| Sheet | In scope | Enabled | Units | Write | Input rows | Formula rows |
|---|---|---|---|---|---:|---:|
| Capitalisation | no — only 0 recognisable line item(s) | no | units | append  | 0 | 0 |
| Model | yes | yes | millions | append AH | 9 | 29 |
| PB_CACHE | no — only 0 recognisable line item(s) | no | units | append  | 0 | 0 |
| KPIs | no — only 2 recognisable line item(s) | no | units | append  | 0 | 0 |

## Mapping

| Sheet!row | Label | Canonical field | Value | Source | Match | Conf |
|---|---|---|---:|---|---|---:|
| Model!6 | Revenue | revenue_from_operations | 192,869.00 | Revenues | alias | 0.95 |
| Model!8 | Cost of revenues | cost_of_revenue | -84,071.00 | Cost of revenues | alias | 0.95 |
| Model!9 | Gross Profit | gross_profit | — formula — | formula from AG9 | alias/formula | 0.95 |
| Model!12 | Interest income | interest_income | 4,256.00 | Interest income | alias | 0.95 |
| Model!13 | Other gains, net | other_income | — formula — | formula from AG13 | alias/formula | 0.85 |
| Model!14 | Selling and marketing expenses | selling_marketing_expense | -11,468.00 | Selling and marketing expenses | alias | 0.95 |
| Model!15 | General and administrative expenses | general_admin_expense | -34,259.00 | General and administrative expenses | alias | 0.95 |
| Model!16 | Operating profit | operating_profit | — formula — | formula from AG16 | alias/formula | 0.95 |
| Model!17 | Finance costs, net | finance_costs | — formula — | — | fuzzy | 0.85 |
| Model!18 | Share of (loss)/profit of associates and joint ventures | share_of_associates | 7,854.00 | Share of profit/(loss) of associates and joint ventures, net | alias | 0.95 |
| Model!19 | Profit before income tax | profit_before_tax | — formula — | formula from AG19 | alias/formula | 0.95 |
| Model!20 | Income tax expenses | tax_expense | -9,785.00 | Income tax expense | alias | 0.95 |
| Model!21 | Net profit/loss | profit_after_tax | — formula — | formula from AG21 | alias/formula | 0.95 |
| Model!23 | Non-controlling interest | non_controlling_interest | 1,810.00 | Non-controlling interests | alias | 0.95 |
| Model!24 | Net profit/loss attributable to equity holders of the Company | profit_attributable_to_owners | — formula — | formula from AG24 | llm/formula | 0.00 |
| Model!37 | EBITDA | ebitda | — formula — | formula from AG37 | alias/formula | 0.95 |
| Model!38 | Operating profit | operating_profit | — formula — | formula from AG38 | llm/formula | 0.00 |
| Model!39 | Depreciation, depletion & amortization | depreciation_amortisation | — formula — | formula from AG39 | alias/formula | 0.85 |
| Model!40 | EBITDA | ebitda | — formula — | formula from AG40 | llm/formula | 0.00 |

## Warnings

- **unmapped_extraction**: Extracted from the PDF but no matching row in the workbook: Earnings per share - Basic, Earnings per share - Diluted, Other comprehensive income, Total comprehensive income

## Notes

- **statement_pages**: Financial statements located on page(s) [5, 6, 7, 8, 9] of 9; the rest was passed as context only.
- **sheet_scanned**: Capitalisation: skipped (only 0 recognisable line item(s)) — 0 input row(s), 0 formula row(s), units=units, write append at C
- **sheet_scanned**: Model: in scope (16 line items, blue/black convention detected; balance_sheet, cash_flow, other section(s) excluded) — 9 input row(s), 29 formula row(s), units=millions, write append at AH
- **sheet_scanned**: PB_CACHE: skipped (only 0 recognisable line item(s)) — 0 input row(s), 0 formula row(s), units=units, write append at C
- **sheet_scanned**: KPIs: skipped (only 2 recognisable line item(s)) — 0 input row(s), 0 formula row(s), units=units, write append at C
- **profile_reused**: Reusing the confirmed layout profile for 'unknown' (fingerprint 361d6a0362d19961).
- **units_converted**: Filing is in millions; rescaled to base units for checking. Each sheet is then converted to its own scale at write time.
- **llm_mapping**: 3 unusual caption(s) resolved by the model; confirm them once and they become saved overrides.
- **period_continuous**: Model: Filing period 2025-09-30 follows the last column (2025-06-30).
- **sign_convention**: Costs are printed in brackets (negative); identities were checked against that convention.
- **rows_unmatched**: 22 workbook row(s) left blank (headings/unknown captions): r7:Model!% of growth, r10:Model!Gross Profit margin, r11:Model!Operating expenses:, r22:Model!% margin, r26:Model!% of sales or stated otherwise, r27:Model!=B12, r28:Model!=B13, r29:Model!=B14 ...

_Generated 2026-08-02 from inbox/Tencent_q325.pdf_