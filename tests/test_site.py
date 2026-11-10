"""The public website and "Book a demo" (5b): saved for the platform admin and emailed to info@."""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import site_routes
from app.db.models import DemoRequest
from app.hosts import redirect_for
from app.main import create_app
from tests.conftest import make_settings
from tests.fakes import FakeConnection
from tests.test_dashboard import _all
from tests.test_platform_admin import _prepare_db, csrf_from, sign_in


@pytest.fixture(autouse=True)
def fresh_rate_limit():
    site_routes.limiter.hits.clear()
    yield
    site_routes.limiter.hits.clear()


@pytest.fixture
def client(tmp_path):
    settings = make_settings(tmp_path)
    asyncio.run(_prepare_db(settings.database_url))
    with TestClient(create_app(settings, FakeConnection)) as c:
        c.db_url = settings.database_url
        c.outbox = c.app.state.services.otp.email.outbox
        yield c


def lead(**overrides) -> dict:
    data = {"name": "Lakshmi", "business_name": "Chennai Spice House", "business_type": "restaurant",
            "email": "lakshmi@example.com", "phone": "", "message": "We get 80 calls a day.",
            "website": "", "t": str(int(time.time()) - 30)}
    data.update(overrides)
    return data


def leads(client) -> list[DemoRequest]:
    return asyncio.run(_all(client.db_url, select(DemoRequest).order_by(DemoRequest.id)))


def test_home_page_and_its_files(client):
    page = client.get("/")
    assert page.status_code == 200
    for text in ("Your business phone,", "answered.", "in any language.", "Hear the call", 'action="/demo"',
                 'name="website"', "/static/site/site.css"):
        assert text in page.text, text
    for path in ("/static/site/site.css", "/static/site/site.js", "/static/site/calls.json",
                 "/static/site/call-restaurant.wav", "/static/site/call-salon.wav"):
        assert client.get(path).status_code == 200, path
    calls = client.get("/static/site/calls.json").json()
    assert "Dhivya" in calls["restaurant"]["lines"][0][2] and "Priya" not in str(calls)


def test_demo_request_is_saved_and_emailed_with_reply_to_the_visitor(client):
    resp = client.post("/demo", data=lead(), headers={"Accept": "application/json"})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    [saved] = leads(client)
    assert (saved.business_name, saved.business_type, saved.status, saved.email_sent) == \
        ("Chennai Spice House", "restaurant", "new", True)
    to, subject, text, reply_to = client.outbox[-1]
    assert to == "info@nativemindsai.com" and "Chennai Spice House" in subject
    assert "We get 80 calls a day." in text and reply_to == ("lakshmi@example.com", "Lakshmi")


def test_phone_only_request_and_no_javascript_fallback(client):
    resp = client.post("/demo", data=lead(email="", phone="+1 312 555 0142"), follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/?demo=sent#demo"
    assert len(client.outbox[-1]) == 3  # no reply-to without an email address


def test_bad_requests_are_refused(client):
    for bad, message in ((lead(email="", phone=""), "email or a phone"), (lead(email="nope"), "email address"),
                         (lead(name=""), "your name"), (lead(message="x" * 4000), "too long")):
        resp = client.post("/demo", data=bad, headers={"Accept": "application/json"})
        assert resp.status_code == 400 and message in resp.json()["message"]
    assert leads(client) == []


def test_bots_are_quietly_ignored(client):
    for bot in (lead(website="http://spam.example"), lead(t=str(int(time.time())))):
        resp = client.post("/demo", data=bot, headers={"Accept": "application/json"})
        assert resp.status_code == 200 and resp.json()["ok"] is True
    assert leads(client) == [] and not client.outbox


def test_rate_limit(client):
    for _ in range(site_routes.RATE_LIMIT):
        assert client.post("/demo", data=lead(), headers={"Accept": "application/json"}).status_code == 200
    assert client.post("/demo", data=lead(), headers={"Accept": "application/json"}).status_code == 429


def test_platform_admin_follows_up_and_onboards(client):
    client.post("/demo", data=lead(), headers={"Accept": "application/json"})
    [saved] = leads(client)
    sign_in(client)
    inbox = client.get("/platform/demo-requests")
    assert inbox.status_code == 200 and "Chennai Spice House" in inbox.text and "lakshmi@example.com" in inbox.text
    client.post(f"/platform/demo-requests/{saved.id}", data={"csrf": csrf_from(inbox.text), "status": "contacted",
                                                            "notes": "Demo Friday 3 PM"})
    [saved] = leads(client)
    assert saved.status == "contacted" and saved.notes == "Demo Friday 3 PM"

    form = client.get(f"/platform/businesses/new?demo={saved.id}")
    assert 'value="Chennai Spice House"' in form.text and 'value="lakshmi@example.com"' in form.text
    from tests.test_onboarding import valid_form
    created = client.post("/platform/businesses", data={**valid_form(owner_email="lakshmi@example.com"),
                                                        "demo_request": str(saved.id), "csrf": csrf_from(form.text)})
    assert created.status_code == 200
    [saved] = leads(client)
    assert saved.status == "onboarded" and "Onboarded as Chennai Spice House" in saved.notes
    assert "Chennai Spice House" not in client.get("/platform/demo-requests").text  # no longer to follow up


def test_site_host_serves_the_website(tmp_path):
    s = make_settings(tmp_path)
    s.owner_host, s.admin_host, s.site_host = "owner.x.com", "admin.x.com", "x.com"
    assert redirect_for(s, "x.com", "GET", "/") is None
    assert redirect_for(s, "x.com", "GET", "/app/login") == "https://owner.x.com/app/login"
    assert redirect_for(s, "x.com", "POST", "/demo") is None
    assert redirect_for(s, "owner.x.com", "GET", "/") == "/app"
