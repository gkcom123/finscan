"""finscan — command line. Two commands:

    python -m finscan.cli company <name>     # one inbox/<name>/ folder
    python -m finscan.cli companies          # every folder under inbox/

Each company run does the same four things, in order (see finscan/runner.py):

1. read     every PDF in the folder -> <pdf>.pdf.json, rewritten every run. The
            read itself is cached in _work/pdfcache by the PDF's SHA-256, so an
            unchanged PDF is not re-read (a vision read costs money and can return
            different digits); a changed PDF has a new hash and is read afresh.
2. mapping  mappings/<key>.json is the one human file, git-tracked.
            * missing: proposed from the workbook and the filing, then written.
            * present: the workbook is checked for writable rows the mapping does
              not cover. Those rows alone are proposed and appended, marked for
              review. With none, the file is not touched.
3. apply    resolve the filing against the mapping and write the new column to
            inbox/<key>_v2_output.<ext>.
4. derived  models/<key>_model.json (the mapping compiled against this run's
            workbook layout) and _work/<key>_<period>_values.json + _report.md are
            rewritten every run. Nothing reads them back; they are for audit.

`company` can also save its outcome as JSON: `--result-json <path>`, or, when the
environment variable FINSCAN_JOB_ID is set (a run started by the service),
`_work/jobs/<FINSCAN_JOB_ID>/result.json`.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from finscan.runner import DEFAULT_SHEET, RunResult, run_company

#: Set by the service on a run it started; the run then saves its outcome there.
JOB_ID_ENV = "FINSCAN_JOB_ID"


def job_result_path(root: Path, job_id: str) -> Path:
    """Where a service-started run saves its outcome."""
    return root / "_work" / "jobs" / job_id / "result.json"


def _run_company(key: str, args) -> int:
    """One company: run it, print the summary, save the outcome when asked."""
    outcome = run_company(key, dry_run=args.dry_run, period_end=args.period_end,
                          no_cache=args.no_cache, sheet=args.sheet)
    _print_run_summary(outcome)
    targets = [Path(p) for p in [getattr(args, "result_json", None)] if p]
    if job_id := os.environ.get(JOB_ID_ENV):
        from finscan.company import project_root
        targets.append(job_result_path(project_root(), job_id))
    for target in targets:
        outcome.save(target)
    return outcome.exit_code


def _run_companies(args) -> int:
    """Every inbox company, without stopping at the first failure."""
    from finscan.company import CompanyError, company_keys

    try:
        keys = company_keys()
    except CompanyError as error:
        print(error, file=sys.stderr)
        return 1

    outcomes: list[tuple[str, int]] = []
    for key in keys:
        print(f"\n{'=' * 12} {key} {'=' * 12}")
        outcome = run_company(key, dry_run=args.dry_run, period_end=args.period_end,
                              no_cache=args.no_cache, sheet=args.sheet)
        _print_run_summary(outcome)
        outcomes.append((key, outcome.exit_code))

    written = [key for key, code in outcomes if code == 0]
    failed = [(key, code) for key, code in outcomes if code != 0]
    print(f"\nBatch complete: {len(written)} succeeded, {len(failed)} failed.")
    if failed:
        print("Failed: " + ", ".join(f"{key} (exit {code})" for key, code in failed))
        return 1
    return 0


def _print_run_summary(outcome: RunResult) -> None:
    """Where the column went, what happened to the mapping, and the audit trail."""
    color = sys.stdout.isatty()

    def paint(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    def short(path) -> str:
        try:
            return str(Path(path).resolve().relative_to(Path.cwd().resolve()))
        except ValueError:
            return str(path)

    ending = outcome.period_end
    try:
        ending = date.fromisoformat(outcome.period_end).strftime("%-d %b %Y")
    except ValueError:
        pass

    print()
    print(f"  {paint(outcome.company.upper(), '1')}"
          + (f"  ·  period ending {ending}" if ending else ""))
    print(f"  {paint('─' * 52, '2')}")

    if outcome.status == "written":
        print(f"  {paint('✓ Written', '1;32')}   {short(outcome.output)}")
        print(f"              {paint(f'{outcome.sheet}!{outcome.column}', '1')}  ·  "
              f"{outcome.values_written} values  ·  {outcome.formulas_copied} formulas  ·  "
              f"{outcome.blank_count} blank")
    elif outcome.status == "dry_run":
        print(f"  {paint('○ Dry run', '1;33')}   nothing written  ·  "
              f"{outcome.values_resolved} values resolved  ·  {outcome.blank_count} blank")
    else:
        reason = outcome.refused or "nothing was written"
        print(f"  {paint('✗ Not written', '1;31')}  {reason}")
        for message in outcome.errors[:3]:
            print(f"                 {message.splitlines()[0] if message else message}")

    width = 11 + (8 if color else 0)
    if outcome.mapping:
        print(f"  {paint('Mapping', '2'):<{width}} {short(outcome.mapping)}: "
              f"{outcome.mapping_status}")
    label = "Derived"
    for path in outcome.artifacts:
        print(f"  {paint(label, '2'):<{width}} {short(path)}")
        label = ""
    print()


def _add_run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--sheet", default=None,
                   help=f"Workbook sheet for a company with no mapping yet "
                        f"(default: {DEFAULT_SHEET!r}); afterwards the mapping's")
    p.add_argument("--period-end", help="ISO date of the period being written")
    p.add_argument("--no-cache", action="store_true",
                   help="Re-read every PDF even if unchanged (re-calls the vision model)")
    p.add_argument("--dry-run", action="store_true",
                   help="Resolve and validate, write no workbook or derived files")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="finscan", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    co = sub.add_parser("company", help="Run the quarter for one inbox company.")
    co.add_argument("key", help="Company folder under inbox/ (case-insensitive)")
    co.add_argument("--result-json", help="Also save the outcome as JSON at this path")
    _add_run_args(co)

    cos = sub.add_parser("companies", help="Run the quarter for every inbox company.")
    _add_run_args(cos)

    args = parser.parse_args(argv)
    if args.cmd == "company":
        return _run_company(args.key, args)
    return _run_companies(args)


if __name__ == "__main__":
    raise SystemExit(main())
