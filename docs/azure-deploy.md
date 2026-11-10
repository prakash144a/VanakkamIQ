# Deploying VanakkamIQ to Azure

**What gets created** (`infra/main.bicep`):

| Resource | Purpose | Rough cost |
|---|---|---|
| Container App (0.5 vCPU, 1 GiB, 1–3 replicas) | The API: webhooks, call control, admin screens | ~$30–45/mo for one always-on replica |
| Azure Container Registry (Basic) | Stores the app image | ~$5/mo |
| Log Analytics workspace | Logs | pay per GB, small at MVP volume |

**Not created: the database.** VanakkamIQ uses an existing Azure PostgreSQL Flexible Server and database. All its tables go in their own schema (`vanakkamiq` by default, created automatically), so nothing else in that database is touched.

The app never scales to zero, because an incoming call must be answered immediately. Images are built in Azure (`az acr build`), so you don't need Docker locally, and the build runs the full test suite: it fails, and nothing ships, if any test fails.

## 1. Secrets file (never committed)

Copy `infra/secrets.example.json` to `infra/secrets.local.json` (git-ignored) and fill in:

- `databaseUrl`: `postgresql+asyncpg://<USER>:<PASSWORD>@<SERVER>.postgres.database.azure.com:5432/<DATABASE>?ssl=require`. URL-encode special characters in the password (`@` → `%40`, `:` → `%3A`, `/` → `%2F`, `#` → `%23`, `%` → `%25`).
- `secretKey`: a long random string (e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"`).
- `openaiApiKey`, `openaiProjectId`: from your OpenAI project. You can leave `openaiWebhookSecret` empty on the first deploy; you create the webhook once the app's URL exists (step 5).
- `telnyxApiKey`: optional (SMS).
- `acsConnectionString`: Azure Communication Services email (`endpoint=https://...;accesskey=...`). **Required**: sign-in codes are emailed, and nobody can sign in without it. The sender (`acsSender`) defaults to `DoNotReply@nativemindsai.com`, the same verified sender EverMindIQ uses.

## 2. Base resources (once)

```bash
az group create --name vanakkamiq-rg --location westus   # same region as the database
az deployment group create -g vanakkamiq-rg -f infra/main.bicep -p deployApp=false
```

Note the `acrName` output.

## 3. Build the image (tests run inside the build)

```bash
az acr build -r <acrName> -t vanakkamiq:v1 .
```

## 4. Deploy the app

```bash
az deployment group create -g vanakkamiq-rg -f infra/main.bicep \
  -p deployApp=true image=<acrName>.azurecr.io/vanakkamiq:v1 databaseSchema=vanakkamiq \
  -p @infra/secrets.local.json
```

Outputs: `appUrl`, `webhookUrl`, and `outboundIps` (the addresses the app uses to reach the database).

On start, the container creates the schema and tables (`scripts/start.sh` runs the migrations).

## 5. Database firewall and OpenAI webhook

Allow each `outboundIps` address through the database firewall:

```bash
az postgres flexible-server firewall-rule create -g <DB_RESOURCE_GROUP> --server-name <SERVER> \
  --rule-name vanakkamiq-app-1 --start-ip-address <IP> --end-ip-address <IP>
```

If the server has an allow-everything rule (0.0.0.0–255.255.255.255), remove it once the app's rules are in place. It lets anyone on the internet try passwords.

In the OpenAI project, add a webhook to `webhookUrl` for incoming realtime calls. Put its signing secret in `openaiWebhookSecret` and run step 4 again.

## 6. Platform admin login

```bash
az containerapp exec -g vanakkamiq-rg -n vanakkamiq --command "python -m app.cli create-platform-admin --email you@example.com"
```

Then sign in at `<appUrl>/platform` and use **Add restaurant**.

## 7. Deploying a new version

Pushes to GitHub never deploy. `.github/workflows/deploy.yml` only runs the tests on every push and pull request.

**Manual deploy from your machine** (the tests run inside the build; a failing test stops it):

```bash
az acr build -r <acrName> -t vanakkamiq:v7 .
az containerapp update -g vanakkamiq-rg -n vanakkamiq --image <acrName>.azurecr.io/vanakkamiq:v7
```

**Optional: deploy from GitHub with a button.** After the one-time setup below, go to Actions → "Test and deploy" → Run workflow on `main`. It builds the image in Azure, rolls it out and checks `/healthz`.

One-time setup (creates an Azure identity that only this repository's `production` environment can use, with no stored password, and saves its IDs in GitHub):

```bash
bash scripts/setup-github-deploy.sh <owner>/<repo> vanakkamiq-rg <acrName>
```

The workflow only changes the image. App settings and secrets stay on the Container App; change them with step 4.

**After changing only a secret's value** (e.g. a new webhook secret), the running app keeps the old value until a new revision starts:

```bash
az containerapp update -g vanakkamiq-rg -n vanakkamiq --revision-suffix s$(date +%m%d%H%M)
```

Each live call runs inside the replica that answered it. During a rollout, the old replica gets up to 5 minutes (`terminationGracePeriodSeconds: 300`) to finish its calls.

## 8. Logs

```bash
az containerapp logs show -g vanakkamiq-rg -n vanakkamiq --follow
```

## Later hardening

- Move secrets to Azure Key Vault, referenced from the Container App.
- Use a dedicated database user with rights only on the `vanakkamiq` schema, instead of the server admin.
- Put the database on a private network (VNet) instead of public firewall rules.
- Add GitHub environment protection rules (required reviewers) on `production`.
- Add a custom domain and managed certificate.
