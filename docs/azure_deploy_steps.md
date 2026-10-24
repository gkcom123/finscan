# Deploying FinScan to Azure — step by step

A copy-and-paste guide, from creating an Azure account to a running FinScan you can
call from the internet. Every step says what it does and why. No Azure experience
needed.

**What you will end up with**

```
Your browser / Copilot Studio
        │  HTTPS + API key
        ▼
finscan-api   (Container App — always reachable, starts runs, serves files)
        │  starts
        ▼
finscan-job   (Container Apps Job — runs `finscan company <name>`, then stops)
        │
        ├─ file share "finscan-inbox"  → /app/inbox   (PDFs, models, outputs)
        └─ file share "finscan-work"   → /app/_work   (read cache, results, reports)
```

Background reading (optional): [FinModelUpdater_Deployment.md](FinModelUpdater_Deployment.md)
explains every Azure term and why each choice was made.

**Time:** about 30–45 minutes the first time. **Cost:** small for occasional use —
mostly OpenAI tokens; see [step 20](#20-keep-costs-under-control).

---

## Part A — Get ready (once)

### 1. Create an Azure account

1. Go to **https://azure.microsoft.com/free** and choose **Start free**.
2. Sign in with a Microsoft account (or create one), verify your phone, and add a
   card (used for identity checks; the free offer gives trial credit first).
3. When it finishes you land in the **Azure portal**: https://portal.azure.com.

You now have a **subscription** — the account Azure bills. Everything below lives
inside it.

### 2. Install the tools on your Mac

Mac terminals use zsh, which by default does **not** treat `# notes` at the end of a
typed command as comments — commands below would break. Switch that on first:

```bash
setopt interactivecomments
echo 'setopt interactivecomments' >> ~/.zshrc
```

Then install:

```bash
brew install azure-cli          # the "az" command
```

`az` already includes the Container Apps commands. Do **not** add the preview
`containerapp` extension — it changes them and was part of a create failure in
step 13. (If you added it: `az extension remove --name containerapp`.)

You normally do **not** need Docker: the image is built in Azure (step 7). Free-trial
subscriptions block that, so keep Docker Desktop handy as the fallback.

### 3. Log in from the terminal

```bash
az login                        # opens a browser; sign in with the same account
az account show --output table  # shows your subscription — check it is the right one
```

If you have more than one subscription:
`az account set --subscription "<name or id>"`.

### 4. Choose names and keep them in one file

Azure names for the registry and storage must be **unique worldwide**, lowercase
letters and digits only. Add your initials and a number.

Create a file `azure-vars.sh` in the project folder (it is personal — do not commit
it) with:

```bash
# ---- azure-vars.sh — run "source azure-vars.sh" in every new terminal ----
export LOC=uksouth                       # Azure region close to you
export RG=finscan-rg                     # resource group (a folder for everything)
export ACR=finscanacrgk01                # container registry — unique, lowercase
export SA=finscanstoregk01               # storage account — unique, lowercase, ≤24 chars
export ENV=finscan-env                   # Container Apps environment
export ID=finscan-id                     # managed identity (the app's own login)
export JOB=finscan-job                   # the Container Apps Job
export API=finscan-api                   # the Container App
export TAG=1.0                           # image version

export OPENAI_KEY="sk-..."               # your OpenAI API key (reads the PDFs)
export OPENAI_MODEL=gpt-5.4
export API_KEY="<paste the output of: openssl rand -hex 24>"  # the FinScan API's password
```

Then load it:

```bash
cd ~/Project/Barings/Barrings_EM_Scan/finscan
source azure-vars.sh
echo "API key: $API_KEY"                 # copy this somewhere safe
```

> Generate the API key **once** (`openssl rand -hex 24` in the terminal) and paste
> the result into the file, so it stays the same every time you load the file.
> Lock the file to yourself: `chmod 600 azure-vars.sh`.

---

## Part B — Create everything in Azure

Run these in order, in the same terminal (variables from step 4 loaded).

### 5. Allow the services this project uses

```bash
for p in Microsoft.App Microsoft.ContainerRegistry Microsoft.Storage \
         Microsoft.OperationalInsights Microsoft.ManagedIdentity; do
  az provider register --namespace $p
done
```

*Why:* a new subscription must opt in to each Azure service once. It can take a
minute or two in the background.

### 6. Create the resource group

```bash
az group create --name $RG --location $LOC
```

*Why:* one folder for everything, so it can be found, costed and deleted together.

### 7. Create the registry and build the image in Azure

```bash
az acr create --name $ACR --resource-group $RG --sku Basic
az acr build --registry $ACR --image finscan:$TAG .
```

*Why:* the registry stores the FinScan image. `az acr build` uploads the project and
builds the `Dockerfile` **in Azure**, for Azure's processor type (a Mac-built image
may not run there). The image contains the code and the reviewed `mappings/`.
Takes 2–4 minutes; the last line says `Run ID: … was successful`.

**If `az acr build` fails with `TasksOperationsNotAllowed`** (free-trial
subscriptions cannot build in Azure), build on your Mac with Docker Desktop and push:

```bash
az acr login --name $ACR
docker buildx build --platform linux/amd64 -t $ACR.azurecr.io/finscan:$TAG --push .
```

`--platform linux/amd64` builds for Azure's processor type (a Mac's differs);
`--push` uploads it. Check with
`az acr repository show-tags --name $ACR --repository finscan -o table`.

### 8. Create the storage and the two file shares

```bash
az storage account create --name $SA --resource-group $RG --location $LOC \
  --sku Standard_LRS --kind StorageV2
az storage share-rm create --storage-account $SA --resource-group $RG --name finscan-inbox --quota 10
az storage share-rm create --storage-account $SA --resource-group $RG --name finscan-work  --quota 10
export SA_KEY=$(az storage account keys list --account-name $SA --resource-group $RG --query "[0].value" -o tsv)
```

*Why:* containers lose their files when they stop. The shares are network drives
that keep `inbox/` (filings, models, outputs) and `_work/` (read cache, results)
between runs, and let the API and the runs see the same files.

### 9. Create the Container Apps environment and connect the shares

```bash
az containerapp env create --name $ENV --resource-group $RG --location $LOC

for s in inbox work; do
  az containerapp env storage set --name $ENV --resource-group $RG --storage-name $s \
    --azure-file-account-name $SA --azure-file-account-key $SA_KEY \
    --azure-file-share-name finscan-$s --access-mode ReadWrite
done
export ENV_ID=$(az containerapp env show --name $ENV --resource-group $RG --query id -o tsv)
```

*Why:* the environment is where both the API and the Job run. It also creates a
log store automatically. Registering the shares here lets both mount them.

### 10. Create the app's identity and let it pull the image

```bash
az identity create --name $ID --resource-group $RG
export ID_RES=$(az identity show -n $ID -g $RG --query id -o tsv)
export ID_CLIENT=$(az identity show -n $ID -g $RG --query clientId -o tsv)
export ID_PRINCIPAL=$(az identity show -n $ID -g $RG --query principalId -o tsv)
export ACR_ID=$(az acr show -n $ACR -g $RG --query id -o tsv)

az role assignment create --assignee-object-id $ID_PRINCIPAL \
  --assignee-principal-type ServicePrincipal --role AcrPull --scope $ACR_ID
```

*Why:* a **managed identity** is a login Azure gives the app itself — no passwords
stored anywhere. `AcrPull` lets it download the image from the registry.

**Wait about 1 minute** before the next step — new permissions take a moment to
spread.

### 11. Create the FinScan Job (the part that does the work)

This writes a settings file with your values filled in, then creates the Job from it:

```bash
cat > job.yaml <<EOF
location: $LOC
identity:
  type: UserAssigned
  userAssignedIdentities:
    $ID_RES: {}
properties:
  environmentId: $ENV_ID
  configuration:
    triggerType: Manual
    replicaTimeout: 3600
    replicaRetryLimit: 0
    manualTriggerConfig:
      parallelism: 1
      replicaCompletionCount: 1
    registries:
      - server: $ACR.azurecr.io
        identity: $ID_RES
    secrets:
      - name: openai-key
        value: $OPENAI_KEY
  template:
    containers:
      - name: finscan
        image: $ACR.azurecr.io/finscan:$TAG
        command: ["finscan"]
        args: ["companies"]
        resources:
          cpu: 1
          memory: 2Gi
        env:
          - name: FINSCAN_LLM_PROVIDER
            value: openai
          - name: OPENAI_MODEL
            value: $OPENAI_MODEL
          - name: OPENAI_API_KEY
            secretRef: openai-key
        volumeMounts:
          - volumeName: inbox
            mountPath: /app/inbox
          - volumeName: work
            mountPath: /app/_work
    volumes:
      - name: inbox
        storageType: AzureFile
        storageName: inbox
      - name: work
        storageType: AzureFile
        storageName: work
EOF

az containerapp job create --name $JOB --resource-group $RG --yaml job.yaml
export JOB_ID=$(az containerapp job show -n $JOB -g $RG --query id -o tsv)
```

*Why:* the Job is FinScan itself — it starts, runs one company, and stops, so you
pay only while it runs. Each run's company is chosen by the API (step 13); `companies`
is just the default. The OpenAI key is stored as a **secret**, not in plain text.
`replicaTimeout: 3600` allows up to an hour for a large new PDF.

### 12. Let the identity start the Job

```bash
az role assignment create --assignee-object-id $ID_PRINCIPAL \
  --assignee-principal-type ServicePrincipal --role Contributor --scope $JOB_ID
```

*Why:* the API (step 13) uses this identity to start Job runs and read their status.
The permission covers **only this Job**.

### 13. Create the FinScan API (the part you talk to)

```bash
cat > api.yaml <<EOF
location: $LOC
identity:
  type: UserAssigned
  userAssignedIdentities:
    $ID_RES: {}
properties:
  environmentId: $ENV_ID
  configuration:
    ingress:
      external: true
      targetPort: 8000
      transport: auto
    registries:
      - server: $ACR.azurecr.io
        identity: $ID_RES
    secrets:
      - name: api-key
        value: $API_KEY
  template:
    containers:
      - name: api
        image: $ACR.azurecr.io/finscan:$TAG
        command: ["finscan-api"]
        resources:
          cpu: 0.5
          memory: 1Gi
        env:
          - name: FINSCAN_API_KEY
            secretRef: api-key
          - name: FINSCAN_RUNNER
            value: azure
          - name: AZURE_SUBSCRIPTION_ID
            value: $(az account show --query id -o tsv)
          - name: AZURE_RESOURCE_GROUP
            value: $RG
          - name: AZURE_JOB_NAME
            value: $JOB
          - name: AZURE_CLIENT_ID
            value: $ID_CLIENT
        volumeMounts:
          - volumeName: inbox
            mountPath: /app/inbox
          - volumeName: work
            mountPath: /app/_work
    scale:
      minReplicas: 0
      maxReplicas: 1
    volumes:
      - name: inbox
        storageType: AzureFile
        storageName: inbox
      - name: work
        storageType: AzureFile
        storageName: work
EOF

az containerapp create --name $API --resource-group $RG --yaml api.yaml
```

**If it fails with `The JSON value could not be converted to System.Boolean`**
(an Azure-side bug in the newer API versions `az` uses to *create* a Container App,
seen October 2026 in UK South), send the same settings with the older stable API
version instead:

```bash
python3 -c "import yaml,json; json.dump(yaml.safe_load(open('api.yaml')), open('api.json','w'))"
az rest --method put --body @api.json \
  --url "https://management.azure.com$(dirname $(dirname $ENV_ID))/containerApps/$API?api-version=2024-03-01"
rm api.json
az containerapp show -n $API -g $RG --query properties.provisioningState -o tsv   # repeat until Succeeded
```

(`python3` needs PyYAML; the project's `.venv/bin/python` has it.) Updating the app
afterwards (step 14 onwards) works with the normal commands.

*Why:* the API is always reachable over HTTPS (Azure provides the address and
certificate). It never reads PDFs itself — it saves uploads, starts Job runs and
hands back results. `minReplicas: 0` means it sleeps when unused (cheapest; the
first call after a quiet spell takes ~10–20 s). `maxReplicas: 1` keeps one copy, so
"one run per company" is enforced in one place.

### 14. Tell the API its own address

```bash
export FQDN=$(az containerapp show -n $API -g $RG --query properties.configuration.ingress.fqdn -o tsv)
az containerapp update -n $API -g $RG \
  --set-env-vars FINSCAN_PUBLIC_URL=https://$FQDN FINSCAN_ALLOWED_HOSTS=$FQDN
echo "FinScan is at: https://$FQDN"
```

*Why:* upload and download links point at this address, and the MCP endpoint only
accepts requests addressed to it.

### 15. Tidy up the settings files

```bash
rm job.yaml api.yaml        # they contain your keys; Azure has its own copy now
```

---

## Part C — Check it works

### 16. Is it up?

```bash
curl -s https://$FQDN/health
```

Expect `{"status":"ok","runner":"azure","api_key_set":true}`. (If it takes a few
seconds, the API was asleep and is starting.)

In a browser: **https://<your FQDN>/docs** shows the API page.

### 17. Upload a company's files

Ask for an upload link and open it in the browser:

```bash
curl -s -H "x-api-key: $API_KEY" -X POST https://$FQDN/upload-links/CocaCola | python3 -m json.tool
```

Upload this quarter's filing PDF(s) and the model workbook. Check:

```bash
curl -s -H "x-api-key: $API_KEY" https://$FQDN/companies | python3 -m json.tool
```

The company should show `"ready": true` and `"has_mapping": true` (mappings come
from the image).

*Alternative — upload from your laptop with the CLI:*

```bash
az storage file upload-batch --account-name $SA --account-key $SA_KEY \
  --destination finscan-inbox --destination-path CocaCola --source ./inbox/CocaCola
```

*Optional — save OpenAI costs by copying your local read cache:*

```bash
az storage file upload-batch --account-name $SA --account-key $SA_KEY \
  --destination finscan-work --destination-path pdfcache --source ./_work/pdfcache
```

### 18. Run it

```bash
# dry run first: resolves and validates, writes nothing
curl -s -H "x-api-key: $API_KEY" -H "content-type: application/json" \
  -d '{"company": "CocaCola", "dry_run": true}' https://$FQDN/jobs | python3 -m json.tool

# check it (repeat until state is succeeded / refused / failed — a minute or more)
curl -s -H "x-api-key: $API_KEY" https://$FQDN/jobs/<job id> | python3 -m json.tool
```

When the dry run looks right, run it for real with `"dry_run": false`, then get the
workbook:

```bash
curl -s -H "x-api-key: $API_KEY" -X POST \
  "https://$FQDN/jobs/<job id>/download-link?kind=workbook" | python3 -m json.tool
```

Open the `url` in a browser to download.

### 19. See it in the portal

In **portal.azure.com → Resource groups → finscan-rg**:

| Open | What you see |
|---|---|
| `finscan-job` → **Execution history** | Every run, its status and duration; open one for its logs |
| `finscan-api` → **Log stream** | The API's live log |
| Storage account → **File shares** | The `inbox` and `work` folders and their files |

From the terminal: `az containerapp job execution list -n $JOB -g $RG -o table`.

---

## Part D — Living with it

### 20. Keep costs under control

- **Set a budget alert:** portal → **Cost Management → Budgets → Add** — e.g. £20/month
  with an email at 80%.
- **What costs money:** OpenAI tokens for newly read PDFs (re-runs use the cache);
  the Job only while running; the API only while awake (`minReplicas: 0`); the
  registry (Basic tier, a small monthly fee); storage (pennies).

### 21. Deploy a new version (code or mapping change)

```bash
source azure-vars.sh
export TAG=1.1
az acr build --registry $ACR --image finscan:$TAG .   # or the Docker route from step 7
az containerapp job update -n $JOB -g $RG --image $ACR.azurecr.io/finscan:$TAG
az containerapp update     -n $API -g $RG --image $ACR.azurecr.io/finscan:$TAG
```

Mappings are inside the image, so a mapping change is also a new version. A run in
Azure that appends new mapping rows reports them in its result — add them to
`mappings/` in git and deploy a new version.

### 22. Change a secret (OpenAI key or API key)

```bash
az containerapp job secret set -n $JOB -g $RG --secrets openai-key=<new key>
az containerapp secret set     -n $API -g $RG --secrets api-key=<new key>
az containerapp revision restart -n $API -g $RG \
  --revision $(az containerapp show -n $API -g $RG --query properties.latestRevisionName -o tsv)
```

### 23. Delete everything

```bash
az group delete --name $RG --yes
```

Removes every resource above in one go — and stops all costs. (The Azure account
itself stays.)

---

## Troubleshooting

| Problem | What to check |
|---|---|
| `az acr build` fails | Run it from the project folder (where `Dockerfile` is). `TasksOperationsNotAllowed` → use the Docker route in step 7. |
| Image pulled but container exits with `exec format error` | It was built on a Mac without `--platform linux/amd64`; rebuild with it. |
| Job or API stuck "provisioning" / image pull error | Step 10's `AcrPull` permission takes ~1 minute; wait and retry the create, or `az containerapp update` with the image again. |
| `/health` times out at first | The API was asleep (`minReplicas: 0`); retry after ~20 s. |
| `401 missing or wrong x-api-key` | Use the `API_KEY` from `azure-vars.sh` (`echo $API_KEY`). |
| Job state `failed`, "could not start" | Step 12's permission on the Job, and `AZURE_CLIENT_ID` on the API. Check the API's Log stream. |
| Job `failed`, "ended without a result" | Open the run in **Execution history** for its log. |
| Job `refused` | Read `result.refused` — usually the filing is not the next quarter for the model, or the folder has filings for different quarters. |
| `409 … is not ready` | `GET /companies` lists the company's `problems` (missing filing or model). |
| Run fails reading PDFs | The OpenAI key (step 22) or account credit. |
| MCP from Copilot Studio rejected | `FINSCAN_ALLOWED_HOSTS` must be the API's FQDN (step 14); the API key goes in header `x-api-key`. |
