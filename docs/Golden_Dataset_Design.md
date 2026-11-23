# Golden Dataset — Design Document

| | |
|---|---|
| **Component** | `golden/`, `scripts/golden_set.py` |
| **Purpose** | Reference values from human-reviewed quarters, used to regression-check FinScan output |
| **Status** | In use — 26 companies, 1,338 reference rows, all passing at time of writing |

## 1. Problem

FinScan turns a company's quarterly results PDF into a new column in that
company's Excel model. Many parts affect each written number: the PDF reader
(the vision model or the rule-based reader), text-layer verification, caption
matching, period selection and de-cumulation, sign handling, and the
per-company mapping in `mappings/<company>.json`.

A change to any of these, such as a parser fix, a new caption alias, a mapping
edit or a model upgrade, can silently change a figure for a company nobody
was looking at. Unit tests (`tests/`) use stubbed models and small in-memory
workbooks; they show that the code works as designed, but not that real
filings still produce the right numbers.

We need a cheap, repeatable way to answer: **"Does FinScan still produce
the numbers a person already confirmed are correct?"**

## 2. Goals and non-goals

**Goals**
- Store, per company and period, the reviewed value of every mapped row.
- Check a fresh run against those values and report each difference precisely.
- Keep the check quick, free and easy to read (plain JSON, diff-friendly in git).
- Treat a confirmed blank as an assertion too, so we catch a row that
  starts producing a value it shouldn't.

**Non-goals**
- Showing the original review was correct. The golden set records what a
  reviewer accepted; it does not independently audit the filing.
- Recording provenance (which caption, page or adjustment). That changes with
  every run and lives in `_work/<key>_<period>_report.md`.
- Testing the Excel write step (column placement, formula copying, styling).
  Unit tests and the run's own validations cover that.

## 3. What a golden file represents

One file per reviewed quarter: `golden/<key>_<period_end>.json`.

```json
{
  "company": "axiata_issue",
  "period_end": "2026-06-30",
  "units": "millions",
  "values": [
    {"label": "Operating revenue",      "section": "income_statement", "value": 2870.302},
    {"label": "Impairment of goodwill", "section": "income_statement", "value": null},
    ...
  ]
}
```

| Field | Meaning |
|---|---|
| `company` | Company key, matching `mappings/<key>.json` and `inbox/<Name>/` |
| `period_end` | Quarter end date (`YYYY-MM-DD`) of the column the run writes |
| `units` | Scale the values are stated in (currently `millions` for all files) |
| `values[]` | One entry per mapped row, **in the order they were resolved** |
| `label` | Row caption in the Excel model (the mapping key) |
| `section` | `income_statement`, `balance_sheet`, `cash_flow` or `other`; keeps the same caption in different statements apart |
| `value` | Expected number, sign as written to the model. `null` = reviewed and confirmed correctly blank |

### Why provenance is stripped
The `_work/<key>_<period>_values.json` run output also carries `resolve`,
`source` and `adjustments` for every row. Those describe *how* a value was found,
and they change for harmless reasons (a different page index, a renamed
resolver step). The golden file keeps only *what* the answer is, so the check
fails on wrong numbers rather than on refactors.

### Why order matters
A label can appear more than once in a section (the same caption used for two
occurrences). Rows are matched by `(label, section)` and then **by position**
within that key, so the n-th golden occurrence is compared with the n-th run
occurrence.

## 4. Lifecycle

```
 inbox/<Company>/*.pdf
          │  finscan company <name>
          ▼
 _work/<key>_<period>_values.json  +  _report.md
          │  human review: report checked line by line against the filing
          ▼
 python scripts/golden_set.py build <key>
          ▼
 golden/<key>_<period>.json        (committed to git)
          │
          │  ...later: code / mapping / model change...
          │  finscan company <name>  (refresh _work/)
          ▼
 python scripts/golden_set.py check [<key> ...]
          ▼
 ok  |  DRIFT / MISSING / EXTRA  (non-zero exit)
```

Rules:
1. **Build only from a reviewed run.** `build` copies whatever is in `_work/`,
   so it must be run only after the `_report.md` has been checked against the
   filing.
2. **Never hand-edit a golden file.** If the truth changes (a mapping
   correction, a restated filing), fix the cause, re-run, review again and
   rebuild. Keeping every change tied to a reviewed run keeps the set honest.
3. **Commit golden changes on their own** so a reviewer can see exactly which
   reference values moved and why.

## 5. The check

`python scripts/golden_set.py check [company ...]` reads each golden file and
the matching `_work/<key>_<period>_values.json`, then reports:

| Result | Condition |
|---|---|
| `ok` | Every golden row has a matching run row within tolerance, and the run has no extra rows |
| `DRIFT` | Values differ beyond tolerance, or one is `null` and the other is not |
| `MISSING label` | A golden row has no counterpart in the run (mapping row removed or renamed) |
| `EXTRA label` | The run has a row golden does not (a new mapping row; rebuild after review) |
| `MISSING run` | No `_work` output for that period; run FinScan first |

**Tolerance:** two values match when they are within **0.01 absolute** or
**0.5% relative**, the same tolerance `finscan.validate` uses. Filings round
their figures, and a rounding difference is not a regression.

The process exits with a non-zero code on any mismatch, so it can gate a pipeline.

## 6. Current coverage

26 golden files, 1,338 reference rows, 476 of them (36%) confirmed blanks.

| Company | Period | Rows | Blank | IS | BS | CF | Other |
|---|---|---:|---:|---:|---:|---:|---:|
| almarai | 2026-09-30 | 38 | 5 | 13 | 11 | 12 | 2 |
| arcelik | 2026-06-30 | 71 | 19 | 20 | 6 | 16 | 29 |
| axiata_issue | 2026-06-30 | 51 | 25 | 22 | 16 | 13 | 0 |
| bharti_airtel | 2026-06-30 | 41 | 31 | 16 | 11 | 9 | 5 |
| ccu | 2026-06-30 | 41 | 5 | 17 | 11 | 13 | 0 |
| cocacola | 2026-06-30 | 70 | 7 | 20 | 10 | 38 | 2 |
| ctpgroup | 2026-06-30 | 44 | 3 | 23 | 8 | 10 | 3 |
| elpuerto | 2026-06-30 | 59 | 4 | 12 | 30 | 17 | 0 |
| entel | 2026-06-30 | 45 | 8 | 16 | 15 | 14 | 0 |
| falabella | 2026-06-30 | 104 | 16 | 32 | 22 | 48 | 2 |
| fibra | 2026-06-30 | 59 | 18 | 33 | 15 | 11 | 0 |
| funo | 2026-06-30 | 53 | 12 | 29 | 9 | 13 | 2 |
| globaltelecom | 2026-06-30 | 42 | 36 | 18 | 13 | 9 | 2 |
| gruma | 2026-06-30 | 72 | 64 | 13 | 11 | 48 | 0 |
| grupotelevisa | 2026-06-30 | 39 | 6 | 13 | 12 | 12 | 2 |
| jd | 2026-06-30 | 38 | 4 | 16 | 13 | 8 | 1 |
| mbrf | 2026-06-30 | 56 | 10 | 12 | 28 | 15 | 1 |
| meituan | 2026-06-30 | 33 | 10 | 16 | 14 | 3 | 0 |
| quiport | 2026-06-30 | 32 | 3 | 20 | 0 | 6 | 6 |
| skhynix | 2026-06-30 | 40 | 30 | 12 | 15 | 13 | 0 |
| tencent | 2025-09-30 | 36 | 13 | 12 | 14 | 10 | 0 |
| turkcell | 2026-06-30 | 115 | 66 | 26 | 16 | 73 | 0 |
| ulker | 2026-06-30 | 45 | 17 | 18 | 13 | 14 | 0 |
| upl | 2026-06-30 | 46 | 33 | 14 | 19 | 13 | 0 |
| vesta | 2026-06-30 | 41 | 20 | 17 | 10 | 13 | 1 |
| xiomi | 2026-06-30 | 27 | 11 | 13 | 11 | 3 | 0 |

IS = income statement, BS = balance sheet, CF = cash flow.

## 7. Design decisions

| Decision | Alternatives considered | Rationale |
|---|---|---|
| Plain JSON per quarter, committed to git | Database; one large file | Easy to read in reviews; a change touches only one company's file |
| Built from run output, not typed by hand | Analysts type expected values | No transcription errors; labels and sections match the mapping exactly |
| Match on `(label, section)` + position | Match on row number | Row numbers move when a model gains rows; the mapping is keyed by label and section, not row |
| Confirmed blanks kept as `null` | Omit blank rows | Catches a row that starts picking up a wrong value |
| Same tolerance as `finscan.validate` | Exact match | Filings round; exact matching would flag rounding noise as regressions |
| Check reads `_work/` rather than running FinScan | Check runs the pipeline itself | Keeps the script simple and offline; the slow model-dependent run is a separate, explicit step |

## 8. Known limitations and risks

1. **Golden only proves consistency, not correctness.** An error the reviewer
   missed becomes part of the reference values. Mitigation: careful review of
   `_report.md` before `build`; periodic back-tests against the analyst's own
   columns in the model.
2. **Blank-heavy companies assert little.** gruma (64/72), globaltelecom
   (36/42), skhynix (30/40), bharti_airtel (31/41) and turkcell (66/115) are
   mostly `null`. A pass there mainly shows that nothing new appeared. Review
   whether those blanks are real (the line is not in the filing) or mapping gaps.
3. **One period per company.** Each company has a single quarter, so
   period-specific logic (de-cumulating year-to-date figures, Q4 = FY − 9M,
   annual-only items) is only exercised for that quarter type.
4. **Not wired into CI.** Neither `pytest` nor any workflow runs `check`; it is
   a manual step. A stale `_work/` file will also "pass" if FinScan was not
   re-run.
5. **Run output is model-dependent.** The default reader is a vision LLM, so
   a fresh run can vary. Text-layer verification limits this, but a
   `DRIFT` can come from model variation as well as from a code change. Read
   caches (`_work/pdfcache`) make re-runs repeatable until the cache is
   bypassed (`--no-cache`) or `PARSER_VERSION` is bumped.
6. **`build` has no guard.** Nothing records that a run was reviewed; `build`
   on an unreviewed run quietly saves wrong values as the reference.

## 9. Recommended next steps

| # | Change | Benefit |
|---|---|---|
| 1 | Add a CI job: `finscan companies --dry-run` (using cached reads) then `golden_set.py check` | Regressions blocked before merge |
| 2 | Record `reviewed_by` / `reviewed_on` / `run_sha` in each golden file; have `build` require them | Traceable references; no accidental builds from unreviewed runs |
| 3 | Add a second period per company, including a Q4 / fiscal-year-end | Covers de-cumulation and annual-only logic |
| 4 | Audit blank-heavy companies; turn real gaps into mapping fixes | Golden values that test more |
| 5 | Report per-company accuracy and blank rate over time | Visible quality trend for stakeholders |

## 10. Operating guide

```bash
# After reviewing a new run and confirming it is correct:
finscan company <name>
#   ...review _work/<key>_<period>_report.md against the filing...
python scripts/golden_set.py build <key>
git add golden/<key>_<period>.json && git commit -m "golden: <key> <period>"

# Before merging a parser / mapping / model change:
finscan companies               # refresh _work/
python scripts/golden_set.py check
```

Handling results:
- **DRIFT** → open the company's `_report.md`. If the new value is right (the
  old reference was wrong), fix, re-review and `build`. Otherwise fix the regression.
- **MISSING / EXTRA label** → expected after a mapping change; review the
  affected rows, then `build`.
- **MISSING run** → run `finscan company <name>` for that period.
