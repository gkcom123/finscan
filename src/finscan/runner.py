"""One company's quarter, as a function that returns its outcome.

The CLI prints the outcome; the service returns it as JSON. The steps are the ones
the CLI has always run:

1. read     every PDF in the company folder -> <pdf>.pdf.json (reads cached in
            _work/pdfcache by the PDF's SHA-256)
2. mapping  created if missing; otherwise only workbook rows it lacks are appended
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


def sync_mapping(paths, doc, sheet: str | None, dry_run: bool = False) -> str:
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


def run_company(key: str, *, root: str | Path | None = None, dry_run: bool = False,
                period_end: str | None = None, no_cache: bool = False,
                sheet: str | None = None) -> RunResult:
    """The whole quarter for one company: read, sync the mapping, apply.

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
                    no_cache=no_cache, sheet=sheet)
    except Exception as error:                   # reported, never swallowed silently
        return RunResult(company=paths.key, status="failed", exit_code=1,
                         dry_run=dry_run, mapping=str(paths.mapping),
                         errors=[f"{type(error).__name__}: {error}",
                                 traceback.format_exc(limit=3)])


def _run(paths, base: Path, *, dry_run: bool, period_end: str | None, no_cache: bool,
         sheet: str | None) -> RunResult:
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

    # 2. mapping — created if missing, appended only for rows it lacks.
    mapping_status = sync_mapping(paths, doc, sheet, dry_run)

    # 3. apply
    result = apply_run(",".join(str(p) for p in paths.pdf_jsons), paths.mapping,
                       paths.excel, paths.output, period_end, dry_run)

    # 4. derived files, rewritten every run.
    artifacts: list[Path] = []
    if not dry_run:
        if result.model is not None:
            artifacts.append(result.model.save(paths.map))
        artifacts.extend(save_artifacts(result, base / "_work"))

    return _outcome(paths, result, artifacts, mapping_status, dry_run)


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
