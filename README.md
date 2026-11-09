# VanakkamIQ

A virtual employee that answers phone calls for businesses. The first product is **VanakkamIQ Restaurant**: it takes pickup and delivery orders, answers questions, greets returning callers by name, and hands the call to a person when needed.

See [milestones.md](milestones.md) for the plan and progress.

## How a call works

Telnyx number → SIP → OpenAI Realtime (voice model). OpenAI sends a webhook to this server. The server accepts the call with the restaurant's menu, rules and the caller's history, then runs the AI's tools (menu search, order cart, confirmation, transfer) over a control WebSocket. Audio never passes through this server.

## Local development

Requires Python 3.12.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"    # Windows (Git Bash)
cp .env.example .env
.venv/Scripts/python -m pytest
.venv/Scripts/python -m app.seed                     # demo restaurant in a local SQLite file
.venv/Scripts/python -m uvicorn app.main:app --reload
```

To take a real call locally, expose port 8000 with ngrok and follow [docs/telnyx-openai-sip.md](docs/telnyx-openai-sip.md).

## Adding restaurants (platform admin)

Create your operator login once, then open http://localhost:8000/platform:

```bash
.venv/Scripts/python -m app.cli create-platform-admin --email you@example.com --name "Your Name"
```

**Add restaurant** takes the details, opening hours, the VanakkamIQ phone number, transfer and alert numbers, the AI's voice, the owner's login, the menu (pasted as text, format shown on the form) and FAQs. After you save it, the restaurant's page shows a setup checklist and the steps to connect the phone number, and lets you pause or resume call answering.

## Deploying

Azure Container Apps + PostgreSQL: [docs/azure-deploy.md](docs/azure-deploy.md).

## Layout

```
app/voice/            provider-neutral VoiceConnection + OpenAI SIP webhook, call API, control socket
app/agent/            CallManager (accepts calls), CallSession (one live call), tool registry, caller context
app/verticals/        business types; restaurant/ has menu search, order cart, tools and the AI's instructions
app/onboarding/       add-a-restaurant form checks, menu text import, setup checklist
app/admin/            platform admin screens (Jinja2 templates, Terracotta Sand theme)
app/handoff/          transfer to staff, or take a callback message
app/notify/           SMS to caller and staff (Telnyx)
app/db/               SQLAlchemy models (multi-tenant: every table has business_id)
infra/main.bicep      Azure resources
```
