---
name: finscan-project
description: "Use when working on the FinScan project: reading company results PDFs, writing a new period column into Excel models, editing per-company mappings, debugging wrong captions/periods/units/signs, or running the finscan CLI."
---

# FinScan Project Skill

## Purpose

FinScan reads a company's quarterly results PDF and writes that period into the
company's Excel model as a new column. It writes input cells only, copies formula
cells forward, and saves a new workbook; the source workbook is never modified.

Everything company-specific lives in one reviewed, git-tracked file,
`mappings/<company>.json`. Code stays company-agnostic: never hardcode a company,
caption or row number in Python.

## Running

```bash
finscan company <name>      # inbox/<Name>/ — case-insensitive folder name
finscan companies           # every folder under inbox/
# options: --dry-run  --period-end YYYY-MM-DD  --no-cache  --sheet <sheet>
```

Each run (`src/finscan/cli.py`):

1. Read every PDF → `<pdf>.pdf.json` (rewritten each run; reads cached in
   `_work/pdfcache` by SHA-256). Reader is the vision model by default;
   `"reader": "rules"` in the mapping selects the rule-based reader. A
   `<pdf>.pdf.supplement.json` beside a PDF adds hand-supplied tables, verified
   against the page text.
2. Mapping: created if missing; otherwise only workbook input rows it lacks are
   appended (marked `added <date>: … review`); otherwise untouched.
3. `apply.run`: discover the sheet, compile the mapping onto it, check the
   period, resolve values, validate, write `inbox/<key>_v2_output.<ext>`.
4. Rewrite `models/<key>_model.json` and `_work/<key>_<period>_values.json` /
   `_report.md`. Nothing reads these back.

## Layout

```
src/finscan/
  cli.py            the two commands
  company.py        inbox/<Company>/ conventions; ambiguity is an error
  apply.py          bind → period → resolve → validate → write
  config.py, llm.py model provider (OpenAI / Azure OpenAI via .env)
  aliases.py        filing spellings of canonical fields
  pdf/              read.py (rules + supplements), llm_read.py (vision), verify.py,
                    ocr.py, statements.py, columns.py, notes.py
  model/            discover.py (sheet layout), style_probe.py (cell colour roles),
                    learn.py + propose_captions.py (mapping proposals)
  mapping/          schema.py (the file), propose.py, compile.py
  match/            select.py (caption lookup), resolve.py (values, periods,
                    de-cumulation, sums), basis.py
  write/column.py   copy workbook, write inputs, copy formulas, comments
mappings/           one per company — the human contract
tests/              offline; the model is stubbed
```

## Invariants — do not break

- **Formula cells are never overwritten.** Inputs are blue (or plain numbers when
  a sheet has no colour convention); a formula with no cell references
  (`=-26.5-10.5`) is an input typed in place and is written, never copied.
- **The mapping is keyed by `(label, section, occurrence)`**, never row number.
  A mapping row that binds to nothing (`mapping_row_unbound`) blocks the write.
- **A run never rewrites existing mapping lines.** New rows are appended with
  `Mapping.append_rows`, which preserves keys the schema does not model.
- **No write across a period gap** or into a column that already holds the period.
- **A vision-read or supplied figure is written only when the page's text layer
  (or the table's arithmetic) confirms it**; otherwise the cell is left blank with
  the reason (`unverified_figure`).
- **Sections stay apart**: a cash-flow "Depreciation" never reads the P&L line.
- **The filing's printed sign is kept** unless the mapping negates the caption
  (a leading `-`) or asserts `sign`.

## Mapping rows

```json
{"label": "Revenue", "section": "income_statement", "pdf": "Net sales"}
{"label": "Taxes", "section": "cash_flow", "pdf": ["-Income taxes paid", "Tax refunds?"]}
{"label": "Adj. EBITDA", "section": "income_statement", "pdf": "EBITDA", "statement": "title:Quarter-on-Quarter"}
{"label": "FX rate", "section": "other", "carry_forward": true, "note": "peg, typed by hand"}
{"label": "FX rate average", "section": "other", "copy_formula": true}
```

- `pdf`: caption | list to sum (`-` subtracts) | `null` (not in the filing).
- In a caption: `?` = optional (0 when not printed); `#2` = the 2nd exact match;
  `Heading > Caption` = the line after a heading; ` @ title:<text>` = read this
  term in another table; ` ## <column header>` = read this term in that column.
- Overrides: `statement` (`title:<text>`, `title:A || B`), `column` (header text,
  `Reported#1`), `basis`, `sign`, `scale`, `value` (constant), `carry_forward`,
  `copy_formula` (copy the row's latest formula).

## Debugging

1. Run with `--dry-run` and read `_work/<key>_<period>_report.md` — every row's
   value, source caption and adjustments.
2. Wrong or blank value → inspect `inbox/<Company>/<pdf>.pdf.json` for the
   statement, its columns (`end`, `months`) and the caption actually printed;
   fix the mapping row, not the code, when the filing is just worded differently.
3. Wrong column/period → check `period_dates` and `write_col` in
   `models/<key>_model.json`.
4. Back-test a mapping against a period the model already has: resolve it for
   that period and compare each value with the analyst's column.
5. Parser bugs belong in `pdf/`; bump `PARSER_VERSION` in `schema.py` so stale
   caches are re-parsed.

## Deployment

Azure Container Apps Job plus an optional Job API for Foundry:
`docs/FinModelUpdater_Deployment.md`.

## Testing

```bash
pytest -q
```

Add a focused test beside the module you change (`tests/test_v2_<area>.py`),
built from a minimal in-memory workbook or a literal page of text.
