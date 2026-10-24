"""finscan — command line. Two commands:

    python -m finscan.cli company <name>     # one inbox/<name>/ folder
    python -m finscan.cli companies          # every folder under inbox/

Each company run does the same four things, in order:

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
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from finscan.pdf.read import read_pdf

DEFAULT_CACHE = "_work/pdfcache"

#: The sheet learned for a company that has no mapping yet.
DEFAULT_SHEET = "Model"

#: The reader a company gets unless its mapping names another. The vision model
#: reads layouts the rules keep missing; `"reader": "rules"` opts a company back.
DEFAULT_READER = "llm"


def _company_reader(paths) -> str:
    """The company's chosen reader: `"reader"` in its mapping, else the default."""
    from finscan.mapping.schema import Mapping

    if not paths.mapping.exists():
        return DEFAULT_READER
    return Mapping.load(paths.mapping).reader or DEFAULT_READER


def _read_filings(paths, args) -> list:
    """Stage 1 for every PDF in the folder. Each pdf.json is rewritten."""
    reader = _company_reader(paths)
    docs = []
    for pdf_path, pdf_json in zip(paths.pdfs or [paths.pdf], paths.pdf_jsons):
        doc = read_pdf(pdf_path,
                       cache_dir=None if args.no_cache else DEFAULT_CACHE,
                       use_cache=not args.no_cache,
                       reader=reader)
        pdf_json.write_text(json.dumps(doc.to_dict(), indent=2, ensure_ascii=False),
                            encoding="utf-8")
        docs.append(doc)
    return docs


def _sync_mapping(paths, doc, sheet: str | None, dry_run: bool = False) -> str:
    """Create the mapping, or append the workbook rows it does not cover yet.

    A dry run still creates a missing mapping (there is nothing to lose and apply
    needs one) but only reports rows it would append to an existing one.
    Returns a one-line account of what happened to the file, for the summary.
    """
    from finscan.mapping.propose import propose_mapping
    from finscan.mapping.schema import Mapping
    from finscan.model.discover import discover_sheet
    from finscan.model.learn import propose as propose_model

    if not paths.mapping.exists():
        layout = discover_sheet(str(paths.excel), sheet or DEFAULT_SHEET)
        proposal, _ = propose_mapping(layout, paths.key, doc)
        proposal.save(paths.mapping)
        return f"created ({len(proposal.rows)} rows) — review it, then git add"

    mapping = Mapping.load(paths.mapping)
    layout = discover_sheet(str(paths.excel), mapping.sheet)
    known = set(mapping.by_key())

    # Cheap, deterministic check first: which rows would a proposal make inputs
    # that the mapping lacks? Headings such as "As % of sales unless stated" are
    # writable cells but never inputs, so they must not trigger a model call on
    # every run only to be discarded.
    candidates = propose_model(layout, paths.key)
    if not any(spec.kind == "input" and spec.key.normalized() not in known
               for spec in candidates.rows):
        return "unchanged"

    # Proposed against the whole sheet, so each new row's occurrence number is the
    # one the sheet gives it; only the rows the mapping lacks are kept.
    proposal, _ = propose_mapping(layout, paths.key, doc)
    added = [row for row in proposal.rows if row.key not in known]
    if not added:
        return "unchanged"
    if dry_run:
        return (f"{len(added)} new row(s) found, not appended (dry run): "
                + ", ".join(repr(r.label) for r in added[:5])
                + ("…" if len(added) > 5 else ""))
    stamp = f"added {date.today().isoformat()}: new in the workbook, review"
    for row in added:
        row.note = f"{stamp} · {row.note}" if row.note else stamp
    Mapping.append_rows(paths.mapping, added)
    return (f"{len(added)} new row(s) appended — review: "
            + ", ".join(repr(r.label) for r in added[:5])
            + ("…" if len(added) > 5 else ""))


def _run_company(key: str, args) -> int:
    """The whole quarter for one company: read, sync the mapping, apply."""
    from finscan.apply import ApplyResult
    from finscan.apply import run as apply_run
    from finscan.apply import save_artifacts
    from finscan.company import CompanyError, resolve
    from finscan.schema import Issue, PdfDoc

    try:
        paths = resolve(key)
    except CompanyError as error:
        print(f"{error}", file=sys.stderr)
        return 1

    # 1. read
    for pdf_path in paths.pdfs or [paths.pdf]:
        if not Path(pdf_path).exists():
            print(f"No such file: {pdf_path}", file=sys.stderr)
            return 1
    docs = _read_filings(paths, args)
    doc = PdfDoc.merge(docs) if len(docs) > 1 else docs[0]

    if not doc.statements:
        names = ", ".join(p.name for p in (paths.pdfs or [paths.pdf]))
        refused = ApplyResult(company=paths.key, refused=(
            "no financial statement was found in the filing"))
        refused.issues.append(Issue(
            code="no_statements", severity="error",
            message=f"Stage 1 identified no statement in {names}, so there is nothing "
                    f"to read" + ("" if paths.mapping.exists() else
                                  " and no mapping was proposed") + "."))
        _print_run_summary(paths, refused, [], mapping_status="not checked")
        return 1

    # 2. mapping — created if missing, appended only for rows it lacks.
    mapping_status = _sync_mapping(paths, doc, args.sheet, args.dry_run)

    # 3. apply
    result = apply_run(",".join(str(p) for p in paths.pdf_jsons), paths.mapping,
                       paths.excel, paths.output, args.period_end, args.dry_run)

    # 4. derived files, rewritten every run.
    artifacts = []
    if not args.dry_run:
        if result.model is not None:
            artifacts.append(result.model.save(paths.map))
        artifacts.extend(save_artifacts(result))

    _print_run_summary(paths, result, artifacts, mapping_status)
    return result.exit_code


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
        try:
            code = _run_company(key, args)
        except Exception as error:
            print(f"Unexpected failure for {key}: {type(error).__name__}: {error}",
                  file=sys.stderr)
            code = 1
        outcomes.append((key, code))

    written = [key for key, code in outcomes if code == 0]
    failed = [(key, code) for key, code in outcomes if code != 0]
    print(f"\nBatch complete: {len(written)} succeeded, {len(failed)} failed.")
    if failed:
        print("Failed: " + ", ".join(f"{key} (exit {code})" for key, code in failed))
        return 1
    return 0


def _print_run_summary(paths, result, artifacts, mapping_status: str) -> None:
    """Where the column went, what happened to the mapping, and the audit trail."""
    color = sys.stdout.isatty()

    def paint(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    def short(path) -> str:
        try:
            return str(Path(path).resolve().relative_to(Path.cwd().resolve()))
        except ValueError:
            return str(path)

    ending = result.period_end
    try:
        ending = date.fromisoformat(result.period_end).strftime("%-d %b %Y")
    except ValueError:
        pass

    print()
    print(f"  {paint(paths.key.upper(), '1')}"
          + (f"  ·  period ending {ending}" if ending else ""))
    print(f"  {paint('─' * 52, '2')}")

    if result.refused or not result.write:
        reason = result.refused or "nothing was written"
        print(f"  {paint('✗ Not written', '1;31')}  {reason}")
        for issue in [i for i in result.issues if i.severity == 'error'][:3]:
            print(f"                 {issue.message}")
    else:
        w = result.write
        print(f"  {paint('✓ Written', '1;32')}   {short(w.output_path)}")
        print(f"              {paint(f'{w.sheet}!{w.column}', '1')}  ·  "
              f"{w.values_written} values  ·  {w.formulas_copied} formulas  ·  "
              f"{w.blanks_annotated} blank")

    width = 11 + (8 if color else 0)
    print(f"  {paint('Mapping', '2'):<{width}} {short(paths.mapping)}: {mapping_status}")
    label = "Derived"
    for path in artifacts:
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
    _add_run_args(co)

    cos = sub.add_parser("companies", help="Run the quarter for every inbox company.")
    _add_run_args(cos)

    args = parser.parse_args(argv)
    if args.cmd == "company":
        return _run_company(args.key, args)
    return _run_companies(args)


if __name__ == "__main__":
    raise SystemExit(main())
