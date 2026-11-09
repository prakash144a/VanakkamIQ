"""OpenAI Realtime SIP call-control endpoints.

All endpoint paths live here. The newer GPT-Live API uses /v1/live/sessions/{id}/...;
switching to it later only touches this module and connection.py.
"""

import logging

import httpx

log = logging.getLogger(__name__)


class RealtimeCallsAPI:
    def __init__(self, client: httpx.AsyncClient, api_key: str, base_url: str = "https://api.openai.com/v1") -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def _post(self, call_id: str, action: str, json: dict | None = None) -> None:
        url = f"{self._base}/realtime/calls/{call_id}/{action}"
        resp = await self._client.post(url, json=json, headers=self._headers, timeout=10.0)
        if resp.status_code >= 400:
            log.error("OpenAI %s failed for call %s: %s %s", action, call_id, resp.status_code, resp.text[:500])
            resp.raise_for_status()

    async def accept(self, call_id: str, session: dict) -> None:
        await self._post(call_id, "accept", session)

    async def reject(self, call_id: str, status_code: int = 603) -> None:
        await self._post(call_id, "reject", {"status_code": status_code})

    async def refer(self, call_id: str, target_uri: str) -> None:
        await self._post(call_id, "refer", {"target_uri": target_uri})

    async def hangup(self, call_id: str) -> None:
        await self._post(call_id, "hangup")
