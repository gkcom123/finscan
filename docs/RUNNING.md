# Running FinScan locally

The CLI on your machine. No server, no cloud deployment.

---

## Step 1: install (once)

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
pytest -q          # offline, no API key needed
```

If `finscan` is not found, use `python -m finscan.cli` in its place.

## Step 2: add the model key

```bash
cp .env.example .env
```

Set the provider and its key in `.env`:

```
FINSCAN_LLM_PROVIDER=openai          # or azure, with the AZURE_OPENAI_* lines
OPENAI_API_KEY=sk-...
```

Verify:

```bash
python -c "from finscan.llm import chat_model; chat_model(); print('LLM ok')"
```

## Step 3: put the files in

One folder per company under `inbox/`:

```
inbox/Almarai/<results>.pdf
inbox/Almarai/<model>.xlsx
```

- More than one PDF is fine when they report the **same quarter** (a report and
  a press release). Filings for different quarters in one folder are refused.
- Exactly one source workbook. Generated files (`*_output*`, `*_updated*`,
  `*_v2_*`) and Excel lock files (`~$...`) are ignored.
- The mapping file is named after the folder, lower-cased: `inbox/Bharti_Airtel/`
  uses `mappings/bharti_airtel.json`. Renaming a folder orphans its mapping.

## Step 4: dry run (writes nothing)

```bash
finscan company almarai --dry-run
```

Reads the PDF, checks the mapping and resolves every row, but writes no workbook
and no derived files. For an existing mapping it lists any new workbook rows it
would append, without touching the file.

## Step 5: the real run

```bash
finscan company almarai
```

The summary shows where the column went, what happened to the mapping and where
the audit files are:

```
  ALMARAI  ·  period ending 30 Jun 2026
  ✓ Written   inbox/almarai_v2_output.xlsx
              Model!AV  ·  24 values  ·  171 formulas  ·  14 blank
  Mapping     mappings/almarai.json: unchanged
  Derived     models/almarai_model.json
              _work/almarai_2026-06-30_values.json
              _work/almarai_2026-06-30_report.md
```

Open the output workbook and check:

- the new column is where you expected it, with the period date in the header
- formula cells still show formulas, now pointing at the new column
- each written cell carries a comment naming the filing caption it came from;
  blank cells carry the reason they were left blank

The `_report.md` lists every row: value, source caption and any adjustment
(sign, scale, year-to-date de-cumulation).

Exit codes: `0` = written cleanly, `2` = written with row errors to review,
`1` = refused, nothing written.

## Step 6: a new company

With no `mappings/<company>.json`, the first run proposes one from the workbook
and the filing, writes it, and applies it. Review it before trusting the column:

```bash
git diff mappings/            # or open the new file
```

Rows the proposal could not match have `"pdf": null` and a note saying why. Fix
them by hand (see "The mapping" in the README), re-run, then commit the mapping.

If the model's sheet is not called `Model`, name it on that first run:

```bash
finscan company gruma --sheet Model_USD
```

## Step 7: the next quarter

Replace last quarter's PDF in the folder with the new one and use the previous
output as the model (or update the source workbook), then run again. The mapping
is reused unchanged unless the workbook gained rows; new rows are appended and
marked `added <date>: … review`, so check `git diff mappings/` after a run.

## All companies

```bash
finscan companies             # every folder under inbox/
finscan companies --dry-run
```

The batch continues past a failing company and lists the failures at the end.
Close generated workbooks in Excel before running.

---

## Troubleshooting

**`mapping_row_unbound`: nothing written**
The mapping names a workbook row that is not in the sheet any more, usually
because a caption was renamed. The message lists the closest captions; update
that row's `label` in the mapping. This blocks on purpose: a renamed row would
otherwise silently stop being filled.

**A row came out blank (`unresolved_row`)**
The filing does not print the caption the mapping names this quarter, usually
because its wording changed. The report says what was searched for. Update that
row's `pdf` caption.

**`period_not_contiguous` / `period_not_in_filing`: nothing written**
The filing's period is not the next column the model expects. If the model's
last quarter is Mar-2026, the next column is Jun-2026; a Sep-2026 filing would
book the wrong quarter, so it is refused. Fill the missing quarter first.

**`write_column_occupied` / `duplicate_period`**
The model already has this period. Nothing was written.

**`filings_disagree`**
The PDFs in the folder report different quarters. Leave only this quarter's.

**`unverified_figure`: a cell left blank**
The vision reader returned a figure that neither the PDF's own text layer nor
the table's arithmetic confirms. It is not written. Check it against the PDF
and type it in.

**`identity_failed` (warning)**
Written figures do not add up (for example PBT − tax ≠ PAT). Usually a row reads
the wrong line or is missing a component. The message names the rows and the gap.

**`units_unknown`**
No table in the filing states its scale (thousands, millions…). Check the
filing; the figures cannot be scaled safely.

**Read a filing with the rule-based reader**
Add `"reader": "rules"` at the top of the company's mapping, beside `"sheet"`.

**Force a fresh read of an unchanged PDF**
`--no-cache`. This calls the vision model again and may return slightly
different digits, so only do it when a read was wrong.
