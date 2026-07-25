import logging
import uuid
from datetime import datetime

import psycopg2

from database.db import get_connection
from integrations.store import DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)

valid_booking_statuses: set[str] = {
    "pending_confirmation",  # Booking created, owner hasn't confirmed the class will happen
    "confirmed",              # Owner confirmed the trial class
    "cancelled",               # Cancelled by the lead or the owner
}


def count_active_bookings(calendar_event_id: str) -> int:
    """Count non-cancelled bookings tied to a single Calendar event.

    This is the number that get_available_slots() compares against a class
    type's capacity, and the number create_booking_with_lock() re-checks
    inside the advisory lock before inserting a new booking.

    Args:
        calendar_event_id (str): ID of the Calendar event representing the slot.

    Returns:
        int: Number of bookings for this event with status != 'cancelled'.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS active_count
                FROM trial_bookings
                WHERE calendar_event_id = %s AND status != 'cancelled'
                """,
                (calendar_event_id,),
            )
            active_count: int = cur.fetchone()["active_count"]

    return active_count


def list_active_bookings_by_sender(sender: str) -> list[dict]:
    """List a lead's non-cancelled bookings, earliest slot first.

    The AI conversation injects these so it always knows what the lead already
    has booked — even after the 1-hour inactivity timeout wipes the session
    history, a lead who booked a class and comes back later ("posso remarcar?")
    must not land in a conversation that is blind to the existing booking.

    Args:
        sender (str): Lead's WhatsApp number, e.g. "5521999999999".

    Returns:
        list[dict]: Booking rows with status != 'cancelled', ordered by slot_start.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM trial_bookings
                WHERE sender = %s AND status != 'cancelled'
                ORDER BY slot_start
                """,
                (sender,),
            )
            rows = cur.fetchall()

    return [dict(row) for row in rows]


def create_booking_with_lock(
    calendar_event_id: str,
    sender: str,
    lead_name: str,
    class_type: str,
    slot_start: datetime,
    slot_end: datetime,
    capacity: int | None,
    child_name: str | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> dict:
    """Reserve a seat in a Calendar event's slot under a Postgres advisory lock.

    Locking, counting and inserting all happen in the same transaction so two
    concurrent callers can never both observe "one seat left" and both insert.
    pg_advisory_xact_lock is skipped when capacity is None (unlimited adult
    classes never fill up, so there's nothing to serialize).

    Args:
        calendar_event_id (str): ID of the Calendar event representing the slot.
        sender (str): Lead's WhatsApp number, e.g. "5521999999999".
        lead_name (str): Lead's name, as provided by the AI. For child classes
            this is the responsible adult who chats on WhatsApp.
        class_type (str): One of scheduling.CLASS_CAPACITY's keys.
        slot_start (datetime): Timezone-aware start of the slot.
        slot_end (datetime): Timezone-aware end of the slot.
        capacity (int | None): Max active bookings for this event, or None for unlimited.
        child_name (str | None): Name of the child attending, for [BABY]/[CRIANCAS]
            classes. Stays NULL for adult classes (where it does not apply).
        tenant_id (str): Tenant identifier. Fixed to DEFAULT_TENANT_ID for the pilot.

    Returns:
        dict: {"status": "created", "booking_id": str, "active_count": int}
            on success; {"status": "full", "active_count": int} if the slot has
            no seats left; {"status": "duplicate"} if this sender already has
            an active booking for this event.
    """
    booking_id: str = str(uuid.uuid4())

    with get_connection() as conn:
        with conn.cursor() as cur:
            if capacity is not None:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (calendar_event_id,))

            cur.execute(
                """
                SELECT COUNT(*) AS active_count
                FROM trial_bookings
                WHERE calendar_event_id = %s AND status != 'cancelled'
                """,
                (calendar_event_id,),
            )
            active_count: int = cur.fetchone()["active_count"]

            if capacity is not None and active_count >= capacity:
                conn.rollback()
                logger.info(
                    "Booking rejected, event %s is full (%d/%d).",
                    calendar_event_id, active_count, capacity,
                )
                return {"status": "full", "active_count": active_count}

            try:
                cur.execute(
                    """
                    INSERT INTO trial_bookings
                        (id, tenant_id, sender, lead_name, child_name, calendar_event_id, class_type, slot_start, slot_end, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending_confirmation')
                    """,
                    (booking_id, tenant_id, sender, lead_name, child_name, calendar_event_id, class_type, slot_start, slot_end),
                )
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                logger.info(
                    "Booking rejected, sender %s already booked event %s.",
                    sender, calendar_event_id,
                )
                return {"status": "duplicate"}

            conn.commit()

    logger.info("Booking %s created for event %s.", booking_id, calendar_event_id)
    return {"status": "created", "booking_id": booking_id, "active_count": active_count + 1}


def get_booking(booking_id: str) -> dict | None:
    """Fetch a single booking by id.

    Args:
        booking_id (str): UUID4 id of the booking.

    Returns:
        dict | None: The booking row, or None if no booking has this id.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM trial_bookings WHERE id = %s", (booking_id,))
            row = cur.fetchone()

    return dict(row) if row is not None else None


def list_bookings_by_status(status: str) -> list[dict]:
    """List all bookings with a given status, newest first.

    Args:
        status (str): One of valid_booking_statuses.

    Returns:
        list[dict]: Matching booking rows.

    Raises:
        ValueError: If status is not one of valid_booking_statuses.
    """
    if status not in valid_booking_statuses:
        raise ValueError(f"Invalid booking status: {status}")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM trial_bookings WHERE status = %s ORDER BY created_at DESC",
                (status,),
            )
            rows = cur.fetchall()

    return [dict(row) for row in rows]


def update_booking_status(booking_id: str, status: str) -> bool:
    """Update a booking's status.

    Args:
        booking_id (str): UUID4 id of the booking.
        status (str): One of valid_booking_statuses.

    Returns:
        bool: True if a booking was found and updated, False otherwise.

    Raises:
        ValueError: If status is not one of valid_booking_statuses.
    """
    if status not in valid_booking_statuses:
        raise ValueError(f"Invalid booking status: {status}")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE trial_bookings
                SET status = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (status, booking_id),
            )
            updated: bool = cur.rowcount > 0

        conn.commit()

    if updated:
        logger.info("Booking %s status updated to '%s'.", booking_id, status)
    else:
        logger.warning("Booking %s not found for status update.", booking_id)

    return updated
