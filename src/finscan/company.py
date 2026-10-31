"""Resolve a company's files from its inbox folder, by convention.

    inbox/<Company>/<anything>.pdf          the filing
    inbox/<Company>/<anything>.xlsx         the model
    inbox/<Company>/<filing>.pdf.json       stage 1 output
    mappings/<key>.json                     the human mapping, git-tracked
    $FINSCAN_MAPPING_DIR/<key>.json         a learned one, where `mappings/` is
                                            read-only (a container image)
    models/<key>_model.json                 the derived layout
    _work/<key>_values.json                 stage 3 output
    inbox/<key>_v2_output.xlsx              stage 4 output

So `finscan company almarai match` replaces three quoted paths.

Every resolution is reported before the stage runs, and an ambiguity is an error
rather than a pick. "One of these two PDFs" is how a run silently reads last
quarter's filing — the same failure as a stale cache, arriving by a different
route.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

#: Where learned and appended mappings are written. Unset, it is `mappings/` —
#: the git-tracked folder, as on a laptop. In a container the image's `mappings/`
#: is thrown away after each run, so the Job points this at the file share
#: (`_work/mappings`): a learned mapping then outlives the run, the next quarter
#: reads it, and an analyst can download it to review.
MAPPING_DIR_ENV = "FINSCAN_MAPPING_DIR"

#: Names that mark a workbook as something this pipeline produced, not a source.
_OUTPUT_MARKERS = ("_output", "_updated", "_v2_", "~$")

_EXCEL_SUFFIXES = (".xlsx", ".xlsm", ".xltx", ".xltm")


class CompanyError(Exception):
    """A path could not be resolved. The message names the fix."""


def project_root(start: Path | None = None) -> Path:
    """The directory holding `inbox/`, found by walking up from `start`."""
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "inbox").is_dir():
            return candidate
    return here


def mapping_dir(root: Path) -> Path:
    """The folder learned mappings are written to (see MAPPING_DIR_ENV)."""
    configured = os.environ.get(MAPPING_DIR_ENV, "").strip()
    if not configured:
        return root / "mappings"
    path = Path(configured)
    return path if path.is_absolute() else root / path


def mapping_paths(root: Path, key: str) -> tuple[Path, Path]:
    """(the mapping to read, the path a learned or appended one is written to).

    A mapping in the writable folder wins: it is either learned for a company the
    image has no mapping for, or the image's own mapping with rows appended.
    """
    slug = key.lower()
    written = mapping_dir(root) / f"{slug}.json"
    shipped = root / "mappings" / f"{slug}.json"
    return (written if written.exists() or not shipped.exists() else shipped), written


def company_keys(root: Path | None = None) -> list[str]:
    """Return every direct company folder under the project's inbox."""
    root = root or project_root()
    inbox = root / "inbox"
    if not inbox.is_dir():
        raise CompanyError(f"No inbox/ directory under {root}.")
    return sorted((folder.name for folder in inbox.iterdir() if folder.is_dir()),
                  key=str.casefold)


def _one(paths: list[Path], what: str, folder: Path, hint: str) -> Path:
    if not paths:
        raise CompanyError(f"No {what} in {folder}. {hint}")
    if len(paths) > 1:
        names = "\n    ".join(sorted(p.name for p in paths))
        raise CompanyError(
            f"{len(paths)} {what}s in {folder}, so none was chosen:\n    {names}\n"
            f"  Pass the one you mean explicitly ({hint}).")
    return paths[0]


@dataclass
class CompanyPaths:
    key: str
    folder: Path
    pdf: Path
    excel: Path
    pdf_json: Path
    map: Path
    mapping: Path
    values: Path
    output: Path
    #: Every filing for the quarter. A company may publish its figures across two
    #: documents — Gruma's BMV report carries the cash flow, its press release the
    #: by-subsidiary table — and both are read and merged. `pdf` is the first.
    pdfs: list[Path] = field(default_factory=list)
    #: Where a learned mapping, or one with rows appended, is written. The same
    #: as `mapping` unless that is read-only (see MAPPING_DIR_ENV).
    mapping_out: Path | None = None

    @property
    def pdf_jsons(self) -> list[Path]:
        return [p.with_suffix(".pdf.json") for p in (self.pdfs or [self.pdf])]

    def describe(self) -> str:
        filings = [("pdf", p) for p in (self.pdfs or [self.pdf])]
        rows = [("folder", self.folder), *filings, ("excel", self.excel),
                *[("pdf.json", p) for p in self.pdf_jsons], ("mapping", self.mapping),
                ("layout", self.map), ("values.json", self.values),
                ("output", self.output)]
        return "\n".join(
            f"  {name:<12} {path}{'' if path.exists() else '   (not yet)'}"
            for name, path in rows)

    def require(self, name: str, stage_hint: str) -> Path:
        """A stage input that must already exist, or the command that makes it."""
        path: Path = getattr(self, name)
        if not path.exists():
            raise CompanyError(f"{path} does not exist yet. Run:\n    {stage_hint}")
        return path


def resolve(key: str, root: Path | None = None, *, pdf: str | None = None,
            excel: str | None = None, sheet_out: str | None = None) -> CompanyPaths:
    """Locate one company's files. Case-insensitive on the folder name."""
    root = root or project_root()
    inbox = root / "inbox"
    if not inbox.is_dir():
        raise CompanyError(f"No inbox/ directory under {root}.")

    folders = [d for d in inbox.iterdir() if d.is_dir() and d.name.lower() == key.lower()]
    if not folders:
        available = ", ".join(sorted(d.name for d in inbox.iterdir() if d.is_dir()))
        raise CompanyError(f"No folder inbox/{key}. Available: {available}")
    folder = folders[0]

    if pdf:
        pdf_paths = [Path(pdf)]
    else:
        pdf_paths = sorted(p for p in folder.glob("*.pdf") if not p.name.startswith("."))
        if not pdf_paths:
            raise CompanyError(f"No PDF in {folder}. --pdf <path>")
    pdf_path = pdf_paths[0]

    if excel:
        excel_path = Path(excel)
    else:
        candidates = [p for p in folder.iterdir()
                      if p.suffix.lower() in _EXCEL_SUFFIXES
                      and not any(marker in p.name for marker in _OUTPUT_MARKERS)]
        excel_path = _one(candidates, "Excel model", folder, "--excel <path>")

    slug = key.lower()
    mapping, mapping_out = mapping_paths(root, slug)
    return CompanyPaths(
        key=slug,
        folder=folder,
        pdf=pdf_path,
        pdfs=pdf_paths,
        excel=excel_path,
        pdf_json=pdf_path.with_suffix(".pdf.json"),
        map=root / "models" / f"{slug}_model.json",
        mapping=mapping,
        mapping_out=mapping_out,
        values=root / "_work" / f"{slug}_values.json",
        output=Path(sheet_out) if sheet_out
        else root / "inbox" / f"{slug}_v2_output{excel_path.suffix}",
    )
