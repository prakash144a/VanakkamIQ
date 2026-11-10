"""AI-written call summaries (milestone 7a).

When a call ends, the dashboard first shows the short rule-based summary (order placed, handed
to staff, ...). Then one small text-model call reads the transcript and writes a better one:
what the caller wanted, what was done, and anything the team still has to do. If the model call
fails or AI summaries are off, the rule-based summary simply stays.
"""

import logging

import httpx

log = logging.getLogger(__name__)

MAX_TRANSCRIPT_CHARS = 12_000

INSTRUCTIONS = """You write the one-line call log for a small business's phone assistant.
Read the call transcript and facts, then write ONE or TWO short sentences for the owner:
what the caller wanted, what happened, and any follow-up the team must do (call back, check something).
Rules: plain words; no greeting; no markdown; never invent details that are not in the transcript;
include the caller's name and order number when known; do not repeat phone numbers or addresses;
at most 280 characters."""


def transcript_text(events: list, facts: list[str]) -> str:
    lines = []
    for e in events:
        if e.role == "caller" and e.text:
            lines.append(f"Caller: {e.text}")
        elif e.role == "agent" and e.text:
            lines.append(f"Assistant: {e.text}")
        elif e.role == "tool" and e.tool_name:
            ok = (e.data or {}).get("result", {}).get("ok")
            lines.append(f"[action {e.tool_name}{'' if ok is not False else ' failed'}]")
    text = "\n".join(lines)
    if len(text) > MAX_TRANSCRIPT_CHARS:  # keep the end: that's where outcomes are
        text = "…\n" + text[-MAX_TRANSCRIPT_CHARS:]
    return "Facts:\n" + "\n".join(f"- {f}" for f in facts) + "\n\nTranscript:\n" + text


def _output_text(body: dict) -> str:
    """The text of a Responses API reply (no SDK, so read the output items directly)."""
    if isinstance(body.get("output_text"), str):
        return body["output_text"]
    parts = []
    for item in body.get("output") or []:
        if item.get("type") == "message":
            for content in item.get("content") or []:
                if content.get("type") in ("output_text", "text") and content.get("text"):
                    parts.append(content["text"])
    return "".join(parts)


class Summarizer:
    def __init__(self, http: httpx.AsyncClient, api_key: str, base_url: str, model: str, enabled: bool) -> None:
        self._http = http
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self.model = model
        self.enabled = enabled and bool(api_key) and bool(model)

    async def summarize(self, events: list, facts: list[str]) -> str | None:
        """One or two sentences, or None when off, empty or failed (the caller keeps the old summary)."""
        if not self.enabled or not any(e.role == "caller" and e.text for e in events):
            return None
        try:
            resp = await self._http.post(
                f"{self._base_url}/responses",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self.model, "instructions": INSTRUCTIONS, "input": transcript_text(events, facts),
                      "max_output_tokens": 400, "store": False},
                timeout=30.0,
            )
            resp.raise_for_status()
            text = " ".join(_output_text(resp.json()).split())
        except Exception:  # noqa: BLE001 - a summary must never break call handling
            log.warning("AI call summary failed; keeping the basic summary", exc_info=True)
            return None
        return text[:400] or None
