"""Command line entry point: finscan run | batch | inspect | profiles."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from finscan.config import settings
from finscan.excel.discovery import discover
from finscan.graph.build import run as run_graph
from finscan.profiles import ProfileStore


def _emit(state: dict, report_path: Path | None) -> int:
    print(state.get("report", ""))
    if report_path:
        report_path.write_text(state.get("report", ""), encoding="utf-8")
        print(f"\nReport saved to {report_path}")
    return {"ok": 0, "awaiting_confirmation": 3}.get(state.get("status"), 2)


def _add_run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", help="Output workbook (default: <name>_updated.xlsx)")
    p.add_argument("--company", help="Force the profile key instead of reading it from the PDF")
    p.add_argument("--sheets", nargs="*", help="Restrict writing to these sheet names")
    p.add_argument("--period", help="Override the column header text")
    p.add_argument("--profiles", help="Profile store directory")
    p.add_argument("--report", help="Write the markdown review report here")
    p.add_argument("--yes", action="store_true",
                   help="Compatibility flag; confirmation is already skipped by default")
    p.add_argument("--require-confirmation", action="store_true",
                   help="Require one-off profile confirmation before writing (legacy behavior)")
    p.add_argument("--dry-run", action="store_true", help="Extract and map without writing")
    p.add_argument("--allow-period-gap", action="store_true",
                   help="TESTING ONLY: write even when the filing period does not follow "
                        "the last column. Produces a knowingly wrong model.")
    p.add_argument("--no-llm-mapping", action="store_true",
                   help="Alias+fuzzy caption matching only")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="finscan", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="Process one PDF into one workbook.")
    r.add_argument("pdf")
    r.add_argument("excel")
    _add_run_args(r)

    b = sub.add_parser("batch", help="Process a folder of PDFs into one workbook, oldest first.")
    b.add_argument("pdf_dir")
    b.add_argument("excel")
    _add_run_args(b)

    i = sub.add_parser("inspect", help="Show what FinScan sees in a workbook. Writes nothing.")
    i.add_argument("excel")
    i.add_argument("--json", action="store_true")

    ip = sub.add_parser("inspect-pdf",
                        help="Show which PDF pages hold the statements. No AI call.")
    ip.add_argument("pdf")
    ip.add_argument("--show", action="store_true", help="Print the text sent to the model")

    pr = sub.add_parser("profiles", help="Manage stored company layout profiles.")
    pr.add_argument("action", choices=["list", "show", "confirm", "map", "compose", "unmap"])
    pr.add_argument("key", nargs="?")
    pr.add_argument("--profiles", help="Profile store directory")
    pr.add_argument("--by", default="cli")
    pr.add_argument("--sheets", nargs="*", help="For 'confirm': the exact sheets to enable")
    pr.add_argument("--label", help="For 'map': the workbook caption")
    pr.add_argument("--field", help="For 'map'/'compose': the canonical field")
    pr.add_argument("--of", nargs="*",
                    help="For 'compose': the filing fields this row actually sums")
    pr.add_argument("--sheet", help="For 'unmap': the sheet name")
    pr.add_argument("--row", type=int, help="For 'unmap': the row to stop matching")

    a = p.parse_args(argv)

    if a.cmd == "inspect":
        return _inspect(a)
    if a.cmd == "inspect-pdf":
        return _inspect_pdf(a)
    if a.cmd == "profiles":
        return _profiles(a)

    common = dict(
        company=a.company, sheets=a.sheets, period_label=a.period,
        profile_store=a.profiles, require_confirmation=not a.yes,
        use_llm_mapping=not a.no_llm_mapping,
        allow_period_gap=a.allow_period_gap,
    )

    if not a.require_confirmation:
        common["require_confirmation"] = False

    if a.cmd == "run":
        state = run_graph(a.pdf, a.excel, output_path=a.out, dry_run=a.dry_run, **common)
        return _emit(state, Path(a.report) if a.report else None)

    pdfs = sorted(Path(a.pdf_dir).glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs in {a.pdf_dir}", file=sys.stderr)
        return 1
    current = a.excel
    out = a.out or str(Path(a.excel).with_name(Path(a.excel).stem + "_updated.xlsx"))
    worst = 0
    for pdf in pdfs:
        print(f"\n=== {pdf.name} ===")
        state = run_graph(str(pdf), current, output_path=out, **common)
        worst = max(worst, _emit(state, None))
        if state.get("write_result"):
            current = out          # columns accumulate across the batch
    return worst


def _inspect(a) -> int:
    plan = discover(a.excel)
    if a.json:
        print(json.dumps({
            "fingerprint": plan.fingerprint,
            "colors_found": plan.colors_found,
            "sheets": [
                {"sheet": s.sheet, "in_scope": s.in_scope, "reason": s.reason,
                 "units": s.units, "write": s.write_col_letter, "mode": s.write_mode,
                 "input_rows": len(s.writable_rows), "formula_rows": len(s.formula_rows)}
                for s in plan.sheets
            ],
        }, indent=2))
        return 0

    print(f"{a.excel}\nfingerprint {plan.fingerprint} · "
          f"blue/black convention: {'found' if plan.colors_found else 'NOT found'}\n")
    for s in plan.sheets:
        print(f"  {s.summary()}")
        for rp in s.rows:
            if rp.label_only and rp.field is None:
                kind = "input " if rp.writable else "locked "
                print(f"      r{rp.row:<4} {kind} {rp.label[:34]:<34} -> (label match)")
            elif rp.field:
                kind = "formula" if rp.carries_formula else ("input " if rp.writable else "locked ")
                print(f"      r{rp.row:<4} {kind} {rp.label[:34]:<34} -> {rp.field}")
    return 0


def _inspect_pdf(a) -> int:
    """Which pages will the model actually be pointed at, and is there text there?"""
    from finscan.extract.pdf_reader import read_pdf, statement_score

    doc = read_pdf(a.pdf)
    wanted = set(doc.statement_pages())
    print(f"{a.pdf}\n{len(doc.pages)} page(s); statements detected on "
          f"{sorted(wanted)}\n")
    print(f"{'page':>5} {'score':>7} {'chars':>7} {'ocr':>5}  role      first line")
    for p in doc.pages:
        role = "STATEMENT" if p.page in wanted else "context"
        head = (p.text.strip().splitlines() or ["(no text)"])[0][:44]
        print(f"{p.page:>5} {statement_score(p):>7.2f} {len(p.text):>7} "
              f"{'yes' if p.ocr_used else '-':>5}  {role:<9} {head}")

    empty = [p.page for p in doc.pages if not p.text.strip()]
    if empty:
        print(f"\nPages with no text layer: {empty}. These need OCR "
              f"(brew install tesseract poppler).")
    if a.show:
        print("\n" + "=" * 70)
        print(doc.as_prompt_text())
    return 0


def _profiles(a) -> int:
    store = ProfileStore(a.profiles or settings.finscan_profile_store)

    if a.action == "list":
        keys = store.list()
        if not keys:
            print("No profiles stored yet.")
        for k in keys:
            pr = store.get(k)
            print(f"{k:<28} {'confirmed' if pr.confirmed else 'UNCONFIRMED':<12} "
                  f"fp {pr.fingerprint}  sheets: {', '.join(sorted(pr.enabled_sheets())) or '-'}")
        return 0

    if not a.key:
        print("A profile key is required.", file=sys.stderr)
        return 1
    pr = store.get(a.key)
    if pr is None:
        print(f"No profile '{a.key}'.", file=sys.stderr)
        return 1

    if a.action == "show":
        print(pr.to_json())
        return 0

    if a.action == "map":
        if not (a.label and a.field):
            print("--label and --field are both required.", file=sys.stderr)
            return 1
        pr.label_overrides[a.label] = a.field
        store.save(pr)
        print(f"'{a.label}' -> {a.field} saved for {a.key}.")
        return 0

    if a.action == "compose":
        if not (a.field and a.of):
            print("--field and --of are both required.", file=sys.stderr)
            return 1
        pr.field_composites[a.field] = list(a.of)
        store.save(pr)
        print(f"{a.key}: {a.field} = {' + '.join(a.of)}")
        return 0

    if a.action == "unmap":
        if not (a.sheet and a.row):
            print("--sheet and --row are both required.", file=sys.stderr)
            return 1
        rows = pr.unmapped_rows.setdefault(a.sheet, [])
        if a.row not in rows:
            rows.append(a.row)
        store.save(pr)
        print(f"{a.key}: {a.sheet} row {a.row} will no longer be matched.")
        return 0

    if a.sheets is not None:
        wanted = set(a.sheets)
        for s in pr.sheets:
            s.enabled = s.sheet in wanted
    pr.confirm(by=a.by)
    store.save(pr)
    print(f"{a.key} confirmed. Sheets in scope: {', '.join(sorted(pr.enabled_sheets())) or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
