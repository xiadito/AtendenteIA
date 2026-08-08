"""Data access for `trial_bookings`, the reservation ledger.

TENANT SCOPE (Module S3b). Every read here is scoped to one gym, including the
ones that look up a booking by its own primary key. `id` is a uuid4 and already
unique, so the tenant is not needed to FIND the row — it is there as a GUARD, so
that a route belonging to gym A cannot read or decide gym B's booking by putting
another id in the URL. Treat it as part of the lookup, never as optional.

The advisory lock in create_booking_with_lock() is deliberately NOT scoped: it
keys on the Google Calendar event id, which is globally unique, and two gyms read
two different calendars. What is scoped is the capacity count taken inside it.
"""

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


def count_active_bookings(
    calendar_event_id: str,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> int:
    """Count one gym's non-cancelled bookings tied to a single Calendar event.

    This is the number that get_available_slots() compares against a class
    type's capacity, and the number create_booking_with_lock() re-checks
    inside the advisory lock before inserting a new booking.

    Args:
        calendar_event_id (str): ID of the Calendar event representing the slot.
        tenant_id (str): The gym whose ledger to count. An event belongs to one
            gym's calendar, so this cannot change a correct answer — but it stops
            a stray row filed under another tenant from eating a seat here.

    Returns:
        int: Number of bookings for this event with status != 'cancelled'.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS active_count
                FROM trial_bookings
                WHERE tenant_id = %s AND calendar_event_id = %s AND status != 'cancelled'
                """,
                (tenant_id, calendar_event_id),
            )
            active_count: int = cur.fetchone()["active_count"]

    return active_count


def list_active_bookings_by_sender(
    sender: str,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> list[dict]:
    """List a lead's non-cancelled bookings at one gym, earliest slot first.

    The AI conversation injects these so it always knows what the lead already
    has booked — even after the 1-hour inactivity timeout wipes the session
    history, a lead who booked a class and comes back later ("posso remarcar?")
    must not land in a conversation that is blind to the existing booking.

    Scoping matters here in a way the lead would notice: without it, a person who
    trains at two gyms would have gym B's class read back to them by gym A's
    attendant.

    Args:
        sender (str): Lead's WhatsApp number, e.g. "5521999999999".
        tenant_id (str): The gym whose bookings to list.

    Returns:
        list[dict]: Booking rows with status != 'cancelled', ordered by slot_start.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM trial_bookings
                WHERE tenant_id = %s AND sender = %s AND status != 'cancelled'
                ORDER BY slot_start
                """,
                (tenant_id, sender),
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

    THE LOCK KEY STAYS GLOBAL, THE COUNT BECOMES TENANT-SCOPED (Module S3b). The
    Calendar event id is unique across all of Google, so hashing it still
    serializes exactly the callers who are competing for the same seat; adding
    the tenant would only make the key longer. The COUNT below is a different
    question — "how many seats has THIS gym sold?" — and is scoped accordingly.

    Args:
        calendar_event_id (str): ID of the Calendar event representing the slot.
        sender (str): Lead's WhatsApp number, e.g. "5521999999999".
        lead_name (str): Lead's name, as provided by the AI. For child classes
            this is the responsible adult who chats on WhatsApp.
        class_type (str): One of the tenant's class_types.marker values.
        slot_start (datetime): Timezone-aware start of the slot.
        slot_end (datetime): Timezone-aware end of the slot.
        capacity (int | None): Max active bookings for this event, or None for unlimited.
        child_name (str | None): Name of the child attending, for class types
            flagged requires_child_name. Stays NULL for the others (where it
            does not apply).
        tenant_id (str): The gym this reservation belongs to. Since migration 011
            the UNIQUE is (tenant_id, calendar_event_id, sender), so the same
            lead booking the same event at two gyms is two valid rows.

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
                WHERE tenant_id = %s AND calendar_event_id = %s AND status != 'cancelled'
                """,
                (tenant_id, calendar_event_id),
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


def get_booking(booking_id: str, tenant_id: str = DEFAULT_TENANT_ID) -> dict | None:
    """Fetch a single booking by id, inside one gym.

    The id alone would find the row — it is a uuid4. The tenant is a GUARD (see
    the module docstring): a dashboard route builds this call from a URL segment,
    and without it gym A could read gym B's booking by pasting its id.

    Args:
        booking_id (str): UUID4 id of the booking.
        tenant_id (str): The gym asking. A booking belonging to anyone else
            answers None, exactly like an id that does not exist — which is what
            makes the routes' "not found" path cover both cases.

    Returns:
        dict | None: The booking row, or None if this gym has no such booking.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM trial_bookings WHERE tenant_id = %s AND id = %s",
                (tenant_id, booking_id),
            )
            row = cur.fetchone()

    return dict(row) if row is not None else None


def list_bookings_by_status(status: str, tenant_id: str = DEFAULT_TENANT_ID) -> list[dict]:
    """List one gym's bookings with a given status, newest first.

    Args:
        status (str): One of valid_booking_statuses.
        tenant_id (str): The gym whose bookings to list.

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
                """
                SELECT * FROM trial_bookings
                WHERE tenant_id = %s AND status = %s
                ORDER BY created_at DESC
                """,
                (tenant_id, status),
            )
            rows = cur.fetchall()

    return [dict(row) for row in rows]


def list_bookings_for_review(tenant_id: str = DEFAULT_TENANT_ID) -> list[dict]:
    """List ONE GYM's bookings for the owner's review screen, pending ones first.

    Uncapped, like messages.get_conversation(): the owner opening the screen
    wants to see what still needs a decision AND what was already decided,
    without choosing a filter first. The ordering does the triage instead —
    everything still in 'pending_confirmation' floats to the top, and inside each
    group the earliest class comes first, so the trial that happens tomorrow is
    answered before the one three weeks out.

    Args:
        tenant_id (str): The gym whose review screen this is.

    Returns:
        list[dict]: Every booking row of this tenant, pending_confirmation first,
        then by slot_start ascending.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM trial_bookings
                WHERE tenant_id = %s
                ORDER BY (status = 'pending_confirmation') DESC, slot_start
                """,
                (tenant_id,),
            )
            rows = cur.fetchall()

    return [dict(row) for row in rows]


def update_booking_status(
    booking_id: str,
    status: str,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> bool:
    """Update a booking's status, inside one gym.

    Args:
        booking_id (str): UUID4 id of the booking.
        status (str): One of valid_booking_statuses.
        tenant_id (str): The gym the booking must belong to. Same guard as
            get_booking(): a decision taken in one dashboard must never be able
            to land on another gym's class.

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
                WHERE tenant_id = %s AND id = %s
                """,
                (status, tenant_id, booking_id),
            )
            updated: bool = cur.rowcount > 0

        conn.commit()

    if updated:
        logger.info("Booking %s status updated to '%s'.", booking_id, status)
    else:
        logger.warning("Booking %s not found for status update.", booking_id)

    return updated
