"""Custom domains: admin.* opens the platform, owner.* opens the business dashboard."""

from fastapi.testclient import TestClient

from app.hosts import redirect_for
from app.main import create_app
from tests.conftest import make_settings
from tests.fakes import FakeConnection

OWNER, ADMIN = "owner.vanakkamiq.example.com", "admin.vanakkamiq.example.com"


def settings(tmp_path, owner=OWNER, admin=ADMIN):
    s = make_settings(tmp_path)
    s.owner_host, s.admin_host = owner, admin
    return s


def test_routing_rules(tmp_path):
    s = settings(tmp_path)
    assert redirect_for(s, ADMIN, "GET", "/") == "/platform"
    assert redirect_for(s, OWNER, "GET", "/") == "/app"
    assert redirect_for(s, ADMIN, "GET", "/platform/usage") is None
    assert redirect_for(s, OWNER, "GET", "/app/orders") is None
    assert redirect_for(s, OWNER, "GET", "/o/abc") is None
    assert redirect_for(s, OWNER, "GET", "/platform", "days=7") == f"https://{ADMIN}/platform?days=7"
    assert redirect_for(s, ADMIN, "GET", "/app/login") == f"https://{OWNER}/app/login"
    assert redirect_for(s, ADMIN, "GET", "/o/tok") == f"https://{OWNER}/o/tok"
    # The default Azure address sends people to the right domain, but keeps webhook and health checks.
    default = "vanakkamiq.azurecontainerapps.io"
    assert redirect_for(s, default, "GET", "/app") == f"https://{OWNER}/app"
    assert redirect_for(s, default, "GET", "/platform/login") == f"https://{ADMIN}/platform/login"
    assert redirect_for(s, default, "POST", "/voice/openai/webhook") is None
    assert redirect_for(s, default, "GET", "/healthz") is None
    assert redirect_for(s, default, "GET", "/static/theme.css") is None
    assert redirect_for(s, default, "GET", "/application") is None  # only whole path segments
    # Posts are never redirected (the form body would be lost).
    assert redirect_for(s, OWNER, "POST", "/platform/login") is None


def test_no_custom_domains_changes_nothing(tmp_path):
    s = settings(tmp_path, owner="", admin="")
    for path in ("/", "/app", "/platform", "/o/x"):
        assert redirect_for(s, "anything.example.com", "GET", path) is None


def test_middleware_redirects_by_host(tmp_path):
    with TestClient(create_app(settings(tmp_path), FakeConnection)) as client:
        resp = client.get("/", headers={"host": ADMIN}, follow_redirects=False)
        assert resp.status_code == 307 and resp.headers["location"] == "/platform"
        resp = client.get("/platform/login", headers={"host": OWNER}, follow_redirects=False)
        assert resp.status_code == 308 and resp.headers["location"] == f"https://{ADMIN}/platform/login"
        login = client.get("/platform/login", headers={"host": ADMIN})
        assert login.status_code == 200 and "/static/brand/vanakkamiq-logo.svg" in login.text
        assert 'rel="icon" href="/static/brand/vanakkamiq-favicon.svg"' in login.text
        assert client.get("/static/brand/vanakkamiq-logo.svg").status_code == 200
        assert client.get("/healthz").status_code == 200
