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
