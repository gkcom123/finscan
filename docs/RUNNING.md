# Running FinScan locally

No Copilot Studio, no Azure, no server. Just the CLI on your machine.

---

## Step 1 — install (once)

```bash
cd ~/Project/Barings/Barrings_EM_Scan/finscan

python3 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt
pip install -e .
```

Check it landed:

```bash
finscan --help
pytest -q          # 70 tests, no network, no API key needed
```

If `finscan` is not found, use `python -m finscan.cli` in place of `finscan`
everywhere below.

## Step 2 — add your OpenAI key

```bash
cp .env.example .env
```

Open `.env` and set two lines:

```
FINSCAN_LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
```

Leave everything else alone for now. `.env` is gitignored.

Verify:

```bash
python -c "from finscan.llm.factory import get_chat_model; get_chat_model(); print('LLM ok')"
```

## Step 3 — put your files in

Copy your PDF and Excel into the project, e.g.:

```bash
mkdir -p inbox
cp ~/Downloads/your_results.pdf   inbox/
cp ~/Downloads/your_model.xlsx    inbox/
```

## Step 4 — inspect the workbook first (no AI, nothing written)

**Do this before anything else.** It costs nothing and tells you whether the
model is readable — which decides how much you should trust the run.

```bash
finscan inspect inbox/your_model.xlsx
```

Read three things in the output:

| Look for | Good | Bad |
|---|---|---|
| `blue/black convention:` | `found` | `NOT found` → inputs are being guessed; every run will be flagged `needs_review` |
| tabs marked `in scope` | your P&L tab(s) | your P&L tab marked `skipped` → captions aren't being recognised, see troubleshooting |
| `units=` | matches the sheet's real scale | `units=units` when the sheet is in crores → the scale marker wasn't found, see below |

If the sheet stacks several statements in one column, the reason line says which
were excluded, e.g. `balance_sheet, cash_flow, other section(s) excluded`. That
is deliberate: `Taxes` in a cash flow block is cash tax paid, not the P&L
charge, and only the income statement block is eligible for P&L fields.

Each in-scope row prints as `input`, `formula` or `locked`:

- **input** — blue cell, will receive a value
- **formula** — black cell, its formula gets copied forward, no value pasted
- **locked** — matched a field but is neither, so it is left alone

## Step 5 — dry run (extracts, writes nothing)

```bash
finscan run inbox/your_results.pdf inbox/your_model.xlsx \
       --dry-run --report dryrun.md
```

Open `dryrun.md`. Check the **Mapping** table row by row: every value should sit
against the caption you'd expect, and the scale should look right. `— formula —`
in the Value column is correct and expected — that row's calculation is being
carried forward instead of a number being pasted.

This is the step where you catch problems. It costs one LLM call and changes
nothing on disk.

## Step 6 — confirm the layout (once per company)

The first real run stops with `status: awaiting_confirmation` and writes
nothing. That is deliberate — it is the one-time sign-off on which tabs are in
scope.

```bash
finscan run inbox/your_results.pdf inbox/your_model.xlsx     # proposes the profile
finscan profiles list                                        # see the key it chose
finscan profiles confirm <company_key> --sheets "P&L" "Quarterly"
```

Sheet names must match the tab names exactly, quoted if they contain spaces.

To skip the gate entirely while experimenting, add `--yes` to `finscan run`.

## Step 7 — the real run

```bash
finscan run inbox/your_results.pdf inbox/your_model.xlsx \
       --out inbox/your_model_updated.xlsx \
       --report run.md
```

Your original file is never modified. Open `your_model_updated.xlsx` and check:

- the new column is where you expected it
- black formula cells still show formulas, now pointing at the new column
- hover any written cell — there's a comment saying which PDF caption it came
  from and how confident the match was
- the `FinScan_Audit` tab lists every write

Exit codes: `0` = ok, `2` = needs_review, `3` = awaiting_confirmation.

## Step 8 — the next quarter

Nothing to confirm. Feed the previous output back in so columns accumulate:

```bash
finscan run inbox/q2_results.pdf inbox/your_model_updated.xlsx \
       --out inbox/your_model_updated.xlsx --report q2.md
```

---

## Troubleshooting

**`blue/black convention: NOT found`**
The model has no colour coding, or uses fills rather than font colour. FinScan
falls back to geometry and flags the run. Confirm the mapping in the dry-run
report carefully; colouring the input cells once fixes it permanently.

**Your P&L tab is `skipped (only N recognisable line items)`**
Fewer than 3 captions matched the canonical vocabulary. See which ones failed:

```bash
finscan inspect inbox/your_model.xlsx | grep -v '\->'
```

Teach it the odd ones — no code change, saved per company:

```bash
finscan profiles map <company_key> --label "Turnover (net)" --field revenue_from_operations
```

Field names are in `src/finscan/schemas.py` under `FIELD_LABELS`.

**`units=units` but the sheet is in crores**
The scale marker wasn't found in the header rows. Add a cell reading
`(Rs. in Crores)` above the header row, or the values will be written at the
wrong scale. Check this in the dry run — it is the easiest error to miss.

**Values landed in the wrong period**
Results tables show 4 periods side by side. Check the `Errors` section of the
report — the arithmetic identities usually catch it. Force the right column with
a hint about which period you want via `--period "Q1 FY2027"` for the header,
and re-read the mapping table.

**`period_gap` — nothing was written**
The filing's period does not follow the last column in the model. Example: the
model's last quarter is Jun-2025 and you fed it a Q1-2026 release — the next
column is the Sep-2025 slot, so writing there would book the wrong quarter's
numbers into it. Every ratio and LTM figure downstream would then be wrong, and
no arithmetic check could see it, because the numbers are internally consistent.

Fix the model first: fill the missing quarters, then re-run. FinScan will not
write across a gap.

**`period_already_present`**
You are re-uploading a period the model already has. Nothing was written.

**`status: needs_review` with `identity_*` errors**
The extracted numbers don't add up internally: revenue + other income ≠ total
income, or PBT − tax ≠ PAT. Usually means values came from more than one column.
The figures were still written — check them before use.

**`no_line_items` — the model found nothing**
Check what the model was actually given:

```bash
finscan inspect-pdf inbox/your_results.pdf
```

Every page should show a score and a role. Statement pages score several times
higher than prose and are marked `STATEMENT`; those are put first in the prompt
and the rest is passed as context only. If a statement page shows `chars` near
zero, it is an image and needs OCR (see below).

FinScan already retries once against the statement pages alone before reporting
this, so a genuine `no_line_items` usually means no text layer, or a document
that is commentary with no statement in it. `--show` prints the exact text sent.

**Scanned PDF, no text found**
Install OCR support:

```bash
brew install tesseract poppler
```

**Nothing written, `no_target_sheets`**
The profile has no enabled sheets. `finscan profiles show <key>` to look, then
re-confirm with the right `--sheets`.

---

## Useful commands

```bash
finscan inspect <xlsx>                          # workbook layout, no AI
finscan inspect-pdf <pdf>                       # which pages hold the statements, no AI
finscan inspect <xlsx> --json                   # machine-readable
finscan run <pdf> <xlsx> --dry-run              # extract, write nothing
finscan run <pdf> <xlsx> --yes                  # skip the confirmation gate
finscan run <pdf> <xlsx> --sheets "P&L"         # restrict to one tab
finscan run <pdf> <xlsx> --period "Q1 FY2027"   # force the column header
finscan run <pdf> <xlsx> --no-llm-mapping       # alias/fuzzy caption matching only
finscan batch <pdf_dir> <xlsx>                  # a folder of PDFs, oldest first
finscan profiles list | show <key> | confirm <key> | map <key>
```

## Trying it on the samples first

If you want to see a known-good run before using your own files:

```bash
python samples/make_samples.py
finscan inspect samples/acme_model.xlsx
finscan run samples/northwind_q1_fy27_results.pdf samples/northwind_model.xlsx \
       --yes --out /tmp/out.xlsx --report /tmp/report.md
```
