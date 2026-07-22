import logging
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import integrations.store as store
from bot import bookings
from integrations.google_calendar import NeedsReconnectError, get_calendar_service

logger = logging.getLogger(__name__)

TIMEZONE = ZoneInfo("America/Sao_Paulo")

# Business rule, not Calendar data: Google Calendar remains the source of truth
# for which slots exist, when, and of what type (via the title marker below).
# How many leads fit in each type is a decision that lives in code so it can
# change without anyone editing the calendar.
CLASS_CAPACITY: dict[str, int | None] = {
    "BABY": 2,
    "CRIANCAS": 4,
    "ADULTOS": None,  # unlimited
}

CLASS_TYPE_LABELS: dict[str, str] = {
    "BABY": "Baby Class",
    "CRIANCAS": "Crianças",
    "ADULTOS": "Adultos",
}

# Matches a "[MARKER]" at the start of an event title, tolerant of extra
# spaces and accented letters (e.g. "[ CRIANÇAS ]").
_TITLE_MARKER_PATTERN = re.compile(r"^\s*\[\s*([a-zA-ZÀ-ÿ]+)\s*\]")

# Section header appended to an event's description on the first booking.
# Later bookings for the same event append a line under it instead of
# duplicating the header, since capacity > 1 means more than one lead can
# book the same slot over time.
BOOKING_SECTION_MARKER = "--- Reservas Corujai ---"

_WEEKDAY_NAMES_PT = [
    "segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
    "sexta-feira", "sábado", "domingo",
]


class IntegrationNotConnectedError(Exception):
    """Raised when the owner hasn't connected Google Calendar, or calendar_id is missing."""


class IntegrationNeedsReconnectError(Exception):
    """Raised after Google rejects the stored refresh_token (invalid_grant).

    By the time this is raised, store.mark_needs_reconnect() has already run.
    """


def _strip_accents(value: str) -> str:
    """Remove accents so the title marker parser is accent-insensitive."""
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _parse_class_type(title: str) -> str:
    """Parse the class type marker from a Calendar event title.

    Args:
        title (str): Event summary, e.g. "[CRIANCAS] Aula Experimental".

    Returns:
        str: One of CLASS_CAPACITY's keys. Falls back to "ADULTOS" (unlimited)
        for any title that doesn't start with a recognized marker, so a
        mis-typed slot never blocks a booking.
    """
    match = _TITLE_MARKER_PATTERN.match(title or "")
    if match:
        marker = _strip_accents(match.group(1)).upper()
        if marker in CLASS_CAPACITY:
            return marker

    logger.warning("Unrecognized class marker in event title '%s'; defaulting to ADULTOS.", title)
    return "ADULTOS"


def _parse_rfc3339(value: str) -> datetime:
    """Parse an RFC3339 timestamp (as returned by the Calendar API) into São Paulo time.

    Args:
        value (str): e.g. "2026-07-20T18:00:00-03:00".

    Returns:
        datetime: Timezone-aware datetime in America/Sao_Paulo.
    """
    return datetime.fromisoformat(value).astimezone(TIMEZONE)


def _format_slot_label(start: datetime, class_type: str) -> str:
    """Build a Portuguese, human-readable label for a slot."""
    weekday = _WEEKDAY_NAMES_PT[start.weekday()].capitalize()
    class_label = CLASS_TYPE_LABELS.get(class_type, class_type.title())
    return f"{weekday}, {start.strftime('%d/%m')} às {start.strftime('%H:%M')} — {class_label}"


def _get_service_or_raise() -> tuple[Any, str]:
    """Load owner credentials and build an authenticated Calendar service.

    Shared by get_available_slots() and book_slot() so both fail the same way
    for the same reasons.

    Returns:
        tuple[Any, str]: (calendar service client, calendar_id).

    Raises:
        IntegrationNotConnectedError: No connected integration, or calendar_id
            is missing for the tenant.
        IntegrationNeedsReconnectError: Google rejected the stored
            refresh_token; store.mark_needs_reconnect() has already run.
    """
    owner = store.get_owner_credentials()
    if owner is None or owner["integration_status"] != "connected" or not owner["calendar_id"]:
        raise IntegrationNotConnectedError("Google Calendar integration is not connected.")

    try:
        service = get_calendar_service(owner["refresh_token"])
    except NeedsReconnectError as exc:
        store.mark_needs_reconnect()
        raise IntegrationNeedsReconnectError("Owner must reconnect Google Calendar.") from exc

    return service, owner["calendar_id"]


def get_available_slots(days_ahead: int = 14) -> list[dict]:
    """List Calendar slots that still have open seats.

    Args:
        days_ahead (int): How many days ahead of now to look for slots.

    Returns:
        list[dict]: Each item has event_id, class_type, start, end,
        remaining_slots (int | None; None means unlimited) and label,
        ordered by start time. Full slots and all-day events are omitted.

    Raises:
        IntegrationNotConnectedError: The owner hasn't connected Google
            Calendar, or calendar_id is missing.
        IntegrationNeedsReconnectError: Google rejected the refresh_token.
    """
    service, calendar_id = _get_service_or_raise()

    now = datetime.now(TIMEZONE)
    events_result = service.events().list(
        calendarId=calendar_id,
        timeMin=now.isoformat(),
        timeMax=(now + timedelta(days=days_ahead)).isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    slots: list[dict] = []
    for event in events_result.get("items", []):
        start_raw = event.get("start", {}).get("dateTime")
        end_raw = event.get("end", {}).get("dateTime")
        if not start_raw or not end_raw:
            continue  # all-day event, no time component to book against

        start = _parse_rfc3339(start_raw)
        end = _parse_rfc3339(end_raw)
        if start < now:
            continue  # defensive; timeMin already excludes past instances

        class_type = _parse_class_type(event.get("summary", ""))
        capacity = CLASS_CAPACITY[class_type]
        active_count = bookings.count_active_bookings(event["id"])

        if capacity is not None and active_count >= capacity:
            continue  # slot full

        remaining = None if capacity is None else capacity - active_count
        slots.append({
            "event_id": event["id"],
            "class_type": class_type,
            "start": start,
            "end": end,
            "remaining_slots": remaining,
            "label": _format_slot_label(start, class_type),
        })

    return slots


def book_slot(event_id: str, lead: dict[str, str]) -> dict:
    """Book a lead into a Calendar event's slot.

    Postgres is written first, under create_booking_with_lock()'s advisory
    lock, and only then is the Calendar event patched. If the patch fails
    after the booking was already committed, the booking still stands and
    still counts correctly (capacity is always computed from Postgres, never
    from Calendar attendees) — the only consequence is that the event's
    description/extendedProperties in Google fall out of sync until a later
    booking (or a manual retry) refreshes them. That is surfaced via
    calendar_synced, not by rolling back or raising.

    Args:
        event_id (str): Calendar event id, as returned by get_available_slots().
        lead (dict[str, str]): Must contain "sender" (WhatsApp number, e.g.
            "5521999999999") and "name" (lead's name, already resolved by the AI).

    Returns:
        dict: On success, {"status": "created", "booking_id": str,
        "calendar_synced": bool}. If Postgres rejected the booking before any
        Calendar call was made: {"status": "full", "active_count": int} or
        {"status": "duplicate"}. If the integration itself is unusable:
        {"status": "integration_not_connected"} or {"status": "needs_reconnect"}
        (mark_needs_reconnect() has already run in the latter case).
    """
    try:
        service, calendar_id = _get_service_or_raise()
    except IntegrationNotConnectedError:
        return {"status": "integration_not_connected"}
    except IntegrationNeedsReconnectError:
        return {"status": "needs_reconnect"}

    event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    class_type = _parse_class_type(event.get("summary", ""))
    capacity = CLASS_CAPACITY[class_type]
    start = _parse_rfc3339(event["start"]["dateTime"])
    end = _parse_rfc3339(event["end"]["dateTime"])

    result = bookings.create_booking_with_lock(
        calendar_event_id=event_id,
        sender=lead["sender"],
        lead_name=lead["name"],
        class_type=class_type,
        slot_start=start,
        slot_end=end,
        capacity=capacity,
    )

    if result["status"] != "created":
        return result

    try:
        _patch_event_with_booking(service, calendar_id, event, lead, result["active_count"])
        result["calendar_synced"] = True
    except Exception:
        logger.exception(
            "Booking %s was committed to Postgres but the Calendar patch failed for event %s.",
            result["booking_id"], event_id,
        )
        result["calendar_synced"] = False

    return result


def _patch_event_with_booking(
    service: Any,
    calendar_id: str,
    event: dict,
    lead: dict[str, str],
    booked_count: int,
) -> None:
    """Patch a Calendar event's description and metadata after a successful booking.

    Appends the lead's info under a stable section marker instead of
    overwriting the owner's original description, since more than one lead
    can book the same event over time. corujai_booked_count is written to
    extendedProperties.private, which is invisible in the Calendar UI and
    unaffected by the owner editing the event by hand.

    Args:
        service (Any): Authenticated Calendar API client.
        calendar_id (str): ID of the "Aulas Experimentais" calendar.
        event (dict): The event resource fetched via events.get().
        lead (dict[str, str]): Must contain "sender" and "name".
        booked_count (int): Active booking count for this event, after the insert.
    """
    description = event.get("description") or ""
    confirmed_at = datetime.now(TIMEZONE).strftime("%d/%m/%Y %H:%M")
    booking_line = f"- {lead['name']} ({lead['sender']}) — confirmado em {confirmed_at}"

    if BOOKING_SECTION_MARKER in description:
        new_description = f"{description}\n{booking_line}"
    else:
        separator = "\n\n" if description else ""
        new_description = f"{description}{separator}{BOOKING_SECTION_MARKER}\n{booking_line}"

    service.events().patch(
        calendarId=calendar_id,
        eventId=event["id"],
        body={
            "description": new_description,
            "extendedProperties": {"private": {"corujai_booked_count": str(booked_count)}},
        },
    ).execute()
