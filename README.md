# VanakkamIQ

A virtual employee that answers phone calls for any business. Every business gets the core: it answers questions from saved answers, hours and closures, greets returning callers by name, takes messages for a call back, and hands the call to a person when needed. Businesses that sell things can switch on **phone orders**: the AI takes pickup and delivery orders from their menu or product list, staff work them on a live orders board, and callers get a text with a status link.

The **business type** (restaurant, shop, service business, other) only changes wording and defaults: "menu" or "products", "Preparing" or "Being packed", which questions need a person.

See [milestones.md](milestones.md) for the plan and progress.

## How a call works

Telnyx number → SIP → OpenAI Realtime (voice model). OpenAI sends a webhook to this server. The server accepts the call with the business's details, saved answers, catalog (if it takes orders) and the caller's history, then runs the AI's tools (saved answers, messages, transfer, and with orders: catalog search, cart, confirmation) over a control WebSocket. Audio never passes through this server.

## Local development

Requires Python 3.12.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"    # Windows (Git Bash)
cp .env.example .env
.venv/Scripts/python -m pytest
.venv/Scripts/python -m app.seed                     # demo restaurant (takes orders) in a local SQLite file
.venv/Scripts/python -m uvicorn app.main:app --reload
```

To take a real call locally, expose port 8000 with ngrok and follow [docs/telnyx-openai-sip.md](docs/telnyx-openai-sip.md).

## Screens

| Who | Where | What |
|---|---|---|
| VanakkamIQ operators | `/platform` | Add a business (type, orders on/off, hours, numbers, catalog, saved answers, owner login), edit it (including its timezone), pause it, see calls and transcripts, usage and estimated AI cost, manage operator logins |
| Business owner / staff | `/app` | Live orders board with sound alerts, calls (live transcript while on the phone), messages to call back, customers and notes for the AI, menu/products with one-tap "unavailable" (add items one by one, edit as text, or upload / download a JSON file: see `docs/sample-menu.json`), saved answers, settings (hours, holiday closures, numbers, voice, greeting, transcript retention) |
| Callers | `/o/<token>` | Order status page, linked from the confirmation text |

**Sign-in is passwordless** for operators and business staff alike: enter your email, type the 6-digit code emailed to you (Azure Communication Services, same setup as EverMindIQ: valid 10 minutes, 5 tries, 60 seconds between codes), and the session lasts 30 days from your last visit. Locally, without `ACS_CONNECTION_STRING`, the code is printed in the server log. Staff logins (`role = staff`) can work orders, messages and availability but can't change settings or the catalog.

## Adding a business (platform admin)

Create your operator login once, then open http://localhost:8000/platform:

```bash
.venv/Scripts/python -m app.cli create-platform-admin --email you@example.com --name "Your Name"
```

**Add business** asks for the type and whether it takes phone orders, then the details, opening hours and closures, the VanakkamIQ phone number, transfer and alert numbers, the catalog (pasted as text, format shown on the form) and saved answers, the AI's voice and greeting, and the owner's login. The business's page then shows a setup checklist and the steps to connect the phone number.

## Deploying

Azure Container Apps + PostgreSQL: [docs/azure-deploy.md](docs/azure-deploy.md).

## Layout

```
app/voice/            provider-neutral VoiceConnection + OpenAI SIP webhook, call API, control socket
app/agent/            CallManager (accepts calls), CallSession (one live call), assistant builder (instructions,
                      greeting, tools from the business type + switched-on modules), tool registry, caller context
app/capabilities/     core.py: saved answers, messages, transfer, end call (every business)
                      ordering/: catalog search, order cart, confirmation, status flow (when takes_orders is on)
app/business_types.py wording and defaults per business type
app/onboarding/       business form checks (shared by add, edit and owner settings), catalog text import/export
app/admin/            /platform (operators), /app (business dashboard), /o (caller's order page); Jinja2, Terracotta Sand
app/retention.py      deletes transcripts past each business's retention setting
app/handoff/          transfer to staff, or take a callback message
app/notify/           SMS to caller and staff (Telnyx)
app/db/               SQLAlchemy models (multi-tenant: every table has business_id)
infra/main.bicep      Azure resources
```
