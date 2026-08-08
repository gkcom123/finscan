# Copilot Studio demo — local server + test folder

This guide sets up the demo where you type in Copilot:

> **Return me an updated excel for Q325 for company name Tancent**

Files live in `test/tencent/` on your machine. Copilot calls your local FinScan API
(via ngrok). No PDF/Excel upload in chat.

---

## Part 1 — Local FinScan (15 min)

### 1. Install and configure

```bash
cd ~/Project/Barings/Barrings_EM_Scan/finscan
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
cp .env.example .env
```

Edit `.env`:

```env
FINSCAN_LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...

FINSCAN_API_KEY=pick-a-long-random-secret
FINSCAN_PROFILE_STORE=./profiles
FINSCAN_TEST_DIR=./test
FINSCAN_DEMO_ENABLED=true
```

### 2. Demo files (already set up for Tencent)

```
test/tencent/
  Tencent_q325.pdf
  model.xlsx
profiles/tencent.json   ← confirmed layout (required for straight-through write)
```

To refresh files from inbox:

```bash
cp inbox/Tencent_q325.pdf test/tencent/
cp inbox/Tencent_test.xlsx test/tencent/model.xlsx
```

### 3. Verify CLI once (optional)

```bash
source .venv/bin/activate
finscan run test/tencent/Tencent_q325.pdf test/tencent/model.xlsx \
  --company tencent --out /tmp/tencent_demo.xlsx --report /tmp/report.md
```

Expect **Status: ok** and a written column.

### 4. Start the API

```bash
chmod +x scripts/start_demo_server.sh
./scripts/start_demo_server.sh
```

Or manually:

```bash
source .venv/bin/activate
uvicorn finscan.api.main:app --host 0.0.0.0 --port 8080
```

### 5. Smoke-test the demo endpoint

```bash
curl -s http://127.0.0.1:8080/health | python3 -m json.tool

curl -s -X POST http://127.0.0.1:8080/demo/update \
  -H "x-api-key: YOUR_FINSCAN_API_KEY" \
  -F "company=Tancent" \
  -F "period=q325" | python3 -m json.tool
```

Expect `"status": "ok"` and a `"download_url"`. Download:

```bash
curl -o updated.xlsx -H "x-api-key: YOUR_FINSCAN_API_KEY" \
  http://127.0.0.1:8080/jobs/JOB_ID/download
```

---

## Part 2 — Expose localhost to Copilot (ngrok)

Copilot Studio needs **HTTPS**. Your laptop is HTTP only, so use a tunnel:

1. Install [ngrok](https://ngrok.com/download) (or Cloudflare Tunnel).
2. With the API running on 8080:

```bash
ngrok http 8080
```

3. Copy the **https** host, e.g. `abc123.ngrok-free.app` (no `https://` prefix).

---

## Part 3 — Custom connector in Power Platform (20 min)

1. Open [Power Apps](https://make.powerapps.com) → **Custom connectors** → **New connector** → **Import an OpenAPI file**.
2. Upload `docs/copilot_connector.yaml`.
3. **General** → set **Host** to your ngrok host: `abc123.ngrok-free.app`
4. **Security** → **API Key** → Parameter name `x-api-key`, Location **Header**.
5. **Test** → Create connection → paste `FINSCAN_API_KEY` from `.env` → run **Health**.
6. Test **DemoUpdateFromFolder** with:
   - company: `Tancent`
   - period: `q325`
7. **Create connector**.

> Re-import the YAML whenever you change the API. Power Platform needs Swagger 2.0
> (this file), not FastAPI’s `/openapi.json`.

---

## Part 4 — Copilot Studio agent (25 min)

### Option A — Agent with tool (recommended for natural language)

1. [Copilot Studio](https://copilotstudio.microsoft.com) → **Create** → **Agent**.
2. **Settings** → enable **Generative orchestration** (if available).
3. **Tools** → **Add tool** → **Connector** → your FinScan connector.
4. Add actions:
   - **DemoUpdateFromFolder** (primary for this demo)
   - **DownloadWorkbook**
   - (optional) **ListDemoCompanies**, **ExtractAndApply**

5. **Instructions** (paste into agent system prompt):

```
You update financial Excel models from quarterly results PDFs via FinScan.

When the user asks for an updated Excel for a company and period (e.g. "Q325 for Tancent"):
1. Call DemoUpdateFromFolder with company=<name they said> and period=<code like q325>.
2. Read the response status:
   - ok: call DownloadWorkbook with job_id, tell the user the file is ready, show summary verbatim.
   - awaiting_confirmation: explain layout must be confirmed once (demo: use company tencent with pre-confirmed profile).
   - needs_review: show issues list; do not claim success.
3. Never invent financial numbers — only use summary and mappings from the tool.
4. Common aliases: Tancent/Tencent → company "Tancent" (server maps to tencent).
```

6. **Test** in the preview pane:

> Return me an updated excel for Q325 for company name Tancent

### Option B — Classic topic (deterministic)

1. **Topics** → **Add topic** → **From blank**.
2. **Trigger phrases**: `updated excel`, `Q325`, `Tancent`, `update model`
3. **Question**: "Which company?" → save to `CompanyName` (default: Tancent)
4. **Question**: "Which period?" → save to `Period` (default: q325)
5. **Call an action** → **DemoUpdateFromFolder**:
   - company = `Topic.CompanyName`
   - period = `Topic.Period`
6. **Condition** `Topic.status = ok`:
   - Message: `Topic.summary`
   - **DownloadWorkbook** → attach file to chat
7. **Else** branch: show `Topic.report_markdown` or first error from `issues`

---

## Part 5 — Demo script (live meeting)

| Step | What you do | What audience sees |
|------|-------------|-------------------|
| 1 | "Files are already on the server under test/tencent" | Sets expectation — no upload friction |
| 2 | Type: *Return me an updated excel for Q325 for company name Tancent* | Natural language |
| 3 | Wait ~15s | Agent calls FinScan |
| 4 | Agent returns summary + file | "Tencent — 3Q2025: wrote Model!AH (11v/27f)" |
| 5 | Open Excel | New column AH, blue inputs filled, formulas intact, audit comments |

**If something fails:**

| Symptom | Fix |
|---------|-----|
| Connector test fails | ngrok URL changed — update connector host |
| 401 Unauthorized | `x-api-key` mismatch between connector and `.env` |
| awaiting_confirmation | Run `finscan profiles confirm tencent --sheets Model` or use `--company tencent` with confirmed profile |
| 404 company not found | Folder must be `test/tencent/` with PDF + model.xlsx |
| LLM error | Check `OPENAI_API_KEY`, `curl /health` → `llm_configured: true` |

---

## Architecture

```
You in Copilot Studio
        │  "Q325 for Tancent"
        ▼
   ngrok HTTPS
        ▼
   localhost:8080  POST /demo/update
        │  reads test/tencent/Tencent_q325.pdf + model.xlsx
        ▼
   FinScan pipeline → writes column → returns job_id
        ▼
   GET /jobs/{id}/download → .xlsx back to Copilot
```

---

## Production note

`POST /demo/update` is for **local demos only**. Production should use:

- `POST /extract-and-apply` with files from SharePoint, or
- SharePoint-triggered Power Automate flow (see `docs/copilot_studio_setup.md`)

Set `FINSCAN_DEMO_ENABLED=false` on any shared/hosted server.
