"""Owner-notification cron job: drain pending rows and deliver them via WhatsApp.

Runs as a separate Railway service (see CLAUDE.md's Deployment section),
started as `python -m jobs.drain_notifications` on a `* * * * *` schedule
with root directory `src/`. Railway's cron skips overlapping runs, so this
module does not use an advisory lock — it only needs to do its work and
exit, without leaving a server, a thread, or an open connection behind.

THE TENANT COMES FROM THE ROW (Module S3b). This process runs outside Flask
entirely: there is no request, no current_user, and nothing to read a "current"
tenant from — and assuming the pilot's would name another gym's classes in a
message their owner receives. The queue is drained globally, in one pass, and
each notification is composed against the tenant_id stored on it.

Class labels are loaded ONCE PER TENANT, not once per run and not once per row.
Once per run was right while one gym existed; once per row would be a query per
notification. The small dict below is the middle, and it is deliberately local to
main() so it cannot outlive the run and go stale.
"""

import logging
import sys

import bot.bookings as bookings
import bot.class_types as class_types
import bot.owner_notifications as owner_notifications
import whatsapp.whatsapp_service as whatsapp_service

logger = logging.getLogger(__name__)

# After this many failed delivery attempts a notification is marked "failed"
# and stops being retried (still visible for the owner to chase down manually).
MAX_ATTEMPTS: int = 5


def _compose_message(notification: dict, class_labels: dict[str, str]) -> str:
    """Build the Portuguese WhatsApp text for one pending notification.

    Args:
        notification (dict): A row from owner_notifications, including the
            tenant_id this notification belongs to.
        class_labels (dict[str, str]): marker → label FOR THAT TENANT, resolved
            by main() and passed in because this is called once per pending row
            — reading them here would be one query per notification.

    Returns:
        str: The message to send to the owner.
    """
    if notification["event_type"] == "booking":
        booking = bookings.get_booking(
            notification["booking_id"], tenant_id=notification["tenant_id"]
        )
        if booking is None:
            return "Uma reserva foi feita, mas não encontrei os detalhes. Confira no painel."

        class_label = class_labels.get(booking["class_type"], booking["class_type"])
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

    # marker → label, per tenant, filled lazily as the drain meets each gym. A
    # run touching one gym costs exactly one read, as it did before S3b.
    labels_by_tenant: dict[str, dict[str, str]] = {}

    for notification in pending:
        try:
            tenant_id: str = notification["tenant_id"]
            if tenant_id not in labels_by_tenant:
                labels_by_tenant[tenant_id] = class_types.load_class_types(tenant_id)["labels"]

            text = _compose_message(notification, labels_by_tenant[tenant_id])
            whatsapp_service.send_message(notification["owner_phone"], text)
            owner_notifications.mark_sent(notification["id"])
            logger.info("Notification %s delivered to owner.", notification["id"])
        except Exception:
            logger.exception("Failed to send notification %s; recording the attempt.", notification["id"])
            owner_notifications.mark_attempt_failed(notification["id"], MAX_ATTEMPTS)

    return 0


if __name__ == "__main__":
    sys.exit(main())
