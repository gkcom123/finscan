"""The operations behind both the REST API and the MCP tools.

Each method returns plain dicts, so an endpoint and its MCP tool return exactly the
same thing. Errors are `ServiceError`s carrying an HTTP status.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

from finscan.company import project_root
from finscan.service import files, links
from finscan.service.jobs import AzureJobLauncher, CompanyBusy, JobStore, LocalLauncher

KINDS = ("workbook", "report")


class ServiceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


class Service:
    def __init__(self, settings, root: Path | None = None, launcher=None):
        self.s = settings
        self.root = Path(root or settings.finscan_root or project_root()).resolve()
        if launcher is None:
            launcher = (AzureJobLauncher(settings) if settings.finscan_runner == "azure"
                        else LocalLauncher(self.root))
        self.jobs = JobStore(self.root, launcher)

    # ---- links -------------------------------------------------------------
    @property
    def secret(self) -> str:
        return self.s.finscan_link_secret or self.s.finscan_api_key

    def _link(self, purpose: str, subject: str, path: str) -> dict:
        token, expires = links.sign(self.secret, purpose, subject, self.s.finscan_link_minutes)
        base = self.s.finscan_public_url.rstrip("/")
        return {"url": f"{base}{path}?token={quote(token)}", "expires_at": _iso(expires)}

    def check_link(self, purpose: str, subject: str, token: str) -> None:
        if not links.verify(self.secret, purpose, subject, token or ""):
            raise ServiceError(403, "this link is invalid or has expired; ask for a new one")

    # ---- companies and uploads ----------------------------------------------
    def list_companies(self) -> list[dict]:
        active = {j["company"]: j["id"] for j in self.jobs.all()
                  if self.jobs.view(j)["state"] in ("queued", "running")}
        return [{**c, "active_job": active.get(c["company"])}
                for c in files.list_companies(self.root)]

    def company(self, company: str) -> dict:
        try:
            folder = files.company_folder(self.root, company)
        except files.UploadError as error:
            raise ServiceError(400, str(error))
        if folder is None:
            raise ServiceError(404, f"no company '{company}'; upload its files first")
        return files.describe(self.root, folder)

    def upload_link(self, company: str) -> dict:
        try:
            files.company_folder(self.root, company)        # validates the name
        except files.UploadError as error:
            raise ServiceError(400, str(error))
        link = self._link("upload", company.lower(), f"/upload/{quote(company)}")
        return {"company": company, "upload_url": link["url"], "expires_at": link["expires_at"],
                "instructions": "Open the link and upload this quarter's filing PDF(s); add "
                                "the model workbook only if it changed."}

    def upload(self, company: str, items: list[tuple[str, BinaryIO]]) -> dict:
        try:
            return files.save_uploads(self.root, company, items,
                                      self.s.finscan_max_upload_mb << 20)
        except files.UploadError as error:
            raise ServiceError(400, str(error))

    # ---- jobs --------------------------------------------------------------
    def start_job(self, company: str, dry_run: bool = True, period_end: str | None = None) -> dict:
        info = self.company(company)
        if not info["ready"]:
            raise ServiceError(409, f"{info['company']} is not ready: " + "; ".join(info["problems"]))
        try:
            job = self.jobs.start(info["company"], dry_run=dry_run, period_end=period_end)
        except CompanyBusy as error:
            raise ServiceError(409, str(error))
        return self.jobs.view(job)

    def _job(self, job_id: str) -> dict:
        job = self.jobs.load(job_id)
        if job is None:
            raise ServiceError(404, f"no job '{job_id}'")
        return job

    def job_status(self, job_id: str) -> dict:
        return self.jobs.view(self._job(job_id))

    def list_jobs(self, company: str | None = None, limit: int = 20) -> list[dict]:
        jobs = [j for j in self.jobs.all() if not company or j["company"] == company.lower()]
        return [self.jobs.view(j) for j in jobs[:limit]]

    def job_file(self, job_id: str, kind: str) -> Path:
        """The finished run's workbook or report, as a path to stream."""
        if kind not in KINDS:
            raise ServiceError(400, f"kind must be one of {', '.join(KINDS)}")
        view = self.job_status(job_id)
        result = view.get("result")
        if view["state"] != "succeeded" or not result:
            raise ServiceError(409, f"job {job_id} has no output (state: {view['state']})")
        if kind == "workbook":
            if not result.get("output"):
                raise ServiceError(409, "a dry run writes no workbook; run it without dry_run")
            path = Path(result["output"])
        else:
            path = next((Path(p) for p in result.get("artifacts", []) if p.endswith("_report.md")), None)
            if path is None:
                raise ServiceError(409, "this run produced no report")
        path = path if path.is_absolute() else self.root / path
        if not path.exists():
            raise ServiceError(410, f"{path.name} is no longer available")
        return path

    def download_link(self, job_id: str, kind: str = "workbook") -> dict:
        path = self.job_file(job_id, kind)
        link = self._link("download", f"{job_id}/{kind}", f"/files/{job_id}/{kind}")
        return {"job_id": job_id, "kind": kind, "file": path.name, **link}
