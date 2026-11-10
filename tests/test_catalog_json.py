"""Catalog as JSON: checking a file, previewing changes, uploading and downloading."""

import json
from pathlib import Path

from sqlalchemy import select

from app.db.models import CatalogItem
from app.onboarding.catalog_json import parse_catalog_json
from tests.test_dashboard import _all, client, csrf_from, sign_in  # noqa: F401 - client is a fixture

SAMPLE = {
    "sections": [
        {"name": "Biryani", "items": [{
            "id": "BIR-001", "name": "Chicken Biryani", "price": 15.99, "description": "Basmati rice, spiced chicken",
            "aliases": ["chicken biriyani"], "tags": ["Spicy", "halal"],
            "options": [
                {"name": "Spice level", "min": 1, "max": 1, "choices": [{"name": "Mild"}, {"name": "Hot"}]},
                {"name": "Add-ons", "max": 2, "choices": [{"name": "Extra raita", "price": "1.50"},
                                                          {"name": "Boiled egg", "price": 2, "available": False}]},
            ]}]},
        {"name": "Drinks", "items": [{"name": "Mango Lassi", "price": "4.99", "available": False}]},
    ]
}


def test_valid_file_is_read_with_defaults():
    catalog, errors = parse_catalog_json(json.dumps(SAMPLE))
    assert errors == []
    biryani = catalog[0].items[0]
    assert (biryani.external_id, biryani.price_cents, biryani.tags) == ("BIR-001", 1599, ["spicy", "halal"])
    spice, addons = biryani.groups
    assert (spice.min_select, spice.max_select) == (1, 1)
    assert (addons.min_select, addons.max_select) == (0, 2)
    assert addons.options == [("Extra raita", 150, True), ("Boiled egg", 200, False)]
    assert catalog[1].items[0].available is False and catalog[1].items[0].price_cents == 499


def test_every_problem_is_reported_with_its_location():
    bad = {"sections": [{"name": "Dosa", "items": [
        {"name": "Masala Dosa", "price": "12.999"},
        {"name": "Masala Dosa", "price": 10, "colour": "red"},
        {"price": 5},
        {"name": "Thali", "price": 20, "id": "X1", "options": [{"name": "Curry", "max": 3, "choices": [{"name": "Dal"}]}]},
        {"name": "Lassi", "price": -4, "id": "X1", "available": "yes"},
    ]}]}
    _, errors = parse_catalog_json(json.dumps(bad))
    text = "\n".join(errors)
    for expected in ("Dosa → Masala Dosa: \"price\" should be a positive amount", "unknown field colour",
                     "is already in Dosa", "items[2]: \"name\" is required", "Thali → options[0] (Curry): \"max\" is 3",
                     "Lassi: \"available\" should be true or false", "id \"X1\" is also used by Dosa → Thali"):
        assert expected in text, expected


def test_not_json_or_wrong_shape():
    assert "isn't valid JSON" in parse_catalog_json("{oops")[1][0]
    assert "\"sections\" list" in parse_catalog_json("[]")[1][0]
    assert "no items" in parse_catalog_json('{"sections": []}')[1][0]
    assert "too large" in parse_catalog_json(b" " * 1_000_001)[1][0]


def test_sample_file_in_docs_is_valid():
    sample = Path(__file__).parent.parent / "docs" / "sample-menu.json"
    if sample.exists():  # docs aren't copied into the image build
        catalog, errors = parse_catalog_json(sample.read_bytes())
        assert errors == [] and sum(len(c.items) for c in catalog) > 30


def _upload(client, payload: dict | str, name: str = "menu.json"):
    page = client.get("/app/catalog/upload")
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return client.post("/app/catalog/upload", data={"csrf": csrf_from(page.text)},
                       files={"file": (name, body.encode(), "application/json")})


def test_upload_previews_then_replaces(client):
    sign_in(client)
    preview = _upload(client, SAMPLE)
    assert preview.status_code == 200 and "Check the changes" in preview.text
    assert "Chicken Biryani" in preview.text and "Masala Dosa" in preview.text  # new / removed
    items = asyncio_all(client)
    assert [i.name for i in items] == ["Masala Dosa"]  # nothing saved yet

    payload = csrf_from(preview.text)
    hidden = preview.text.split('name="catalog_json" hidden aria-hidden="true">')[1].split("</textarea>")[0]
    import html
    resp = client.post("/app/catalog/upload/confirm", data={"csrf": payload, "catalog_json": html.unescape(hidden)},
                       follow_redirects=False)
    assert resp.status_code == 303
    items = {i.name: i for i in asyncio_all(client)}
    assert set(items) == {"Chicken Biryani", "Mango Lassi"}
    assert items["Chicken Biryani"].external_id == "BIR-001" and items["Mango Lassi"].is_available is False
    egg = items["Chicken Biryani"].modifier_groups[1].modifiers[1]
    assert egg.name == "Boiled egg" and egg.is_available is False


def test_upload_with_problems_changes_nothing(client):
    sign_in(client)
    resp = _upload(client, {"sections": [{"name": "X", "items": [{"name": "Y"}]}]})
    assert resp.status_code == 422 and "&#34;price&#34; is required" in resp.text
    assert [i.name for i in asyncio_all(client)] == ["Masala Dosa"]
    assert _upload(client, "not json").status_code == 422


def test_download_round_trips_through_upload(client):
    sign_in(client)
    exported = client.get("/app/catalog/export.json")
    assert exported.status_code == 200 and "attachment" in exported.headers["content-disposition"]
    data = exported.json()
    assert data["sections"][0]["items"][0]["name"] == "Masala Dosa"
    catalog, errors = parse_catalog_json(json.dumps(data))
    assert errors == []
    preview = _upload(client, data)
    assert ">0</span>" in preview.text.split("Changed")[1][:120]  # nothing changes on a round trip


def test_staff_cannot_upload(client):
    import asyncio

    from app.db.models import StaffUser
    from app.db.session import make_engine, make_session_factory

    async def demote():
        engine = make_engine(client.db_url)
        async with make_session_factory(engine)() as db:
            (await db.get(StaffUser, 1)).role = "staff"
            await db.commit()
        await engine.dispose()
    asyncio.run(demote())
    sign_in(client)
    assert client.get("/app/catalog/upload").status_code == 403
    assert client.get("/app/catalog/export.json").status_code == 200  # reading is fine


def asyncio_all(client) -> list[CatalogItem]:
    import asyncio
    return asyncio.run(_all(client.db_url, select(CatalogItem).where(CatalogItem.business_id == client.ids["shop"])
                                                              .order_by(CatalogItem.id)))
