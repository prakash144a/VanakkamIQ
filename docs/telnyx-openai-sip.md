# Connecting a Telnyx number to OpenAI (Realtime over SIP)

Calls go Telnyx → OpenAI directly. Our server only receives a webhook and controls the call.

## 1. OpenAI (platform.openai.com), all in ONE project

1. **Settings → General**: copy the **Project ID** (`proj_...`) into `openaiProjectId`.
2. **API keys**: create a key in this project and put it in `openaiApiKey`.
3. **Webhooks**: add an endpoint:
   - URL: `https://<your-app>/voice/openai/webhook`
   - Event: `realtime.call.incoming`
   - Copy the signing secret (`whsec_...`) into `openaiWebhookSecret`, then redeploy.

A key, webhook or project ID from a different project causes `session_id_not_found` errors.

## 2. Telnyx: SIP connection to OpenAI

**Mission Control → Voice → SIP Trunking → Add SIP Connection**

1. Type **FQDN**, name `OpenAI Realtime`.
2. **Authentication and routing → Add FQDN**: `sip.api.openai.com`, DNS record type **A**, port **5061**.
3. **Inbound → SIP transport protocol: TLS**. Save.
4. **Encrypted media: SRTP** (in the connection's configuration section). Set it *after* TLS is saved, because SRTP requires TLS signaling.
5. Codecs: Telnyx's defaults include PCMU/PCMA (G.711), which OpenAI accepts. Opus is better if offered. Confirm the negotiated codec in call diagnostics.
6. Don't enable Media Streaming on this connection.
7. For transfers to staff: attach an **Outbound Voice Profile** that allows US/Canada calls (**Voice → Outbound Voice Profiles**), and make sure SIP REFER / transfers are allowed.

## 3. Telnyx: assign the number

**Numbers → My Numbers →** the number:

1. Connection: **OpenAI Realtime**.
2. **Voice → Translated Number**: the OpenAI Project ID including `proj_`. OpenAI routes the call to your project by this value.
3. For SMS (later): attach a Messaging Profile; US business texting also needs 10DLC registration.

## 4. VanakkamIQ

The number must be the restaurant's **VanakkamIQ number** (platform admin → restaurant). The app finds the restaurant by looking for a registered number in these SIP headers, in order: `X-Called-Number`, `Diversion`, `P-Called-Party-ID`, `To` (setting `SIP_CALLED_NUMBER_HEADERS`).

Because the Translated Number replaces the dialed number with the project ID, check the first call's log line `Incoming call ... SIP headers: [...]` to see which header (if any) still carries the dialed number.

## 5. Test call and logs

```bash
az containerapp logs show -g vanakkamiq-rg -n vanakkamiq --tail 100 --format text
```

| Log shows | Meaning |
|---|---|
| nothing | Webhook not arriving: URL, event subscription, number assignment, Translated Number |
| `Rejected webhook: ...` | Signing secret mismatch |
| `Ignoring OpenAI webhook event 'live....'` | The webhook is subscribed to a different event type; subscribe to `realtime.call.incoming` |
| `no active business matches the dialed number; rejecting` | Dialed number not in the expected headers; see the SIP headers on the line above |
| call connects, no audio | TLS / SRTP / codec negotiation: check Telnyx SIP call diagnostics |
