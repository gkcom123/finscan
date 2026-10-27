"""Company folders under inbox/: what is there, uploads into them, outputs out.

Folder rules are the CLI's (finscan/company.py): one company per folder, any
number of filings for ONE quarter, exactly one source workbook, generated workbooks
recognised by their name. An upload keeps those rules true: a new filing moves the
previous quarter's filings to `past/`, a new workbook moves the old one there too.
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from finscan.company import _EXCEL_SUFFIXES, _OUTPUT_MARKERS

SUPPLEMENT_SUFFIX = ".pdf.supplement.json"


class UploadError(ValueError):
    """An upload was refused. The message says why."""


def company_folder(root: Path, company: str, *, create: bool = False) -> Path | None:
    """The company's inbox folder (case-insensitive), created if asked."""
    name = company.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.&-]{0,79}", name):
        raise UploadError(f"'{company}' is not a usable company name")
    inbox = root / "inbox"
    if inbox.is_dir():
        for folder in inbox.iterdir():
            if folder.is_dir() and folder.name.lower() == name.lower():
                return folder
    if not create:
        return None
    folder = inbox / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _filings(folder: Path) -> list[Path]:
    return sorted(p for p in folder.glob("*.pdf") if not p.name.startswith("."))


def _workbooks(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir()
                  if p.suffix.lower() in _EXCEL_SUFFIXES
                  and not any(m in p.name for m in _OUTPUT_MARKERS))


def describe(root: Path, folder: Path) -> dict:
    """One company's readiness, as the API and MCP tools report it."""
    key = folder.name.lower()
    filings, workbooks = _filings(folder), _workbooks(folder)
    problems = []
    if not filings:
        problems.append("no filing PDF uploaded")
    if not workbooks:
        problems.append("no model workbook uploaded")
    if len(workbooks) > 1:
        problems.append(f"{len(workbooks)} model workbooks; keep one")
    return {
        "company": key,
        "folder": folder.name,
        "filings": [p.name for p in filings],
        "supplements": sorted(p.name for p in folder.glob(f"*{SUPPLEMENT_SUFFIX}")),
        "model": workbooks[0].name if len(workbooks) == 1 else None,
        "has_mapping": (root / "mappings" / f"{key}.json").exists(),
        "ready": not problems,
        "problems": problems,
    }


def list_companies(root: Path) -> list[dict]:
    inbox = root / "inbox"
    if not inbox.is_dir():
        return []
    return [describe(root, f) for f in sorted(inbox.iterdir(), key=lambda p: p.name.lower())
            if f.is_dir() and not f.name.startswith(".")]


def kind_of(filename: str) -> str:
    """filing · supplement · model — or refused."""
    low = filename.lower()
    if low.endswith(SUPPLEMENT_SUFFIX):
        return "supplement"
    if low.endswith(".pdf"):
        return "filing"
    if Path(low).suffix in _EXCEL_SUFFIXES:
        if any(m in filename for m in _OUTPUT_MARKERS):
            raise UploadError(f"'{filename}' looks like a generated workbook, not a model")
        return "model"
    raise UploadError(f"'{filename}': only PDF filings, Excel models and "
                      f"'{SUPPLEMENT_SUFFIX}' files are accepted")


def save_uploads(root: Path, company: str, files: list[tuple[str, BinaryIO]],
                 max_bytes: int) -> dict:
    """Store one batch of files for a company and archive what they replace.

    A batch is one quarter: if it holds any filing, every filing already in the
    folder (with its read and supplement) moves to `past/<timestamp>/`; if it holds
    a model, the current model moves there too. Several filings for the same quarter
    (a report and a press release) are uploaded together, in one batch.
    """
    if not files:
        raise UploadError("no files were sent")
    batch = []
    for raw_name, stream in files:
        name = Path(raw_name or "").name
        if not name or name.startswith("."):
            raise UploadError(f"'{raw_name}' is not a usable file name")
        batch.append((name, kind_of(name), stream))

    folder = company_folder(root, company, create=True)
    kinds = {kind for _, kind, _ in batch}
    archive = folder / "past" / datetime.now().strftime("%Y%m%d-%H%M%S")
    archived: list[str] = []
    if "filing" in kinds:
        for old in _filings(folder):
            # The filing, its read (X.pdf.json) and its supplement (X.pdf.supplement.json).
            for path in (old, old.with_suffix(".pdf.json"), Path(f"{old}.supplement.json")):
                if path.exists():
                    archive.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), archive / path.name)
                    archived.append(path.name)
    if "model" in kinds:
        for old in _workbooks(folder):
            archive.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), archive / old.name)
            archived.append(old.name)

    saved = []
    for name, kind, stream in batch:
        target = folder / name
        written = 0
        with open(target, "wb") as out:
            while chunk := stream.read(1 << 20):
                written += len(chunk)
                if written > max_bytes:
                    out.close()
                    target.unlink(missing_ok=True)
                    raise UploadError(f"'{name}' is larger than {max_bytes // (1 << 20)} MB")
                out.write(chunk)
        saved.append({"file": name, "kind": kind, "bytes": written})
    return {"company": folder.name.lower(), "saved": saved, "archived": archived,
            "status": describe(root, folder)}
