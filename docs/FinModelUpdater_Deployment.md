# FinModelUpdater (FinScan) on Azure — Beginner's Deployment Guide

How FinScan is deployed in Azure, how analysts use it from Copilot Studio and Teams,
and why each piece was chosen. Written for someone new to Azure: each section starts
in plain words, then gives the Azure term.

> **Status.** Built and tested locally: the run result (`result.json`), the FastAPI
> service with its MCP endpoint (`finscan-api`), the local and Azure job launchers,
> and the Docker image (both roles tested in containers). **Not yet done:** the
> Azure resources (Bicep), the Copilot Studio agent and Teams publishing, and a run
> of the Azure launcher against a real subscription. Section 14 tracks each item;
> [RUNNING.md](RUNNING.md) shows how to run the service locally.

**Decisions made for the first cut**

| Topic | Choice |
|---|---|
| Where FinScan runs | Azure **Container Apps Job** |
| How analysts trigger it | **Copilot Studio** agent, published to **Teams** |
| How the agent talks to FinScan | An **MCP server** inside a small **FastAPI** app |
| How files get in and out | **Upload through the API**; output comes back as a **download link** |
| Model that reads PDFs | **Azure OpenAI** deployed in **Azure AI Foundry** (Claude later) |
| Security for the pilot | **API key**; **Entra ID** sign-in later |

Related: [finscan_architecture.md](finscan_architecture.md), [RUNNING.md](RUNNING.md).

---

## Contents

1. [The big picture](#1-the-big-picture)
2. [Azure basics for beginners](#2-azure-basics-for-beginners)
3. [Why a Container Apps Job, not a service](#3-why-a-container-apps-job-not-a-service)
4. [Do we need a FastAPI app? An MCP server? How do we know a job finished?](#4-do-we-need-a-fastapi-app-an-mcp-server-how-do-we-know-a-job-finished)
5. [Uploading files from Copilot Studio](#5-uploading-files-from-copilot-studio)
6. [Azure AI Foundry — what it is and how we use it](#6-azure-ai-foundry--what-it-is-and-how-we-use-it)
7. [Connecting to Copilot Studio and Teams](#7-connecting-to-copilot-studio-and-teams)
8. [Option: SharePoint instead of the inbox folder](#8-option-sharepoint-instead-of-the-inbox-folder)
9. [API Management (APIM) — what, where, why](#9-api-management-apim--what-where-why)
10. [What you need in the Azure portal and tenant](#10-what-you-need-in-the-azure-portal-and-tenant)
11. [Resource list and cost drivers](#11-resource-list-and-cost-drivers)
12. [Suggestions that fit this design](#12-suggestions-that-fit-this-design)
13. [Step-by-step deployment (first cut)](#13-step-by-step-deployment-first-cut)
14. [Build backlog](#14-build-backlog)
15. [Glossary](#15-glossary)

---

## 1. The big picture

An analyst in Teams asks the FinScan agent to update a company's model. The agent
calls FinScan's API, the API starts a FinScan run in Azure, the run reads the PDF
(with help from an AI model), writes the new column into a copy of the Excel model,
and the analyst gets a summary and a download link.

```
 Analyst in Teams
     │  "Update Tencent's model for 3Q25"
     ▼
 Copilot Studio agent  ──(MCP tools, API key)──►  FinScan API  (Container App, always on)
                                                    │  • receives uploads
                                                    │  • starts a run, tracks it
                                                    │  • serves the output download
                                                    ▼
                                         FinScan Job  (Container Apps Job, runs then stops)
                                                    │  finscan company tencent
                                     ┌──────────────┼──────────────────────┐
                                     ▼              ▼                      ▼
                           Azure Files share   Azure Files share     Azure OpenAI (Foundry)
                           inbox/ : PDFs,      _work/ : read cache,   reads PDF pages
                           models, outputs     values, reports
```

Two containers, one image:

- **FinScan API** — small, always on, answers requests. Never does the financial work.
- **FinScan Job** — the existing `finscan` command. Starts, does the work, exits.

---

## 2. Azure basics for beginners

| Plain words | Azure term | What it is | How FinScan uses it |
|---|---|---|---|
| The account that pays | **Subscription** | A billing and access boundary in Azure. | Everything below lives in one subscription. |
| A folder for related resources | **Resource group** | A named group of resources you create, secure and delete together. | `finscan-rg` holds all FinScan resources. |
| A sealed box with an app and everything it needs | **Container** | A running copy of an app plus its exact Python version and libraries, isolated from the machine it runs on. | FinScan runs the same way on a laptop and in Azure. |
| The recipe for that box | **Dockerfile** | A text file of build steps: base image, install requirements, copy code. | `Dockerfile` at the repo root builds FinScan. |
| The packed box, ready to ship | **Image** | The built result of a Dockerfile, versioned with a tag (`finscan:1.0`). | Contains the code **and the reviewed mappings**, so a version records exactly what ran. |
| The warehouse for boxes | **Azure Container Registry (ACR)** | Private storage for images. | Azure pulls the FinScan image from here. |
| The neighbourhood where boxes run | **Container Apps environment** | A shared, managed space where container apps and jobs run, with networking and logs. | Hosts both the API and the Job. |
| A box that is always open | **Container App** | A container that runs continuously and answers web requests (HTTPS), scaling with traffic. | The FinScan **API**. |
| A box that opens, does a task, closes | **Container Apps Job** | A container that runs to completion, on demand, on a schedule or on an event; each run is an *execution* with a status. | The FinScan **run** (`finscan company …`). |
| A shared network drive | **Azure Files share** | A cloud file share that containers mount as a folder. | `inbox/` (PDFs, models, outputs) and `_work/` (cache, reports). |
| A safe for passwords | **Key Vault** | Secure storage for secrets and keys. | The model key and the API key. |
| A login for an app, not a person | **Managed identity** | An identity Azure gives a resource so it can access other resources without stored passwords. | Lets the API start the Job and the containers read Key Vault. |
| The logbook | **Log Analytics workspace** | Central store for logs and metrics. | Container logs, run history, errors. |

---

## 3. Why a Container Apps Job, not a service

**What FinScan does when it runs:** read the PDFs → resolve the mapping → write the
workbook and reports → **exit**. It does not listen for requests and does not need to
stay running. It is used a few times a quarter, for minutes at a time.

| | Container App (service) | **Container Apps Job** |
|---|---|---|
| Built for | A long-running program listening on a port | A program that runs, finishes and exits |
| When FinScan exits | Treated as a crash and restarted again and again | Normal: the run is marked *Succeeded* or *Failed* |
| Started by | Incoming web traffic | On demand, on a schedule, or by an event |
| Cost when idle | Pays for any replicas kept running | **Nothing** — pays only while a run executes |
| Long work | Web requests time out after about 4 minutes | Runs up to the timeout you set (e.g. 60 minutes) |
| Per-run input | — | Each run can take its own arguments (`company tencent`) |
| History | Build it yourself | Built-in list of runs, status and logs |

**Rejected alternatives and why**

- **Azure Functions** — the Consumption plan stops a function after at most 10 minutes;
  reading a new 150-page PDF can take longer, and FinScan is not written as a function.
- **Azure Container Instances** — can run a container once, but has no built-in
  schedule, run history or retry handling.
- **AKS (Kubernetes) / Azure Batch** — far more infrastructure than one batch command needs.

So "no web server" is half the reason; the other half is that the work is
**occasional and long-running** — exactly what Jobs are for.

---

## 4. Do we need a FastAPI app? An MCP server? How do we know a job finished?

### 4.1 The FastAPI app — **yes, needed**

Something must be always available to receive requests from Copilot Studio — the Job
cannot do that because it only exists while running. The FastAPI app is that piece:

| It does | It does not |
|---|---|
| Accept uploaded PDFs and models into the right company folder | Read PDFs or touch financial figures |
| Start a Job execution for a company (`company tencent`) | Change mappings |
| Keep a record of each job and report its status | Do any calculation |
| Serve the finished workbook and report for download | |
| Host the MCP server (4.2) | |

It runs as a **Container App** (service), using the **same image** as the Job.
**FastAPI** is chosen because it is the standard modern Python web framework, small,
fast, and it generates an OpenAPI description automatically.

### 4.2 The MCP server — **recommended, inside the same FastAPI app**

**What MCP is.** The Model Context Protocol is an open standard for connecting AI
agents to tools. An MCP server lists its tools — each with a name, a description and
its inputs — and an agent (Copilot Studio, a Foundry agent, Claude, VS Code) can
discover and call them without custom wiring.

**FinScan's MCP tools**

| Tool | What the agent uses it for |
|---|---|
| `list_companies` | Which companies are set up, and which have files ready |
| `create_upload` | Get a short-lived upload link for a company's PDF / model (section 5) |
| `start_job` | Start a run for a company; `dry_run` to preview without writing |
| `get_job_status` | Running / finished / failed, plus the summary when done |
| `list_jobs` | Recent runs, newest first, optionally for one company |
| `get_download_link` | A time-limited link to the updated workbook and report |

**Why MCP rather than only a REST API with a custom connector**

| | REST API + Copilot Studio custom connector | **MCP server** |
|---|---|---|
| Setup in Copilot Studio | Import an OpenAPI file, configure each action by hand | Point at one URL; tools are discovered |
| When a tool changes | Re-import and re-configure the connector | Copilot Studio picks up the change |
| Tool descriptions for the AI | Written separately in the connector | Written once, in code, next to the tool |
| Reuse by other agents | Copilot Studio only | Foundry agents, Claude, VS Code, others |

MCP is **not a separate deployment**: it is an extra endpoint (`/mcp`) on the same
FastAPI app, and each tool is a thin wrapper over the same functions the REST
endpoints use. The REST endpoints stay for uploads, downloads and automation
(e.g. Power Automate).

### 4.3 How do we know a job finished?

1. `start_job` returns a **job id**. The API stores a small job record linking that id
   to the Azure Job **execution**.
2. While it runs, the API reads the execution's status from Azure:
   **Running → Succeeded / Failed**.
3. At the end, the FinScan run writes a **`result.json`** for that job (period
   written, values written, blank rows and why, warnings).
4. The agent calls `get_job_status`, which combines both and returns either
   "still running" or the finished summary and download link.

**In the chat:** the analyst asks "Is it done?" and the agent calls `get_job_status`.
Runs take minutes, so the agent never waits on a single call.

**Suggested improvement:** when a run finishes, post a message to the analyst in Teams
(via Power Automate or a Teams webhook), so nobody has to ask.

---

## 5. Uploading files from Copilot Studio

**Recommended flow (first cut)**

```
Analyst: "Upload the 3Q25 filing for Tencent"
Agent  → create_upload(company="tencent")      → returns a link valid for ~15 minutes
Analyst opens the link → simple upload page served by the FinScan API
                       → picks the PDF (and the model workbook if it changed) → Upload
Agent  → start_job(company="tencent", dry_run=true)   → preview
Analyst confirms → start_job(company="tencent")       → writes the column
Agent  → get_job_status / get_download_link          → summary + workbook link
```

**Why a link, not attaching the PDF in chat:** filings are often several MB, and
passing large files through a chat message and a tool call is unreliable and hard to
secure. A browser upload to the API is simple, fast and keeps the file under the API's
control. The upload page puts files into `inbox/<Company>/` on the file share and
moves the previous quarter's filing to `past/`.

**Later option:** Teams file attachment → Power Automate flow → the API's upload
endpoint — no link to click, at the cost of building and maintaining a flow.

---

## 6. Azure AI Foundry — what it is and how we use it

**What it is.** Azure AI Foundry is Microsoft's platform for AI on Azure. Two parts
matter here:

1. **Model deployments** — you deploy a model (for example an Azure OpenAI GPT model)
   in your own Azure tenant and call it through an endpoint and key.
2. **Agents** — you can build AI agents in Foundry that call tools.

**How FinScan uses it**

| Part | Used? | Why |
|---|---|---|
| Model deployment | **Yes** | FinScan's PDF reader sends page images to a vision-capable model. Deploying an Azure OpenAI model in Foundry keeps the data in your tenant and region, under your company's Azure agreements. |
| Foundry agent | **No (first cut)** | Copilot Studio is the front end because it publishes natively to Teams. A Foundry agent could call the same MCP server later if needed. |

**Configuration (already supported by the code — no change needed)**

```
FINSCAN_LLM_PROVIDER=azure
AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=<deployment name>     # a vision-capable GPT model available in your region
AZURE_OPENAI_API_VERSION=<api version>
AZURE_OPENAI_API_KEY=<from Key Vault>
```

The PDF read cache (`_work/pdfcache`) is keyed by the file's content **and the model
deployment name**, so each filing is sent to the model once; reruns reuse the read.

**Claude.** Anthropic's Claude models can be used either through Azure AI Foundry
(where offered for your subscription type and region) or through Anthropic's own API.
FinScan's code currently supports OpenAI and Azure OpenAI only; adding Claude is a
small change in `finscan/llm.py` and `finscan/config.py` (section 14, item 8).

---

## 7. Connecting to Copilot Studio and Teams

### 7.1 Copilot Studio

**What it is.** Microsoft's low-code tool for building AI agents (chat assistants)
that work in Teams, Microsoft 365 Copilot and websites. It is licensed through the
Power Platform.

**Steps** (menu names may differ slightly between Copilot Studio versions)

1. Create an agent, e.g. **"FinScan Model Updater"**.
2. **Add a tool → Model Context Protocol.** Give the server a name and description,
   enter the URL `https://<finscan-api>/mcp`, and choose **API key** authentication
   (the key is stored in the connection, not in the agent).
3. Write the agent's **instructions**, for example:
   > You update financial models with FinScan. Use the tools to upload filings, start
   > jobs and report results. Always run a dry run first and ask the analyst to
   > confirm before writing. Repeat figures, blank rows and warnings exactly as the
   > tools return them. Never calculate, estimate or reinterpret financial values.
4. Test in the **test pane**: "List companies", "Run a dry run for Tencent".

### 7.2 Teams

1. In Copilot Studio: **Channels → Teams and Microsoft 365 Copilot → Add channel**.
2. Make it available to chosen users, or submit it for **admin approval** in the
   Teams admin center (IT approves once).
3. Analysts find the agent in Teams and chat with it.

**Example conversation**

> **Analyst:** Update Tencent for 3Q25.
> **Agent:** Here is an upload link for Tencent (valid 15 minutes): …
> **Analyst:** Done.
> **Agent:** Dry run: 3Q25 → Model!AH, 23 values, 13 blank (cash-flow rows not in the
> quarterly announcement; FX rate typed by hand). Write it?
> **Analyst:** Yes.
> **Agent:** Running… finished. 23 values written, 0 errors. Download: …

---

## 8. Option: SharePoint instead of the inbox folder

*Not chosen for the first cut — documented so it is ready if needed.*

**Idea.** Analysts already keep filings and models in SharePoint. Instead of
uploading, they drop files into `/FinScan/<Company>/` and the updated workbook appears
in `/FinScan/<Company>/Output/`.

**How it would work — "sync in, sync out"**

1. The Job starts with a company name.
2. It downloads that company's SharePoint folder into the container (via Microsoft Graph).
3. It runs FinScan exactly as today on the local copy.
4. It uploads the output workbook and report back to SharePoint.

**Work needed**

| Item | Detail |
|---|---|
| Permissions | An Entra app (or the Job's managed identity) granted **`Sites.Selected`** on the FinScan site only; **admin consent** from IT |
| Code | A small sync-in / sync-out step around the run (worker entrypoint). **No change to the core pipeline.** |
| What stays put | Mappings stay in git (inside the image). The PDF read cache stays on the Azure Files share — SharePoint is not suited to frequent machine reads/writes. |

**Why deferred:** it needs IT permission work; API upload is enough to prove the
service. It is a natural second step once analysts use FinScan regularly.

---

## 9. API Management (APIM) — what, where, why

**What it is.** A managed **gateway** that sits in front of APIs. Callers talk to
APIM; APIM checks them and forwards the request to the real API.

| APIM gives | Why it matters |
|---|---|
| One stable public URL | The backend can move without changing Copilot Studio |
| Keys per consumer (Copilot Studio, Power Automate, …) | Revoke one without affecting others |
| **Entra ID token checks** | Only signed-in, authorised users reach FinScan |
| Rate limits / quotas | Stops runaway or accidental mass runs |
| Request logging | Audit of who started which job |
| Versioning | Change the API without breaking callers |

**Where it sits**

```
Copilot Studio / Foundry / Power Automate  →  APIM  →  FinScan API (Container App)  →  FinScan Job
```

**Why not in the first cut:** for a pilot, the Container App's built-in HTTPS endpoint
plus an API key is enough. Add APIM when moving to Entra ID sign-in, when several
consumers need separate keys, or when audit/policy requirements apply. Use a
low-cost tier (**Basic v2** or **Consumption**).

---

## 10. What you need in the Azure portal and tenant

### 10.1 In Azure

| Need | Detail |
|---|---|
| **Azure subscription** | Usually provided by IT under the company's agreement (Enterprise Agreement / Microsoft Customer Agreement), or Pay-As-You-Go for a trial. |
| **Resource group** | e.g. `finscan-rg` in your chosen region. |
| **Your access** | **Contributor** on the resource group (create resources) and **User Access Administrator** or **Owner** (to give the managed identity its roles). |
| **Resource providers registered** | `Microsoft.App` (Container Apps), `Microsoft.ContainerRegistry`, `Microsoft.Storage`, `Microsoft.KeyVault`, `Microsoft.OperationalInsights` (logs), `Microsoft.CognitiveServices` (Foundry / Azure OpenAI); later `Microsoft.ApiManagement`. |
| **Model quota** | Azure OpenAI quota for a vision-capable model in your region (requested in Foundry if needed). |

### 10.2 Outside Azure (Microsoft 365 / Power Platform)

| Need | Detail |
|---|---|
| Copilot Studio licence | Plus a Power Platform environment to build the agent in. |
| Teams | App publishing allowed, or admin approval for the agent. |
| SharePoint / Graph consent | Only if section 8 is adopted. |

---

## 11. Resource list and cost drivers

| Resource | Purpose | Suggested tier |
|---|---|---|
| Resource group | Container for everything | — |
| Container Registry | Stores the FinScan image | Basic |
| Log Analytics workspace | Logs for the environment | Pay-as-you-go |
| Container Apps environment | Where API and Job run | Consumption |
| Container App — FinScan API | Always-on API + MCP | Smallest CPU/memory; min 1 replica (or 0 to save cost, with a slower first call) |
| Container Apps Job — FinScan run | Does the work | ~1 vCPU / 2 GiB, 60-minute timeout |
| Storage account + 2 file shares | `inbox`, `_work` | Standard |
| Key Vault | Model key, API key | Standard |
| User-assigned managed identity | Access without passwords | — |
| Azure AI Foundry / Azure OpenAI | Model deployment | Standard deployment |
| APIM *(later)* | Gateway | Basic v2 / Consumption |

**What drives cost:** model tokens dominate (reading new PDFs); the read cache keeps
repeats free. The Job costs only while running. An always-on API replica is a small
steady cost. Storage and Key Vault are negligible. APIM adds a fixed monthly cost
depending on tier. Use the Azure pricing calculator for figures in your region.

---

## 12. Suggestions that fit this design

1. **One image for both API and Job** — one build, one version, both always in step.
2. **Keep `_work` on a persistent share** — the PDF read cache means each filing is
   paid for once.
3. **Dry run first, then write** — the agent shows a preview and asks to confirm;
   nothing is written by accident.
4. **One run per company at a time** — the API refuses a second run for the same
   company while one is active.
5. **Mappings change only through git** — reviewed pull request → new image. Never
   edited from chat.
6. **Teams message when a run finishes** — no polling by the analyst.
7. **Managed identity + Key Vault everywhere** — no keys in config files or images.
8. **Infrastructure as code (Bicep)** — every resource in a template, so test and
   production are identical and rebuildable.
9. **CI/CD with GitHub Actions + OIDC** — tests → build image → push to ACR → deploy
   to test → smoke test → promote. No stored Azure passwords.
10. **Keep supplements beside PDFs** — where a company uses a
    `<pdf>.pdf.supplement.json` (e.g. Grupo Bimbo), upload it with the PDF.

---

## 13. Step-by-step deployment (first cut)

Order matters; each step depends on the one before. Commands assume the Azure CLI
(`az login` first). Names are examples.

```bash
RG=finscan-rg; LOC=uksouth
ACR=finscanacr01; SA=finscanstore01; KV=finscan-kv01; ENV=finscan-env
```

1. **Resource group**
   ```bash
   az group create -n $RG -l $LOC
   ```
2. **Registry and image** (from the repo's `Dockerfile`)
   ```bash
   az acr create -n $ACR -g $RG --sku Basic
   az acr build -r $ACR -t finscan:1.0 .
   ```
3. **Storage and file shares**
   ```bash
   az storage account create -n $SA -g $RG -l $LOC --sku Standard_LRS
   az storage share-rm create --storage-account $SA -n finscan-inbox
   az storage share-rm create --storage-account $SA -n finscan-work
   ```
4. **Key Vault** — store the model key and the API key
   ```bash
   az keyvault create -n $KV -g $RG -l $LOC
   az keyvault secret set --vault-name $KV -n azure-openai-key --value <key>
   az keyvault secret set --vault-name $KV -n finscan-api-key --value <generated>
   ```
5. **Model deployment** — in Azure AI Foundry, create a resource in the region and
   deploy a vision-capable GPT model; note the endpoint and deployment name.
6. **Container Apps environment with the shares registered**
   ```bash
   az containerapp env create -n $ENV -g $RG -l $LOC
   KEY=$(az storage account keys list -n $SA -g $RG --query [0].value -o tsv)
   for s in inbox work; do
     az containerapp env storage set -n $ENV -g $RG --storage-name $s \
       --azure-file-account-name $SA --azure-file-account-key $KEY \
       --azure-file-share-name finscan-$s --access-mode ReadWrite
   done
   ```
7. **The FinScan Job** — created from a YAML file (`az containerapp job create --yaml job.yaml`):
   image `finscan:1.0`; command `finscan company <name>` (overridden per run);
   volumes `inbox` → `/app/inbox`, `work` → `/app/_work`; environment variables from
   section 6 with the key as a Key Vault secret reference; manual trigger;
   60-minute timeout; the managed identity.
8. **The FinScan API Container App** — same image, command
   `finscan-api`, external HTTPS ingress, the same two volumes,
   the API key from Key Vault, the managed identity — which is granted permission to
   start the Job (e.g. the built-in *Container Apps Jobs Operator* role on the Job).
9. **Copilot Studio agent** — section 7.1, pointing at `https://<api-url>/mcp`.
10. **Teams** — section 7.2.
11. **Smoke test** — upload a known filing for a company already back-tested
    (e.g. Tencent 3Q25), run a dry run, then a real run; compare with a local run.

---

## 14. Build backlog

| # | Item | State | Notes |
|---|---|---|---|
| 1 | Structured run result | **done** | `finscan/runner.py` → `RunResult`; the CLI prints it; `--result-json` or `FINSCAN_JOB_ID` saves `_work/jobs/<id>/result.json`. |
| 2 | FastAPI service `finscan/service/` | **done** | `GET /health`, `/companies`, `/companies/{c}`; `POST /upload-links/{c}`, `/uploads/{c}` + signed upload page; `POST /jobs`, `GET /jobs`, `/jobs/{id}`; `GET /jobs/{id}/download`, `POST /jobs/{id}/download-link` + signed `/files/…`; one active run per company; `x-api-key`. |
| 3 | MCP endpoint `/mcp` | **done** | MCP SDK 2.x, Streamable HTTP, stateless, in the same app; tools `list_companies`, `create_upload`, `start_job`, `get_job_status`, `list_jobs`, `get_download_link`. |
| 4 | Job launchers | **done** (Azure: unit-tested only) | `FINSCAN_RUNNER=local` runs `finscan company …` as a process; `azure` starts a Container Apps Job execution (args + `FINSCAN_JOB_ID`) and reads its status. Needs a real subscription for an end-to-end test. |
| 5 | Packaging | **done** | `Dockerfile` (one image: `finscan …` for the Job, `finscan-api` for the API), `.dockerignore`; dependencies in `requirements.txt`. |
| 6 | Infrastructure (Bicep) `infra/` | to do | Log Analytics, ACR, storage + 2 shares, Key Vault, managed identity + role assignments, Container Apps environment + storage, Job, API Container App, Foundry/Azure OpenAI deployment. |
| 7 | Copilot Studio + Teams | to do | Agent, MCP tool, instructions, Teams publish. |
| 8 | Later | — | APIM + Entra ID; Teams completion notice; Claude provider in `finscan/llm.py`; SharePoint sync (section 8); Blob Storage instead of file shares; CI/CD. |

---

## 15. Glossary

| Term | Meaning |
|---|---|
| **ACR** | Azure Container Registry — private store for container images. |
| **APIM** | Azure API Management — gateway in front of APIs (keys, auth, limits, logs). |
| **Azure AI Foundry** | Microsoft's AI platform: model deployments and agents. |
| **Azure Files** | Cloud file share mountable as a folder. |
| **Bicep** | Azure's language for describing resources as code. |
| **Container / image** | A packaged app with its dependencies / the built, versioned package. |
| **Container App** | An always-on container answering web requests. |
| **Container Apps Job** | A container that runs to completion; each run is an *execution*. |
| **Copilot Studio** | Microsoft's tool for building AI agents for Teams and Microsoft 365. |
| **Dry run** | A FinScan run that resolves and validates but writes nothing. |
| **Entra ID** | Microsoft's identity service (formerly Azure AD) — users, apps, sign-in. |
| **FastAPI** | A Python framework for building web APIs. |
| **Key Vault** | Secure store for secrets. |
| **Managed identity** | An Azure-provided identity for a resource, so it needs no stored passwords. |
| **MCP** | Model Context Protocol — open standard for AI agents to discover and call tools. |
| **Mapping** | `mappings/<company>.json` — the reviewed rules linking workbook rows to filing lines. |
| **OIDC** | A way for GitHub Actions to sign in to Azure without stored passwords. |
| **Resource group** | A folder of related Azure resources. |
| **Subscription** | The Azure billing and access boundary. |
