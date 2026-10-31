"""One company's quarter, as a function that returns its outcome.

The CLI prints the outcome; the service returns it as JSON. The steps are the ones
the CLI has always run:

1. read     every PDF in the company folder -> <pdf>.pdf.json (reads cached in
            _work/pdfcache by the PDF's SHA-256)
2. mapping  learned if missing (by value against the analyst's own figures, then
            by caption); otherwise only workbook rows it lacks are learned and
            appended. Checked against the model either way; a learned row whose
            figures differ is withheld (written blank, with the reason)
3. apply    resolve, validate, write inbox/<key>_v2_output.<ext>
4. derived  models/<key>_model.json and _work/<key>_<period>_values.json + _report.md

Paths are taken from a project root (the folder holding inbox/, mappings/, models/,
_work/), so the same code runs on a laptop and against a mounted file share.
"""
from __future__ import annotations

import json
import traceback
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from finscan.pdf.read import read_pdf

#: Relative to the project root.
DEFAULT_CACHE = "_work/pdfcache"

#: The sheet learned for a company that has no mapping yet.
DEFAULT_SHEET = "Model"

#: The reader a company gets unless its mapping names another. The vision model
#: reads layouts the rules keep missing; `"reader": "rules"` opts a company back.
DEFAULT_READER = "llm"


@dataclass
class Blank:
    row: int
    label: str
    reason: str


@dataclass
class RunResult:
    """What one company run did. Serialisable: this is what the service returns."""

    company: str
    #: written · dry_run · refused (nothing written, by design) · failed (error)
    status: str
    exit_code: int
    dry_run: bool = False
    period_end: str = ""
    sheet: str | None = None
    column: str | None = None
    output: str | None = None
    #: Rows the filing resolved; for a dry run, the values that would be written.
    values_resolved: int = 0
    values_written: int = 0
    formulas_copied: int = 0
    blank_count: int = 0
    blanks: list[Blank] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    refused: str = ""
    mapping: str | None = None
    mapping_status: str = ""
    #: The mapping resolved for the periods the filing prints as comparatives and
    #: compared with the model: verified · differs · unchecked, with the rows that
    #: are not verified.
    mapping_check: dict = field(default_factory=dict)
    #: Learned rows withheld because their figures differ from the analyst's.
    withheld: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("written", "dry_run")

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
                        encoding="utf-8")
        return path


def company_reader(paths) -> str:
    """The company's chosen reader: `"reader"` in its mapping, else the default."""
    from finscan.mapping.schema import Mapping

    if not paths.mapping.exists():
        return DEFAULT_READER
    return Mapping.load(paths.mapping).reader or DEFAULT_READER


def read_filings(paths, *, cache_dir: Path | None) -> list:
    """Stage 1 for every PDF in the folder. Each pdf.json is rewritten."""
    reader = company_reader(paths)
    docs = []
    for pdf_path, pdf_json in zip(paths.pdfs or [paths.pdf], paths.pdf_jsons):
        doc = read_pdf(pdf_path, cache_dir=cache_dir, use_cache=cache_dir is not None,
                       reader=reader)
        pdf_json.write_text(json.dumps(doc.to_dict(), indent=2, ensure_ascii=False),
                            encoding="utf-8")
        docs.append(doc)
    return docs


@dataclass
class MappingSync:
    """What happened to the mapping before the apply, and how it checked out."""

    path: Path
    status: str
    #: Rows decided by this run rather than by a person: a new company's every
    #: row, or the rows appended for new workbook lines.
    learned: int = 0
    #: Learned rows whose figures did not reproduce the analyst's; left blank.
    withheld: list[str] = field(default_factory=list)
    check: dict = field(default_factory=dict)


def _learn(paths, doc, sheet: str):
    """A whole mapping from the workbook and the filing, values first."""
    from finscan.mapping.propose import propose_mapping
    from finscan.model.discover import discover_sheet

    layout = discover_sheet(str(paths.excel), sheet)
    proposal, _ = propose_mapping(layout, paths.key, doc, workbook=str(paths.excel))
    return proposal, layout


def sync_mapping(paths, doc, sheet: str | None, dry_run: bool = False,
                 relearn: bool = False) -> MappingSync:
    """Learn the mapping, or append the workbook rows it lacks; check it either way.

    * No mapping (a new company), or `relearn`: the whole mapping is learned — by
      value against the analyst's own figures first (mapping/learn_values.py),
      then by caption, then by a model for what is left.
    * A mapping: rows for new workbook lines are learned and appended.

    Every learned row is then checked by resolving the periods the filing prints
    as comparatives and comparing with the model; a learned row that differs is
    withheld (written blank, with the reason). Rows a person wrote are checked
    too but never withheld — the check is reported, the decision stays theirs.

    A dry run saves a NEW company's mapping (nothing to lose, and the analyst can
    review it before the real run); a relearn or appended rows are only reported.
    """
    import shutil
    import tempfile

    from finscan.mapping.learn_values import check_mapping
    from finscan.mapping.propose import propose_mapping
    from finscan.mapping.schema import Mapping
    from finscan.match.resolve import expected_period_end
    from finscan.model.discover import discover_sheet
    from finscan.model.learn import propose as propose_model

    out = paths.mapping_out or paths.mapping
    existing = Mapping.load(paths.mapping) if paths.mapping.exists() else None

    if existing is None or relearn:
        sheet_name = sheet or (existing.sheet if existing else DEFAULT_SHEET)
        mapping, layout = _learn(paths, doc, sheet_name)
        if existing is not None and existing.reader:
            mapping.reader = existing.reader
        learned_rows = list(mapping.rows)
    else:
        mapping = existing
        layout = discover_sheet(str(paths.excel), mapping.sheet)
        known = set(mapping.by_key())
        # Cheap, deterministic check first: which rows would a proposal make inputs
        # that the mapping lacks? Headings such as "As % of sales unless stated" are
        # writable cells but never inputs, so they must not trigger a model call on
        # every run only to be discarded.
        candidates = propose_model(layout, paths.key)
        learned_rows = []
        if any(spec.kind == "input" and spec.key.normalized() not in known
               for spec in candidates.rows):
            # Proposed against the whole sheet, so each new row's occurrence number
            # is the one the sheet gives it; only the rows the mapping lacks are kept.
            proposal, _ = propose_mapping(layout, paths.key, doc,
                                          workbook=str(paths.excel))
            learned_rows = [row for row in proposal.rows if row.key not in known]
            mapping = Mapping(company=existing.company, sheet=existing.sheet,
                              rows=[*existing.rows, *learned_rows],
                              mapping_version=existing.mapping_version,
                              reader=existing.reader, units=existing.units)

    target = expected_period_end(layout.period_dates, layout.cadence_months)
    check = check_mapping(mapping, layout, doc, str(paths.excel), target)
    learned_keys = {row.key for row in learned_rows}
    # Withheld, not dropped: a learned row whose figures differ from the
    # analyst's is kept in the mapping with what was learned and why it was
    # withheld, so a person can restore it, and is written blank until then.
    # A wrong number with a plausible source is worse than a gap.
    withheld = []
    by_row = {r.row: r for r in check.rows.values()}
    for row in learned_rows:
        result = _check_for(row, layout, by_row)
        if result is None or result.status != "differs":
            continue
        learned = row.pdf if row.pdf is not None else "the learned line"
        row.note = f"withheld: learned {learned!r} but {result.detail} — check it, then restore"
        row.pdf, row.statement, row.basis, row.sign = None, None, None, None
        withheld.append(row.label)

    summary = check.summary()
    learned_count = len(learned_keys)
    matched = sum(1 for row in learned_rows if row.pdf is not None)
    detail = (f"; {matched} matched to the filing, {len(withheld)} withheld, "
              f"{learned_count - matched - len(withheld)} with no match"
              if learned_count else "")

    if existing is None:
        mapping.save(out)
        status = (f"learned ({len(mapping.rows)} rows{detail}) — review it")
        return MappingSync(out, status, learned_count, withheld, summary)

    if relearn:
        if dry_run:
            # Applied from a scratch copy so the dry run shows what the relearned
            # mapping would write, without replacing the mapping in use.
            scratch = Path(tempfile.mkdtemp()) / out.name
            mapping.save(scratch)
            return MappingSync(scratch, f"relearned, not saved (dry run){detail}",
                               learned_count, withheld, summary)
        archive = out.parent / "past" / f"{paths.key}-{date.today().isoformat()}.json"
        archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(paths.mapping, archive)
        mapping.save(out)
        return MappingSync(out, f"relearned ({len(mapping.rows)} rows{detail}); the "
                           f"previous one is in {archive.parent.name}/{archive.name}",
                           learned_count, withheld, summary)

    if not learned_rows:
        return MappingSync(paths.mapping, "unchanged", 0, [], summary)
    names = (", ".join(repr(r.label) for r in learned_rows[:5])
             + ("…" if len(learned_rows) > 5 else ""))
    if dry_run:
        return MappingSync(paths.mapping, f"{len(learned_rows)} new row(s) found, not "
                           f"appended (dry run): {names}", learned_count, withheld, summary)
    stamp = f"added {date.today().isoformat()}: new in the workbook, review"
    for row in learned_rows:
        row.note = f"{stamp} · {row.note}" if row.note else stamp
    if out != paths.mapping and not out.exists():
        # The shipped mapping is read-only here; its copy in the writable folder
        # takes the new rows and is read from now on.
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(paths.mapping, out)
    Mapping.append_rows(out, learned_rows)
    return MappingSync(out, f"{len(learned_rows)} new row(s) appended{detail} — review: "
                       f"{names}", learned_count, withheld, summary)


def _check_for(row, layout, by_row):
    """The check result for a mapping row, found through the sheet row it binds to."""
    from finscan.model.schema import normalize_label

    seen: dict[tuple[str, str], int] = {}
    for line in layout.rows:
        normalized = normalize_label(line.label)
        occurrence = seen.get((normalized, line.section), 0) + 1
        seen[(normalized, line.section)] = occurrence
        if (normalized, line.section, occurrence) == row.key:
            return by_row.get(line.row)
    return None


def run_company(key: str, *, root: str | Path | None = None, dry_run: bool = False,
                period_end: str | None = None, no_cache: bool = False,
                sheet: str | None = None, relearn: bool = False) -> RunResult:
    """The whole quarter for one company: read, learn or check the mapping, apply.

    A company with no mapping is learned on its first run — that is how a new
    company is set up. `relearn` learns an existing company's mapping afresh.

    Never raises for a company-level problem: a missing folder, an unreadable
    filing or an unexpected error all come back as a `failed` result with the
    reason, so a caller (the CLI loop, the service) always has something to report.
    """
    from finscan.company import CompanyError, resolve

    try:
        paths = resolve(key, Path(root) if root else None)
    except CompanyError as error:
        return RunResult(company=key, status="failed", exit_code=1, dry_run=dry_run,
                         errors=[str(error)])
    base = paths.folder.parent.parent            # <root>/inbox/<Company> -> <root>
    try:
        return _run(paths, base, dry_run=dry_run, period_end=period_end,
                    no_cache=no_cache, sheet=sheet, relearn=relearn)
    except Exception as error:                   # reported, never swallowed silently
        return RunResult(company=paths.key, status="failed", exit_code=1,
                         dry_run=dry_run, mapping=str(paths.mapping),
                         errors=[f"{type(error).__name__}: {error}",
                                 traceback.format_exc(limit=3)])


def _run(paths, base: Path, *, dry_run: bool, period_end: str | None, no_cache: bool,
         sheet: str | None, relearn: bool = False) -> RunResult:
    from finscan.apply import run as apply_run
    from finscan.apply import save_artifacts
    from finscan.schema import PdfDoc

    for pdf_path in paths.pdfs or [paths.pdf]:
        if not Path(pdf_path).exists():
            return RunResult(company=paths.key, status="failed", exit_code=1,
                             dry_run=dry_run, errors=[f"No such file: {pdf_path}"])

    # 1. read
    docs = read_filings(paths, cache_dir=None if no_cache else base / DEFAULT_CACHE)
    doc = PdfDoc.merge(docs) if len(docs) > 1 else docs[0]
    if not doc.statements:
        names = ", ".join(p.name for p in (paths.pdfs or [paths.pdf]))
        return RunResult(
            company=paths.key, status="refused", exit_code=1, dry_run=dry_run,
            refused="no financial statement was found in the filing",
            mapping=str(paths.mapping), mapping_status="not checked",
            errors=[f"No statement was identified in {names}, so there is nothing to read"
                    + ("" if paths.mapping.exists() else " and no mapping was proposed")
                    + "."])

    # 2. mapping — learned if missing, appended only for rows it lacks, checked.
    synced = sync_mapping(paths, doc, sheet, dry_run, relearn)

    # 3. apply
    result = apply_run(",".join(str(p) for p in paths.pdf_jsons), synced.path,
                       paths.excel, paths.output, period_end, dry_run)

    # 4. derived files, rewritten every run.
    artifacts: list[Path] = []
    if not dry_run:
        if result.model is not None:
            artifacts.append(result.model.save(paths.map))
        artifacts.extend(save_artifacts(result, base / "_work"))
        _add_check_to_report(artifacts, synced)

    outcome = _outcome(paths, result, artifacts, synced.status, dry_run)
    outcome.mapping = str(synced.path)
    outcome.mapping_check = synced.check
    outcome.withheld = synced.withheld
    return outcome


def _add_check_to_report(artifacts: list[Path], synced: MappingSync) -> None:
    """Append the mapping check to the run's report, where an analyst reads it."""
    report = next((p for p in artifacts if p.name.endswith("_report.md")), None)
    check = synced.check
    if report is None or not check:
        return
    lines = ["", "## Mapping check", "",
             f"Mapping: {synced.status}", "",
             f"Compared with the model for {', '.join(check['compared_with']) or 'no period'}: "
             f"**{check['verified']} verified**, {check['differs']} differ, "
             f"{check['unchecked']} not checkable.", ""]
    if synced.withheld:
        lines += ["Withheld (learned, but the figures differ from the model; written "
                  "blank): " + ", ".join(synced.withheld), ""]
    rows = [r for r in check["rows"] if r["status"] == "differs"]
    if rows:
        lines += ["| row | label | detail |", "|---|---|---|"]
        lines += [f"| {r['row']} | {r['label']} | {r['detail']} |" for r in rows]
        lines.append("")
    with report.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def _outcome(paths, result, artifacts, mapping_status: str, dry_run: bool) -> RunResult:
    """An ApplyResult and its side files, as a RunResult."""
    issues = list(result.issues) + list(result.values.issues if result.values else [])
    write = result.write
    blanks = [Blank(row=v.row, label=v.label, reason=v.unresolved or "")
              for v in (result.values.values if result.values else []) if not v.ok]
    if result.refused:
        status = "refused"
    elif dry_run:
        status = "dry_run"
    elif write is not None:
        status = "written"
    else:
        status = "failed"
    return RunResult(
        company=paths.key, status=status, exit_code=result.exit_code, dry_run=dry_run,
        period_end=result.period_end or "",
        values_resolved=sum(1 for v in (result.values.values if result.values else []) if v.ok),
        sheet=write.sheet if write else None, column=write.column if write else None,
        output=str(write.output_path) if write else None,
        values_written=write.values_written if write else 0,
        formulas_copied=write.formulas_copied if write else 0,
        blank_count=len(blanks), blanks=blanks,
        errors=[i.message for i in issues if i.severity == "error"],
        warnings=[f"{i.code}: {i.message}" for i in issues if i.severity == "warning"],
        refused=result.refused or "",
        mapping=str(paths.mapping), mapping_status=mapping_status,
        artifacts=[str(p) for p in artifacts])
