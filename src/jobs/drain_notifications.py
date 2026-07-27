"""Owner-notification cron job: drain pending rows and deliver them via WhatsApp.

Runs as a separate Railway service (see CLAUDE.md's Deployment section),
started as `python -m jobs.drain_notifications` on a `* * * * *` schedule
with root directory `src/`. Railway's cron skips overlapping runs, so this
module does not use an advisory lock — it only needs to do its work and
exit, without leaving a server, a thread, or an open connection behind.
"""

import logging
import sys

import bot.bookings as bookings
import bot.owner_notifications as owner_notifications
import whatsapp.whatsapp_service as whatsapp_service
from bot.scheduling import CLASS_TYPE_LABELS

logger = logging.getLogger(__name__)

# After this many failed delivery attempts a notification is marked "failed"
# and stops being retried (still visible for the owner to chase down manually).
MAX_ATTEMPTS: int = 5


def _compose_message(notification: dict) -> str:
    """Build the Portuguese WhatsApp text for one pending notification.

    Args:
        notification (dict): A row from owner_notifications.

    Returns:
        str: The message to send to the owner.
    """
    if notification["event_type"] == "booking":
        booking = bookings.get_booking(notification["booking_id"])
        if booking is None:
            return "Uma reserva foi feita, mas não encontrei os detalhes. Confira no painel."

        class_label = CLASS_TYPE_LABELS.get(booking["class_type"], booking["class_type"])
        who = booking["lead_name"]
        if booking.get("child_name"):
            who = f"{booking['child_name']} (responsável: {booking['lead_name']})"

        return (
            f"📅 Nova aula experimental agendada!\n"
            f"{who} — {class_label}\n"
            f"Horário: {booking['slot_start']:%d/%m/%Y %H:%M}\n"
            "Responda *1* para confirmar ou *2* para cancelar."
        )

    return (
        f"🙋 O lead {notification['lead_sender']} pediu para falar com um atendente humano.\n"
        "A conversa está pausada aguardando você assumir ou delegar o atendimento."
    )


def main() -> int:
    """Drain every pending notification once and exit.

    Returns:
        int: Process exit code — 0 on a normal run (individual send failures
        are recorded per-notification, not treated as a run failure), 1 if
        the run itself failed unexpectedly (e.g. the database is unreachable).
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    try:
        pending = owner_notifications.list_pending_notifications(MAX_ATTEMPTS)
    except Exception:
        logger.exception("Could not list pending owner notifications; aborting this run.")
        return 1

    logger.info("Draining %d pending owner notification(s).", len(pending))

    for notification in pending:
        try:
            text = _compose_message(notification)
            whatsapp_service.send_message(notification["owner_phone"], text)
            owner_notifications.mark_sent(notification["id"])
            logger.info("Notification %s delivered to owner.", notification["id"])
        except Exception:
            logger.exception("Failed to send notification %s; recording the attempt.", notification["id"])
            owner_notifications.mark_attempt_failed(notification["id"], MAX_ATTEMPTS)

    return 0


if __name__ == "__main__":
    sys.exit(main())
