# FinScan

Reads a company's quarterly results PDF and writes the figures into that
company's Excel model as a new period column. It writes the **input cells only**,
copies the **formula cells** forward, and saves a new workbook; the source model
is never modified.

What goes where is decided by one reviewed file per company,
`mappings/<company>.json`. Every quarterly run is a lookup against that file,
not a fresh guess, so the same filing always produces the same column.

## Setup (once)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
cp .env.example .env        # then set FINSCAN_LLM_PROVIDER and its key
```

The LLM is OpenAI or Azure OpenAI, chosen by `FINSCAN_LLM_PROVIDER` in `.env`.

## Running

Put the filing and the model in a folder per company:

```
inbox/<Company>/<anything>.pdf      # one or more filings for the same quarter
inbox/<Company>/<anything>.xlsx     # the model (.xlsm also works)
```

Then run one company, or all of them:

```bash
finscan company almarai
finscan companies
```

(`python -m finscan.cli ...` works the same without the install step.)

Each company run:

1. **Reads** every PDF and rewrites its `.pdf.json`. Reads are cached in
   `_work/pdfcache` by the file's SHA-256, so an unchanged PDF is not sent to
   the model again; a changed PDF is read afresh.
2. **Checks the mapping.** Missing: it is proposed from the workbook and the
   filing, and written. Present: only workbook rows it does not cover yet are
   appended, marked `added <date>: … review`. No new rows: the file is untouched.
3. **Writes** the new column to `inbox/<company>_v2_output.<ext>`.
4. **Regenerates** `models/<company>_model.json` and
   `_work/<company>_<period>_values.json` + `_report.md` (audit only).

Options: `--dry-run` (write nothing; report rows that would be added),
`--period-end YYYY-MM-DD`, `--no-cache` (re-read PDFs), `--sheet` (the sheet for
a company with no mapping yet; default `Model`).

## Files

| File | What it is | In git |
|---|---|---|
| `mappings/<company>.json` | The human decisions: workbook row → filing caption(s) | **yes** |
| `models/<company>_model.json` | The mapping laid onto this run's workbook layout | derived |
| `inbox/<Company>/<pdf>.pdf.json` | The filing as read | derived |
| `_work/` | PDF read cache, values and report per period | no |
| `inbox/<company>_v2_output.*` | The updated model | no |

## The mapping

One row per workbook line, keyed by `(label, section, occurrence)`, never by
row number, so inserting rows in Excel changes nothing:

```json
{"label": "Revenue", "section": "income_statement", "pdf": "Net sales"}
{"label": "Working Capital", "section": "cash_flow", "pdf": ["Receipts from customers", "-Payments to suppliers"]}
{"label": "Adj. EBITDA", "section": "income_statement", "pdf": "EBITDA", "statement": "title:Quarter-on-Quarter"}
{"label": "Impairment of goodwill", "section": "income_statement", "pdf": null, "note": "not printed separately"}
```

`pdf` is one caption, a list to sum (a leading `-` subtracts), or `null` when
the filing has no such line. Optional overrides: `statement`, `column`, `basis`,
`sign`, `scale`, `value` (a constant), `carry_forward`. `scale` replaces currency
unit conversion with a numeric multiplier; `"scale": "percent"` means `0.01`,
so a printed `4.3%` is written as the Excel fraction `0.043`.
A top-level `"reader": "rules"`
switches a company from the vision-model reader to the rule-based one.

## How the workbook is read

Discovered fresh on every run from the mapping's sheet: the label column, the
section of each row (from headings such as "Cash flow"), the dated period
columns and their cadence, the column to write (a pre-formatted empty slot, else
the next one), and each row's role from its reference cell — blue = input,
black = formula copied forward, a formula with no cell references (`=-26.5-10.5`)
= an input typed in place.

### CocaCola BMV filings

The CocaCola mapping uses the LLM vision reader (`"reader": "llm"`) and stable BMV statement
codes (`[210000]`, `[310000]`, `[520000]`) plus the detailed notes (`[800100]`,
`[800200]`). These captions and sums were checked against the Q1 filing and the
model's Q1 column. Keep historical PDFs in a subfolder such as `q1/`; only PDFs
directly in the company folder are read for the current run.

Run `finscan company cocacola` with both current-quarter PDFs (the Spanish BMV
filing and the English results release) and the model in the company folder.
Both PDFs are read with the LLM reader and merged, retaining each table's units
and source filename. The BMV filing supplies detailed financial statements;
the results release supplies operating/non-operating splits and segment tables.
The standalone income-statement quarter is selected by date, closing balances
are not de-cumulated, and year-to-date cash flow is reduced by the model's prior
quarters. Continuation pages and their zero-valued rows are retained.
Adjusted EBITDA combines the release's operating income with the BMV operational
D&A note, converting their different source units before summing. Missing or
ambiguous inputs remain unresolved rather than substituting broader totals or
last quarter's figures. The existing explicitly carried FX rates still require
manual review.

## Tests

```bash
pytest -q        # offline; the model is stubbed
```

## Layout

```
src/finscan/
  cli.py          company <name> | companies
  company.py      inbox/<Company>/ file conventions
  apply.py        bind, check period, resolve, validate, write
  config.py       model provider settings from .env
  llm.py          OpenAI / Azure OpenAI client
  aliases.py      filing-side spellings of canonical fields
  pdf/            stage 1: rule-based and vision readers, OCR, verification
  model/          workbook discovery, colour roles, mapping proposals
  mapping/        the mapping file: schema, proposal, compile to a layout
  match/          caption lookup, sums, periods, value resolution
  write/          copy the workbook, write inputs, copy formulas, comments
mappings/         one reviewed mapping per company (git-tracked)
docs/             architecture and running guide
```

Architecture: [docs/finscan_architecture.md](docs/finscan_architecture.md).
