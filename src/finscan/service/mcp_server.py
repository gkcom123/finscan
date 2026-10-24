"""The MCP server: FinScan's operations as tools an AI agent can discover and call.

Each tool is a thin wrapper over `Service`, so it returns exactly what the matching
REST endpoint returns. The descriptions are what the agent reads to decide which
tool to use — keep them precise.
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from finscan.service.core import Service, ServiceError

INSTRUCTIONS = (
    "FinScan updates a company's Excel financial model from its quarterly filing. "
    "Typical flow: list_companies → create_upload (the user uploads files through the "
    "link) → start_job with dry_run=true → show the preview → only after the user "
    "confirms, start_job with dry_run=false → get_job_status until finished → "
    "get_download_link. Report figures, blank rows and warnings exactly as returned; "
    "never calculate or estimate financial values."
)


def build_mcp(service: Service) -> MCPServer:
    mcp = MCPServer("FinScan", instructions=INSTRUCTIONS)

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ServiceError as error:
            return {"error": str(error), "status": error.status}

    @mcp.tool(description="List companies set up in FinScan, whether each has the files "
                          "it needs (ready), and any run currently active.")
    def list_companies() -> dict:
        return {"companies": call(service.list_companies)}

    @mcp.tool(description="Get a short-lived link where the user uploads a company's "
                          "filing PDF(s) for the quarter (and the model workbook if it "
                          "changed). Uploading a filing moves the previous quarter's to "
                          "past/. Use the company's folder name, e.g. 'Tencent'.")
    def create_upload(company: str) -> dict:
        return call(service.upload_link, company)

    @mcp.tool(description="Start a FinScan run for a company. dry_run=true (the default) "
                          "resolves and validates without writing; run with dry_run=false "
                          "only after the user confirms. period_end (YYYY-MM-DD) is "
                          "optional. Returns a job id; runs take minutes.")
    def start_job(company: str, dry_run: bool = True, period_end: str | None = None) -> dict:
        return call(service.start_job, company, dry_run, period_end)

    @mcp.tool(description="Check a run: state is queued, running, succeeded, refused "
                          "(nothing written by design, with the reason) or failed. When "
                          "finished, 'result' has the period, values written, blank rows "
                          "with reasons, and warnings.")
    def get_job_status(job_id: str) -> dict:
        return call(service.job_status, job_id)

    @mcp.tool(description="List recent runs, newest first, optionally for one company.")
    def list_jobs(company: str | None = None, limit: int = 10) -> dict:
        return {"jobs": call(service.list_jobs, company, limit)}

    @mcp.tool(description="Get a short-lived download link for a finished run's output: "
                          "kind='workbook' (the updated Excel model) or 'report' (the "
                          "audit report). A dry run saves neither; its outcome is in "
                          "get_job_status.")
    def get_download_link(job_id: str, kind: str = "workbook") -> dict:
        return call(service.download_link, job_id, kind)

    return mcp
