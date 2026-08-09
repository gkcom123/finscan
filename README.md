# FinScan

Reads a listed company's results PDF and writes the figures into that company's
Excel model — into the **blue input cells only**, across **every relevant tab**,
leaving the **black formula cells** calculating exactly as the analyst built them.

Built on **LangGraph**, running against **OpenAI or Azure OpenAI** (one env var),
exposed as a **FastAPI** service that **Copilot Studio** can call as a custom
connector — though the CLI works standalone with no server at all.

---

## Designed for 100 different models

The hard part is not extraction, it is that no two companies keep their model the
same way — and hardcoding any of that is how the system rots. So FinScan holds
**zero format-specific knowledge**. Everything it needs, it derives from two
signals the workbook itself already carries.

### 1. The blue/black convention decides what may be written

The one convention every financial model shares:

| Font colour | Meaning | FinScan's behaviour |
|---|---|---|
| **Blue** | hardcoded input | writes the extracted value here |
| **Black** | formula / calculated | **never touched** — the formula is copied forward |
| Green | link to another sheet | left alone |
| Red | link to another workbook | left alone |

Getting this right needs more than reading `font.color.rgb`. A colour can be
stored as literal RGB, a legacy palette index, or a **theme slot plus a tint** —
and corporate templates use the last one constantly. `style_probe.py` resolves
all four forms (parsing `theme1.xml` and applying the ECMA-376 tint algorithm),
then classifies by hue, so any blue works: `0000FF`, `0070C0`, `1F4E79`,
`4472C4`. Office's desaturated `#44546A` "Text 2" is deliberately excluded — it
is a *heading* colour, and treating it as an input would overwrite section
titles.

Two vetoes sit on top:

- **A formula is never overwritten**, however it is painted, checked against the
  live target cell at write time — not just against the plan.
- **A blue-painted formula is still a formula.**

### 2. The previous period column is the template

For each row, FinScan looks at the same row in the most recent existing column:

- that cell is a **hardcode** → paste the extracted value;
- that cell is a **formula** → copy the formula forward, re-pointed at the new
  column (`=E5+E6` becomes `=F5+F6`).

So FinScan never decides that a row "should" be a subtotal. It reproduces
whatever the company already did last quarter. Subtotals keep recalculating
instead of being frozen to whatever the PDF happened to print.

### 3. Multi-tab: discovered, then confirmed once

Every tab is scanned and scored on how many captions resolve to canonical
financial fields. Cover sheets, assumptions and segment breakdowns score zero
and are excluded. What remains is proposed to a human **once per company**:

```
upload 1      discover the layout, propose it, human confirms  -> profile saved
upload 2..n   profile reused silently, straight through
layout drifts fingerprint mismatch -> human asked again, their sheet choices kept
```

Profiles are **data, not code** — one editable JSON file per company holding the
enabled tabs and any caption overrides a reviewer supplied. Adding the 101st
company is an upload and a confirmation, never a deployment.

### 4. The filing must belong in the column

Before anything is written, the target column's own date header is compared
against the period the filing reports. A model last updated to Jun-2025 fed a
Q1-2026 release would otherwise book those numbers into the Sep-2025 slot — and
every ratio, LTM and growth figure downstream would be wrong in a way no
arithmetic check can detect, because the numbers are internally consistent. They
are simply in the wrong place. A gap is a blocking error, not a warning.

Cadence is inferred from the spacing of the existing columns, so quarterly,
half-yearly and annual models all work, with tolerance for 4-4-5 calendars.

### 5. Statement sections are kept apart

Analyst models stack several statements in one column, and the same caption
appears in more than one. `Taxes` in the cash flow block is cash tax paid;
`Income tax expense` in the income statement is the P&L charge. Matching
captions without knowing which block they sit in eventually writes one into the
other, and nothing downstream would catch it. Sections are detected and only
the income statement block is eligible for P&L fields.

### 6. Units are reconciled per sheet

A summary tab in crores next to a detail tab in lakhs is common, so values are
carried in base units and rescaled per sheet at write time. EPS and other
per-share figures are excluded from rescaling.

Sign conventions are detected too: Indian filings print costs positive and
subtract, IFRS releases print them in brackets and add. Both are correct, and
the arithmetic identities only hold once one is chosen.

### 7. When there is no convention at all

Workbooks with no colour coding fall back to geometry detection, are written,
and are forced to `status = needs_review` so a human always checks them.

---

## Pipeline

```
ingest_pdf ─► discover ─► extract ─► resolve_profile ─► normalize ─► refine_mapping
                            ▲                                                  │
                            │                                        check_periods
                            │                                                  │
                            │                                             validate
                            │                                                  │
                            └───────── prepare_retry ◄──── arithmetic errors, once ─────┤
                                                                                       │
                                        report ◄─── write_excel ◄─── ok/needs_review ───┤
                                          ▲                                            │
                                          └───── awaiting_confirmation · period gap ────┘
                                                       (nothing is written)
```

| Node | What it does |
|---|---|
| `ingest_pdf` | pdfplumber text + tables per page, OCR fallback for scans |
| `discover` | Scans every tab: label column, header row, statement sections, period dates, units, blue input rows, black formula rows, where the new column goes |
| `extract` | Structured output into the canonical taxonomy, per-item confidence, verbatim source caption |
| `resolve_profile` | Loads/creates this company's profile; detects layout drift |
| `normalize` | Base units; derives absent subtotals, flagged as derived |
| `refine_mapping` | Reviewer overrides, then the LLM only for captions alias+fuzzy could not resolve |
| `check_periods` | Does this filing follow the last column? A gap blocks the write |
| `validate` | P&L arithmetic identities, sign convention, plausibility, coverage |
| `write_excel` | Values into blue cells, formulas copied forward, per-sheet unit scaling, provenance comment on every cell, `FinScan_Audit` tab |
| `report` | Markdown review report: sheet table, mapping table, errors and warnings |

## Correctness guarantees

- The source file is never modified; a copy is produced.
- Formula cells are never overwritten — enforced at write time.
- A row is filled only if it is a blue input **and** joint confidence
  (extraction × caption match) clears `FINSCAN_MIN_CONFIDENCE`.
- A canonical field can fill at most one row per sheet; contested rows are
  reported, not doubly filled.
- Cells inside merged ranges are skipped rather than guessed at.
- A filing whose period does not follow the last column is never written.
- Only the income statement section is eligible for P&L fields; cash flow and
  balance sheet blocks carrying the same captions are left alone.
- Every written cell carries an Excel comment: field, PDF caption, match method,
  confidence. Formula cells record what the filing reported, for eyeballing
  against the model's own calculation.
- `status` ∈ `ok` | `needs_review` | `awaiting_confirmation`.

## Quick start

Step-by-step local instructions, including troubleshooting, are in
[`docs/RUNNING.md`](docs/RUNNING.md).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e .
cp .env.example .env          # set FINSCAN_LLM_PROVIDER + the matching key

python samples/make_samples.py                 # PDF + 3 divergent models

# see what FinScan makes of a workbook — writes nothing, no LLM needed
finscan inspect samples/acme_model.xlsx

# first upload for a company: proposes a profile, writes nothing
finscan run samples/northwind_q1_fy27_results.pdf samples/northwind_model.xlsx

# confirm the layout once
finscan profiles confirm northwind_industries --sheets "P&L Summary"

# from now on it runs straight through
finscan run samples/northwind_q1_fy27_results.pdf samples/northwind_model.xlsx \
            --out samples/northwind_model_updated.xlsx --report report.md
```

Teach it a company-specific caption once:

```bash
finscan profiles map acme_manufacturing --label "Turnover (net)" --field revenue_from_operations
```

Other commands: `finscan batch <pdf_dir> <xlsx>`, `finscan profiles list|show`.
Flags: `--dry-run`, `--yes` (skip the confirmation gate), `--sheets`,
`--period`, `--company`, `--no-llm-mapping`.

## As a service

```bash
uvicorn finscan.api.main:app --port 8080     # docs at /docs
```

| Endpoint | Purpose |
|---|---|
| `POST /inspect` | What FinScan sees in a workbook. No PDF, no LLM, nothing written. |
| `POST /jobs` | Extract + discover + map. Writes nothing; returns the proposal. |
| `POST /profiles/{key}/confirm` | One-off human sign-off on a company's layout |
| `POST /jobs/{id}/apply` | Write the column into every enabled sheet |
| `POST /extract-and-apply` | Straight-through; honours the confirmation gate |
| `GET /jobs/{id}/download` | Fetch the updated workbook |
| `GET /profiles`, `GET /profiles/{key}` | Inspect stored profiles |

Copilot Studio wiring — connector import, topic design, the SharePoint-triggered
variant — is in [`docs/copilot_studio_setup.md`](docs/copilot_studio_setup.md).

**Local demo with test folder + ngrok:** see [`docs/COPILOT_DEMO_LOCAL.md`](docs/COPILOT_DEMO_LOCAL.md).

**Azure deployment (when IT blocks tunnels):**

- Portal UI + access check: [`docs/AZURE_DEPLOY_PORTAL.md`](docs/AZURE_DEPLOY_PORTAL.md)
- Azure CLI: [`docs/AZURE_DEPLOY.md`](docs/AZURE_DEPLOY.md)
The Swagger 2.0 file Power Platform needs is
[`docs/copilot_connector.yaml`](docs/copilot_connector.yaml).

## Tests

```bash
pytest -q     # 70 tests, no network — the LLM is stubbed
```

The suite runs against three models that agree on nothing structural:

| | northwind | acme | zenith |
|---|---|---|---|
| labels | column A | column B, behind note numbers | column A |
| header row | 4 | 7 | 3 |
| input colour | literal `0000FF` | **theme** accent5 + tint | none |
| units | crores | crores *and* lakhs on sibling tabs | crores |
| formulas | yes | yes | none |
| tabs | 3 (1 in scope) | 4 (2 in scope) | 1 |
| next column | appended | pre-formatted blank, filled | appended |

Covered: theme-colour resolution, blue-painted formulas staying protected,
heading colours not misread as inputs, formulas translated correctly on copy,
per-sheet unit divergence, EPS never rescaled, out-of-scope tabs untouched,
the confirmation gate holding a new company, drift forcing re-confirmation,
repeat uploads appending successive columns, and a direct assault on the
formula-protection invariant.

## Extending

- **New line item** — add it to `Field_`, `FIELD_LABELS` and `FIELD_ALIASES` in
  `schemas.py`, plus any identity it belongs to in `validate.py`.
- **Different colour convention** — the thresholds live in
  `style_probe.classify_color`; nothing else needs to change.
- **A company with an odd caption** — `finscan profiles map`, no code change.

## Layout

```
src/finscan/
  schemas.py          canonical taxonomy + contracts
  profiles.py         per-company layout profiles, fingerprint, drift
  validate.py         arithmetic / plausibility / coverage / sign convention
  periods.py          does this filing belong in the next column?
  extract/            pdf_reader, extractor (LLM), normalize (units, derivations)
  excel/              style_probe (colour semantics), discovery (layout),
                      mapper (captions), writer (formula-safe writes)
  graph/              state, nodes, build
  api/main.py         FastAPI service
  cli.py              run | batch | inspect | profiles
docs/                 Copilot Studio setup, Azure deploy, Swagger 2.0 connector
samples/              generator for the PDF and the three divergent models
tests/                70 offline tests
```
