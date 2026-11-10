"""Text messages to callers (order confirmation and status, message received) and staff
(new orders, escalations, callback requests).

SMS never blocks or breaks a call: failures are logged. Logs never contain message text or
full phone numbers, since both are customer data.
"""

import logging
from collections import deque

from app.db.models import Business, Order
from app.money import format_money
from app.telephony.telnyx import TelnyxClient

log = logging.getLogger(__name__)

RECENT_SMS_KEPT = 100


def mask_phone(number: str) -> str:
    return f"***{number[-4:]}" if len(number) > 4 else "***"


class Notifier:
    def __init__(self, telnyx: TelnyxClient | None, public_base_url: str = "") -> None:
        self._telnyx = telnyx
        self._base_url = public_base_url.rstrip("/")
        # Last few messages (to, text) for tests and debugging; bounded so it can't grow forever.
        self.sent: deque[tuple[str, str]] = deque(maxlen=RECENT_SMS_KEPT)

    async def _send(self, business: Business, to: str | None, text: str) -> None:
        if not to:
            return
        self.sent.append((to, text))
        if self._telnyx is None or not business.sms_from:
            reason = "Telnyx not configured" if self._telnyx is None else "business has no SMS sender number"
            log.info("SMS to %s not sent (%s), %d characters", mask_phone(to), reason, len(text))
            return
        try:
            await self._telnyx.send_sms(business.sms_from, to, text)
        except Exception:
            log.exception("SMS to %s failed", mask_phone(to))

    def status_link(self, order: Order) -> str | None:
        """Public order-status page, or None when the app has no public address (local dev)."""
        if not order.public_token or not self._base_url or "localhost" in self._base_url:
            return None
        return f"{self._base_url}/o/{order.public_token}"

    async def order_confirmed(self, business: Business, order: Order) -> None:
        items = ", ".join(f"{i.quantity} x {i.name}" for i in order.items)
        total = format_money(order.total_cents, business.currency)
        when = f" {order.fulfillment.capitalize()}: {order.requested_time}." if order.requested_time else ""
        link = self.status_link(order)
        await self._send(
            business, order.customer_phone,
            f"{business.name}: thanks{', ' + order.customer_name if order.customer_name else ''}! "
            f"Order #{order.number} confirmed: {items}. Total {total}.{when}"
            + (f" Track it: {link}" if link else ""),
        )
        await self._send(
            business, business.notify_phone,
            f"New phone order #{order.number} ({order.fulfillment}) from {order.customer_name or 'caller'} "
            f"{order.customer_phone or ''}: {items}. Total {total}.",
        )

    async def order_ready(self, business: Business, order: Order) -> None:
        if order.fulfillment == "delivery":
            text = f"{business.name}: your order #{order.number} is on its way!"
        else:
            where = f" at {business.address}" if business.address else ""
            text = f"{business.name}: your order #{order.number} is ready for pickup{where}. See you soon!"
        await self._send(business, order.customer_phone, text)

    async def order_cancelled(self, business: Business, order: Order) -> None:
        await self._send(
            business, order.customer_phone,
            f"{business.name}: your order #{order.number} was cancelled. Questions? Just call us back.",
        )

    async def callback_received(self, business: Business, phone: str | None) -> None:
        await self._send(business, phone, f"{business.name}: we got your message. A team member will call you back soon.")

    async def escalation(self, business: Business, caller_phone: str | None, reason: str) -> None:
        await self._send(business, business.notify_phone, f"VanakkamIQ is transferring a caller {caller_phone or ''} to you. Reason: {reason}")

    async def callback_requested(self, business: Business, name: str | None, phone: str | None, message: str) -> None:
        await self._send(
            business, business.notify_phone,
            f"Callback requested by {name or 'a caller'} {phone or ''}: {message}",
        )
