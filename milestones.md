# VanakkamIQ Restaurant: MVP Plan

## Context
VanakkamIQ is a "virtual employee" SaaS for businesses. It answers customer calls in a warm, human-sounding voice, recognizes returning callers, handles routine work itself, and brings in a person only when needed. It reports status and conversation history to both the caller and the business admin.

The first product is **VanakkamIQ Restaurant**: phone order taking, FAQ answering, human handoff, and an admin dashboard. Later products (VanakkamIQ Receptionist for appointments, then other business types) plug into the same core. WhatsApp and web chat come later through a text-chat path that reuses the same business logic.

Decisions made:
- **Speech-to-speech voice**, with no separate speech-to-text or text-to-speech step.
- **Carrier: Telnyx** (US/Canada launch).
- **Call path for the MVP: OpenAI Realtime over SIP.** Telnyx sends calls straight to OpenAI, and our server controls each call through webhooks, the calls API and a control WebSocket. Audio never touches our server.
- **Later: our own WebSocket audio bridge**, so Gemini Live and other models can be used. It reuses the same agent core.
- **OpenAI API: Realtime (`gpt-realtime-2.1`), not GPT-Live** (decided 2026-11-08, while building). GPT-Live has no ordinary tool calls: the voice model only signals that it wants help, so we would need a second AI agent to work out what to do, and access may be early-only. The Realtime API's SIP calls support normal tool calls, which order taking needs. All endpoint paths live in `app/voice/openai_sip/calls_api.py` and `connection.py`, so moving to GPT-Live later only touches those files.
- **Python + FastAPI**, multi-business SaaS.
- **Hosting: Azure**: Container Apps (always at least 1 replica) + Container Registry. **Database: your existing Azure PostgreSQL server and database** (details only in the git-ignored `infra/secrets.local.json`), with all VanakkamIQ tables in their own `vanakkamiq` schema (`DATABASE_SCHEMA`). The deployment creates no database resources. See `infra/main.bicep` and `docs/azure-deploy.md`.

## Architecture

```
Caller ─PSTN─► Telnyx number ─SIP (TLS)─► sip:<openai_project_id>@sip.api.openai.com
                                                │ audio + gpt-realtime model
             ┌──────────────────────────────────┘
             │ 1. webhook realtime.call.incoming (call_id, SIP headers)
             ▼
   FastAPI POST /voice/openai/webhook  (verify signature)
             │ 2. resolve business by the dialed number (SIP_CALLED_NUMBER_HEADERS), caller by "From"
             │ 3. POST /v1/realtime/calls/{call_id}/accept  (instructions, voice, tools, VAD, transcription)
             │    (or /reject: e.g. unknown number)
             │ 4. open control WS  wss://api.openai.com/v1/realtime?call_id={call_id}
             ▼
   CallSession (one per call, provider-neutral core)
     ├─ vertical: RestaurantVertical → prompt, greeting, tools
     ├─ ToolRegistry → executes tool calls against the DB, returns results
     ├─ transcript + events → Postgres → admin dashboard (SSE live view)
     └─ actions: inject context, update session, transfer (/refer → staff tel:), hangup

Later: MediaStreamConnection (Telnyx WS ↔ our server ↔ Gemini Live / others) implements the same
VoiceConnection interface. ChatChannel (WhatsApp / web chat) → text LLM, same vertical + tools.
```

Key design rules:
1. **The `VoiceConnection` interface separates the agent from transport.** It emits events (`TranscriptDelta`, `ToolCall`, `CallerSpeechStarted`, `CallEnded`) and accepts commands (`send_tool_result`, `inject_context`, `update_instructions`, `say` / `response_create`, `transfer(to)`, `hangup()`). `OpenAISipConnection` comes first and `MediaStreamConnection` (the bridge) later. `CallSession` never knows which one it's using.
2. **Tools are defined once** as JSON-schema definitions plus async handlers, and each connection maps them to its provider's format.
3. **Business types are plugins.** A `Vertical` supplies `system_prompt(business, caller)`, `greeting(caller)` and `tools()`. Restaurant is the first one; Receptionist will be a sibling package.

## Project layout (uv-managed)

```
pyproject.toml, .env.example, docker-compose.yml (postgres), alembic/
app/
  main.py                     FastAPI app, router wiring
  config.py                   pydantic-settings (OpenAI key + project id + webhook secret, Telnyx key, DB URL)
  db/models.py, db/session.py SQLAlchemy 2 async models + session
  voice/
    base.py                   VoiceConnection interface + event/command dataclasses
    openai_sip/
      webhook.py              POST /voice/openai/webhook: verify signature, handle realtime.call.incoming
      calls_api.py            httpx client: accept / reject / refer / hangup
      connection.py           OpenAISipConnection: control WS, maps realtime events ↔ VoiceConnection
    session_config.py         builds the provider session (instructions, voice, tools, turn detection,
                              input transcription) from business + vertical + caller context
  agent/
    call_session.py           per-call orchestrator: sends the greeting, routes tool calls, logs the transcript,
                              finishes the call (summary, customer update)
    tools.py                  ToolRegistry
    caller_context.py         look up a returning customer by phone (name, last orders, language)
  verticals/
    base.py                   Vertical protocol
    restaurant/
      vertical.py             persona prompt (warm, "Vanakkam" style), greeting, rules, tool list
      menu_service.py         menu search, item and modifier validation, pricing
      order_service.py        draft order cart → confirm → submit
      tools.py                search_menu, add_to_order, update_order_item, remove_from_order,
                              review_order, confirm_order, answer_faq, transfer_to_human, end_call
  handoff/service.py          transfer_to_human → connection.transfer(staff number); record the reason;
                              fallback when the business is closed or nobody is available: log a callback request
  telephony/telnyx.py         Telnyx API: SMS sending (number setup is documented, not automated, in the MVP)
  notify/sms.py               SMS to caller (order confirmation) and staff (new order / escalation)
  admin/
    routes.py, auth.py        per-business staff login (session cookie, argon2)
    templates/ static/        Jinja2 + HTMX: live calls, orders board, conversation history,
                              menu/FAQ/settings editor; SSE for live updates
tests/
```

## Admin dashboard theme: Terracotta Sand
Light, modern and warm: sandy background, clay accent, rounded geometric type. Chosen from the four options on the theme canvas (https://claude.ai/artifact/LRuXrW6W6HANiLHGLE9zL8, board "2 · Terracotta Sand").

The colors and sizes are defined once as CSS custom properties in `app/admin/static/theme.css`, and every template uses those variables instead of hard-coded colors:

| Token | Value | Use |
|---|---|---|
| `--bg` | `#F7F1EB` | Page background |
| `--surface` | `#FFFDFA` | Cards, sidebar, panels |
| `--surface-2` | `#EFE5DA` | Board columns, chips, AI chat bubbles |
| `--border` | `#E4D6C8` | Card and input borders |
| `--ink` | `#2A1F1A` | Main text |
| `--muted` | `#6E5E53` | Secondary text, labels |
| `--accent` | `#A9472A` | Primary buttons, active icons, badges, links |
| `--accent-soft` | `#F6E2D8` | Active nav item, "New" chip, caller chat bubbles |
| `--on-accent` | `#FFFFFF` | Text on accent buttons |
| `--success` / `--success-soft` | `#3B6A4A` / `#E2EEE4` | "Ready" status, "AI is answering", confirmed orders |
| `--radius` | `18px` (cards), `12px` (nav items, small cards), `999px` (buttons, chips) | Corner rounding |
| `--shadow` | `0 2px 6px rgba(90,50,30,0.06)` | Soft card lift |

- **Font:** Manrope (Google Fonts; weights 400–800). Headings and big numbers use 800 with tight letter spacing (-0.03em). Body text is 14px, weight 400–600.
- **Components:** pill-shaped buttons with a minimum height of 44px, chosen so they're easy to tap. The primary button is accent with white text; the secondary is surface with a border. Status chips are soft-colored pills. Cards have a 1px border plus the soft shadow.
- **Layout:** a sidebar menu and a main content area. On phones the sidebar moves above the content and the order board columns stack.
- **Readability:** every text color passes the 4.5:1 contrast rule on the background it sits on. Accent-colored text goes only on white or near-white backgrounds, never on `--accent-soft`.

## Data model (Postgres; every table carries `business_id`)
- `businesses` (name, vertical, timezone, hours, voice name, persona/tone, handoff number, language, voice_path = `openai_sip`, later `media_stream`)
- `phone_numbers` (e164 → business, carrier)
- `staff_users` (business, email, password_hash, role)
- `menu_categories`, `menu_items` (price, available), `modifier_groups`, `modifiers`
- `faqs` (question, answer); short FAQ lists go into the prompt, and `answer_faq` searches long ones
- `customers` (phone, name, preferred language, notes, last_order_id)
- `conversations` (channel, provider, provider_call_id, caller phone, status: active/completed/escalated/abandoned, summary, timestamps)
- `conversation_events` (role: caller/agent/tool/system, text, tool name, args/result JSON, timestamp)
- `orders` (customer, conversation, status: draft/confirmed/in_kitchen/ready/completed/cancelled, pickup/delivery, address, totals), `order_items` (+ modifiers JSON)
- `handoffs` (conversation, reason, outcome)

## Call flow (restaurant)
1. A caller dials the restaurant's Telnyx number. Telnyx routes the call over its SIP connection to OpenAI's SIP URI.
2. OpenAI posts `realtime.call.incoming` to `/voice/openai/webhook`. We verify the signature and find the business from the dialed number in the SIP headers; an unknown number gets `/reject`. We look up the caller from `From`, then build the session config: persona, menu summary, hours and FAQs, caller context, tools, voice, VAD, and input transcription. Then we `/accept`. This must be fast, so the caller lookup is one indexed query and menus are cached per business.
3. We create the `conversation` row and open the control WebSocket for the `call_id`. We send `response.create` so the AI speaks the greeting, personalized for returning callers (e.g. "Vanakkam Priya! Would you like your usual chicken biryani?").
4. Tool calls arrive over the WebSocket. `CallSession` runs the handler, saves the order changes, and returns the result. Transcripts are stored and pushed live to the dashboard.
5. Before calling `confirm_order`, the prompt requires the AI to read the full order and total back to the caller. Confirming sets the order to confirmed, sends it to the dashboard orders board, and texts the caller (items, total, pickup time) and staff.
6. `transfer_to_human` is used when the caller asks for a person, the AI fails to resolve the request twice, the caller has a complaint or an allergy-safety question, or a large order exceeds a threshold. The call is transferred with `/refer` to `tel:<staff number>` and the dashboard shows "escalated".
7. When the call ends (`end_call` → `/hangup`, or the caller hangs up and the WebSocket closes), mark the conversation finished, generate a summary (one text-LLM call over the transcript), and update the customer record.

## Build milestones
Status as of 2026-11-09: milestones 1–4 and 5a are written, and all 36 tests pass (run inside the Azure image build, since PyPI is blocked on the dev network). The app is deployed to Azure and healthy. Not yet tested with a real phone call.

0. ✅ **Plan saved** as `milestones.md` (this file).
1. 🟡 **Scaffold** (written): pip/venv project (`pyproject.toml`), config, SQLAlchemy models, Alembic, seed script with a demo restaurant, menu and FAQs. SQLite locally and in tests, PostgreSQL on Azure. No docker-compose needed.
2. 🟡 **First call** (written): OpenAI webhook + signature check, calls API, control WebSocket connection, `VoiceConnection` base, `CallManager` + `CallSession` with greeting and transcript logging. Setup steps: `docs/telnyx-openai-sip.md`.
3. 🟡 **Restaurant agent** (written): tool registry, menu search tolerant of spoken variants, order cart with options and tax, restaurant tools and instructions, returning-caller context.
4. 🟡 **Handoff + notifications** (written): transfer with `/refer`, callback-message fallback when nobody is available or the restaurant is closed, SMS through Telnyx.
5a. 🟡 **Platform admin: add a restaurant** (written): operator login (`python -m app.cli create-platform-admin`), restaurant list, an add-restaurant form covering details, hours, pickup/delivery, tax, phone numbers, voice, owner login, a menu pasted as text, and FAQs, plus a per-restaurant setup checklist, phone-connection steps and a pause/resume switch for answering. All in the Terracotta Sand theme, with CSRF-protected forms.
5. ⬜ **Restaurant dashboard**: owner login, live calls, orders board, conversation history with transcripts, menu/FAQ/settings editor (Terracotta Sand theme). Also: transcript retention setting; let platform admins edit restaurant details after creation.
5b. ⬜ **Self-service sign-up** (later): sign-up wizard, AI menu import from a photo or PDF, automatic Telnyx number purchase, billing.
6. ✅ **Azure deployment**: Container App `vanakkamiq` in resource group `vanakkamiq-rg` (West US), running on the existing PostgreSQL database (schema `vanakkamiq`). The image is built in Azure with the tests inside the build (`Dockerfile` test stage). GitHub Actions (`.github/workflows/deploy.yml`) tests every push and deploys `main`; OIDC setup is in `scripts/setup-github-deploy.sh`. Guide: `docs/azure-deploy.md`.
7. ⬜ **Later**: media-stream bridge (Telnyx WebSocket ↔ server) with Gemini Live and backup-model support, ChatChannel (WhatsApp / web chat), Receptionist vertical (appointments), payments, POS integrations, LLM-written call summaries.
6b. ⬜ **Routing calls for several restaurants** (needed before the second restaurant): Telnyx's Translated Number replaces the dialed number with the OpenAI project ID, and the SIP headers then carry only the caller's number (seen on the first real call, 2026-11-09). Today `single_restaurant_fallback` answers for the only active restaurant that has a number, and never guesses when there are two or more. Options: ask Telnyx to add the dialed number as a custom header (e.g. `X-Called-Number`, which the app already checks); route through TeXML `<Dial><Sip>` with a header; or use one OpenAI project per restaurant.

## Verification
- **Unit tests (pytest)**: menu and order services (pricing, modifiers, unavailable items), tool handlers, session config builder, and webhook signature verification.
- **Integration test**: post a recorded `realtime.call.incoming` payload to the webhook with `calls_api` mocked (respx), and check that `/accept` gets the correct per-business config. A fake control-WebSocket server sends scripted events (tool calls, transcripts); check that an order is created, the tool results go back, and transfer triggers `/refer`.
- **End-to-end**: run `uvicorn` + `ngrok`, set the ngrok URL as the OpenAI project webhook, point the Telnyx number's SIP connection at the OpenAI SIP URI, then call it:
  - Place an order, hear the readback, receive the SMS, and see the order appear live on the dashboard.
  - Call again from the same phone and check the personalized greeting.
  - Say "let me talk to a person" and check the call goes to the staff phone and the dashboard shows "escalated".
  - Call from an unassigned number and check the call is rejected.

## Prerequisites from you (at build time)
- Telnyx account with a US number and a SIP connection set to send calls to OpenAI's SIP URI (Telnyx publishes a guide for this), plus a Telnyx API key for SMS.
- OpenAI **API platform** account (platform.openai.com, pay-as-you-go with prepaid credits; this is separate from a ChatGPT subscription). You'll need:
  - A project that can receive SIP calls. If incoming calls never reach the webhook, ask OpenAI support to confirm SIP is enabled for the project.
  - The project ID, an API key from that same project, and a webhook endpoint subscribed to `realtime.call.incoming` with its signing secret.
- An Azure subscription (for deployment).
  - Expect roughly $0.02–$0.15/min in model usage depending on the model. Higher usage tiers raise the limit on how many calls can run at once.
- A staff phone number for handoff tests.
- **SMS compliance**: US business texting needs 10DLC brand and campaign registration (or toll-free number verification). Start it early because approval can take days to weeks. Voice readback and the dashboard work without it.
