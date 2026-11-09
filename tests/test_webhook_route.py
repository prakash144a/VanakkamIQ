import json
import time

from fastapi.testclient import TestClient

from app.main import create_app
from app.voice.webhook_signature import sign
from tests.conftest import WEBHOOK_SECRET, make_settings
from tests.fakes import FakeConnection


def _signed(body: dict, msg_id: str = "wh_1") -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    return raw, {
        "webhook-id": msg_id, "webhook-timestamp": ts,
        "webhook-signature": sign(WEBHOOK_SECRET, msg_id, ts, raw), "content-type": "application/json",
    }


def test_webhook_rejects_bad_signature(tmp_path):
    app = create_app(make_settings(tmp_path), FakeConnection)
    with TestClient(app) as client:
        resp = client.post("/voice/openai/webhook", content=b"{}", headers={
            "webhook-id": "x", "webhook-timestamp": str(int(time.time())), "webhook-signature": "v1,bad",
        })
        assert resp.status_code == 400


def test_webhook_ignores_other_events_and_duplicates(tmp_path):
    app = create_app(make_settings(tmp_path), FakeConnection)
    with TestClient(app) as client:
        raw, headers = _signed({"type": "realtime.call.ended", "data": {}})
        first = client.post("/voice/openai/webhook", content=raw, headers=headers)
        assert first.status_code == 200 and first.json()["ignored"] == "realtime.call.ended"
        again = client.post("/voice/openai/webhook", content=raw, headers=headers)
        assert again.json().get("duplicate") is True
