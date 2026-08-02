# Wiring FinScan into Copilot Studio

Copilot Studio is the front door. The user drops a results PDF and a model into
chat; FinScan does the extraction and the Excel surgery; Copilot handles the one
conversation that matters — the first upload for a company, where a human
confirms which tabs are in scope.

After that confirmation, every later upload for that company runs without asking.

## 1. Deploy the API

Anywhere that gives you an HTTPS URL. Azure Container Apps or App Service is the
path of least resistance since you are already on Azure OpenAI.

```bash
uvicorn finscan.api.main:app --host 0.0.0.0 --port 8080      # local
docker build -t finscan . && docker run -p 8080:8080 --env-file .env finscan
```

Set on the host: `AZURE_OPENAI_*`, a real `FINSCAN_API_KEY`, and
`FINSCAN_PROFILE_STORE` pointing at **persistent** storage — an Azure Files
mount or a blob-backed volume. Profiles are the accumulated knowledge of your
100 companies; losing them means re-confirming all 100.

Confirm `GET /health` returns `{"status":"ok","llm_configured":true}`.

## 2. Create the custom connector

Power Apps / Power Automate → **Custom connectors** → **New** → **Import an
OpenAPI file** → upload `docs/copilot_connector.yaml`.

- Edit `host:` in the YAML to your deployed hostname first.
- **Security** tab → API Key, parameter label `x-api-key`, location **Header**.
- **Test** tab → create a connection, paste the key, run `Health`.

The file is Swagger 2.0 deliberately — Power Platform rejects OpenAPI 3, which
is what FastAPI serves at `/openapi.json`. Use the checked-in file, not the
generated one.

## 3. Add the actions

Agent → **Tools** → **Add a tool** → **Connector** → FinScan. Add
`ProposeUpdate`, `ConfirmProfile`, `ApplyUpdate`, `DownloadWorkbook`, and
optionally `InspectWorkbook`.

## 4. Topic design

One topic, **Update financial model**. The shape that matters is the branch on
`status`, because it is what turns 100 companies into 100 one-time
conversations rather than 400 approvals a year.

1. **Trigger phrases** — "update the model", "add this quarter's results",
   "extract results from this PDF".
2. **Question nodes** — ask for the results PDF → `Topic.PdfFile`, then the
   workbook → `Topic.Workbook` (both type *File*).
3. **Action: ProposeUpdate** — pass both files. Nothing is written.
4. **Condition on `status`:**

   **`awaiting_confirmation`** — a company you have not set up yet, or one whose
   layout just changed. Show `summary`, then an Adaptive Card built from the
   `sheets` array: one toggle per sheet, pre-ticked where `in_scope` is true,
   labelled with `sheet`, `units`, `input_rows` and `formula_rows`. On submit,
   call **ConfirmProfile** with `company_key` and the ticked sheet names, then
   **ApplyUpdate**.

   > Mention `colors_found` on this card. When it is false the model has no
   > blue/black convention and FinScan is guessing at the input cells — that is
   > worth a sentence to the reviewer, and worth someone colouring the model.

   **`needs_review`** — written, but the arithmetic checks or a missing colour
   convention flagged it. Show `summary` plus the `issues` list and link the
   download. Do not auto-approve these.

   **`ok`** — show `summary` and a compact table from `mappings`
   (`excel_label` → `value`), then **DownloadWorkbook**.

5. **Deliver** — attach the file in chat, or push it to the company's SharePoint
   folder with the SharePoint connector.

### Rendering the mapping honestly

In the `mappings` array a `value` of `null` means that row is a **formula row** —
FinScan copied the model's own calculation forward rather than pasting a number.
Show those as "formula carried forward", not as a blank or a failure. Reviewers
who see blanks assume something broke.

## 5. Keep the numbers out of generative answers

Do not hand extracted figures to Copilot's generative answers to summarise —
it will round and reword them. Use the connector's output fields verbatim
(`summary`, `mappings`, `issues`) in Adaptive Cards. The LLM reasoning belongs
inside FinScan where it is schema-constrained and validated.

## 6. SharePoint-triggered pipeline

Once profiles exist, most of the volume should not involve chat at all.

> **When a file is created** in `/Finance/Results Inbox`
> → **Get file** matching the company workbook from `/Finance/Models`
> → **ExtractAndApply**
> → **Condition on `status`:**
>   - `ok` → check the updated workbook back into `/Finance/Models`, post
>     `summary` to the Teams channel
>   - `needs_review` → leave the model untouched, post `report_markdown` to Teams
>     and @mention the analyst
>   - `awaiting_confirmation` → post a card asking someone to confirm the layout,
>     linking the Copilot Studio agent

This is also the migration path for the SharePoint stage you mentioned: nothing
in FinScan reads from the local disk directly — the API takes uploads, so the
same service works whether files arrive from chat, SharePoint or a batch job.

## 7. Onboarding the first 100 companies

Run `POST /inspect` over the whole model library before wiring any chat. It
needs no PDF and no LLM, and it tells you which workbooks have a usable
blue/black convention. Fix the handful that do not *before* they are in the
pipeline — it is far cheaper than reviewing their output forever.
