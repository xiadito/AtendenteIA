"""Owner-notification queue: enqueue, drain, and record the owner's reply.

A request that closes a booking or triggers a handoff never sends WhatsApp to
the owner itself — it only enqueues a pending row here (see
enqueue_notification). A separate cron service (jobs/drain_notifications.py)
drains pending rows on its own schedule, sends the WhatsApp message, and
retries up to a cap. This keeps a slow or failing Twilio call from ever
blocking the reply to the lead.

The functions here only ever write to owner_notifications — none of them
touches trial_bookings. What changed in Module 6 is who acts on the result:
register_owner_response() now RETURNS the stamped row (event_type, booking_id)
instead of a bare bool, and its caller (webhook/routes.py) hands a 'booking'
row to bot/confirmations.py, which is the single place that closes a booking
out. Keeping the write here and the decision there is what lets the dashboard
close a booking through the same coordinator without going through WhatsApp at
all.

TENANT SCOPE (Module S3b). Two of the three reads here are scoped and one is
deliberately NOT:

- register_owner_response() is scoped, and it matters most. It answers "which
  notification is this 1/2 replying to?", and the owner of gym B replying to
  their own notification must never stamp — and therefore close — a booking of
  gym A's. The webhook already knows the tenant it resolved from Twilio's "To".
- register_response_for_booking() takes the tenant as a guard, matching
  bookings.get_booking().
- list_pending_notifications() stays GLOBAL, on purpose. It is the system's
  outbound queue, drained by a cron that serves every gym in one pass; each row
  carries its own tenant_id and jobs/drain_notifications.py resolves the sending
  details from THAT. Filtering here would mean either N queries per run or a
  tenant the cron has no way to choose.
"""

import logging

import psycopg2

from database.db import get_connection
from integrations.store import DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)

valid_event_types: set[str] = {
    "booking",  # A trial-class booking was closed by the AI
    "handoff",  # A lead asked to talk to a human
}

valid_notification_statuses: set[str] = {
    "pending",  # Enqueued, not yet delivered
    "sent",     # Delivered to the owner via WhatsApp
    "failed",   # Delivery exhausted its retry attempts
}

valid_owner_responses: set[str] = {
    "confirmed",
    "cancelled",
}


def enqueue_notification(
    owner_id: int,
    owner_phone: str,
    event_type: str,
    lead_sender: str,
    booking_id: str | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> bool:
    """Enqueue a pending owner notification, idempotently.

    A partial unique index on owner_notifications makes the INSERT a no-op
    when this event was already enqueued: one row per booking_id for
    event_type='booking', and at most one unanswered row per lead_sender for
    event_type='handoff'. The caller (bot/handlers.py) is trusted internal
    code, so an invalid event_type raises rather than degrading silently.

    Args:
        owner_id (int): id of the owners row this notification is for.
        owner_phone (str): Owner's number, e.g. "5521999999999", snapshotted
            at enqueue time so a later phone change doesn't rewrite history.
        event_type (str): One of valid_event_types.
        lead_sender (str): The lead's WhatsApp number the event came from.
        booking_id (str | None): trial_bookings.id, required for "booking",
            left None for "handoff".
        tenant_id (str): The gym this notification belongs to. Stamped on the row
            so the cron can resolve the right owner and class labels later.

    Returns:
        bool: True if a new row was inserted, False if an equivalent
        notification was already pending/sent (the unique index caught it).

    Raises:
        ValueError: If event_type is not one of valid_event_types.
    """
    if event_type not in valid_event_types:
        raise ValueError(f"Invalid event_type: {event_type}")

    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO owner_notifications
                        (tenant_id, owner_id, owner_phone, event_type, lead_sender, booking_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (tenant_id, owner_id, owner_phone, event_type, lead_sender, booking_id),
                )
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                logger.info(
                    "Notification already enqueued for lead %s (event_type=%s); skipping.",
                    lead_sender, event_type,
                )
                return False

            conn.commit()

    logger.info("Notification enqueued for lead %s (event_type=%s).", lead_sender, event_type)
    return True


def list_pending_notifications(max_attempts: int) -> list[dict]:
    """List EVERY tenant's notifications still worth attempting, oldest first.

    Deliberately not scoped by tenant — see the module docstring. This is the
    system's outbound queue and the cron drains all of it in one pass; each row
    carries the tenant_id its delivery must be resolved against.

    Args:
        max_attempts (int): Notifications with attempts >= this are excluded
            (they already failed permanently).

    Returns:
        list[dict]: Pending notification rows, ordered by created_at. Each row
        includes tenant_id, which the caller MUST use rather than assuming the
        pilot's.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM owner_notifications
                WHERE status = 'pending' AND attempts < %s
                ORDER BY created_at
                """,
                (max_attempts,),
            )
            rows = cur.fetchall()

    return [dict(row) for row in rows]


def mark_sent(notification_id: int) -> None:
    """Mark a notification as delivered.

    Args:
        notification_id (int): id of the owner_notifications row.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE owner_notifications
                SET status = 'sent', sent_at = NOW(), updated_at = NOW()
                WHERE id = %s
                """,
                (notification_id,),
            )
            conn.commit()

    logger.info("Notification %s marked as sent.", notification_id)


def mark_attempt_failed(notification_id: int, max_attempts: int) -> None:
    """Record a failed delivery attempt, failing the notification at the cap.

    Args:
        notification_id (int): id of the owner_notifications row.
        max_attempts (int): Once attempts reaches this, status becomes
            'failed' instead of staying 'pending' for the next cron cycle.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE owner_notifications
                SET attempts = attempts + 1,
                    status = CASE WHEN attempts + 1 >= %s THEN 'failed' ELSE status END,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING attempts, status
                """,
                (max_attempts, notification_id),
            )
            row = cur.fetchone()
            conn.commit()

    if row is not None and row["status"] == "failed":
        logger.warning("Notification %s failed permanently after %d attempt(s).", notification_id, row["attempts"])
    else:
        logger.warning("Notification %s attempt failed; will retry next cron cycle.", notification_id)


def register_owner_response(
    owner_phone: str,
    response: str,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> dict | None:
    """Record the owner's reply on their gym's most recent open notification.

    Finds the latest 'sent' notification for this owner_phone IN THIS TENANT
    still missing an owner_response and stamps it. This function itself still
    only writes to
    owner_notifications; what it now does is hand the caller enough of the row
    to act on it — event_type says whether a booking is even involved, and
    booking_id says which one. Closing the booking out is the caller's job
    (webhook/routes.py -> bot/confirmations.py), so that the dashboard can close
    the same booking through the same coordinator.

    Returning the row is also what makes a double reply harmless: the second "1"
    finds no open notification, gets None, and there is nothing left to do.

    THE TENANT FILTER IS NOT DECORATION (Module S3b). `owner_phone` is unique
    across `owners` since migration 009, so today it already implies one gym —
    but this query reads `owner_notifications`, where nothing enforces that, and
    the row it stamps is what decides which booking gets closed. Scoping it means
    a reply that arrived on gym B's number can only ever resolve gym B's queue.

    Args:
        owner_phone (str): The owner's number the reply came from.
        response (str): One of valid_owner_responses.
        tenant_id (str): The gym the reply arrived at, as resolved from the
            Twilio "To" field by webhook/routes.py.

    Returns:
        dict | None: The stamped row as {"id", "event_type", "booking_id"}, or
        None if there was nothing pending a response (e.g. the owner replied
        twice). booking_id is None on 'handoff' notifications.

    Raises:
        ValueError: If response is not one of valid_owner_responses.
    """
    if response not in valid_owner_responses:
        raise ValueError(f"Invalid owner response: {response}")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE owner_notifications
                SET owner_response = %s, updated_at = NOW()
                WHERE id = (
                    SELECT id FROM owner_notifications
                    WHERE tenant_id = %s
                      AND owner_phone = %s
                      AND status = 'sent'
                      AND owner_response IS NULL
                    ORDER BY sent_at DESC
                    LIMIT 1
                )
                RETURNING id, event_type, booking_id
                """,
                (response, tenant_id, owner_phone),
            )
            row = cur.fetchone()

        conn.commit()

    if row is None:
        logger.warning("Owner %s responded '%s' but no open notification was found.", owner_phone, response)
        return None

    logger.info(
        "Owner %s responded '%s' to notification %s (event_type=%s).",
        owner_phone, response, row["id"], row["event_type"],
    )
    return dict(row)


def register_response_for_booking(
    booking_id: str,
    response: str,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> bool:
    """Stamp the owner_response of the notification tied to one booking.

    The WhatsApp path stamps the notification and then closes the booking; the
    dashboard path closes the booking directly, which would leave that same
    notification open forever and the two records disagreeing about what the
    owner decided. This is the dashboard's way of writing the same fact.

    At most one row can match: the partial unique index
    idx_owner_notifications_booking_unique allows a single 'booking'
    notification per booking_id. A booking with no notification (the queue never
    ran, or the owner has no phone configured) simply matches nothing.

    Args:
        booking_id (str): trial_bookings.id the decision was made about.
        response (str): One of valid_owner_responses.
        tenant_id (str): The gym whose dashboard took the decision — the same
            guard bookings.get_booking() carries.

    Returns:
        bool: True if an open notification was found and stamped, False if there
        was none (no notification for this booking, or already answered).

    Raises:
        ValueError: If response is not one of valid_owner_responses.
    """
    if response not in valid_owner_responses:
        raise ValueError(f"Invalid owner response: {response}")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE owner_notifications
                SET owner_response = %s, updated_at = NOW()
                WHERE tenant_id = %s
                  AND booking_id = %s
                  AND event_type = 'booking'
                  AND owner_response IS NULL
                """,
                (response, tenant_id, booking_id),
            )
            updated: bool = cur.rowcount > 0

        conn.commit()

    if updated:
        logger.info("Notification for booking %s stamped '%s' from the dashboard.", booking_id, response)
    else:
        logger.info("No open notification for booking %s to stamp; nothing to reconcile.", booking_id)

    return updated
