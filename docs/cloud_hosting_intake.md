# FinScan — Cloud Hosting Discovery Intake

Purpose: minimum hosting/environment details for the Cloud Hosting team to begin
discovery and planning. Not a deployment guide — see
[azure_foundry_teams_deployment.md](azure_foundry_teams_deployment.md) and
[infra/main.bicep](../infra/main.bicep) for the existing (draft/reference) IaC.

## 1. What is being hosted

- **Workload**: stateless HTTPS API (FastAPI, `src/finscan/api/main.py`) that accepts
  a PDF + Excel workbook upload, extracts financial figures via an LLM, and returns
  an updated workbook. A second, non-networked CLI tool (`src/finscan2`) also exists
  for batch/offline runs — no hosting needed for that today.
- **Consumers**: Microsoft Copilot Studio custom connector and/or an Azure AI Foundry
  Agent published to Teams (see `docs/copilot_studio_setup.md`,
  `docs/azure_foundry_teams_deployment.md`). Both call the same HTTP API.
- **Current status**: no production deployment yet. `infra/main.bicep` is a working
  draft (Container Apps + supporting resources), not yet deployed to a real subscription.

## 2. Container / runtime

- Docker image, base `python:3.11-slim` ([Dockerfile](../Dockerfile)), built from repo root.
- Runs `uvicorn finscan.api.main:app --host 0.0.0.0 --port 8080`.
- Single process, no background workers/queues — extraction happens synchronously
  inside the HTTP request.
- Reference sizing in the draft Bicep: **1.0 vCPU / 2 GiB memory** per instance
  (not load-tested; treat as a starting point only).
- Health check: `GET /health` → `{"status":"ok","llm_configured":true}`.

## 3. Networking

- Inbound: single HTTPS ingress on port 8080, external (public) — called by
  Copilot Studio / Foundry Agent Service, both outside the Azure tenant's private network.
- Outbound: HTTPS calls to an LLM endpoint — either public OpenAI API or an
  Azure OpenAI resource (same tenant or cross-tenant, TBD by whoever owns the
  Foundry project).
- No inbound network dependency besides the two callers above; no database.

## 4. State / persistent storage

- **Must be persistent, must survive restarts**: `profiles/` directory — one JSON
  file per onboarded company recording its confirmed Excel layout. Losing this
  forces re-confirming every company's layout by a human. This is the single most
  important piece of state in the system.
- `_work/` (`FINSCAN_WORK_DIR`) — scratch space for in-flight job artifacts
  (uploaded files, generated outputs), also wants to persist across restarts for
  in-progress jobs, but is not precious long-term data.
- Job status/metadata itself is currently kept **in-process memory only**
  (`_JOBS: dict` in `main.py`) — not persisted, not shared across replicas. This
  means: no horizontal auto-scaling beyond 1 replica without a code change, and a
  restart loses in-flight (not-yet-applied) job state. Flag this to the app team if
  the hosting plan assumes multi-replica or zero-downtime restarts.
- Draft Bicep mounts an Azure Files share (100 GiB quota, arbitrary starting size)
  for `profiles/` + work dir via Container Apps' Azure Files integration.

## 5. Secrets

- `FINSCAN_API_KEY` — static shared secret checked on the `x-api-key` header for
  every API caller (Copilot Studio / Foundry Agent). App-level auth only, no OAuth/AAD today.
- LLM credentials — either `OPENAI_API_KEY`, or `AZURE_OPENAI_API_KEY` +
  `AZURE_OPENAI_ENDPOINT` (Azure OpenAI path is what's used in the draft Bicep).
- Recommended handling: Key Vault + managed identity (already modeled in the draft
  Bicep) rather than plain container env vars.

## 6. Environment variables (from [.env.example](../.env.example))

| Variable | Purpose | Sensitive? |
|---|---|---|
| `FINSCAN_LLM_PROVIDER` | `openai` or `azure` | no |
| `OPENAI_API_KEY` / `OPENAI_MODEL` / `OPENAI_BASE_URL` | used if provider=openai | key = yes |
| `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_API_VERSION` / `AZURE_OPENAI_DEPLOYMENT` | used if provider=azure | key = yes |
| `FINSCAN_API_KEY` | inbound API auth | yes |
| `FINSCAN_PROFILE_STORE` | path to persistent profiles dir | no |
| `FINSCAN_WORK_DIR` | path to scratch/output dir | no |
| `FINSCAN_REQUIRE_CONFIRMATION`, `FINSCAN_OCR_FALLBACK`, `FINSCAN_MAX_PDF_CHARS`, `FINSCAN_FUZZY_THRESHOLD`, `FINSCAN_TOLERANCE_PCT`, `FINSCAN_MIN_CONFIDENCE` | extraction tuning, no infra impact | no |
| `FINSCAN_INBOX_DIR`, `FINSCAN_TEST_DIR`, `FINSCAN_DEMO_ENABLED` | CLI/local-demo only, not used by the hosted API path | no |

## 7. External dependencies

- An LLM endpoint with function-calling + long-context support (`gpt-4o` or
  equivalent). If Azure OpenAI: needs quota for a `gpt-4o` deployment in a
  supported region (e.g. `eastus2`, `swedencentral`).
- No other external services (no database, no message queue, no cache).

## 8. Observability (as modeled in the draft Bicep)

- Log Analytics workspace + Application Insights, wired to the Container App's
  `appLogsConfiguration`.

## 9. Data sensitivity

- Inputs/outputs are company earnings-release PDFs and financial Excel models —
  potentially confidential/market-sensitive data pre-publication. Flag for whatever
  data-handling/classification policy applies to non-public financial information
  before choosing region, storage redundancy, and access controls.

## 10. Open questions for the hosting team

- Target subscription/tenant, region, and naming convention (draft Bicep uses
  ad hoc `finscan-<env>` naming).
- Whether multi-replica scaling is needed now or later (currently blocked by the
  in-memory job store — see §4).
- Whether the LLM call should go through an existing shared Azure OpenAI resource
  or a new one dedicated to this app.
- Network exposure requirements: fully public ingress vs. restricted to
  Copilot Studio/Foundry service tags or a private endpoint.
