---
name: finscan-project
description: "Use when working on the FinScan project: extracting financial data from company PDFs, mapping values into Excel models, debugging wrong period/unit/sign/label matches, managing profiles, running the CLI/API, or extending the LangGraph pipeline."
---

# FinScan Project Skill

## Purpose

FinScan reads a company's financial-results PDF and writes the current reporting period into that company's Excel model. It writes only hardcoded input cells, preserves the model's formulas, and creates a new output workbook instead of modifying the source workbook.

The project is designed for many companies whose Excel layouts differ. Workbook structure is discovered from the workbook itself; company-specific exceptions are stored in JSON profiles rather than hardcoded in Python.

## Architecture At A Glance

```text
PDF + Excel model
        |
        v
  ingest_pdf              Read PDF text, tables, page metadata, OCR fallback
        |
        v
  discover_workbook       Find relevant sheets, labels, dates, units, colors, rows
        |
        v
  extract_financials      Extract canonical financial fields from the PDF
        |
        v
  resolve_profile         Load/create the company's layout profile
        |
        v
  normalize               Convert values to base units, derive safe missing fields
        |
        v
  refine_mapping          Apply profile overrides, aliases, fuzzy/LLM caption mapping
        |
        v
  extract_label_rows      Resolve rows not covered by canonical extraction
        |
        v
  check_periods           Confirm the PDF period belongs in the next workbook column
        |
        v
  crosscheck              Compare model formulas with reported filing values
        |
        v
  validate                Check identities, signs, coverage, and blocking errors
        |
        +--> retry once for extraction/validation errors
        |
        v
  write_excel             Write blue inputs, copy black formulas, add comments/audit
        |
        v
  report                  Produce the review report and final status
```

Graph wiring is defined in `src/finscan/graph/build.py`; node implementations are in `src/finscan/graph/nodes.py`.

## Repository Layout

```text
src/finscan/
  cli.py                  Command-line interface
  config.py               Environment-backed settings
  profiles.py             Company layout profiles and profile store
  schemas.py              Canonical fields, row mappings, issues, write results
  periods.py              Period continuity checks
  validate.py             Accounting identities, signs, and plausibility checks
  crosscheck.py           Formula/subtotal comparisons
  api/main.py             FastAPI service and HTTP endpoints
  graph/build.py          LangGraph construction and run entry point
  graph/nodes.py          Pipeline node implementations
  graph/state.py          Shared LangGraph state
  excel/discovery.py      Workbook layout discovery
  excel/mapper.py         Alias, fuzzy, and LLM caption mapping
  excel/style_probe.py    Excel color/theme/style interpretation
  excel/writer.py         Workbook writing, formula copying, scaling, comments
  extract/pdf_reader.py   PDF text/table extraction and OCR support
  extract/extractor.py    Structured and label-based LLM extraction
  extract/normalize.py    Units and normalization
  llm/factory.py          OpenAI/Azure model construction

profiles/                  One JSON layout/profile file per company
inbox/<company>/            Source PDF and source Excel model
inbox/<company>_output.*   Generated workbook
_work/                      Working artifacts
samples/                   Local sample generation and fixtures
tests/                     Automated tests
docs/                      API, demo, connector, and run documentation
```

## Core Excel Contract

FinScan derives the workbook's rules instead of assuming a fixed layout.

### Blue and black cells

- Blue hardcoded input cells are eligible for extracted values.
- Black formula cells are copied forward with translated references.
- Green and red linked cells are not treated as ordinary inputs.
- A formula is never overwritten, even if its font is blue.
- Cells inside merged ranges are skipped.
- The source workbook is never modified; the output is a copy.

Color detection supports literal RGB, indexed colors, and theme colors with tint. The implementation is in `excel/style_probe.py`.

### Period columns

`excel/discovery.py` identifies the latest existing period and the next write column. `periods.py` checks that the filing period follows the latest model period using the model's cadence and a month-end tolerance.

A period gap or duplicate period is a safety error. Current behavior is sheet-scoped where possible: a blocked sheet is skipped while unrelated sheets that have valid continuity may still be written.

Do not use `--allow-period-gap` for production data. It is intended only for testing and writes into a slot that may represent a different period.

### Units

Values move through the pipeline in base units. At write time, each sheet is scaled independently using its detected units:

```text
units       1
thousands   1,000
lakhs       100,000
millions    1,000,000
crores      10,000,000
billions    1,000,000,000
```

Per-share fields are not scaled. Sheet units can be inferred from reference-column magnitudes when the workbook header is stale.

### Sign conventions

Some models store expenses as positive numbers and subtract them; others store them as negative numbers and add them. FinScan detects the convention for validation and preserves the workbook row convention when safe.

Important fixed-sign cases:

- `cost_of_revenue` is forced negative when printed positive.
- `non_controlling_interest` is a deduction in models whose attributable-profit formula uses `PAT + NCI`; writer sign handling follows the row's prior-period convention.
- FX, valuation, fair-value, remeasurement, and gain/loss rows are bidirectional and must not be forced to match the prior-period sign.
- Cash paid/tax paid/dividends paid labels are negative; cash interest income/received labels are positive.

Sign and scale guards live mainly in `excel/writer.py`; field-level rules live in `validate.py`.

## Extraction Paths

There are two extraction paths.

### Canonical extraction

`extract.extractor.extract()` asks the model for structured canonical fields such as revenue, cost of revenue, finance costs, tax, and cash flow totals. The result contains values, source captions, confidence, period metadata, and source text.

Use this path whenever the workbook row can be mapped reliably to a canonical field.

### Label fallback

`extract_for_labels()` handles workbook rows that do not map to a canonical field, or canonical rows whose main extraction was missing. The label fallback is the riskier path because a PDF may contain the same caption in multiple statements, periods, currencies, or units.

The normal legacy path sends a batch of labels to one LLM call. It has deterministic protections for:

- mixed six-month versus quarterly columns;
- subtotal matches;
- bare-number/uncaptioned rows;
- raw absolute-currency tables mixed with millions-scale statements;
- cash-flow direction words.

The opt-in reliable path is enabled with:

```powershell
finscan company jd --reliable-labels
```

or:

```text
FINSCAN_RELIABLE_LABELS=true
```

Reliable-label mode resolves each queued label in an individual focused extraction call and reports unresolved rows. In this mode, unresolved label-fallback rows are left blank rather than copied from a prior period or defaulted to zero. This prevents stale values from appearing to be freshly extracted.

The reliable mode is currently a rollout path. Validate it against real company workbooks before making it the global default.

## Profiles

Profiles are stored in `profiles/<company_key>.json` and contain:

- enabled sheets;
- workbook fingerprint;
- confirmation state;
- `label_overrides`: Excel caption to canonical field;
- `label_search_overrides`: Excel caption to actual PDF caption;
- `field_composites`: company-specific calculated field definitions;
- `unmapped_rows`: rows that must not be assigned by fuzzy/LLM mapping;
- profile history.

Profiles are data, not code. Use a profile override when one company's model label has a company-specific meaning. For example, an Excel row named `Operating expenses` may actually correspond to the PDF caption `Maintenance expenses`.

Useful commands:

```powershell
finscan profiles list
finscan profiles show jd
finscan profiles confirm jd --sheets Model
finscan profiles map jd --label "Turnover (net)" --field revenue_from_operations
```

A new or unconfirmed profile is not the same as a period failure. The CLI defaults to no confirmation gate, but API or explicit legacy settings can require confirmation.

## CLI Workflows

Use the project interpreter on Windows:

```powershell
c:/Project/finscan/.venv/Scripts/python.exe -m finscan.cli company jd --reliable-labels --report _tmp_jd.md
```

Common commands:

```powershell
# Inspect workbook layout without PDF or LLM calls
finscan inspect "inbox/jd/JD Inc_2Q26_14 Aug 2026.xlsx" --json

# Inspect PDF statement-page detection
finscan inspect-pdf "inbox/jd/JD.com Announces Second Quarter and Interim 2026 Results.pdf" --show

# Process an explicit PDF and workbook
finscan run input.pdf model.xlsx --out model_updated.xlsx --report report.md

# Process a company folder under inbox/
finscan company jd --reliable-labels --report report.md

# Process several PDFs oldest first
finscan batch path/to/pdf-folder model.xlsx --out model_updated.xlsx

# Skip mapping LLM calls
finscan company jd --no-llm-mapping

# Testing only: override a period gap
finscan company jd --allow-period-gap
```

Avoid using plain `python` in the current Windows setup unless the active environment is known to be the project venv. The plain interpreter may lack reportlab or other project dependencies.

## API Workflow

Start the service locally:

```powershell
uvicorn finscan.api.main:app --port 8080
```

Main endpoints:

- `POST /inspect`: inspect a workbook without PDF extraction or writing.
- `POST /jobs`: extract, discover, and map without writing.
- `POST /profiles/{key}/confirm`: confirm the company layout.
- `POST /jobs/{id}/apply`: write a prepared job.
- `POST /extract-and-apply`: run extraction and writing in one request.
- `GET /jobs/{id}/download`: download the generated workbook.
- `GET /profiles` and `GET /profiles/{key}`: inspect profiles.

Connector and Copilot Studio details are in `docs/copilot_connector.yaml` and `docs/copilot_studio_setup.md`.

## Reports and Provenance

Every run produces a markdown report containing:

- status;
- company and period metadata;
- enabled/in-scope sheets;
- row mappings and confidence;
- errors and warnings;
- extraction and profile notes;
- formula crosschecks;
- rows carried forward, defaulted, skipped, or left blank.

Every written cell receives an Excel comment describing its field, PDF caption, match method, confidence, or formula origin. The writer also maintains a `FinScan_Audit` sheet.

When a value looks wrong, inspect the cell comment and report before changing code. They identify whether the value came from:

- canonical extraction;
- alias/fuzzy/LLM mapping;
- label fallback;
- profile override;
- formula copy;
- carry-forward/default behavior.

## Debugging Workflow

1. Reproduce with a fresh CLI run and a temporary report.
2. Make sure Excel is closed. An open output workbook causes `PermissionError` and leaves an older output file untouched.
3. Inspect the exact output cell with openpyxl using `data_only=False`.
4. Read the cell comment to identify the extraction path.
5. Inspect the raw PDF text directly with `read_pdf(...).as_prompt_text(statements_only=False)`.
6. Compare the printed value, period column, sign, and units with the workbook value.
7. Check whether the row was canonical, label fallback, carried forward, defaulted, or formula-derived.
8. Prefer a deterministic guard at the source path that produced the value. Do not patch only the final Excel cell.
9. Add a focused test and rerun the full suite.

Example cell inspection:

```powershell
c:/Project/finscan/.venv/Scripts/python.exe -c "import openpyxl; ws=openpyxl.load_workbook('inbox/jd_output.xlsx', data_only=False)['Model']; c=ws['AL14']; print(c.value); print(c.comment.text if c.comment else None)"
```

Raw PDF inspection:

```powershell
c:/Project/finscan/.venv/Scripts/python.exe -c "from finscan.extract.pdf_reader import read_pdf; print(read_pdf('input.pdf').as_prompt_text(statements_only=False))"
```

Never trust a previously generated workbook without checking its timestamp, lock state, and provenance comment.

## Testing

Run the complete suite with the project interpreter:

```powershell
c:/Project/finscan/.venv/Scripts/python.exe -m pytest -q
```

The expected suite currently reports 80 passing tests. Tests use stubbed LLM behavior and do not require network access.

For changes to extraction or writing, validation should include:

- static/editor diagnostics;
- the narrowest relevant test;
- the full pytest suite;
- one live CLI run against the affected company when a real filing is available;
- direct openpyxl inspection of the regenerated output cell.

## Extension Rules

- Add a canonical field in `schemas.py`, its aliases and label, then update relevant identities in `validate.py`.
- Do not add company-specific caption meanings to general extraction heuristics; use a profile override.
- Keep source-row text and provenance when adding extraction logic.
- Prefer null plus a visible review issue over a plausible guessed number.
- Do not trust prompt wording as the only protection; add a deterministic check for period, scale, sign, and source-row validity.
- Do not use a single prior-period anchor as the only scale/sign signal when it can be zero, formula-based, or subject to genuine volatility.
- Keep formula cells protected and preserve the source workbook.
- Add tests for every new guard, especially false-positive and ambiguous cases.
- Avoid broad refactors while fixing one company's filing; first identify the exact extraction/write path.

## Current Known Risk Areas

- PDFs may contain several tables with the same caption but different periods, currencies, or units.
- LLM caption mapping is nondeterministic unless constrained by profile data or explicit candidates.
- A raw PDF may have both cumulative and quarterly columns.
- OCR can drop parentheses or alter punctuation.
- Carry-forward behavior in legacy label mode can hide an unresolved extraction by making the output look complete.
- An open Excel output file prevents regeneration and can create a stale-file false alarm.
- A workbook can contain a sheet with a different cadence from the main model; period checks must remain sheet-scoped.

When in doubt, stop the write for the affected row, report the ambiguity, and ask for a profile mapping or human confirmation instead of silently choosing a value.
