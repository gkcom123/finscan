"""FinScan service: an HTTP API and an MCP server in front of FinScan runs.

The service never reads a filing or touches a figure. It receives uploads into
`inbox/<Company>/`, starts runs (a local process, or an Azure Container Apps Job
execution), reports their outcome from the run's own `result.json`, and serves the
output workbook and report.

    finscan-api                      # start it (uvicorn on $PORT, default 8000)

    REST   GET  /health              GET  /companies
           POST /uploads/{company}   POST /upload-links/{company}
           POST /jobs                GET  /jobs, /jobs/{id}
           GET  /jobs/{id}/download  POST /jobs/{id}/download-link
    MCP    /mcp  (Streamable HTTP)   tools mirror the REST calls
"""
