"""finscan2 — command line for the v2 pipeline.

One command per quarter:

    python -m finscan2.cli company almarai run      # read every PDF, learn the
                                                    # mapping only if missing, apply

which is these three, still available one at a time:

    python -m finscan2.cli company almarai read     # PDF -> pdf.json
    python -m finscan2.cli company almarai learn    # -> mappings/almarai.json (review it)
    python -m finscan2.cli company almarai apply    # resolve + write, one step

`apply` joins what used to be `match` and `write`. They were separate, with
values.json between them, and nothing checked its age — so editing the mapping and
running `write` produced a workbook from a stale resolution, silently. Now nothing
persists between resolving and writing; values.json is an audit artifact only.

    mappings/<company>.json         the human file. git-tracked. one row per decision.
    models/<company>_model.json     the derived layout. regenerated freely.

Migration from the old single map:

    python -m finscan2.cli mapping-init models/almarai_model.json

The older commands (match, write, bind, check, confirm) still work during the
transition and take explicit paths.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from finscan2.pdf.read import read_pdf
from finscan2.pdf.statements import column_alignment

DEFAULT_CACHE = "_work/pdfcache"

#: The sheet learned for a company that has no mapping yet.
DEFAULT_SHEET = "Model"


def _severity_mark(severity: str) -> str:
    return {"error": "x", "warning": "!", "info": "+"}.get(severity, "-")


def _print_summary(doc, verbose: bool = False) -> None:
    print(f"{doc.path}")
    print(f"  sha256   {doc.sha256[:16]}…")
    print(f"  pages    {len(doc.pages)}")
    print(f"  units    {doc.units or '(unknown)'}    currency {doc.currency or '(unknown)'}")
    if doc.ocr_pages:
        print(f"  ocr      {doc.ocr_engine} on page(s) {doc.ocr_pages}")

    print(f"\n  {'page':>5} {'source':<14} {'chars':>7}  statement")
    by_page = {s.page: s for s in doc.statements}
    for page in doc.pages:
        statement = by_page.get(page.page)
        label = ""
        if statement:
            align = column_alignment(statement)
            label = (f"{statement.kind}  "
                     f"{align['columns']} col(s), "
                     f"{align['aligned']}/{align['rows']} rows aligned")
        print(f"  {page.page:>5} {page.source:<14} {page.chars:>7}  {label}")

    for statement in doc.statements:
        print(f"\n  --- page {statement.page}: {statement.kind}")
        print(f"      {statement.title[:78]}")
        if statement.heading:
            print(f"      {statement.heading[:78]}")
        for column in statement.columns:
            span = (f"{column.months} month(s)" if column.months
                    else ("point in time" if column.kind == "point_in_time" else "?"))
            print(f"      col {column.index}: {column.header[:38]:<38} {span:<16} end {column.end}")
        if verbose:
            for row in statement.rows[:40]:
                print(f"        {row.caption[:44]:<44} {row.values}")

    if doc.issues:
        print()
        for issue in doc.issues:
            page = f" (page {issue.page})" if issue.page else ""
            print(f"  [{_severity_mark(issue.severity)}] {issue.code}{page}: {issue.message}")


def _learn(args) -> int:
    """Propose the human mapping, and refresh the derived layout beside it."""
    from finscan2.mapping.propose import changes, propose_mapping
    from finscan2.mapping.schema import Mapping
    from finscan2.model.learn import learn

    mapping_path = (Path(args.mapping) if getattr(args, "mapping", None)
                    else Path("mappings") / f"{args.company}.json")
    overwrite = getattr(args, "overwrite", False)

    # The sheet is a human decision recorded in the mapping. Re-learning without
    # naming one must keep it: a hard-coded "Model" default silently moved Gruma
    # from its live Model_USD sheet onto a Pesos sheet that stops in 2021.
    if not args.sheet:
        args.sheet = (Mapping.load(mapping_path).sheet if mapping_path.exists()
                      else DEFAULT_SHEET)

    pdf_json = getattr(args, "pdf_json", None)
    model, layout, counts = learn(args.excel, args.sheet, args.company, pdf_json,
                                  use_llm=not getattr(args, "no_llm", False))

    # The derived layout is always rewritten: nothing human lives in it.
    out = Path(args.out) if args.out else Path("models") / f"{args.company}_model.json"
    model.save(out)

    doc = None
    if pdf_json:
        from finscan2.schema import PdfDoc
        doc = PdfDoc.from_dict(json.loads(Path(pdf_json).read_text(encoding="utf-8")))
    proposal, _ = propose_mapping(layout, args.company, doc,
                                  use_llm=not getattr(args, "no_llm", False))

    if mapping_path.exists() and not overwrite:
        # Never overwritten. The short list of CHANGED decisions is the review.
        beside = mapping_path.with_suffix(".proposed.json")
        proposal.save(beside)
        differences = changes(Mapping.load(mapping_path), proposal)
        print(f"{mapping_path} exists and was NOT changed.")
        print(f"  proposal: {beside}")
        if not differences:
            print("  no decision differs from the mapping you already have.")
        else:
            print(f"  {len(differences)} decision(s) differ:")
            for change in differences:
                print(f"    [{change.kind:<7}] {change.label[:36]:<36} "
                      f"{change.was!r} -> {change.now!r}")
        print()
    else:
        if mapping_path.exists():
            print(f"--overwrite: replacing {mapping_path}; its hand edits are "
                  f"recoverable only from git.")
        proposal.save(mapping_path)
        needs = [r for r in proposal.rows if r.pdf is None and r.value is None]
        print(f"Wrote {mapping_path}  ({len(proposal.rows)} row(s), "
              f"{len(needs)} needing a caption)")
        print(f"  review it, then: git add {mapping_path}\n")

    print(f"{args.excel}  [{args.sheet}]")
    print(f"  units {model.units} · cadence {model.cadence_months}m · "
          f"fingerprint {model.fingerprint}")
    print(f"  reference col {model.reference_col} · write col {model.write_col} "
          f"({model.write_mode})")
    if not layout.colors_found:
        print("  ! no blue/black convention found — every row needs review")

    kinds: dict[str, int] = {}
    for row in model.rows:
        kinds[row.kind] = kinds.get(row.kind, 0) + 1
    print(f"  rows {len(model.rows)}: " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())))

    if counts:
        print(f"  matched against {pdf_json}:")
        print(f"    {counts.get('exact', 0)} exact · {counts.get('derived', 0)} derived · "
              f"{counts.get('model', 0)} chosen by a model · "
              f"{counts.get('unmatched', 0)} unmatched")
    elif not pdf_json:
        print("  ! no pdf.json given, so rows carry the WORKBOOK caption to search for")

    review = [r for r in model.inputs if r.review]
    canonical = [r for r in model.inputs if (r.resolve or "").startswith("field:")]
    print(f"  inputs {len(model.inputs)}: {len(canonical)} mapped to a canonical field, "
          f"{len(review)} need review")

    if args.rows:
        print()
        for row in model.rows:
            if row.kind == "formula":
                continue
            print(f"  r{row.row_hint:<4} {row.kind:<6} {row.key.section:<17} "
                  f"{row.key.label[:30]:<30} {str(row.resolve or '')[:36]:<36} "
                  f"{row.basis or ''}")

    print(f"\nWrote the derived layout to {out}")
    return 0


def _check(args) -> int:
    from finscan2.model.load import load

    resolved = load(args.map, args.excel)
    model = resolved.model
    print(f"{args.map} against {args.excel}")
    print(f"  {len(resolved.bound)} of {len(model.rows)} mapped row(s) bound to the sheet")
    moved = [b for b in resolved.bound if b.moved_from]
    if moved:
        print(f"  {len(moved)} row(s) moved since the map was written")
    for issue in resolved.issues:
        print(f"  [{_severity_mark(issue.severity)}] {issue.code}: {issue.message}")
    if resolved.ok:
        print("  map resolves cleanly")
    return 2 if resolved.errors else 0


def _match(args) -> int:
    from finscan2.match.resolve import run

    values = run(args.pdf_json, args.map, args.excel, args.period_end)
    out = Path(args.out) if args.out else Path("_work") / f"{values.company}_values.json"
    values.save(out)

    print(f"{values.company} · period ending {values.period_end} · "
          f"write col {values.write_col} · units {values.units}")
    print(f"  {len(values.resolved)} resolved, {len(values.blank)} left blank\n")
    for value in values.values:
        if value.ok:
            note = ", ".join(a.kind for a in value.adjustments)
            print(f"  r{value.row:<4} {value.label[:30]:<30} {value.value:>18,.2f}  {note}")
        else:
            print(f"  r{value.row:<4} {value.label[:30]:<30} {'BLANK':>18}  {value.unresolved[:40]}")
    print()
    for issue in values.issues:
        print(f"  [{_severity_mark(issue.severity)}] {issue.code}: {issue.message}")
    print(f"\nWrote {out}")
    return 2 if values.errors else 0


def _write(args) -> int:
    from finscan2.write.column import run

    values, result = run(args.values_json, args.map, args.excel, args.out)
    print(f"{result.output_path}")
    print(f"  {result.sheet}!{result.column} · period ending {values.period_end}")
    print(f"  {result.values_written} value(s) written")
    print(f"  {result.formulas_copied} formula(s) copied")
    print(f"  {result.blanks_annotated} row(s) left blank with a reason")
    print(f"  {result.rows_skipped} row(s) skipped")
    for issue in result.issues:
        print(f"  [{_severity_mark(issue.severity)}] {issue.code}: {issue.message}")
    return 2 if result.errors else 0


def _apply(args) -> int:
    """Resolve and write in one step: no intermediate to go stale."""
    from finscan2.apply import run, save_artifacts

    result = run(args.pdf_json, args.mapping, args.excel, args.out,
                 args.period_end, args.dry_run)

    print(f"{result.company} · period ending {result.period_end or '(unknown)'}")
    if result.values:
        print(f"  {len(result.values.resolved)} resolved, "
              f"{len(result.values.blank)} left blank\n")
        for value in result.values.values:
            if value.ok:
                note = ", ".join(a.kind for a in value.adjustments)
                print(f"  r{value.row:<4} {value.label[:32]:<32} "
                      f"{value.value:>18,.2f}  {note}")
            else:
                print(f"  r{value.row:<4} {value.label[:32]:<32} {'BLANK':>18}  "
                      f"{(value.unresolved or '')[:44]}")
        print()

    for issue in list(result.issues) + list(
            result.values.issues if result.values else []):
        print(f"  [{_severity_mark(issue.severity)}] {issue.code}: {issue.message}")

    if result.refused:
        print(f"\nREFUSED: {result.refused}", file=sys.stderr)
        return 1

    if args.dry_run:
        print("\nDry run: nothing written. Drop --dry-run to write the column.")
        return result.exit_code

    if result.write:
        print(f"\n{result.write.output_path}")
        print(f"  {result.write.sheet}!{result.write.column} · "
              f"{result.write.values_written} value(s), "
              f"{result.write.formulas_copied} formula(s), "
              f"{result.write.blanks_annotated} blank(s)")
        for issue in result.write.issues:
            print(f"  [{_severity_mark(issue.severity)}] {issue.code}: {issue.message}")

    for path in save_artifacts(result):
        print(f"  wrote {path}")
    return result.exit_code


def _mapping_init(args) -> int:
    """Migration: an old map's human decisions, in the lean format."""
    from finscan2.mapping.convert import from_model_map
    from finscan2.model.schema import ModelMap

    mapping = from_model_map(ModelMap.load(args.map))
    out = Path(args.out) if args.out else Path("mappings") / f"{mapping.company}.json"
    if out.exists() and not args.overwrite:
        print(f"{out} already exists. Pass --overwrite to replace it.", file=sys.stderr)
        return 1
    mapping.save(out)

    needs = [r for r in mapping.rows if r.pdf is None and r.value is None]
    print(f"Wrote {out}  ({len(mapping.rows)} row(s))")
    print(f"  {len(mapping.rows) - len(needs)} carry a filing caption or a constant")
    print(f"  {len(needs)} need one:")
    for row in needs:
        print(f"    {row.label[:40]:<40} {row.note[:60]}")
    print("\nReview it, then: git add " + str(out))
    return 0


def _read_pdf_stage(args) -> int:
    """Stage 1: read the PDF, print what was seen, and write pdf.json for `read`."""
    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"No such file: {pdf_path}", file=sys.stderr)
        return 1

    doc = read_pdf(pdf_path,
                   cache_dir=None if args.no_cache else args.cache_dir,
                   use_cache=not args.no_cache,
                   ocr_enabled=not args.no_ocr)

    if args.cmd == "read":
        out = Path(args.out) if args.out else pdf_path.with_suffix(".pdf.json")
        out.write_text(json.dumps(doc.to_dict(), indent=2, ensure_ascii=False),
                       encoding="utf-8")
        print(f"Wrote {out}  ({len(doc.statements)} statement(s), "
              f"{len(doc.pages)} page(s))")

    _print_summary(doc, verbose=args.rows)
    return 2 if doc.errors else 0


def _company(args) -> int:
    """Run one stage for a company whose files are found by convention."""
    from argparse import Namespace

    from finscan2.company import CompanyError, resolve

    try:
        paths = resolve(args.key, pdf=args.pdf, excel=args.excel, sheet_out=args.out)
    except CompanyError as error:
        print(f"{error}", file=sys.stderr)
        return 1

    stage = "run" if args.stage == "all" else args.stage
    if stage == "run":
        try:
            return _run_quarter(args, paths)
        except CompanyError as error:
            print(f"{error}", file=sys.stderr)
            return 1

    print(f"{paths.key} · stage '{stage}'")
    print(paths.describe())
    print()

    def _cmd(text: str) -> str:
        return f"python -m finscan2.cli company {paths.key} {text}"

    try:
        if stage in {"read", "all"}:
            code = 0
            for pdf_path, pdf_json in zip(paths.pdfs or [paths.pdf], paths.pdf_jsons):
                code = max(code, _read_pdf_stage(Namespace(
                    cmd="read", pdf=str(pdf_path), out=str(pdf_json),
                    cache_dir=args.cache_dir, no_cache=args.no_cache,
                    no_ocr=args.no_ocr, rows=args.rows)))
                print()
            if code or stage == "read":
                return code

        if stage in {"learn", "all"}:
            if stage == "all" and paths.map.exists():
                print(f"  {paths.map} exists; leaving it alone.\n")
            else:
                # The filing is used when it has been read, because binding each
                # row to the caption it actually printed is the whole point of
                # reviewing a map before it is frozen.
                code = _learn(Namespace(
                    excel=str(paths.excel), company=paths.key, sheet=args.sheet,
                    out=str(paths.map), rows=args.rows,
                    pdf_json=str(paths.pdf_json) if paths.pdf_json.exists() else None,
                    no_llm=args.no_llm, overwrite=args.overwrite,
                    mapping=str(paths.mapping)))
                if code or stage == "learn":
                    return code

        if stage == "confirm":
            if not args.by:
                print("confirm needs --by <name>.", file=sys.stderr)
                return 1
            paths.require("map", _cmd("learn"))
            return _confirm(Namespace(map=str(paths.map), by=args.by))

        if stage == "bind":
            paths.require("values", _cmd("match"))
            paths.require("map", _cmd("learn"))
            return _bind(Namespace(values_json=str(paths.values), map=str(paths.map),
                                   dry_run=args.dry_run))

        if stage == "apply":
            for pdf_json in paths.pdf_jsons:
                if not pdf_json.exists():
                    raise CompanyError(f"{pdf_json} does not exist yet. Run:\n    "
                                       f"{_cmd('read')}")
            paths.require("mapping", "python -m finscan2.cli mapping-init "
                                    + str(paths.map))
            return _apply(Namespace(
                pdf_json=",".join(str(p) for p in paths.pdf_jsons),
                mapping=str(paths.mapping),
                excel=str(paths.excel), out=str(paths.output),
                period_end=args.period_end, dry_run=args.dry_run))

        if stage == "check":
            paths.require("map", _cmd("learn"))
            return _check(Namespace(map=str(paths.map), excel=str(paths.excel)))

        if stage in {"match", "all"}:
            paths.require("pdf_json", _cmd("read"))
            paths.require("map", _cmd("learn"))
            code = _match(Namespace(
                pdf_json=str(paths.pdf_json), map=str(paths.map), excel=str(paths.excel),
                period_end=args.period_end, out=str(paths.values)))
            if code or stage == "match":
                return code

        if stage in {"write", "all"}:
            paths.require("values", _cmd("match"))
            paths.require("map", _cmd("learn"))
            return _write(Namespace(
                values_json=str(paths.values), map=str(paths.map),
                excel=str(paths.excel), out=str(paths.output)))
    except CompanyError as error:
        print(f"{error}", file=sys.stderr)
        return 1

    return 0


def _run_quarter(args, paths) -> int:
    """The whole quarter in one step: read, learn only if needed, apply.

    * Every filing in the folder is read on every run — the PDF is the source of
      truth, and a new quarter's PDF is new. The SHA-256 cache only returns what
      reading these exact bytes with this exact parser already produced (and keeps
      vision transcriptions from being re-read with different digits).
    * The mapping is the one human decision file. It is used when it exists and
      never overwritten; it is proposed only for a company that has none.
    * The output workbook and this period's values/report are rewritten.

    The stages run quietly; one summary is printed. Everything they would have
    printed is in the report written beside the values.
    """
    import contextlib
    import io
    import json
    from argparse import Namespace

    from finscan2.apply import run as apply_run
    from finscan2.apply import save_artifacts
    from finscan2.schema import PdfDoc

    quiet = contextlib.redirect_stdout(io.StringIO())

    # 1. read
    for pdf_path, pdf_json in zip(paths.pdfs or [paths.pdf], paths.pdf_jsons):
        with contextlib.redirect_stdout(io.StringIO()):
            code = _read_pdf_stage(Namespace(
                cmd="read", pdf=str(pdf_path), out=str(pdf_json),
                cache_dir=args.cache_dir, no_cache=args.no_cache,
                no_ocr=args.no_ocr, rows=False))
        # Stage-1 findings are judged row by row in apply; only a missing file stops.
        if code == 1:
            print(f"Could not read {pdf_path}.", file=sys.stderr)
            return code

    # 2. learn — only for a company with no mapping yet, and only from filings
    # that hold a statement: a mapping proposed from nothing is all blanks, and
    # since a mapping is never overwritten it would outlive the fix that reads
    # the filing properly.
    fresh_mapping = not paths.mapping.exists()
    readable = any(json.loads(p.read_text(encoding="utf-8")).get("statements")
                   for p in paths.pdf_jsons)
    if not readable:
        from finscan2.apply import ApplyResult
        from finscan2.schema import Issue

        names = ", ".join(p.name for p in (paths.pdfs or [paths.pdf]))
        refused = ApplyResult(company=paths.key, refused=(
            "no financial statement was found in the filing"))
        refused.issues.append(Issue(
            code="no_statements", severity="error",
            message=f"Stage 1 identified no statement in {names}, so there is nothing "
                    f"to read" + ("" if paths.mapping.exists() else
                                  " and no mapping was proposed") + "."))
        _print_run_summary(paths, refused, [], fresh_mapping=False)
        return 1
    if fresh_mapping:
        pdf_json = paths.pdf_jsons[0]
        if len(paths.pdf_jsons) > 1:
            merged = PdfDoc.merge([PdfDoc.from_dict(json.loads(p.read_text(encoding="utf-8")))
                                   for p in paths.pdf_jsons])
            pdf_json = paths.values.with_name(f"{paths.key}_merged.pdf.json")
            pdf_json.parent.mkdir(parents=True, exist_ok=True)
            pdf_json.write_text(json.dumps(merged.to_dict(), indent=2, ensure_ascii=False),
                                encoding="utf-8")
        with quiet:
            code = _learn(Namespace(
                excel=str(paths.excel), company=paths.key, sheet=args.sheet,
                out=str(paths.map), rows=False, pdf_json=str(pdf_json),
                no_llm=args.no_llm, overwrite=False, mapping=str(paths.mapping)))
        if code:
            print(f"Could not propose a mapping for {paths.key}.", file=sys.stderr)
            return code

    # 3. apply
    result = apply_run(",".join(str(p) for p in paths.pdf_jsons), paths.mapping,
                       paths.excel, paths.output, args.period_end, args.dry_run)
    artifacts = [] if args.dry_run else save_artifacts(result)

    _print_run_summary(paths, result, artifacts, fresh_mapping)
    return result.exit_code


def _print_run_summary(paths, result, artifacts, fresh_mapping: bool) -> None:
    """Where the column went, and where its audit trail is. Nothing else."""
    from datetime import date

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

    if artifacts:
        label = "Audit"
        for path in artifacts:
            print(f"  {paint(label, '2'):<{11 + (8 if color else 0)}} {short(path)}")
            label = ""
    if fresh_mapping:
        print(f"  {paint('! Review', '1;33')}    {short(paths.mapping)} was generated by "
              f"this run; check it before trusting the column.")
    print()


def _bind(args) -> int:
    """Record the filing's own captions into the map, for review."""
    import json

    from finscan2.match.schema import Values as _V
    from finscan2.model.bind import apply as apply_bindings
    from finscan2.model.schema import ModelMap
    from finscan2.write.column import run as _write_run   # noqa: F401  (shared loader)

    model = ModelMap.load(args.map)
    payload = json.loads(Path(args.values_json).read_text(encoding="utf-8"))
    values = _values_from_dict(payload)

    changed = apply_bindings(values, model)
    if not changed:
        print("Every matched row already reads the caption the filing printed.")
        return 0

    for binding in changed:
        arrow = f"{binding.was!r} -> " if binding.was else ""
        print(f"  r{binding.row:<4} {binding.label[:34]:<34} {arrow}{binding.now!r}")

    if args.dry_run:
        print(f"\n{len(changed)} row(s) would be recorded. Re-run without --dry-run.")
        return 0

    model.save(args.map)
    print(f"\nRecorded {len(changed)} caption(s) in {args.map}.")
    return 0


def _values_from_dict(payload: dict):
    from finscan2.match.schema import Adjustment, ResolvedValue, Source, Values
    from finscan2.schema import Issue

    values = Values(company=payload["company"], sheet=payload["sheet"],
                    period_end=payload["period_end"], write_col=payload.get("write_col"),
                    units=payload.get("units", "units"),
                    issues=[Issue(**i) for i in payload.get("issues", [])])
    for item in payload.get("values", []):
        source = item.get("source")
        units = item.get("units") or {}
        values.values.append(ResolvedValue(
            row=item["row"], label=item["label"], section=item.get("section", ""),
            value=item.get("value"), resolve=item.get("resolve", ""),
            source=Source(**source) if source else None,
            adjustments=[Adjustment(**a) for a in item.get("adjustments", [])],
            units_from=units.get("from"), units_to=units.get("to"),
            unresolved=item.get("unresolved")))
    return values


def _confirm(args) -> int:
    from datetime import datetime, timezone

    from finscan2.model.schema import ModelMap

    model = ModelMap.load(args.map)
    outstanding = [r for r in model.inputs if r.review]
    model.confirmed_by = args.by
    model.confirmed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    model.save(args.map)
    print(f"{args.map} confirmed by {args.by}.")
    if outstanding:
        print(f"  note: {len(outstanding)} input row(s) still carry a review flag.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="finscan2", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name, help_text in (("read", "Read a PDF and write pdf.json."),
                            ("inspect", "Read a PDF and print what stage 1 saw.")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("pdf")
        p.add_argument("--out", help="Where to write pdf.json (default: alongside the PDF)")
        p.add_argument("--cache-dir", default=DEFAULT_CACHE,
                       help=f"Cache directory keyed by SHA-256 (default: {DEFAULT_CACHE})")
        p.add_argument("--no-cache", action="store_true",
                       help="Ignore and do not write the cache; re-transcribes image pages")
        p.add_argument("--no-ocr", action="store_true",
                       help="Skip vision transcription; image-only pages stay empty")
        p.add_argument("--rows", action="store_true", help="Also print parsed rows")

    lrn = sub.add_parser("learn", help="Read an Excel model and propose model.json.")
    lrn.add_argument("excel")
    lrn.add_argument("--company", required=True)
    lrn.add_argument("--sheet", default=None,
                     help="Workbook sheet (default: the mapping's sheet, else "
                          f"{DEFAULT_SHEET!r})")
    lrn.add_argument("--out", help="Where to write the map (default: models/<company>_model.json)")
    lrn.add_argument("--rows", action="store_true", help="Print every mapped row")
    lrn.add_argument("--pdf-json", dest="pdf_json",
                     help="Bind each row to the caption this filing printed")
    lrn.add_argument("--mapping", help="Where the human mapping lives "
                                      "(default: mappings/<company>.json)")
    lrn.add_argument("--overwrite", action="store_true",
                     help="Replace an existing mapping, discarding hand edits")
    lrn.add_argument("--no-llm", action="store_true",
                     help="Deterministic matching only; leave the rest unmatched")

    chk = sub.add_parser("check", help="Bind a map to a workbook and report problems.")
    chk.add_argument("map")
    chk.add_argument("excel")

    mt = sub.add_parser("match", help="Resolve a filing against a map -> values.json.")
    mt.add_argument("pdf_json")
    mt.add_argument("map")
    mt.add_argument("excel")
    mt.add_argument("--period-end", help="ISO date of the period being written")
    mt.add_argument("--out", help="Where to write values.json")

    wr = sub.add_parser("write", help="Write values.json into a copy of the workbook.")
    wr.add_argument("values_json")
    wr.add_argument("map")
    wr.add_argument("excel")
    wr.add_argument("--out", help="Output workbook (default: <name>_updated.<ext>)")

    bd = sub.add_parser("bind", help="Record the filing's captions into the map.")
    bd.add_argument("values_json")
    bd.add_argument("map")
    bd.add_argument("--dry-run", action="store_true", help="Show, change nothing")

    ap = sub.add_parser("apply", help="Resolve a filing and write the column, in one step.")
    ap.add_argument("pdf_json")
    ap.add_argument("mapping")
    ap.add_argument("excel")
    ap.add_argument("--out", help="Output workbook")
    ap.add_argument("--period-end", help="ISO date of the period being written")
    ap.add_argument("--dry-run", action="store_true",
                    help="Resolve and validate, print the table, write nothing")

    mi = sub.add_parser("mapping-init",
                        help="Convert an existing models/<company>_model.json into a mapping.")
    mi.add_argument("map")
    mi.add_argument("--out")
    mi.add_argument("--overwrite", action="store_true")

    cfm = sub.add_parser("confirm", help="Mark a map reviewed.")
    cfm.add_argument("map")
    cfm.add_argument("--by", required=True)

    co = sub.add_parser("company",
                        help="Run a stage for a company, finding its files by convention.")
    co.add_argument("key", help="Company folder under inbox/ (case-insensitive)")
    co.add_argument("stage", choices=["run", "read", "learn", "confirm", "check", "match",
                                      "write", "bind", "apply", "all", "paths"],
                    help="run = read every PDF, learn only if there is no mapping, "
                         "apply (all is the same)")
    co.add_argument("--pdf", help="Override the filing (default: the one PDF in the folder)")
    co.add_argument("--excel", help="Override the model (default: the one workbook)")
    co.add_argument("--out", help="Override the output workbook")
    co.add_argument("--sheet", default=None,
                    help="For `learn`: workbook sheet (default: the mapping's sheet)")
    co.add_argument("--by", help="Reviewer name, for `confirm`")
    co.add_argument("--period-end", help="ISO date of the period being written")
    co.add_argument("--cache-dir", default=DEFAULT_CACHE)
    co.add_argument("--no-cache", action="store_true")
    co.add_argument("--no-ocr", action="store_true")
    co.add_argument("--rows", action="store_true")
    co.add_argument("--dry-run", action="store_true", help="For `bind`: show, change nothing")
    co.add_argument("--no-llm", action="store_true",
                    help="For `learn`: deterministic caption matching only")
    co.add_argument("--overwrite", action="store_true",
                    help="For `learn`: replace an existing map, discarding hand edits")

    args = parser.parse_args(argv)

    if args.cmd == "apply":
        return _apply(args)
    if args.cmd == "mapping-init":
        return _mapping_init(args)
    if args.cmd == "company":
        if args.stage == "paths":
            from finscan2.company import CompanyError, resolve
            try:
                paths = resolve(args.key, pdf=args.pdf, excel=args.excel,
                                sheet_out=args.out)
            except CompanyError as error:
                print(f"{error}", file=sys.stderr)
                return 1
            print(f"{paths.key}\n{paths.describe()}")
            return 0
        return _company(args)
    if args.cmd == "learn":
        return _learn(args)
    if args.cmd == "check":
        return _check(args)
    if args.cmd == "match":
        return _match(args)
    if args.cmd == "write":
        return _write(args)
    if args.cmd == "bind":
        return _bind(args)
    if args.cmd == "confirm":
        return _confirm(args)

    return _read_pdf_stage(args)


if __name__ == "__main__":
    raise SystemExit(main())
