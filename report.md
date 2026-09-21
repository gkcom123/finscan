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
| Model | yes | yes | millions | append AH | 12 | 27 |
| PB_CACHE | no — only 0 recognisable line item(s) | no | units | append  | 0 | 0 |
| KPIs | no — only 2 recognisable line item(s) | no | units | append  | 0 | 0 |

## Mapping

| Sheet!row | Label | Canonical field | Value | Source | Match | Conf |
|---|---|---|---:|---|---|---:|
| Model!6 | Revenue | revenue_from_operations | 192,869.00 | Revenues | alias | 0.95 |
| Model!8 | Cost of revenues | cost_of_revenue | -84,071.00 | Cost of revenues | alias | 0.95 |
| Model!9 | Gross Profit | gross_profit | — formula — | formula from AG9 | alias/formula | 0.95 |
| Model!12 | Interest income | interest_income | 4,256.00 | Interest income | alias | 0.95 |
| Model!13 | Other gains, net | other_income | **NOT FOUND** | — | alias | 0.85 |
| Model!14 | Selling and marketing expenses | selling_marketing_expense | -11,468.00 | Selling and marketing expenses | alias | 0.95 |
| Model!15 | General and administrative expenses | general_admin_expense | -34,259.00 | General and administrative expenses | alias | 0.95 |
| Model!16 | Operating profit | operating_profit | — formula — | formula from AG16 | alias/formula | 0.85 |
| Model!17 | Finance costs, net | finance_costs | -3,756.00 | Finance costs | fuzzy | 0.87 |
| Model!18 | Share of (loss)/profit of associates and joint ventures | share_of_associates | 7,854.00 | Share of profit/(loss) of associates and joint ventures, net | alias | 0.95 |
| Model!19 | Profit before income tax | profit_before_tax | — formula — | formula from AG19 | alias/formula | 0.95 |
| Model!20 | Income tax expenses | tax_expense | -9,785.00 | Income tax expense | alias | 0.95 |
| Model!21 | Net profit/loss | profit_after_tax | — formula — | formula from AG21 | alias/formula | 0.95 |
| Model!23 | Non-controlling interest | non_controlling_interest | 1,810.00 | Non-controlling interests | alias | 0.95 |
| Model!24 | Net profit/loss attributable to equity holders of the Company | profit_attributable_to_owners | — formula — | formula from AG24 | llm/formula | 0.00 |
| Model!37 | EBITDA | ebitda | — formula — | formula from AG37 | alias/formula | 0.95 |
| Model!39 | Depreciation, depletion & amortization | depreciation_amortisation | **NOT FOUND** | — | alias | 0.85 |

## Input rows left empty — the filing yielded no value

- Model!13 Other gains, net (other_income)
- Model!39 Depreciation, depletion & amortization (depreciation_amortisation)

## Errors

- **formula_crosscheck** (profit_before_tax): Model!AH19 ('Profit before income tax'): the model's own formula computes 71,425.00 for Profit before tax, but the filing reports 74,728.00 (out by -3,303.00). An input row feeding this subtotal does not hold what its caption suggests — check the rows above.
- **formula_crosscheck** (profit_after_tax): Model!AH21 ('Net profit/loss'): the model's own formula computes 61,640.00 for Profit after tax, but the filing reports 64,943.00 (out by -3,303.00). An input row feeding this subtotal does not hold what its caption suggests — check the rows above.
- **formula_crosscheck** (profit_attributable_to_owners): Model!AH24 ('Net profit/loss attributable to equity holders of the Company'): the model's own formula computes 59,830.00 for Profit attributable to equity holders, but the filing reports 63,133.00 (out by -3,303.00). An input row feeding this subtotal does not hold what its caption suggests — check the rows above.

## Warnings

- **ambiguous_row** (operating_profit): Model: rows 38 and 16 both resolve to 'operating_profit' ('Operating profit' / 'Operating profit'). Row 16 kept; row 38 left alone.
- **ambiguous_row** (ebitda): Model: rows 40 and 37 both resolve to 'ebitda' ('EBITDA' / 'EBITDA'). Row 37 kept; row 40 left alone.
- **unmapped_extraction**: Extracted from the PDF but no matching row in the workbook: Earnings per share - Basic, Earnings per share - Diluted, Net gains/(losses) from investments and others, Other comprehensive income, Total comprehensive income

## Notes

- **statement_pages**: Financial statements located on page(s) [5, 6, 7, 8, 9] of 9; the rest was passed as context only.
- **sheet_scanned**: Capitalisation: skipped (only 0 recognisable line item(s)) — 0 input row(s), 0 formula row(s), units=units, write append at C
- **sheet_scanned**: Model: in scope (16 line items, blue/black convention detected; balance_sheet, cash_flow, other section(s) excluded) — 12 input row(s), 27 formula row(s), units=millions, write append at AH
- **sheet_scanned**: PB_CACHE: skipped (only 0 recognisable line item(s)) — 0 input row(s), 0 formula row(s), units=units, write append at C
- **sheet_scanned**: KPIs: skipped (only 2 recognisable line item(s)) — 0 input row(s), 0 formula row(s), units=units, write append at C
- **profile_reused**: Reusing the confirmed layout profile for 'unknown' (fingerprint 361d6a0362d19961).
- **units_converted**: Filing is in millions; rescaled to base units for checking. Each sheet is then converted to its own scale at write time.
- **llm_mapping**: 3 unusual caption(s) resolved by the model; confirm them once and they become saved overrides.
- **label_match**: Direct PDF label match filled 1 additional row(s): Adj. EBITDA
- **period_continuous**: Model: Filing period 2025-09-30 follows the last column (2025-06-30).
- **sign_convention**: Costs are printed in brackets (negative); identities were checked against that convention.
- **rows_unmatched**: 24 workbook row(s) left blank (headings/unknown captions): r7:Model!% of growth, r10:Model!Gross Profit margin, r11:Model!Operating expenses:, r22:Model!% margin, r26:Model!% of sales or stated otherwise, r27:Model!=B12, r28:Model!=B13, r29:Model!=B14 ...

_Generated 2026-08-08 from ./inbox/Tencent_q325.pdf_