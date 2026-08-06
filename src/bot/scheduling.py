import logging
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import integrations.store as store
from bot import bookings, class_types
from integrations.google_calendar import NeedsReconnectError, get_calendar_service
from integrations.store import DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)

TIMEZONE = ZoneInfo("America/Sao_Paulo")

# Business rule, not Calendar data: Google Calendar remains the source of truth
# for which slots exist, when, and of what type (via the title marker below).
# How many leads fit in each type is the tenant's decision, and since Module S2
# it lives in the class_types table rather than in a dict here — see
# bot/class_types.py. Every function below that needs it loads the tenant's
# types ONCE and passes the bundle down; nothing in this module reads them
# per slot.

# Matches a "[MARKER]" at the start of an event title, tolerant of extra
# spaces and accented letters (e.g. "[ CRIANÇAS ]").
_TITLE_MARKER_PATTERN = re.compile(r"^\s*\[\s*([a-zA-ZÀ-ÿ]+)\s*\]")

# Section header appended to an event's description on the first booking.
# Later bookings for the same event append a line under it instead of
# duplicating the header, since capacity > 1 means more than one lead can
# book the same slot over time.
BOOKING_SECTION_MARKER = "--- Reservas Corujai ---"

# Written into the description of an event created from the settings screen, so
# the owner scrolling their calendar knows where it came from. It sits ABOVE the
# booking section marker that _patch_event_with_booking() appends later, and is
# never parsed — the class type always comes from the title.
CREATED_FROM_PANEL_NOTE = "Aula criada pelo painel Corujai."

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


def _parse_class_type(title: str, tenant_types: dict[str, Any]) -> str:
    """Parse the class type marker from a Calendar event title.

    The marker read from the title goes through the SAME normalizer the
    settings screen uses to store one (class_types.normalize_marker), so
    "[ Crianças ]" typed into the calendar and "crianças" typed into the form
    can never end up as two different types.

    Args:
        title (str): Event summary, e.g. "[CRIANCAS] Aula Experimental".
        tenant_types (dict[str, Any]): The bundle from
            class_types.load_class_types(), loaded once by the caller.

    Returns:
        str: A key of tenant_types["capacities"] — guaranteed, because the
        fallback is itself one (see load_class_types()). Any title without a
        recognized marker falls back, so a mis-typed slot never blocks a
        booking, and callers can look capacity up directly without a KeyError.
    """
    match = _TITLE_MARKER_PATTERN.match(title or "")
    if match:
        marker: str | None = class_types.normalize_marker(match.group(1))
        if marker is not None and marker in tenant_types["capacities"]:
            return marker

    fallback: str = tenant_types["fallback"]
    logger.warning(
        "Unrecognized class marker in event title '%s'; defaulting to %s.", title, fallback,
    )
    return fallback


def _parse_rfc3339(value: str) -> datetime:
    """Parse an RFC3339 timestamp (as returned by the Calendar API) into São Paulo time.

    Args:
        value (str): e.g. "2026-07-20T18:00:00-03:00".

    Returns:
        datetime: Timezone-aware datetime in America/Sao_Paulo.
    """
    return datetime.fromisoformat(value).astimezone(TIMEZONE)


def _format_slot_label(start: datetime, class_type: str, labels: dict[str, str]) -> str:
    """Build a Portuguese, human-readable label for a slot.

    Args:
        start (datetime): Slot start, in São Paulo time.
        class_type (str): The slot's class type marker.
        labels (dict[str, str]): marker → label, from the tenant's bundle.
    """
    weekday = _WEEKDAY_NAMES_PT[start.weekday()].capitalize()
    class_label = labels.get(class_type, class_type.title())
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


def create_class_event(
    marker: str,
    start: datetime,
    end: datetime,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> dict:
    """Create one class occurrence on the owner's Calendar.

    THIS IS THE ONLY events.insert IN THE PROJECT, and it does not weaken the
    rule it appears to. Google Calendar stays the single source of truth for
    which slots exist and when: this writes the event and then forgets it, the
    same way the owner typing into Google Calendar does. Nothing is stored on
    our side, so there is no second record of availability to drift. What was
    refused (Module S2, decision 7A) is a *grid* — a recurring rule in Postgres
    that defines availability and has to generate and reconcile events.

    The title is BUILT, never typed: "[MARKER] Label", both taken from the
    tenant's registered class type. The marker is what _parse_class_type() reads
    the event back with — building it removes the whole class of typos that
    would otherwise silently drop an event into the fallback — and the label is
    there so the owner reading their own Google Calendar sees a class name
    ("Crianças"), not just a machine key.

    Args:
        marker (str): Canonical marker of a class type registered for the
            tenant. Validated here against class_types, not trusted.
        start (datetime): Timezone-aware start of the class.
        end (datetime): Timezone-aware end of the class.
        tenant_id (str): Tenant whose class types and calendar apply.

    Returns:
        dict: {"status": "created", "event_id": str, "summary": str,
        "label": str} on success; {"status": "unknown_class_type"} if the marker
        is not one of the tenant's; {"status": "integration_not_connected"} or
        {"status": "needs_reconnect"} if the calendar is unusable.
    """
    tenant_types: dict[str, Any] = class_types.load_class_types(tenant_id)
    if marker not in tenant_types["capacities"]:
        logger.warning("Refused to create an event for unknown class type '%s'.", marker)
        return {"status": "unknown_class_type"}

    try:
        service, calendar_id = _get_service_or_raise()
    except IntegrationNotConnectedError:
        return {"status": "integration_not_connected"}
    except IntegrationNeedsReconnectError:
        return {"status": "needs_reconnect"}

    summary = f"[{marker}] {tenant_types['labels'][marker]}"
    event = service.events().insert(
        calendarId=calendar_id,
        body={
            "summary": summary,
            "description": CREATED_FROM_PANEL_NOTE,
            "start": {"dateTime": start.isoformat(), "timeZone": str(TIMEZONE)},
            "end": {"dateTime": end.isoformat(), "timeZone": str(TIMEZONE)},
        },
    ).execute()

    logger.info("Class event %s created for tenant %s (%s).", event["id"], tenant_id, summary)
    return {
        "status": "created",
        "event_id": event["id"],
        "summary": summary,
        "label": _format_slot_label(start, marker, tenant_types["labels"]),
    }


def get_available_slots(
    days_ahead: int | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> list[dict]:
    """List Calendar slots that still have open seats.

    Args:
        days_ahead (int | None): How many days ahead of now to look for slots.
            None reads the tenant's configured horizon (scheduling_configs,
            default 14); an explicit value overrides it without touching the
            database.
        tenant_id (str): Tenant whose class types and horizon apply.

    Returns:
        list[dict]: Each item has event_id, class_type, start, end,
        remaining_slots (int | None; None means unlimited), requires_child_name
        and label, ordered by start time. Full slots and all-day events are
        omitted.

    Raises:
        IntegrationNotConnectedError: The owner hasn't connected Google
            Calendar, or calendar_id is missing.
        IntegrationNeedsReconnectError: Google rejected the refresh_token.
    """
    service, calendar_id = _get_service_or_raise()

    if days_ahead is None:
        days_ahead = class_types.get_scheduling_config(tenant_id)["days_ahead"]

    # ONE read for the whole sweep. The loop below runs once per Calendar event,
    # so looking a type up in the database per event would turn a single query
    # into one per slot.
    tenant_types: dict[str, Any] = class_types.load_class_types(tenant_id)

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

        class_type = _parse_class_type(event.get("summary", ""), tenant_types)
        # Direct lookup, no .get: _parse_class_type only ever returns a key of
        # this dict — including its fallback. See load_class_types().
        capacity = tenant_types["capacities"][class_type]
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
            "requires_child_name": class_type in tenant_types["child_name_required"],
            "label": _format_slot_label(start, class_type, tenant_types["labels"]),
        })

    return slots


def book_slot(
    event_id: str,
    lead: dict[str, str],
    tenant_id: str = DEFAULT_TENANT_ID,
) -> dict:
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
            For a slot whose class type has requires_child_name set it must also
            carry "child_name" (the child who attends); the responsible adult
            stays in "name".
        tenant_id (str): Tenant whose class types apply.

    Returns:
        dict: On success, {"status": "created", "booking_id": str,
        "active_count": int, "calendar_synced": bool}. If Postgres rejected the
        booking before any Calendar call was made: {"status": "full",
        "active_count": int} or {"status": "duplicate"}. If a child class was
        booked without a child's name: {"status": "missing_child_name"} (the
        conversation stays alive and asks for the name). If the integration
        itself is unusable: {"status": "integration_not_connected"} or
        {"status": "needs_reconnect"} (mark_needs_reconnect() has already run in
        the latter case).
    """
    try:
        service, calendar_id = _get_service_or_raise()
    except IntegrationNotConnectedError:
        return {"status": "integration_not_connected"}
    except IntegrationNeedsReconnectError:
        return {"status": "needs_reconnect"}

    tenant_types: dict[str, Any] = class_types.load_class_types(tenant_id)

    event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    class_type = _parse_class_type(event.get("summary", ""), tenant_types)
    # Direct lookup, no .get — see the identical note in get_available_slots().
    capacity = tenant_types["capacities"][class_type]
    requires_child_name: bool = class_type in tenant_types["child_name_required"]
    start = _parse_rfc3339(event["start"]["dateTime"])
    end = _parse_rfc3339(event["end"]["dateTime"])

    # Child classes require the child's name, and the check lives here (not just
    # in the prompt) for the same reason event_id is validated in the handler:
    # the code never trusts that the AI supplied a required field. Reject before
    # touching Postgres so the conversation can go back and collect the name.
    child_name = (lead.get("child_name") or "").strip()
    if requires_child_name and not child_name:
        logger.info("Booking rejected for event %s: %s class needs a child name.", event_id, class_type)
        return {"status": "missing_child_name"}

    result = bookings.create_booking_with_lock(
        calendar_event_id=event_id,
        sender=lead["sender"],
        lead_name=lead["name"],
        class_type=class_type,
        slot_start=start,
        slot_end=end,
        capacity=capacity,
        child_name=child_name or None,
    )

    if result["status"] != "created":
        return result

    try:
        _patch_event_with_booking(
            service, calendar_id, event, lead, requires_child_name, result["active_count"],
        )
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
    requires_child_name: bool,
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
        lead (dict[str, str]): Must contain "sender" and "name". For child
            classes it also carries "child_name".
        requires_child_name (bool): Whether this slot's class type asks for the
            attending child's name. Passed in rather than re-derived, so it can
            never disagree with the check book_slot() already made. Selects the
            line format: child classes show the child first with the responsible
            adult noted.
        booked_count (int): Active booking count for this event, after the insert.
    """
    description = event.get("description") or ""
    confirmed_at = datetime.now(TIMEZONE).strftime("%d/%m/%Y %H:%M")
    child_name = (lead.get("child_name") or "").strip()
    if requires_child_name and child_name:
        booking_line = f"- {child_name} (resp.: {lead['name']} — {lead['sender']}) — confirmado em {confirmed_at}"
    else:
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
