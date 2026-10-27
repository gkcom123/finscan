"""The FinScan service: REST API, signed links, jobs and the MCP endpoint.

Runs are replaced by a fake launcher; a test plays the run's part by writing the
job's `result.json`, exactly where `finscan company …` writes it.
"""
from __future__ import annotations

import io
import json
import time

import pytest
from fastapi.testclient import TestClient

from finscan.cli import job_result_path
from finscan.config import Settings
from finscan.runner import RunResult, run_company
from finscan.service import links
from finscan.service.app import create_app
from finscan.service.core import Service

KEY = {"x-api-key": "test-key"}


class FakeLauncher:
    def __init__(self):
        self.started: list[dict] = []
        self.running = True

    def start(self, job):
        self.started.append(job)
        return f"fake:{job['id']}"

    def is_running(self, job):
        return self.running


@pytest.fixture
def env(tmp_path):
    (tmp_path / "inbox").mkdir()
    (tmp_path / "mappings").mkdir()
    settings = Settings(finscan_api_key="test-key", finscan_public_url="http://testserver",
                        finscan_root=str(tmp_path), finscan_link_minutes=5)
    launcher = FakeLauncher()
    service = Service(settings, root=tmp_path, launcher=launcher)
    with TestClient(create_app(settings, service)) as client:
        yield client, service, launcher, tmp_path


def _upload(client, company, *names):
    files = [("files", (n, io.BytesIO(b"%PDF-1.4 x" if n.endswith(".pdf") else b"x"))) for n in names]
    return client.post(f"/uploads/{company}", files=files, headers=KEY)


def _finish(service, job_id, root, **fields):
    """Play the run: write its result.json."""
    result = RunResult(company=fields.pop("company", "acme"), status=fields.pop("status", "written"),
                       exit_code=0, **fields)
    result.save(job_result_path(root, job_id))


# ---- auth ---------------------------------------------------------------------

def test_health_is_public_and_everything_else_needs_the_key(env):
    client, *_ = env
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/companies").status_code == 401
    assert client.get("/companies", headers={"x-api-key": "wrong"}).status_code == 401
    assert client.get("/companies", headers=KEY).status_code == 200


# ---- uploads ------------------------------------------------------------------

def test_an_upload_creates_the_company_and_makes_it_ready(env):
    client, *_ , root = env
    response = _upload(client, "Acme", "acme_3q25.pdf", "Acme Model.xlsx")
    assert response.status_code == 200, response.text
    status = response.json()["status"]
    assert status["ready"] and status["filings"] == ["acme_3q25.pdf"]
    assert status["model"] == "Acme Model.xlsx"
    assert (root / "inbox" / "Acme" / "acme_3q25.pdf").exists()


def test_a_new_quarter_moves_the_previous_filing_to_past(env):
    client, *_ , root = env
    _upload(client, "Acme", "acme_2q25.pdf", "Acme Model.xlsx")
    (root / "inbox" / "Acme" / "acme_2q25.pdf.json").write_text("{}")
    response = _upload(client, "acme", "acme_3q25.pdf")       # case-insensitive folder
    body = response.json()
    assert set(body["archived"]) == {"acme_2q25.pdf", "acme_2q25.pdf.json"}
    assert body["status"]["filings"] == ["acme_3q25.pdf"]
    assert body["status"]["model"] == "Acme Model.xlsx"       # model untouched
    assert list((root / "inbox" / "Acme" / "past").glob("*/acme_2q25.pdf"))


def test_two_filings_for_one_quarter_are_uploaded_together(env):
    client, *_ = env
    body = _upload(client, "KOF", "report.pdf", "release.pdf", "model.xlsm").json()
    assert body["status"]["filings"] == ["release.pdf", "report.pdf"]


@pytest.mark.parametrize("name", ["notes.txt", "acme_v2_output.xlsx", "Acme_updated.xlsx"])
def test_unusable_files_are_refused(env, name):
    client, *_ = env
    assert _upload(client, "Acme", name).status_code == 400


def test_an_unusable_company_name_is_refused(env):
    client, *_ = env
    assert _upload(client, "../etc", "a.pdf").status_code in (400, 404)


# ---- signed upload link ---------------------------------------------------------

def test_the_upload_link_opens_a_page_and_accepts_files(env):
    client, *_ , root = env
    link = client.post("/upload-links/Acme", headers=KEY).json()
    path = link["upload_url"].replace("http://testserver", "")
    page = client.get(path)
    assert page.status_code == 200 and "Upload files for Acme" in page.text
    token = path.split("token=")[1]
    posted = client.post("/upload/Acme", data={"token": token},
                         files=[("files", ("a.pdf", io.BytesIO(b"%PDF")))])
    assert posted.status_code == 200 and "a.pdf" in posted.text
    assert (root / "inbox" / "Acme" / "a.pdf").exists()


def test_a_tampered_or_foreign_link_is_refused(env):
    client, *_ = env
    link = client.post("/upload-links/Acme", headers=KEY).json()["upload_url"]
    token = link.split("token=")[1]
    assert client.get(f"/upload/Acme?token={token}x").status_code == 403
    assert client.get(f"/upload/Other?token={token}").status_code == 403


def test_a_link_expires():
    token, _ = links.sign("s", "upload", "acme", minutes=0)
    time.sleep(1)
    assert not links.verify("s", "upload", "acme", token)


# ---- jobs ---------------------------------------------------------------------

def test_a_company_without_its_files_cannot_start(env):
    client, *_ = env
    _upload(client, "Acme", "acme.pdf")                        # no model
    response = client.post("/jobs", json={"company": "Acme"}, headers=KEY)
    assert response.status_code == 409 and "no model workbook" in response.json()["error"]
    assert client.post("/jobs", json={"company": "Nobody"}, headers=KEY).status_code == 404


def test_a_job_runs_and_reports_its_result(env):
    client, service, launcher, root = env
    _upload(client, "Acme", "acme.pdf", "model.xlsx")
    started = client.post("/jobs", json={"company": "Acme", "dry_run": True}, headers=KEY)
    assert started.status_code == 202
    job = started.json()
    assert job["state"] == "running" and job["dry_run"] is True
    assert launcher.started[0]["company"] == "acme"

    _finish(service, job["id"], root, status="dry_run", values_resolved=23, blank_count=2)
    done = client.get(f"/jobs/{job['id']}", headers=KEY).json()
    assert done["state"] == "succeeded" and done["result"]["values_resolved"] == 23


def test_one_active_run_per_company(env):
    client, *_ = env
    _upload(client, "Acme", "acme.pdf", "model.xlsx")
    assert client.post("/jobs", json={"company": "Acme"}, headers=KEY).status_code == 202
    second = client.post("/jobs", json={"company": "acme"}, headers=KEY)
    assert second.status_code == 409 and "already active" in second.json()["error"]


def test_a_run_that_ended_without_a_result_is_failed(env):
    client, _, launcher, _ = env
    _upload(client, "Acme", "acme.pdf", "model.xlsx")
    job = client.post("/jobs", json={"company": "Acme"}, headers=KEY).json()
    launcher.running = False
    view = client.get(f"/jobs/{job['id']}", headers=KEY).json()
    assert view["state"] == "failed" and "without a result" in view["error"]


def test_a_refused_run_is_reported_as_refused(env):
    client, service, _, root = env
    _upload(client, "Acme", "acme.pdf", "model.xlsx")
    job = client.post("/jobs", json={"company": "Acme"}, headers=KEY).json()
    _finish(service, job["id"], root, status="refused", refused="the filings are not for the same period")
    view = client.get(f"/jobs/{job['id']}", headers=KEY).json()
    assert view["state"] == "refused" and "same period" in view["result"]["refused"]


def test_the_output_can_be_downloaded_with_the_key_or_a_signed_link(env):
    client, service, _, root = env
    _upload(client, "Acme", "acme.pdf", "model.xlsx")
    job = client.post("/jobs", json={"company": "Acme", "dry_run": False}, headers=KEY).json()
    out = root / "inbox" / "acme_v2_output.xlsx"
    out.write_bytes(b"workbook")
    report = root / "_work" / "acme_2025-09-30_report.md"
    report.parent.mkdir(exist_ok=True)
    report.write_text("# report")
    _finish(service, job["id"], root, output=str(out), artifacts=[str(report)])

    assert client.get(f"/jobs/{job['id']}/download", headers=KEY).content == b"workbook"
    link = client.post(f"/jobs/{job['id']}/download-link?kind=report", headers=KEY).json()
    assert link["file"] == report.name
    assert client.get(link["url"].replace("http://testserver", "")).text == "# report"
    assert client.get(f"/files/{job['id']}/report?token=bad").status_code == 403


def test_a_dry_run_has_no_workbook_to_download(env):
    client, service, _, root = env
    _upload(client, "Acme", "acme.pdf", "model.xlsx")
    job = client.post("/jobs", json={"company": "Acme"}, headers=KEY).json()
    _finish(service, job["id"], root, status="dry_run")
    response = client.get(f"/jobs/{job['id']}/download", headers=KEY)
    assert response.status_code == 409 and "dry run" in response.json()["error"]


# ---- MCP ------------------------------------------------------------------------

def _rpc(client, method, params=None, id_=1):
    response = client.post("/mcp", headers={**KEY, "accept": "application/json, text/event-stream",
                                            "content-type": "application/json"},
                           json={"jsonrpc": "2.0", "id": id_, "method": method,
                                 "params": params or {}})
    assert response.status_code == 200, response.text
    return response.json()


def test_mcp_lists_the_tools_and_calls_one(env):
    client, *_ = env
    _upload(client, "Acme", "acme.pdf", "model.xlsx")
    init = _rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "test", "version": "1"}})
    assert init["result"]["serverInfo"]["name"] == "FinScan"
    tools = {t["name"] for t in _rpc(client, "tools/list", id_=2)["result"]["tools"]}
    assert tools == {"list_companies", "create_upload", "start_job", "get_job_status",
                     "list_jobs", "get_download_link"}
    called = _rpc(client, "tools/call", {"name": "list_companies", "arguments": {}}, id_=3)
    text = called["result"]["content"][0]["text"]
    assert json.loads(text)["companies"][0]["company"] == "acme"


def test_mcp_needs_the_key(env):
    client, *_ = env
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401


# ---- runner ----------------------------------------------------------------------

def test_an_unknown_company_is_a_failed_result_not_an_exception(tmp_path):
    (tmp_path / "inbox").mkdir()
    result = run_company("nobody", root=tmp_path)
    assert result.status == "failed" and result.exit_code == 1
    assert "No folder inbox/nobody" in result.errors[0]


# ---- Azure launcher (no subscription needed: the client is a stand-in) -------------

def test_the_azure_launcher_keeps_the_jobs_container_and_changes_only_the_run():
    """A run's container definition replaces the Job's for that execution, so it must
    carry the Job's command, environment (the secret model key) and resources."""
    from types import SimpleNamespace

    from azure.mgmt.appcontainers.models import EnvironmentVar

    from finscan.service.jobs import AzureJobLauncher

    calls = {}
    secret_key = EnvironmentVar(name="OPENAI_API_KEY", secret_ref="openai-key")
    job_container = SimpleNamespace(
        name="finscan", image="acr.io/finscan:1", command=["finscan"], resources={"cpu": 1},
        env=[secret_key, EnvironmentVar(name="FINSCAN_JOB_ID", value="stale")])

    class Poller:
        def result(self):
            return SimpleNamespace(name="finscan-job-abc12")

    class Jobs:
        def get(self, rg, job):
            return SimpleNamespace(properties=SimpleNamespace(
                template=SimpleNamespace(containers=[job_container])))

        def begin_start(self, rg, job, template):
            calls.update(rg=rg, job=job, template=template)
            return Poller()

    states = iter(["Running", "Succeeded"])

    class Client:
        jobs = Jobs()

        def job_execution(self, rg, job, name):
            return SimpleNamespace(properties=SimpleNamespace(status=next(states)))

    launcher = object.__new__(AzureJobLauncher)
    launcher.s = SimpleNamespace(azure_resource_group="finscan-rg", azure_job_name="finscan-job",
                                 azure_job_image="")
    launcher.client = Client()

    job = {"id": "j1", "company": "tencent", "dry_run": True, "period_end": "2025-09-30"}
    handle = launcher.start(job)
    container = calls["template"].containers[0]
    assert (calls["rg"], calls["job"], handle) == ("finscan-rg", "finscan-job", "finscan-job-abc12")
    assert container.command == ["finscan"] and container.image == "acr.io/finscan:1"
    assert container.args == ["company", "tencent", "--dry-run", "--period-end", "2025-09-30"]
    assert [(e.name, e.value, e.secret_ref) for e in container.env] == [
        ("OPENAI_API_KEY", None, "openai-key"), ("FINSCAN_JOB_ID", "j1", None)]
    assert container.resources == {"cpu": 1}
    job["handle"] = handle
    assert launcher.is_running(job) is True
    assert launcher.is_running(job) is False


def test_the_azure_launcher_names_its_missing_settings():
    from types import SimpleNamespace

    from finscan.service.jobs import AzureJobLauncher

    with pytest.raises(RuntimeError, match="AZURE_SUBSCRIPTION_ID"):
        AzureJobLauncher(SimpleNamespace(azure_subscription_id="", azure_resource_group="rg",
                                         azure_job_name="j"))
