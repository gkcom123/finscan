"""Jobs: one FinScan run for one company, started and tracked by the service.

A job is a folder `_work/jobs/<id>/` holding `job.json` (what was asked, how it was
started) and, once the run ends, the run's own `result.json` (written by
`finscan company … ` because FINSCAN_JOB_ID is set). Keeping both on the project's
file share means the API and the run agree without a database.

State comes from the run's result whenever it exists; until then from the launcher:

    queued → running → succeeded (result: written / dry_run)
                     → refused   (result: nothing written, by design — e.g. wrong period)
                     → failed    (result: error, or the run ended without a result)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from finscan.cli import JOB_ID_ENV, job_result_path

ACTIVE = ("queued", "running")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Launcher(Protocol):
    def start(self, job: dict) -> str:
        """Start the run; return a handle the launcher can ask about later."""

    def is_running(self, job: dict) -> bool | None:
        """True while running, False once ended, None if it cannot tell."""


class LocalLauncher:
    """Runs `finscan company …` as a process on this machine (for local testing)."""

    def __init__(self, root: Path):
        self.root = root
        self._procs: dict[str, subprocess.Popen] = {}

    def start(self, job: dict) -> str:
        log = open(self.root / "_work" / "jobs" / job["id"] / "run.log", "wb")
        proc = subprocess.Popen(
            [sys.executable, "-m", "finscan.cli", "company", job["company"], *_flags(job)],
            cwd=self.root, stdout=log, stderr=subprocess.STDOUT,
            env={**os.environ, JOB_ID_ENV: job["id"]})
        self._procs[job["id"]] = proc
        return f"pid:{proc.pid}"

    def is_running(self, job: dict) -> bool | None:
        proc = self._procs.get(job["id"])
        if proc is not None:
            return proc.poll() is None
        pid = int(str(job.get("handle", "pid:0")).split(":")[1] or 0)
        if not pid:
            return None
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


class AzureJobLauncher:
    """Starts an execution of the FinScan Container Apps Job, one per run.

    A run's container definition REPLACES the Job's for that execution, so it is
    built from the Job's own: same name, image, command, environment (including
    secret references such as the model key) and resources — with only the
    arguments (`company <name> …`) and FINSCAN_JOB_ID changed. Volumes stay the
    Job's. Authenticates with the service's managed identity
    (DefaultAzureCredential; set AZURE_CLIENT_ID for a user-assigned identity),
    which needs permission to read and start the Job.
    """

    def __init__(self, settings):
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.appcontainers import ContainerAppsAPIClient

        missing = [n for n in ("azure_subscription_id", "azure_resource_group", "azure_job_name")
                   if not getattr(settings, n)]
        if missing:
            raise RuntimeError("FINSCAN_RUNNER=azure needs " + ", ".join(m.upper() for m in missing))
        self.s = settings
        self.client = ContainerAppsAPIClient(DefaultAzureCredential(), settings.azure_subscription_id)

    def start(self, job: dict) -> str:
        from azure.mgmt.appcontainers.models import (EnvironmentVar, JobExecutionContainer,
                                                     JobExecutionTemplate)

        definition = self.client.jobs.get(self.s.azure_resource_group, self.s.azure_job_name)
        base = definition.properties.template.containers[0]
        env = [e for e in (base.env or []) if e.name != JOB_ID_ENV]
        container = JobExecutionContainer(
            name=base.name, image=self.s.azure_job_image or base.image,
            command=base.command or ["finscan"],
            args=["company", job["company"], *_flags(job)],
            env=[*env, EnvironmentVar(name=JOB_ID_ENV, value=job["id"])],
            resources=base.resources)
        poller = self.client.jobs.begin_start(
            self.s.azure_resource_group, self.s.azure_job_name,
            template=JobExecutionTemplate(containers=[container]))
        return poller.result().name

    def is_running(self, job: dict) -> bool | None:
        execution = self.client.job_execution(
            self.s.azure_resource_group, self.s.azure_job_name, job["handle"])
        status = str(getattr(getattr(execution, "properties", None), "status", "") or "")
        if status in ("Running", "Processing"):
            return True
        if status in ("Succeeded", "Failed", "Stopped", "Degraded"):
            return False
        return None


def _flags(job: dict) -> list[str]:
    flags = ["--dry-run"] if job.get("dry_run") else []
    if job.get("period_end"):
        flags += ["--period-end", job["period_end"]]
    return flags


class CompanyBusy(RuntimeError):
    """A run for this company is already active."""


class JobStore:
    """Jobs on disk, with one active run per company at a time."""

    def __init__(self, root: Path, launcher: Launcher):
        self.root = root
        self.dir = root / "_work" / "jobs"
        self.launcher = launcher
        self._lock = threading.Lock()

    def _path(self, job_id: str) -> Path:
        return self.dir / job_id / "job.json"

    def _save(self, job: dict) -> dict:
        path = self._path(job["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(job, indent=2), encoding="utf-8")
        return job

    def load(self, job_id: str) -> dict | None:
        if not job_id.replace("-", "").isalnum():
            return None
        path = self._path(job_id)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def all(self) -> list[dict]:
        if not self.dir.is_dir():
            return []
        jobs = [json.loads(p.read_text(encoding="utf-8")) for p in self.dir.glob("*/job.json")]
        return sorted(jobs, key=lambda j: j["created"], reverse=True)

    def start(self, company: str, *, dry_run: bool, period_end: str | None) -> dict:
        with self._lock:
            for job in self.all():
                if job["company"] == company and self.view(job)["state"] in ACTIVE:
                    raise CompanyBusy(f"a run for {company} is already active (job {job['id']})")
            job = self._save({"id": uuid.uuid4().hex[:12], "company": company,
                              "dry_run": dry_run, "period_end": period_end,
                              "created": _now(), "state": "queued", "handle": None})
        try:
            job["handle"] = self.launcher.start(job)
            job["state"] = "running"
        except Exception as error:
            job["state"], job["error"] = "failed", f"could not start: {error}"
        return self._save(job)

    def result(self, job: dict) -> dict | None:
        path = job_result_path(self.root, job["id"])
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def view(self, job: dict) -> dict:
        """The job as callers see it: its state, and the run's outcome when there is one."""
        out = {k: job.get(k) for k in ("id", "company", "dry_run", "period_end", "created")}
        result = self.result(job)
        if result is not None:
            state = {"written": "succeeded", "dry_run": "succeeded",
                     "refused": "refused"}.get(result["status"], "failed")
            return {**out, "state": state, "result": result}
        if job.get("state") == "failed":
            return {**out, "state": "failed", "error": job.get("error")}
        running = self.launcher.is_running(job) if job.get("handle") else None
        if running is False:
            return {**out, "state": "failed",
                    "error": "the run ended without a result; see its log"}
        return {**out, "state": job.get("state", "queued")}
