# FinModelUpdater (FinScan 2) — Azure Deployment and Foundry Invocation

How to run FinScan 2 in Azure as a container, and how an Azure AI Foundry agent
invokes it.

> **Status.** FinScan 2 today is a command-line tool (`finscan2 company <name>` /
> `finscan2 companies`). The Dockerfile, job definition, Job API and OpenAPI spec
> described below are **not yet in the repository** — this document is the plan
> for building them.

Related: [finscan2_architecture.md](finscan2_architecture.md) §4, §16–25.

---

## 1. Architecture

### Phase A — batch job (no code changes)

FinScan 2 has no web server, so the Azure shape is a **Container Apps Job**: a
container that starts, runs `finscan2 companies` (or one company), writes outputs
and exits. Mounting Azure file shares where `inbox/` and `_work/` would be keeps
the existing folder conventions.

```
Azure Container Registry ── image: finscan2 (code + reviewed mappings)
        │
Container Apps Job (manual or scheduled) ── runs: finscan2 companies
        │  env: FINSCAN_LLM_PROVIDER, model key from a secret / Key Vault
        ├─ /app/inbox  ← Azure Files share: PDFs + models in, *_v2_output.* out
        └─ /app/_work  ← Azure Files share: PDF read cache, values + reports
```

- **Mappings are baked into the image.** They are reviewed, git-tracked files; the
  image version then records exactly which mappings a run used.
- **`_work` must be persistent.** It holds the PDF read cache (keyed by file
  hash). Without it every run re-reads every PDF and pays for the LLM calls again.
- **No system packages needed.** OCR of scanned pages uses the vision model, so
  the image is plain Python (`pdfplumber`, `openpyxl`, `langchain-openai`).
- **Supplements travel with the PDF.** Where a company uses a
  `<pdf>.pdf.supplement.json` (e.g. Grupo Bimbo), upload it beside the PDF.

---

## 2. Deployment steps

### 2.1 Dockerfile

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY mappings ./mappings
RUN pip install --no-cache-dir .
ENTRYPOINT ["finscan2"]
CMD ["companies"]
```

`.dockerignore`:

```
inbox/
_work/
.venv/
.env
**/__pycache__/
```

### 2.2 Registry and image

```bash
RG=finscan-rg; LOC=uksouth; ACR=finscanacr01
az group create -n $RG -l $LOC
az acr create -n $ACR -g $RG --sku Basic
az acr build -r $ACR -t finscan2:1.0 .
```

### 2.3 Storage — one account, two file shares

```bash
SA=finscanstore01
az storage account create -n $SA -g $RG -l $LOC --sku Standard_LRS
az storage share-rm create --storage-account $SA -n finscan-inbox
az storage share-rm create --storage-account $SA -n finscan-work
```

### 2.4 Container Apps environment with the shares registered

```bash
az containerapp env create -n finscan-env -g $RG -l $LOC
KEY=$(az storage account keys list -n $SA -g $RG --query [0].value -o tsv)
for s in inbox work; do
  az containerapp env storage set -n finscan-env -g $RG --storage-name $s \
    --azure-file-account-name $SA --azure-file-account-key $KEY \
    --azure-file-share-name finscan-$s --access-mode ReadWrite
done
```

### 2.5 The job

Volume mounts are set through YAML: `az containerapp job create --yaml job.yaml`.

| Setting | Value |
|---|---|
| Image | `<acr>.azurecr.io/finscan2:1.0` |
| Args | `companies` (default) |
| Volumes | `inbox` → `/app/inbox`, `work` → `/app/_work` |
| Secret | `openai-key` (or a Key Vault reference) |
| Env vars | `FINSCAN_LLM_PROVIDER=openai`, `OPENAI_MODEL=gpt-5.4`, `OPENAI_API_KEY=secretref:openai-key` — or the `AZURE_OPENAI_*` set for Azure OpenAI |
| Replica timeout | ~60 minutes (new PDFs read slowly; cached ones are fast) |
| Trigger | Manual (or a schedule) |

### 2.6 Each quarter

1. Upload filings and models into `finscan-inbox/<Company>/`
   (`az storage file upload-batch`). Keep only the quarter being written in the
   company folder; older filings go in `past/`.
2. Start the job: `az containerapp job start -n finscan2-job -g $RG`.
   For a single company, start with overridden args `company <name>`
   (e.g. *Run with overrides* in the portal).
3. Download `inbox/<company>_v2_output.*` and the audit files in `_work/`
   (`<company>_<period>_values.json`, `<company>_<period>_report.md`).

---

## 3. Invoking from Azure AI Foundry

A Foundry agent cannot start a container job directly; it calls **tools** — an
OpenAPI-described HTTP API, an Azure Function, or an MCP server. So a thin **Job
API** sits in front of the Container Apps Job. Foundry is only the conversational
front end.

### 3.1 Flow

```
Analyst (Foundry playground / Teams)
   │  "Update Tencent for Q3"
   ▼
Foundry agent ── OpenAPI tool ──►  FinScan Job API  (small Container App, HTTPS)
                                     │ POST /jobs {company:"tencent"}
                                     │   └─ starts a Container Apps Job execution
                                     │      with args: company tencent
                                     ▼
                                   finscan2 worker (the Container Apps Job, §2)
                                     │ reads inbox/Tencent/, writes
                                     ▼ tencent_v2_output.xlsx + _work/tencent_<period>_report.md
Agent polls GET /jobs/{id} ──► state, rows written / blank, warnings, download link
   ▼
Analyst gets: "3Q25 written to Model!AH: 23 values, 13 blank (reasons…), 0 errors"
              + workbook link
```

### 3.2 Job API (to build)

A small FastAPI app (~200 lines) deployed as a Container App. Its managed
identity may start the job and read the shares.

| Endpoint | Purpose |
|---|---|
| `GET /companies` | `inbox/` folders that are ready (filing + model present) |
| `POST /jobs` `{company, period_end?}` | Starts a job execution with args `company <name>`; returns `job_id` |
| `GET /jobs/{job_id}` | State (Running / Succeeded / Failed) plus a summary from the run's report: values written, blank rows and why, warnings |
| `GET /jobs/{job_id}/download` | Time-limited download link for the output workbook |

The API starts executions through the Container Apps management API (overriding
the container args per request) and reads execution status from it.

### 3.3 Foundry agent configuration

- **Tool:** OpenAPI, using the Job API's OpenAPI spec. Authentication by API key
  or, preferably, managed identity.
- **Instructions** (architecture doc rule): *"Start a job, poll until it finishes,
  then repeat the figures, blank rows and warnings exactly as the API returns
  them. Never calculate, estimate or reinterpret financial values."*
- **Optional:** publish the agent to Teams.

### 3.4 Things to know

- **Runs take minutes**, longer for a newly uploaded PDF. The API is asynchronous:
  the agent starts a job and polls; it never waits on one call.
- **Files arrive before the run.** The PDF (and supplement, where used) is placed
  in the company folder first. "Latest filing" means the newest approved period,
  not merely the newest upload (architecture doc §18).
- **Mappings change only through git review**, never from chat (architecture doc
  §5). The agent may report rows needing review; a person edits and commits the
  mapping, and a new image is built.

---

## 4. Production hardening (Phase B — requires code work)

- Replace the storage-key mount with **managed identity**; keep the model key in
  **Key Vault**.
- Move from file shares to **Blob Storage**, with the Job API and a **Service Bus**
  queue (one job per request when many analysts use it).
- Put **API Management + Entra ID** in front of the Job API. The Foundry agent gets
  API access only — never direct Storage or Key Vault access.
- Send logs and job metrics to **Application Insights**.
- **CI/CD:** GitHub Actions with OIDC — run tests, build the image, push to ACR,
  deploy to dev, smoke-test a representative filing, promote the immutable image.
  Record the image version and mapping commit in each job's manifest.

---

## 5. Build checklist

| # | Item | State |
|---|---|---|
| 1 | `Dockerfile`, `.dockerignore` | to build |
| 2 | `job.yaml` (Container Apps Job with volume mounts) | to build |
| 3 | `deploy.sh` (commands in §2) | to build |
| 4 | Job API (FastAPI) + OpenAPI spec | to build |
| 5 | Foundry agent with OpenAPI tool and instructions | to configure |
| 6 | Managed identity, Key Vault, Blob, Service Bus, APIM, App Insights | Phase B |
