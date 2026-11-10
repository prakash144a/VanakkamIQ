import logging

import httpx

log = logging.getLogger(__name__)


class TelnyxClient:
    def __init__(self, client: httpx.AsyncClient, api_key: str, base_url: str = "https://api.telnyx.com/v2") -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def send_sms(self, from_number: str, to_number: str, text: str) -> None:
        resp = await self._client.post(
            f"{self._base}/messages",
            json={"from": from_number, "to": to_number, "text": text},
            headers=self._headers,
            timeout=10.0,
        )
        if resp.status_code >= 400:
            log.error("Telnyx SMS to ***%s failed: %s %s", to_number[-4:], resp.status_code, resp.text[:300])
            resp.raise_for_status()

    async def transfer_call(self, call_control_id: str, to: str, *, custom_headers: list[dict] | None = None,
                            from_: str | None = None) -> None:
        """Voice API (Call Control): send a call to a new destination (a SIP URI or a +1 number)."""
        body: dict = {"to": to}
        if custom_headers:
            body["custom_headers"] = custom_headers
        if from_:
            body["from"] = from_
        resp = await self._client.post(
            f"{self._base}/calls/{call_control_id}/actions/transfer", json=body, headers=self._headers, timeout=10.0,
        )
        if resp.status_code >= 400:
            log.error("Telnyx transfer failed: %s %s", resp.status_code, resp.text[:300])
            resp.raise_for_status()
