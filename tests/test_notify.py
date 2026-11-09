import logging

from app.db.models import Business
from app.notify.sms import RECENT_SMS_KEPT, Notifier, mask_phone


async def test_recent_messages_are_bounded():
    notifier = Notifier(telnyx=None)
    business = Business(name="Test", sms_from=None)
    for i in range(RECENT_SMS_KEPT + 50):
        await notifier._send(business, "+15551230000", f"message {i}")
    assert len(notifier.sent) == RECENT_SMS_KEPT
    assert notifier.sent[-1] == ("+15551230000", f"message {RECENT_SMS_KEPT + 49}")


async def test_logs_hide_message_text_and_full_number(caplog):
    caplog.set_level(logging.INFO, logger="app.notify.sms")
    await Notifier(telnyx=None)._send(Business(name="Test"), "+15551234567", "Priya, order #1001: 2 x Biryani")
    logged = caplog.text
    assert "***4567" in logged
    assert "+15551234567" not in logged and "Priya" not in logged and "Biryani" not in logged


def test_mask_phone():
    assert mask_phone("+15551234567") == "***4567"
    assert mask_phone("12") == "***"
