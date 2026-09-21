---
date: 2026-09-11
reviewed_by: Gunjan Kumar
---

# FinScan

Reads a listed company's results PDF and writes the figures into that company's
Excel model — into the **blue input cells only**, across **every relevant tab**,
leaving the **black formula cells** calculating exactly as the analyst built
them. ⟦REVIEW src: src/finscan/api/main.py, lines 29-35 (FastAPI app description)⟧⟦REVIEW⟧

Built on **LangGraph** ⟦REVIEW src: src/finscan/graph/build.py, lines 1-70 (StateGraph wiring)⟧,
running against **OpenAI or Azure OpenAI** selected by one env var,
`FINSCAN_LLM_PROVIDER` ⟦REVIEW src: src/finscan/config.py, line 15⟧, exposed as a
**FastAPI** service ⟦REVIEW src: src/finscan/api/main.py, line 29⟧ that
**Copilot Studio** can call as a custom connector — though the CLI works
standalone with no server at all ⟦REVIEW src: src/finscan/cli.py, lines 79-193 (argparse entry point, no API dependency)⟧.⟦REVIEW⟧

---


New Way
---

=========
python -m finscan2.cli read 'inbox/Tencent/Tencent_q325.pdf'

.venv/bin/python -m finscan2.cli company almarai read
.venv/bin/python -m finscan2.cli company almarai learn    # → mappings/almarai.json
.venv/bin/python -m finscan2.cli company almarai apply    # resolve + write



Example to debug
---
1. Extract Pdf
finscan inspect-pdf "inbox/Funo/documento-NZlEc-1785359073.pdf" --show > _tmp_funo_pdf_5.txt
2. Reading Excel
finscan inspect "inbox/Almarai/Almarai_1Q26_28 Apr 2026.xlsx"
finscan inspect "inbox/Almarai/Almarai_1Q26_28 Apr 2026.xlsx" --json
## Designed for 100 different models

The hard part is not extraction, it is that no two companies keep their model the
same way — and hardcoding any of that is how the system rots. So FinScan holds
**zero format-specific knowledge**. Everything it needs, it derives from signals
the workbook itself already carries.⟦REVIEW⟧

### 1. The blue/black convention decides what may be written

The one convention every financial model shares:

| Font colour | Meaning | FinScan's behaviour |
|---|---|---|
| **Blue** | hardcoded input | writes the extracted value here |
| **Black** | formula / calculated | **never touched** — the formula is copied forward |
| Green | link to another sheet | left alone |
| Red | link to another workbook | left alone |

⟦REVIEW src: src/finscan/excel/style_probe.py, lines 50-54, 167-190 (Role enum and classify_color)⟧

Getting this right needs more than reading `font.color.rgb`. A colour can be
stored as literal RGB, a legacy palette index, or a **theme slot plus a tint** —
and corporate templates use the last one constantly. `style_probe.py` resolves
all four forms, parsing `theme1.xml` and applying the ECMA-376 tint algorithm
⟦REVIEW src: src/finscan/excel/style_probe.py, lines 32, 79-128 (Theme.resolve, apply_tint)⟧,
then classifies by hue, so any blue works: `0000FF`, `0070C0`, `1F4E79`,
`4472C4` ⟦REVIEW src: src/finscan/excel/style_probe.py, lines 169-171 (classify_color docstring)⟧.
Office's desaturated `#44546A` "Text 2" is deliberately excluded — it is a
*heading* colour, and treating it as an input would overwrite section titles
⟦REVIEW src: src/finscan/excel/style_probe.py, lines 116-118, 179-186⟧.⟦REVIEW⟧

Three vetoes sit on top:

- **A formula is never overwritten**, however it is painted, checked against the
  live target cell at write time — not just against the plan.
  ⟦REVIEW src: src/finscan/excel/writer.py, lines 14-15 (module docstring)⟧
- **A blue-painted formula is still a formula.** ⟦REVIEW src: src/finscan/excel/style_probe.py, line 235⟧
- **A "formula" that is really typed-in arithmetic is not a formula.** A cell
  holding `=6076+1575+8134` looks like a formula — black text, leading `=` —
  but it references no other cell, so it is last quarter's hardcoded number
  wearing a formula's clothes. FinScan detects this (`formula_has_references`)
  and, for a mapped row, replaces it with this period's extracted value instead
  of copying it forward, flagging the swap as `constant_formula_replaced` for
  the reviewer to see.
  ⟦REVIEW src: src/finscan/excel/style_probe.py, lines 193-206 (formula_has_references); src/finscan/excel/writer.py, lines 687-705 (constant_formula_replaced)⟧

### 2. The previous period column is the template

For each row, FinScan looks at the same row in the most recent existing column
⟦REVIEW src: src/finscan/excel/discovery.py, lines 149, 421-441 (reference_col resolution)⟧:

- that cell is a **hardcode** → paste the extracted value;
- that cell is a **formula that references other cells** → copy the formula
  forward, re-pointed at the new column (`=E5+E6` becomes `=F5+F6`)
  ⟦REVIEW src: src/finscan/excel/writer.py, lines 420-449 (openpyxl Translator formula copy)⟧;
- that cell is **typed-in arithmetic with no references** → for an unmapped row
  it is carried forward unchanged so the column stays structurally complete;
  for a mapped row it is replaced by this period's value (see the third veto
  above) ⟦REVIEW src: src/finscan/excel/writer.py, lines 452-463⟧.

So FinScan never decides that a row "should" be a subtotal. It reproduces
whatever the company already did last quarter. Subtotals keep recalculating
instead of being frozen to whatever the PDF happened to print.⟦REVIEW⟧

### 3. Multi-tab discovery and per-company profiles

Every tab is scanned and scored on how many captions resolve to canonical
financial fields. Cover sheets, assumptions and segment breakdowns score zero
and are excluded ⟦REVIEW src: src/finscan/excel/discovery.py, lines 460-467 (scope scoring)⟧.
What is discovered is saved as a per-company **profile** — one editable JSON
file holding the enabled tabs, any caption overrides a reviewer supplied,
field composites, and rows to stop matching
⟦REVIEW src: src/finscan/profiles.py; src/finscan/cli.py, lines 113-124 (profiles subcommand actions: list, show, confirm, map, compose, unmap)⟧.
Adding a new company is a run, not a deployment.⟦REVIEW⟧

By default the confirmation gate is **off** — `FINSCAN_REQUIRE_CONFIRMATION`
defaults to `false`, so a first run for a brand-new company still discovers
the layout and creates a profile, but writes straight through with no human
step ⟦REVIEW src: src/finscan/config.py, lines 44-45⟧⟦REVIEW src: src/finscan/graph/build.py, lines 79-98 (run(), require_confirmation resolution)⟧.
Pass `--require-confirmation` on the CLI, or set `FINSCAN_REQUIRE_CONFIRMATION=true`
in `.env`, to restore the one-off gate: the first upload for a new or
layout-drifted company then comes back as `awaiting_confirmation` with nothing
written, until `finscan profiles confirm` (or `POST /profiles/{key}/confirm`)
signs off on the sheets in scope ⟦REVIEW src: src/finscan/cli.py, lines 57-77, 143-148 (--require-confirmation, --yes)⟧⟦REVIEW src: src/finscan/api/main.py, lines 378-397 (confirm_profile)⟧.
Either way, a layout that drifts from what was last confirmed is detected by
fingerprint and, under the gate, asks a human again while keeping their prior
sheet choices ⟦REVIEW src: src/finscan/profiles.py; src/finscan/graph/nodes.py, lines 92-133 (resolve_profile)⟧.⟦REVIEW⟧

```
FINSCAN_REQUIRE_CONFIRMATION=false (default)
  every upload   discover the layout, save/update the profile, write straight through

FINSCAN_REQUIRE_CONFIRMATION=true, or --require-confirmation
  upload 1       discover the layout, propose it, human confirms  -> profile saved
  upload 2..n    profile reused silently, straight through
  layout drifts  fingerprint mismatch -> human asked again, their sheet choices kept
```
⟦REVIEW src: src/finscan/config.py, lines 44-45; src/finscan/cli.py, lines 57-77; src/finscan/graph/nodes.py, lines 92-133⟧

### 4. The filing must belong in the column

Before anything is written, the target column's own date header is compared
against the period the filing reports. A model last updated to Jun-2025 fed a
Q1-2026 release would otherwise book those numbers into the Sep-2025 slot — and
every ratio, LTM and growth figure downstream would be wrong in a way no
arithmetic check can detect, because the numbers are internally consistent.
They are simply in the wrong place. A gap is a blocking error, not a warning,
unless the operator passes `--allow-period-gap` — which is explicitly for
testing and marks the output as overridden
⟦REVIEW src: src/finscan/graph/nodes.py, lines 404-431 (check_periods)⟧.⟦REVIEW⟧

Cadence is inferred from the spacing of the existing columns, so quarterly,
half-yearly and annual models all work, with tolerance for 4-4-5 calendars
⟦REVIEW src: src/finscan/periods.py⟧.⟦REVIEW⟧

### 5. Statement sections are kept apart

Analyst models stack several statements in one column, and the same caption
appears in more than one. `Taxes` in the cash flow block is cash tax paid;
`Income tax expense` in the income statement is the P&L charge. Matching
captions without knowing which block they sit in eventually writes one into the
other, and nothing downstream would catch it. Sections are detected from
heading text (income statement, cash flow, balance sheet) and only the income
statement block is eligible for P&L fields
⟦REVIEW src: src/finscan/excel/discovery.py, lines 42-103, 205-208 (SECTION_KEYWORDS, detect_section, assign_sections)⟧.⟦REVIEW⟧

### 6. Units are reconciled per sheet

A summary tab in crores next to a detail tab in lakhs is common, so values are
carried in base units and rescaled per sheet at write time
⟦REVIEW src: src/finscan/excel/writer.py, line 49 (scale_to_sheet)⟧.
EPS (basic and diluted) is excluded from rescaling
⟦REVIEW src: src/finscan/schemas.py, line 242 (NON_SCALED_FIELDS)⟧.⟦REVIEW⟧

Sign conventions are detected too: Indian filings print costs positive and
subtract, IFRS releases print them in brackets and add. Both are correct, and
the arithmetic identities only hold once one is chosen
⟦REVIEW src: src/finscan/validate.py, lines 26-27, 49-64, 111-112 (detect_sign_convention)⟧.⟦REVIEW⟧

### 7. When there is no convention at all

Workbooks with no colour coding fall back to geometry detection, are written,
and are forced to `status = needs_review` so a human always checks them
⟦REVIEW src: src/finscan/excel/discovery.py, lines 374, 442-467 (colors_found fallback)⟧.⟦REVIEW⟧

---

## Pipeline

```
ingest_pdf ─► discover ─► extract ─► resolve_profile ─► normalize ─► refine_mapping
                                                                          │
                                                                  extract_label_rows
                                                                          │
                                                                    check_periods
                                                                          │
                                                                      crosscheck
                                                                          │
                                                                       validate
                              ▲                                          │
                              └───── prepare_retry ◄── arithmetic errors, once ──┤
                                                                                  │
                              report ◄─── write_excel ◄─── ok / needs_review ────┤
                                ▲                                                │
                                └──────────── awaiting_confirmation ─────────────┘
                                            (nothing is written, gate only)
```
⟦REVIEW src: src/finscan/graph/build.py, lines 1-70 (node/edge wiring, StateGraph)⟧

| Node | What it does |
|---|---|
| `ingest_pdf` | pdfplumber text + tables per page, OCR fallback for scans |
| `discover` | Scans every tab: label column, header row, statement sections, period dates, units, blue input rows, black formula rows, where the new column goes |
| `extract` | Structured output into the canonical taxonomy, per-item confidence, verbatim source caption |
| `resolve_profile` | Loads/creates this company's profile; detects layout drift |
| `normalize` | Base units; derives absent subtotals, flagged as derived |
| `refine_mapping` | Reviewer overrides, then the LLM only for captions alias+fuzzy could not resolve |
| `extract_label_rows` | For blue rows still unresolved to a canonical field, asks the LLM to match a value directly against the row's own label (optionally the stricter, focused `--reliable-labels` pass) |
| `check_periods` | Does this filing follow the last column? A gap blocks the write |
| `crosscheck` | Simulates the new column's formulas and compares the result with the filing's own reported subtotals |
| `validate` | P&L arithmetic identities, sign convention, plausibility, coverage |
| `write_excel` | Values into blue cells, formulas copied forward, per-sheet unit scaling, provenance comment on every cell, `FinScan_Audit` tab |
| `report` | Markdown review report: sheet table, mapping table, errors and warnings |

⟦REVIEW src: src/finscan/graph/build.py, lines 29-42 (add_node calls); src/finscan/graph/nodes.py, lines 304-382 (extract_label_rows), 434-452 (crosscheck_formulas)⟧

## Correctness guarantees

- The source file is never modified; a copy is produced.
- Formula cells that reference other cells are never overwritten — enforced at
  write time. ⟦REVIEW src: src/finscan/excel/writer.py, lines 420-449⟧
- Typed-in arithmetic wearing a formula's clothes is recognised and, for a
  mapped row, replaced rather than blindly carried forward.
  ⟦REVIEW src: src/finscan/excel/writer.py, lines 452-463, 687-705⟧
- A row is filled only if it is a blue input **and** joint confidence
  (extraction × caption match) clears `FINSCAN_MIN_CONFIDENCE`.
  ⟦REVIEW src: src/finscan/config.py, line 39⟧
- A canonical field can fill at most one row per sheet; contested rows are
  reported, not doubly filled. ⟦REVIEW src: src/finscan/graph/nodes.py, lines 232-269 (_dedupe_sheets)⟧
- Cells inside merged ranges are skipped rather than guessed at.
  ⟦REVIEW src: src/finscan/excel/writer.py, lines 195-197, 368-394⟧
- A filing whose period does not follow the last column is never written,
  unless the operator explicitly opts into `--allow-period-gap` for testing.
  ⟦REVIEW src: src/finscan/graph/nodes.py, lines 404-431⟧
- Only the income statement section is eligible for P&L fields; cash flow and
  balance sheet blocks carrying the same captions are left alone.
  ⟦REVIEW src: src/finscan/excel/discovery.py, lines 91-103⟧
- Before writing, the new column's formulas are simulated and cross-checked
  against the filing's own subtotals; a disagreement is reported as a blocking
  `formula_crosscheck` error rather than written silently.
  ⟦REVIEW src: src/finscan/crosscheck.py; src/finscan/graph/nodes.py, lines 434-452⟧
- Every written cell carries an Excel comment: field, PDF caption, match method,
  confidence. Formula cells record what the filing reported, for eyeballing
  against the model's own calculation. ⟦REVIEW src: src/finscan/excel/writer.py, lines 420-461, 687-698⟧
- `status` ∈ `ok` | `needs_review` | `awaiting_confirmation`. ⟦REVIEW src: src/finscan/graph/nodes.py, lines 455-473 (validate_node, route_after_validate)⟧

## Quick start

Step-by-step local instructions, including troubleshooting, are in
[`docs/RUNNING.md`](docs/RUNNING.md).⟦REVIEW⟧

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e .
cp .env.example .env          # set FINSCAN_LLM_PROVIDER + the matching key

python samples/make_samples.py                 # PDF + 3 divergent models

# see what FinScan makes of a workbook — writes nothing, no LLM needed
finscan inspect samples/acme_model.xlsx

# which PDF pages hold the statements, and did OCR kick in — no LLM call
finscan inspect-pdf samples/northwind_q1_fy27_results.pdf

# run: writes straight through by default (no confirmation gate)
finscan run samples/northwind_q1_fy27_results.pdf samples/northwind_model.xlsx \
            --out samples/northwind_model_updated.xlsx --report report.md

# opt into the one-off human sign-off per company instead
finscan run samples/northwind_q1_fy27_results.pdf samples/northwind_model.xlsx \
            --require-confirmation
finscan profiles confirm northwind_industries --sheets "P&L Summary"
```
⟦REVIEW src: src/finscan/cli.py, lines 79-193 (main, run args, _add_run_args)⟧

Teach it a company-specific caption once:

```bash
finscan profiles map acme_manufacturing --label "Turnover (net)" --field revenue_from_operations
```
⟦REVIEW src: src/finscan/cli.py, lines 113-124, 275-282 (profiles map)⟧

Other profile actions: `compose` (sum several filing fields into one modelled
row) and `unmap` (stop matching a specific sheet row)
⟦REVIEW src: src/finscan/cli.py, lines 284-302 (profiles compose, unmap); src/finscan/graph/nodes.py, line 147 (_apply_composites)⟧.

Other commands: `finscan batch <pdf_dir> <xlsx>`, `finscan company <name>`
(processes `inbox/<company>/*.pdf` + `*.xlsx`/`*.xlsm` into
`inbox/<company>_output.*`), `finscan profiles list|show`.
Flags: `--dry-run`, `--yes`, `--sheets`, `--period`, `--company`,
`--no-llm-mapping`, `--reliable-labels`, `--allow-period-gap`.
⟦REVIEW src: src/finscan/cli.py, lines 57-124, 150-178 (company subcommand)⟧

## Data folders

Three similarly-named folders serve different purposes:

| Folder | Used by | Purpose |
|---|---|---|
| `inbox/` | `finscan company <name>` (CLI) | one subfolder per company; output written beside it as `<company>_output.*` |
| `test/` | the local Copilot Studio demo server, via `POST /demo/update` | one subfolder per company, read from disk instead of uploaded in chat |
| `tests/` | `pytest` | the automated offline test suite |

⟦REVIEW src: src/finscan/demo/folder.py, lines 1-138 (resolve_demo_files); src/finscan/config.py, lines 51-56 (finscan_inbox_dir, finscan_test_dir); test/README.md⟧

## As a service

```bash
uvicorn finscan.api.main:app --port 8080     # docs at /docs
```
⟦REVIEW src: Dockerfile, line 21 (CMD)⟧

Every endpoint except `/health` requires an `x-api-key` header matching
`FINSCAN_API_KEY` (default `change-me` — set a real value before exposing the
server) ⟦REVIEW src: src/finscan/api/main.py, lines 42-44 (auth); src/finscan/config.py, line 48⟧.

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness + whether an LLM key is configured. No auth required. |
| `GET /demo/companies` | Lists the company folders under `test/`, for Copilot to discover. |
| `POST /demo/update` | Reads a company's PDF + workbook from `test/<company>/` and writes the update, for the local demo where files live on disk instead of chat uploads. |
| `POST /inspect` | What FinScan sees in a workbook. No PDF, no LLM, nothing written. |
| `POST /jobs` | Extract + discover + map. Writes nothing; returns the proposal. |
| `POST /profiles/{key}/confirm` | One-off human sign-off on a company's layout |
| `POST /jobs/{id}/apply` | Write the column into every enabled sheet |
| `POST /extract-and-apply` | Straight-through; honours the confirmation gate |
| `GET /jobs/{id}/download` | Fetch the updated workbook |
| `GET /profiles`, `GET /profiles/{key}` | Inspect stored profiles |

⟦REVIEW src: src/finscan/api/main.py, lines 180-397 (all route definitions)⟧

Copilot Studio wiring — connector import, topic design, the SharePoint-triggered
variant — is in [`docs/copilot_studio_setup.md`](docs/copilot_studio_setup.md).⟦REVIEW⟧

**New to Azure or Copilot Studio?** Start with
[`docs/BEGINNER_DEPLOYMENT_GUIDE.md`](docs/BEGINNER_DEPLOYMENT_GUIDE.md) — a
single step-by-step walkthrough from an empty Azure subscription to a working
chat connector ⟦REVIEW src: docs/BEGINNER_DEPLOYMENT_GUIDE.md, lines 1-5⟧.

**Local demo with test folder + ngrok:** see [`docs/COPILOT_DEMO_LOCAL.md`](docs/COPILOT_DEMO_LOCAL.md).⟦REVIEW⟧

**Azure deployment (when IT blocks tunnels):**

- Portal UI + access check: [`docs/AZURE_DEPLOY_PORTAL.md`](docs/AZURE_DEPLOY_PORTAL.md)
- Azure CLI: [`docs/AZURE_DEPLOY.md`](docs/AZURE_DEPLOY.md)

The Swagger 2.0 file Power Platform needs is
[`docs/copilot_connector.yaml`](docs/copilot_connector.yaml).⟦REVIEW⟧

## Tests

```bash
pytest -q     # 80 tests collected, no network — the LLM is stubbed
```
⟦REVIEW src: pytest --collect-only -q output, this session (80 tests collected across tests/test_discovery.py, test_end_to_end.py, test_extraction_input.py, test_mapping.py, test_normalize_validate.py, test_periods.py, test_profiles.py, test_style_probe.py)⟧

The suite runs against three models that agree on nothing structural
⟦REVIEW src: samples/make_samples.py, lines 1-16 (module docstring)⟧:

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
formula-protection invariant. ⟦REVIEW src: tests/test_style_probe.py, tests/test_discovery.py, tests/test_normalize_validate.py, tests/test_profiles.py, tests/test_end_to_end.py (file listing, this session)⟧

## Extending

- **New line item** — add it to `Field_`, `FIELD_LABELS` and `FIELD_ALIASES` in
  `schemas.py`, plus any identity it belongs to in `validate.py`.
  ⟦REVIEW src: src/finscan/schemas.py, lines 16, 66, 106⟧
- **Different colour convention** — the thresholds live in
  `style_probe.classify_color`; nothing else needs to change.
  ⟦REVIEW src: src/finscan/excel/style_probe.py, lines 169-189⟧
- **A company with an odd caption** — `finscan profiles map`, no code change.
- **A row that is really a sum of several filing fields** —
  `finscan profiles compose`, no code change.
- **A row that should stop matching entirely** — `finscan profiles unmap`, no
  code change. ⟦REVIEW src: src/finscan/cli.py, lines 275-302⟧

## Layout

```
src/finscan/
  config.py            all tunables, sourced from env / .env
  schemas.py           canonical taxonomy + contracts
  profiles.py          per-company layout profiles, fingerprint, drift
  validate.py          arithmetic / plausibility / coverage / sign convention
  periods.py           does this filing belong in the next column?
  crosscheck.py         simulates the new column's formulas, compares to the filing
  extract/             pdf_reader, extractor (LLM), normalize (units, derivations)
  excel/               style_probe (colour semantics), discovery (layout),
                       mapper (captions), writer (formula-safe writes)
  llm/factory.py        chat-model factory: OpenAI or Azure OpenAI from one env var
  graph/                state, nodes, build
  demo/folder.py         resolves inbox/<company>/ and test/<company>/ file sets
  api/main.py            FastAPI service
  cli.py                 run | company | batch | inspect | inspect-pdf | profiles
docs/                  Copilot Studio setup, beginner walkthrough, Azure deploy,
                       local demo, Swagger 2.0 connector
inbox/                 per-company folders for `finscan company` (CLI)
test/                  per-company folders for the local Copilot demo server
samples/               generator for the PDF and the three divergent models
tests/                 80 offline tests
```
