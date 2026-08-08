"""FastAPI service. This is what Copilot Studio talks to.

The flow is built around the one-off profile confirmation, because that is what
makes 100 companies tractable:

    POST /jobs                  upload PDF + workbook -> proposal, nothing written
    POST /profiles/{key}/confirm  human approves the layout, once per company
    POST /jobs/{id}/apply       write the column, download the result

Once a company's profile is confirmed, `/extract-and-apply` runs straight
through with no human in the loop — which is the state you want 95 of the 100
companies to reach.
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from finscan.config import settings
from finscan.excel.discovery import discover
from finscan.graph.build import run as run_graph
from finscan.profiles import ProfileStore

app = FastAPI(
    title="FinScan",
    version="2.0.0",
    description="Extracts quarterly/annual results from a company PDF and writes them into "
                "the blue input cells of the company's Excel model, across every relevant tab, "
                "leaving formulas intact.",
)

WORK = Path(settings.finscan_work_dir)
WORK.mkdir(parents=True, exist_ok=True)
_JOBS: dict[str, dict] = {}


def auth(x_api_key: str = Header(default="")) -> None:
    if settings.finscan_api_key and x_api_key != settings.finscan_api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing x-api-key.")


# --------------------------------------------------------------------------- #
class MappingOut(BaseModel):
    sheet: str
    excel_row: int
    excel_label: str
    field: str | None
    value: float | None
    pdf_label: str | None
    match_method: str
    confidence: float


class SheetOut(BaseModel):
    sheet: str
    in_scope: bool
    enabled: bool
    reason: str
    units: str
    write_column: str
    write_mode: str
    input_rows: int
    formula_rows: int


class IssueOut(BaseModel):
    severity: str
    code: str
    message: str
    field: str | None = None


class JobOut(BaseModel):
    job_id: str
    status: str
    company_key: str | None = None
    profile_confirmed: bool = False
    profile_state: str | None = None
    fingerprint: str | None = None
    colors_found: bool = False
    period_header: str | None = None
    sheets: list[SheetOut] = []
    mappings: list[MappingOut] = []
    issues: list[IssueOut] = []
    report_markdown: str = ""
    download_url: str | None = None
    summary: str = ""


class ProfileOut(BaseModel):
    company_key: str
    display_name: str
    confirmed: bool
    fingerprint: str
    enabled_sheets: list[str]
    label_overrides: dict[str, str] = {}


class ConfirmIn(BaseModel):
    sheets: list[str] | None = None
    label_overrides: dict[str, str] | None = None
    by: str = "copilot-studio"


# --------------------------------------------------------------------------- #
def _job_dir(job_id: str) -> Path:
    d = WORK / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save(upload: UploadFile, dest: Path) -> Path:
    with dest.open("wb") as fh:
        shutil.copyfileobj(upload.file, fh)
    return dest


def _to_out(job_id: str, state: dict, download: str | None) -> JobOut:
    ex, wr = state.get("extraction"), state.get("write_result")
    plan, profile = state.get("plan"), state.get("profile")
    enabled = set(state.get("enabled_sheets") or [])

    counts = {"error": 0, "warning": 0}
    for i in state.get("issues", []):
        counts[i.severity] = counts.get(i.severity, 0) + 1

    status = state.get("status", "unknown")
    if status == "awaiting_confirmation":
        summary = (
            f"{(ex.meta.company_name if ex else None) or 'This company'} is new. "
            f"I found {len([s for s in (plan.sheets if plan else []) if s.in_scope])} sheet(s) to "
            f"update. Confirm the layout once and nothing will need approving again."
        )
    else:
        summary = (
            f"{(ex.meta.company_name if ex else None) or 'Document'} — "
            f"{state.get('period_header', 'period')}: "
            + (f"wrote {wr.describe()}. " if wr else "nothing written. ")
            + f"{counts['error']} error(s), {counts['warning']} warning(s)."
        )

    return JobOut(
        job_id=job_id,
        status=status,
        company_key=profile.company_key if profile else None,
        profile_confirmed=bool(profile and profile.confirmed),
        profile_state=state.get("profile_state"),
        fingerprint=plan.fingerprint if plan else None,
        colors_found=bool(plan and plan.colors_found),
        period_header=state.get("period_header"),
        sheets=[
            SheetOut(sheet=s.sheet, in_scope=s.in_scope, enabled=s.sheet in enabled,
                     reason=s.reason, units=s.units,
                     write_column=s.write_col_letter if s.in_scope else "",
                     write_mode=s.write_mode, input_rows=len(s.writable_rows),
                     formula_rows=len(s.formula_rows))
            for s in (plan.sheets if plan else [])
        ],
        mappings=[
            MappingOut(
                sheet=m.excel_label.split("!")[0], excel_row=m.excel_row,
                excel_label=m.excel_label.split("!", 1)[-1], field=m.field, value=m.value,
                pdf_label=m.pdf_label, match_method=m.match_method, confidence=m.confidence,
            )
            for m in state.get("mappings", []) if m.field
        ],
        issues=[IssueOut(**i.model_dump()) for i in state.get("issues", [])],
        report_markdown=state.get("report", ""),
        download_url=download,
        summary=summary,
    )


# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "llm_configured": settings.configured,
        "demo_enabled": settings.finscan_demo_enabled,
        "test_dir": str(settings.finscan_test_dir),
    }


@app.get("/demo/companies", dependencies=[Depends(auth)])
def list_demo_companies() -> dict:
    """Companies with a folder under test/ (for Copilot to discover)."""
    root = Path(settings.finscan_test_dir)
    if not root.is_dir():
        return {"companies": []}
    companies = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        pdfs = list(d.glob("*.pdf"))
        books = [p for p in d.glob("*.xlsx") if "_updated" not in p.stem.lower()]
        companies.append({
            "company_key": d.name,
            "pdf_count": len(pdfs),
            "workbook": books[0].name if books else None,
            "pdfs": [p.name for p in pdfs],
        })
    return {"companies": companies}


@app.post("/demo/update", response_model=JobOut, dependencies=[Depends(auth)])
def demo_update_from_folder(
    company: str = Form(..., description="Company name or key, e.g. Tancent or tencent"),
    period: str | None = Form(default=None, description="Period token, e.g. q325"),
    period_label: str | None = Form(default=None, description="Column header override"),
) -> JobOut:
    """Demo endpoint: read PDF + workbook from test/<company>/ and write an updated file.

    Intended for Copilot Studio + local server demos where files live on disk
    instead of being uploaded in chat. Say: "updated excel for Q325 for Tancent".
    """
    if not settings.finscan_demo_enabled:
        raise HTTPException(status_code=403, detail="Demo mode is disabled on this server.")

    from finscan.demo.folder import resolve_demo_files

    try:
        files = resolve_demo_files(settings.finscan_test_dir, company, period)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    header = period_label or files.period_label
    job_id = uuid.uuid4().hex[:12]
    d = _job_dir(job_id)
    pdf_path = d / files.pdf.name
    xl_path = d / files.workbook.name
    shutil.copy2(files.pdf, pdf_path)
    shutil.copy2(files.workbook, xl_path)
    suffix = f"_{files.period_token}" if files.period_token else ""
    out_path = d / f"{xl_path.stem}_updated{suffix}{xl_path.suffix}"

    state = run_graph(
        str(pdf_path),
        str(xl_path),
        output_path=str(out_path),
        company=files.company_key,
        period_label=header,
    )
    _JOBS[job_id] = {
        "pdf": str(pdf_path),
        "xlsx": str(xl_path),
        "company": files.company_key,
        "sheets": None,
        "period_label": header,
        "state": state,
    }
    download = f"/jobs/{job_id}/download" if state.get("write_result") else None
    return _to_out(job_id, state, download)


@app.post("/inspect", dependencies=[Depends(auth)])
async def inspect(workbook: UploadFile = File(...)) -> dict:
    """What FinScan sees in a workbook. No PDF, no LLM, nothing written."""
    d = _job_dir("inspect-" + uuid.uuid4().hex[:8])
    plan = discover(_save(workbook, d / (workbook.filename or "model.xlsx")))
    return {
        "fingerprint": plan.fingerprint,
        "colors_found": plan.colors_found,
        "sheets": [
            {"sheet": s.sheet, "in_scope": s.in_scope, "reason": s.reason, "units": s.units,
             "write_column": s.write_col_letter, "write_mode": s.write_mode,
             "input_rows": len(s.writable_rows), "formula_rows": len(s.formula_rows),
             "unmatched_labels": [r.label for r in s.rows if r.field is None][:20]}
            for s in plan.sheets
        ],
    }


@app.post("/jobs", response_model=JobOut, dependencies=[Depends(auth)])
async def create_job(
    pdf: UploadFile = File(..., description="Company results PDF"),
    workbook: UploadFile = File(..., description="Company Excel model (.xlsx/.xlsm)"),
    company: str | None = Form(default=None),
    sheets: str | None = Form(default=None, description="Comma-separated sheet names"),
    period_label: str | None = Form(default=None),
) -> JobOut:
    """Extract, discover the layout and map — but write nothing."""
    job_id = uuid.uuid4().hex[:12]
    d = _job_dir(job_id)
    pdf_path = _save(pdf, d / (pdf.filename or "input.pdf"))
    xl_path = _save(workbook, d / (workbook.filename or "model.xlsx"))
    sheet_list = [s.strip() for s in sheets.split(",")] if sheets else None

    state = run_graph(str(pdf_path), str(xl_path), company=company, sheets=sheet_list,
                      period_label=period_label, dry_run=True)
    _JOBS[job_id] = {"pdf": str(pdf_path), "xlsx": str(xl_path), "company": company,
                     "sheets": sheet_list, "period_label": period_label, "state": state}
    return _to_out(job_id, state, download=None)


@app.post("/jobs/{job_id}/apply", response_model=JobOut, dependencies=[Depends(auth)])
def apply_job(job_id: str) -> JobOut:
    """Approve the proposal and write the new column into every enabled sheet."""
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job_id.")
    out_path = Path(job["xlsx"]).with_name(Path(job["xlsx"]).stem + "_updated.xlsx")
    state = run_graph(job["pdf"], job["xlsx"], output_path=str(out_path),
                      company=job["company"], sheets=job["sheets"],
                      period_label=job["period_label"], require_confirmation=False)
    job["state"] = state
    return _to_out(job_id, state, download=f"/jobs/{job_id}/download")


@app.post("/extract-and-apply", response_model=JobOut, dependencies=[Depends(auth)])
async def extract_and_apply(
    pdf: UploadFile = File(...),
    workbook: UploadFile = File(...),
    company: str | None = Form(default=None),
    sheets: str | None = Form(default=None),
    period_label: str | None = Form(default=None),
) -> JobOut:
    """One-shot. Honours the confirmation gate: a company whose profile has never
    been confirmed comes back as `awaiting_confirmation` with nothing written."""
    job_id = uuid.uuid4().hex[:12]
    d = _job_dir(job_id)
    pdf_path = _save(pdf, d / (pdf.filename or "input.pdf"))
    xl_path = _save(workbook, d / (workbook.filename or "model.xlsx"))
    out_path = xl_path.with_name(xl_path.stem + "_updated.xlsx")
    sheet_list = [s.strip() for s in sheets.split(",")] if sheets else None

    state = run_graph(str(pdf_path), str(xl_path), output_path=str(out_path),
                      company=company, sheets=sheet_list, period_label=period_label)
    _JOBS[job_id] = {"pdf": str(pdf_path), "xlsx": str(xl_path), "company": company,
                     "sheets": sheet_list, "period_label": period_label, "state": state}
    download = f"/jobs/{job_id}/download" if state.get("write_result") else None
    return _to_out(job_id, state, download)


@app.get("/jobs/{job_id}/download", dependencies=[Depends(auth)])
def download(job_id: str) -> FileResponse:
    job = _JOBS.get(job_id)
    if not job or not job["state"].get("write_result"):
        raise HTTPException(status_code=404, detail="No updated workbook for this job.")
    path = job["state"]["write_result"].workbook_path
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=Path(path).name,
    )


# --------------------------------------------------------------------------- #
@app.get("/profiles", response_model=list[ProfileOut], dependencies=[Depends(auth)])
def list_profiles() -> list[ProfileOut]:
    store = ProfileStore(settings.finscan_profile_store)
    out = []
    for key in store.list():
        pr = store.get(key)
        out.append(ProfileOut(company_key=pr.company_key, display_name=pr.display_name,
                              confirmed=pr.confirmed, fingerprint=pr.fingerprint,
                              enabled_sheets=sorted(pr.enabled_sheets()),
                              label_overrides=pr.label_overrides))
    return out


@app.get("/profiles/{key}", response_model=ProfileOut, dependencies=[Depends(auth)])
def get_profile(key: str) -> ProfileOut:
    pr = ProfileStore(settings.finscan_profile_store).get(key)
    if pr is None:
        raise HTTPException(status_code=404, detail=f"No profile '{key}'.")
    return ProfileOut(company_key=pr.company_key, display_name=pr.display_name,
                      confirmed=pr.confirmed, fingerprint=pr.fingerprint,
                      enabled_sheets=sorted(pr.enabled_sheets()),
                      label_overrides=pr.label_overrides)


@app.post("/profiles/{key}/confirm", response_model=ProfileOut, dependencies=[Depends(auth)])
def confirm_profile(key: str, body: ConfirmIn) -> ProfileOut:
    """One-off human sign-off on a company's layout. After this, uploads for this
    company run straight through until the layout changes."""
    store = ProfileStore(settings.finscan_profile_store)
    pr = store.get(key)
    if pr is None:
        raise HTTPException(status_code=404, detail=f"No profile '{key}'.")
    if body.sheets is not None:
        wanted = set(body.sheets)
        for s in pr.sheets:
            s.enabled = s.sheet in wanted
    if body.label_overrides:
        pr.label_overrides.update(body.label_overrides)
    pr.confirm(by=body.by)
    store.save(pr)
    return ProfileOut(company_key=pr.company_key, display_name=pr.display_name,
                      confirmed=pr.confirmed, fingerprint=pr.fingerprint,
                      enabled_sheets=sorted(pr.enabled_sheets()),
                      label_overrides=pr.label_overrides)
