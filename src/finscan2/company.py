"""Resolve a company's files from its inbox folder, by convention.

    inbox/<Company>/<anything>.pdf          the filing
    inbox/<Company>/<anything>.xlsx         the model
    inbox/<Company>/<filing>.pdf.json       stage 1 output
    mappings/<key>.json                     the human mapping, git-tracked
    maps/<key>.json                         the derived layout
    _work/<key>_values.json                 stage 3 output
    inbox/<key>_v2_output.xlsx              stage 4 output

So `finscan2 company almarai match` replaces three quoted paths.

Every resolution is reported before the stage runs, and an ambiguity is an error
rather than a pick. "One of these two PDFs" is how a run silently reads last
quarter's filing — the same failure as a stale cache, arriving by a different
route.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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

    def describe(self) -> str:
        rows = [("folder", self.folder), ("pdf", self.pdf), ("excel", self.excel),
                ("pdf.json", self.pdf_json), ("mapping", self.mapping),
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
        pdf_path = Path(pdf)
    else:
        pdf_path = _one([p for p in folder.glob("*.pdf") if not p.name.startswith(".")],
                        "PDF", folder, "--pdf <path>")

    if excel:
        excel_path = Path(excel)
    else:
        candidates = [p for p in folder.iterdir()
                      if p.suffix.lower() in _EXCEL_SUFFIXES
                      and not any(marker in p.name for marker in _OUTPUT_MARKERS)]
        excel_path = _one(candidates, "Excel model", folder, "--excel <path>")

    slug = key.lower()
    return CompanyPaths(
        key=slug,
        folder=folder,
        pdf=pdf_path,
        excel=excel_path,
        pdf_json=pdf_path.with_suffix(".pdf.json"),
        map=root / "maps" / f"{slug}.json",
        mapping=root / "mappings" / f"{slug}.json",
        values=root / "_work" / f"{slug}_values.json",
        output=Path(sheet_out) if sheet_out
        else root / "inbox" / f"{slug}_v2_output{excel_path.suffix}",
    )
