# Running the FinScan service locally — quick reference

How to start the FinScan API + MCP server on your machine, check it is running, and
try it. For the command-line runs (`finscan company …`) see [RUNNING.md](RUNNING.md);
for deploying to Azure see [FinModelUpdater_Deployment.md](FinModelUpdater_Deployment.md).

You need two terminal windows: one runs the server, the other sends it requests.

---

## 1. One-time setup

In the project folder, open `.env` and add a key of your choice — it is the API's
password:

```
FINSCAN_API_KEY=mysecret123
```

Optional settings are listed in `.env.example` (runner, public URL for links, link
lifetime, upload size limit).

## 2. Start the server (terminal 1)

```bash
cd ~/Project/Barings/Barrings_EM_Scan/finscan
source .venv/bin/activate
finscan-api
```

You should see `Uvicorn running on http://0.0.0.0:8000`. Leave it open — it logs
every request. Stop it with **Ctrl+C**.

Different port: `PORT=8010 finscan-api` (then use `localhost:8010` below).

## 3. Check it is running (browser)

| Open | You should see |
|---|---|
| http://localhost:8000/health | `{"status":"ok","runner":"local","api_key_set":true}` |
| http://localhost:8000/docs | The API's page listing every endpoint |

The "Try it out" buttons on `/docs` do not send the API key yet, so use the
commands below.

## 4. Use it (terminal 2)

```bash
cd ~/Project/Barings/Barrings_EM_Scan/finscan
K=mysecret123
```

**Which companies are set up and ready?**

```bash
curl -s -H "x-api-key: $K" localhost:8000/companies | python3 -m json.tool
```

Each company shows `ready` and, if not ready, its `problems` (e.g. no model
workbook, more than one).

**Start a dry run** (resolves and validates, writes nothing):

```bash
curl -s -H "x-api-key: $K" -H "content-type: application/json" \
     -d '{"company": "CocaCola", "dry_run": true}' localhost:8000/jobs | python3 -m json.tool
```

It returns a job `id` with `"state": "running"`.

**Check the job** (repeat until it finishes):

```bash
curl -s -H "x-api-key: $K" localhost:8000/jobs/<job id> | python3 -m json.tool
```

| `state` | Meaning |
|---|---|
| `running` | Still working |
| `succeeded` | Finished; `result` has the period, values, blank rows with reasons, warnings |
| `refused` | Nothing written, by design — `result.refused` says why (e.g. wrong period, filings disagree) |
| `failed` | An error — `error` or `result.errors` says what |

The run's files are in `_work/jobs/<job id>/`: `job.json`, `result.json`, `run.log`.

**List recent jobs:**

```bash
curl -s -H "x-api-key: $K" "localhost:8000/jobs?company=cocacola" | python3 -m json.tool
```

**Get the upload page** (what an analyst would use):

```bash
curl -s -H "x-api-key: $K" -X POST localhost:8000/upload-links/CocaCola | python3 -m json.tool
```

Open the `upload_url` from the output in a browser. It expires after 30 minutes.
Uploading a new filing moves the previous quarter's into the company's `past/`
folder; upload a report and its press release together if both are needed.

**Download a finished real run's output:**

```bash
curl -s -H "x-api-key: $K" -o output.xlsx "localhost:8000/jobs/<job id>/download?kind=workbook"
curl -s -H "x-api-key: $K" -o report.md   "localhost:8000/jobs/<job id>/download?kind=report"
```

Or ask for a short-lived link to hand to someone:

```bash
curl -s -H "x-api-key: $K" -X POST "localhost:8000/jobs/<job id>/download-link?kind=workbook"
```

A dry run saves no files, so it has nothing to download — its outcome is in the
job status.

## 5. MCP — what Copilot Studio will use

List the tools an agent sees:

```bash
curl -s -H "x-api-key: $K" -H "content-type: application/json" \
     -H "accept: application/json, text/event-stream" \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' localhost:8000/mcp | python3 -m json.tool
```

Tools: `list_companies`, `create_upload`, `start_job`, `get_job_status`, `list_jobs`,
`get_download_link`.

To try it from Copilot Studio before deploying: expose port 8000 with a tunnel
(e.g. ngrok or VS Code port forwarding), set `FINSCAN_PUBLIC_URL` in `.env` to the
tunnel's URL (so upload/download links point to it), restart `finscan-api`, and add
an MCP tool in Copilot Studio with `<tunnel URL>/mcp` and the API key as header
`x-api-key`.

## 6. Run it in Docker (as it will run in Azure)

```bash
docker build -t finscan:dev .

# The API
docker run --rm -p 8000:8000 -e FINSCAN_API_KEY=$K \
  -v "$PWD/inbox:/app/inbox" -v "$PWD/_work:/app/_work" finscan:dev finscan-api

# The Job's role: one company run
docker run --rm -v "$PWD/inbox:/app/inbox" -v "$PWD/_work:/app/_work" \
  finscan:dev finscan company cocacola --dry-run
```

Mappings are baked into the image — rebuild after changing a mapping.

---

## Things to know

- **Use dry runs (`"dry_run": true`) while trying it.** The server works on your real
  project folder. A real run writes `inbox/<company>_v2_output.*` and updates
  `models/<company>_model.json`, which is tracked in git.
- **Pick a company that is ready.** A folder holding several quarters' PDFs is
  refused (keep only the current quarter; older ones go in `past/`). A company whose
  PDF has not been read before calls the model (OpenAI / Azure OpenAI) and needs a
  working key with credit.
- **One run per company at a time.** Starting a second while one is active returns
  409 "already active".
- **Model reads are cached** in `_work/pdfcache`, so re-running the same PDF costs
  nothing.

## Troubleshooting

| Problem | Fix |
|---|---|
| `401 missing or wrong x-api-key` | The header value must match `FINSCAN_API_KEY` in `.env`; restart `finscan-api` after editing `.env`. |
| `503 FINSCAN_API_KEY is not set` | Add `FINSCAN_API_KEY` to `.env` and restart. |
| `Address already in use` | Another program uses port 8000: `PORT=8010 finscan-api`. |
| `finscan-api: command not found` | Activate the environment (`source .venv/bin/activate`) or run `pip install -e .` once. |
| Job `failed`, "ended without a result" | Read `_work/jobs/<job id>/run.log`. |
| Job `refused` | Read `result.refused` — usually the filing is not the next quarter for the model, or the folder holds filings for different quarters. |
| `409 … is not ready` | `GET /companies` shows the company's `problems` — upload the missing filing or model. |
| Upload link says invalid or expired | Ask for a new one (`POST /upload-links/<company>`). |
