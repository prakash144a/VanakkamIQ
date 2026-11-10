"""Owners manage their team; each call records how it was routed to its business."""

import re

import httpx
import respx
from sqlalchemy import select

from app.agent.call_manager import SINGLE_BUSINESS
from app.db.models import Business, Conversation, PhoneNumber, StaffUser
from tests.conftest import DEMO_NUMBER
from tests.test_call_flow import ACCEPT, end_call, start_call, telnyx_translated_headers
from tests.test_dashboard import DENTIST, OWNER, _all, client, csrf_from, get, sign_in  # noqa: F401 - client is a fixture


# --- team ----------------------------------------------------------------------------------
def team(client):
    return asyncio_all(client, select(StaffUser).where(StaffUser.business_id == client.ids["shop"]).order_by(StaffUser.id))


def asyncio_all(client, query):
    import asyncio
    return asyncio.run(_all(client.db_url, query))


def test_owner_adds_staff_who_can_sign_in(client):
    sign_in(client)
    page = client.get("/app/team")
    assert page.status_code == 200 and OWNER in page.text and "You" in page.text
    resp = client.post("/app/team", data={"csrf": csrf_from(page.text), "email": "Cook@ChennaiSpice.com",
                                          "name": "Ravi", "role": "staff"})
    assert resp.status_code == 200 and "cook@chennaispice.com can now sign in" in resp.text
    [_, cook] = team(client)
    assert (cook.email, cook.name, cook.role) == ("cook@chennaispice.com", "Ravi", "staff")

    # Emails are unique across all businesses.
    again = client.post("/app/team", data={"csrf": csrf_from(resp.text), "email": DENTIST, "role": "staff"})
    assert again.status_code == 422 and "already has a VanakkamIQ login" in again.text

    # The new staff member signs in with an emailed code and sees no Team or Settings tab.
    sign_in(client, "cook@chennaispice.com")
    orders = client.get("/app/orders")
    assert 'href="/app/team"' not in orders.text and 'href="/app/settings"' not in orders.text
    assert client.get("/app/team").status_code == 403


def test_last_owner_is_protected_and_self_removal_blocked(client):
    sign_in(client)
    page = client.get("/app/team")
    me = re.search(r"/app/team/(\d+)/role", page.text).group(1)
    demote = client.post(f"/app/team/{me}/role", data={"csrf": csrf_from(page.text), "role": "staff"})
    assert demote.status_code == 422 and "at least one owner" in demote.text
    remove = client.post(f"/app/team/{me}/delete", data={"csrf": csrf_from(page.text)})
    assert remove.status_code == 422 and "remove your own login" in remove.text
    assert [u.role for u in team(client)] == ["owner"]


def test_role_change_and_removal(client):
    sign_in(client)
    page = client.get("/app/team")
    client.post("/app/team", data={"csrf": csrf_from(page.text), "email": "partner@chennaispice.com", "role": "staff"})
    [_, partner] = team(client)
    client.post(f"/app/team/{partner.id}/role", data={"csrf": csrf_from(page.text), "role": "owner"})
    assert get(client, StaffUser, partner.id).role == "owner"
    client.post(f"/app/team/{partner.id}/delete", data={"csrf": csrf_from(page.text)})
    assert get(client, StaffUser, partner.id) is None
    # Another business's login can't be touched.
    client.post("/app/team/2/delete", data={"csrf": csrf_from(page.text)})
    assert get(client, StaffUser, 2) is not None


# --- routing -------------------------------------------------------------------------------
@respx.mock
async def test_call_records_dialed_number_and_header(services):
    respx.post(url__regex=ACCEPT).mock(return_value=httpx.Response(200))
    _, task, conn = await start_call(services, "rtc_r1", "+15551230000")
    await end_call(conn, task)
    async with services.session_factory() as db:
        convo = await db.scalar(select(Conversation))
    assert (convo.dialed_number, convo.routed_by) == (DEMO_NUMBER, "To")


@respx.mock
async def test_call_without_dialed_number_records_fallback(services):
    respx.post(url__regex=ACCEPT).mock(return_value=httpx.Response(200))
    await services.call_manager.handle_incoming("rtc_r2", telnyx_translated_headers("+14255550188"))
    conn = services.connections[-1]
    await conn.wait_until_greeted()
    await end_call(conn, services.call_manager.active["rtc_r2"])
    async with services.session_factory() as db:
        convo = await db.scalar(select(Conversation))
    assert convo.dialed_number is None and convo.routed_by == SINGLE_BUSINESS


async def test_dialed_number_header_routes_between_two_businesses(services):
    async with services.session_factory() as db:
        second = Business(name="Second", slug="second", hours={}, is_active=True)
        db.add(second)
        await db.flush()
        db.add(PhoneNumber(business_id=second.id, e164="+15555550199"))
        await db.commit()
        headers = telnyx_translated_headers("+14255550188") + [
            {"name": "X-Called-Number", "value": "+15555550199"}]
        route = await services.call_manager.route_call(db, headers)
    assert route.business.name == "Second" and route.routed_by == "X-Called-Number"
