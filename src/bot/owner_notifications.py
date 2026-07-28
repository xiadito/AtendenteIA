"""Owner-notification queue: enqueue, drain, and record the owner's reply.

A request that closes a booking or triggers a handoff never sends WhatsApp to
the owner itself — it only enqueues a pending row here (see
enqueue_notification). A separate cron service (jobs/drain_notifications.py)
drains pending rows on its own schedule, sends the WhatsApp message, and
retries up to a cap. This keeps a slow or failing Twilio call from ever
blocking the reply to the lead.

register_owner_response() only ever writes owner_response on this table. It
never touches trial_bookings — closing out the booking based on the owner's
reply is a future feature, not this one.
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
        tenant_id (str): Tenant identifier. Fixed to DEFAULT_TENANT_ID for the pilot.

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
    """List notifications still worth attempting, oldest first.

    Args:
        max_attempts (int): Notifications with attempts >= this are excluded
            (they already failed permanently).

    Returns:
        list[dict]: Pending notification rows, ordered by created_at.
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


def register_owner_response(owner_phone: str, response: str) -> bool:
    """Record the owner's reply on the most recent open notification.

    Finds the latest 'sent' notification for this owner_phone still missing
    an owner_response and stamps it. Never touches trial_bookings — closing
    out the booking based on this response belongs to a future feature.

    Args:
        owner_phone (str): The owner's number the reply came from.
        response (str): One of valid_owner_responses.

    Returns:
        bool: True if an open notification was found and updated, False if
        there was nothing pending a response (e.g. the owner replied twice).

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
                    WHERE owner_phone = %s AND status = 'sent' AND owner_response IS NULL
                    ORDER BY sent_at DESC
                    LIMIT 1
                )
                """,
                (response, owner_phone),
            )
            updated: bool = cur.rowcount > 0

        conn.commit()

    if updated:
        logger.info("Owner %s responded '%s' to the latest open notification.", owner_phone, response)
    else:
        logger.warning("Owner %s responded '%s' but no open notification was found.", owner_phone, response)

    return updated
