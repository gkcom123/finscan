# Golden set

Reference values for evaluation, one file per reviewed quarter:
`golden/<key>_<period>.json`. Each holds the company, the period, the units,
and every mapped row's `label` / `section` / `value` — the provenance
(`resolve`, `source`, `adjustments`) is stripped out because that is
run-specific detail, not the truth being asserted. `value: null` means the
row was reviewed and confirmed to be correctly left blank for that quarter.

Built from `_work/<key>_<period>_values.json` runs that have already been
reviewed by hand (the `_report.md` beside each one checked against the
filing). Never hand-edit a golden file; regenerate it from a freshly reviewed
run instead.

## Usage

```bash
# After reviewing a new run and confirming it's correct, add/refresh its
# golden file:
python scripts/golden_set.py build <company>

# Regression-check the current _work/ output for every (or selected)
# company against its golden file:
python scripts/golden_set.py check [company ...]
```

`check` re-reads whatever is currently in `_work/<key>_<period>_values.json`
— run `finscan company <name>` (or `finscan companies`) first to refresh it —
and reports any label that drifted from golden beyond the tolerance used in
`finscan.validate` (0.5% relative, or 0.01 absolute near zero), or that is
newly missing or added. Exit code is non-zero if anything mismatched.
