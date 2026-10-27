"""The FinScan service: REST API + MCP server in one FastAPI app.

Run locally:   FINSCAN_API_KEY=... finscan-api          (http://localhost:8000)
Docs:          /docs (OpenAPI)        MCP: /mcp (Streamable HTTP)

Every endpoint except /health, the docs, and the signed-link pages requires the
header `x-api-key: <FINSCAN_API_KEY>`. Signed links (upload page, file downloads)
carry their own short-lived token instead, so they can be handed to a person.
"""
from __future__ import annotations

import html
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from mcp.server.transport_security import TransportSecuritySettings

from finscan.config import settings as default_settings
from finscan.service.core import Service, ServiceError
from finscan.service.mcp_server import build_mcp

#: Reachable without the API key: health, docs, and pages that check a signed token.
PUBLIC_PREFIXES = ("/health", "/docs", "/openapi.json", "/upload/", "/files/")


def create_app(settings=None, service: Service | None = None) -> FastAPI:
    settings = settings or default_settings
    service = service or Service(settings)
    mcp = build_mcp(service)
    hosts = [h.strip() for h in settings.finscan_allowed_hosts.split(",") if h.strip()]
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=bool(hosts), allowed_hosts=hosts,
            allowed_origins=[f"https://{h}" for h in hosts] + [f"http://{h}" for h in hosts]))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="FinScan", version="1.0", lifespan=lifespan,
                  description="Updates Excel financial models from quarterly filings.")
    app.state.service = service

    @app.middleware("http")
    async def require_api_key(request: Request, call_next):
        path = request.url.path
        if path == "/" or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)
        if not settings.finscan_api_key:
            return JSONResponse({"error": "FINSCAN_API_KEY is not set on the server"}, 503)
        if request.headers.get("x-api-key") != settings.finscan_api_key:
            return JSONResponse({"error": "missing or wrong x-api-key"}, 401)
        return await call_next(request)

    @app.exception_handler(ServiceError)
    async def service_error(_request: Request, error: ServiceError):
        return JSONResponse({"error": str(error)}, error.status)

    # ---- REST ------------------------------------------------------------
    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "runner": settings.finscan_runner,
                "api_key_set": bool(settings.finscan_api_key)}

    @app.get("/companies")
    def companies() -> dict:
        return {"companies": service.list_companies()}

    @app.get("/companies/{company}")
    def company(company: str) -> dict:
        return service.company(company)

    @app.post("/upload-links/{company}")
    def upload_link(company: str) -> dict:
        return service.upload_link(company)

    @app.post("/uploads/{company}")
    def upload(company: str, files: list[UploadFile] = File(...)) -> dict:
        return service.upload(company, [(f.filename, f.file) for f in files])

    @app.post("/jobs", status_code=202)
    def start_job(company: str = Body(...), dry_run: bool = Body(True),
                  period_end: str | None = Body(None)) -> dict:
        return service.start_job(company, dry_run, period_end)

    @app.get("/jobs")
    def jobs(company: str | None = None, limit: int = Query(20, le=200)) -> dict:
        return {"jobs": service.list_jobs(company, limit)}

    @app.get("/jobs/{job_id}")
    def job(job_id: str) -> dict:
        return service.job_status(job_id)

    @app.get("/jobs/{job_id}/download")
    def download(job_id: str, kind: str = "workbook") -> FileResponse:
        path = service.job_file(job_id, kind)
        return FileResponse(path, filename=path.name)

    @app.post("/jobs/{job_id}/download-link")
    def download_link(job_id: str, kind: str = "workbook") -> dict:
        return service.download_link(job_id, kind)

    # ---- signed-link pages (no API key; the token is the permission) -------
    @app.get("/files/{job_id}/{kind}")
    def signed_download(job_id: str, kind: str, token: str = "") -> FileResponse:
        service.check_link("download", f"{job_id}/{kind}", token)
        path = service.job_file(job_id, kind)
        return FileResponse(path, filename=path.name)

    @app.get("/upload/{company}", response_class=HTMLResponse)
    def upload_page(company: str, token: str = "") -> str:
        service.check_link("upload", company.lower(), token)
        return _page(company, f"/upload/{company}", token)

    @app.post("/upload/{company}", response_class=HTMLResponse)
    def upload_from_page(company: str, token: str = Form(""),
                         files: list[UploadFile] = File(...)) -> str:
        service.check_link("upload", company.lower(), token)
        done = service.upload(company, [(f.filename, f.file) for f in files])
        saved = "".join(f"<li>{html.escape(s['file'])} ({s['kind']})</li>" for s in done["saved"])
        ready = done["status"]
        note = ("Ready — go back to the chat and start the run." if ready["ready"]
                else "Not ready yet: " + html.escape("; ".join(ready["problems"])))
        return _shell(f"<h1>Uploaded for {html.escape(company)}</h1><ul>{saved}</ul><p>{note}</p>")

    # ---- MCP: its route joins the app; its session manager runs in the lifespan ----
    for route in mcp_app.routes:
        app.router.routes.append(route)
    return app


def _shell(body: str) -> str:
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<title>FinScan upload</title><style>body{font-family:system-ui,sans-serif;"
            "max-width:36rem;margin:3rem auto;padding:0 1rem;line-height:1.5}"
            "button{padding:.5rem 1.2rem}</style>" + body)


def _page(company: str, action: str, token: str) -> str:
    return _shell(
        f"<h1>Upload files for {html.escape(company)}</h1>"
        "<p>Choose this quarter's filing PDF(s) — upload a report and its press release "
        "together if the company publishes both. Add the model workbook only if it "
        "changed. Previous files are kept in <code>past/</code>.</p>"
        f"<form method=post enctype=multipart/form-data action='{html.escape(action)}'>"
        f"<input type=hidden name=token value='{html.escape(token)}'>"
        "<p><input type=file name=files multiple required "
        "accept='.pdf,.xlsx,.xlsm,.json'></p><button type=submit>Upload</button></form>")


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))

