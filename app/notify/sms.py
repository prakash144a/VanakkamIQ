"""Text messages to callers (order confirmation) and staff (new orders, escalations).

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
    def __init__(self, telnyx: TelnyxClient | None) -> None:
        self._telnyx = telnyx
        # Last few messages (to, text) for tests and debugging; bounded so it can't grow forever.
        self.sent: deque[tuple[str, str]] = deque(maxlen=RECENT_SMS_KEPT)

    async def _send(self, business: Business, to: str | None, text: str) -> None:
        if not to:
            return
        self.sent.append((to, text))
        if self._telnyx is None or not business.sms_from:
            reason = "Telnyx not configured" if self._telnyx is None else "restaurant has no SMS sender number"
            log.info("SMS to %s not sent (%s), %d characters", mask_phone(to), reason, len(text))
            return
        try:
            await self._telnyx.send_sms(business.sms_from, to, text)
        except Exception:
            log.exception("SMS to %s failed", mask_phone(to))

    async def order_confirmed(self, business: Business, order: Order) -> None:
        items = ", ".join(f"{i.quantity} x {i.name}" for i in order.items)
        total = format_money(order.total_cents, business.currency)
        when = f" {order.fulfillment.capitalize()}: {order.requested_time}." if order.requested_time else ""
        await self._send(
            business, order.customer_phone,
            f"{business.name}: thanks{', ' + order.customer_name if order.customer_name else ''}! "
            f"Order #{order.number} confirmed: {items}. Total {total}.{when}",
        )
        await self._send(
            business, business.notify_phone,
            f"New phone order #{order.number} ({order.fulfillment}) from {order.customer_name or 'caller'} "
            f"{order.customer_phone or ''}: {items}. Total {total}.",
        )

    async def escalation(self, business: Business, caller_phone: str | None, reason: str) -> None:
        await self._send(business, business.notify_phone, f"VanakkamIQ is transferring a caller {caller_phone or ''} to you. Reason: {reason}")

    async def callback_requested(self, business: Business, name: str | None, phone: str | None, message: str) -> None:
        await self._send(
            business, business.notify_phone,
            f"Callback requested by {name or 'a caller'} {phone or ''}: {message}",
        )
