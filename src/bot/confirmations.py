"""Booking confirmation: the single place a trial class is closed out.

The owner can decide a trial class in two places — by replying 1/2 to the
WhatsApp notification, or by clicking Confirmar/Cancelar on the dashboard — and
both go through confirm_or_cancel_booking(). Duplicating the rule in the two
call sites is the mistake this module exists to prevent: the transition guard
lives here, so both channels inherit it instead of each remembering to check.

Two orderings in here are load-bearing:

- STATUS FIRST, LEAD SECOND. update_booking_status() is the authoritative fact;
  the WhatsApp notice to the lead is a courtesy on top of it. The notice runs in
  its own try/except that only logs, because send_message() re-raises: a Twilio
  blip must not roll the status back, and must not blow up the owner's webhook
  (which would make Twilio retry the owner's "1" and stamp a second time).

- GUARD BEFORE EVERYTHING. Only a booking still in 'pending_confirmation' can be
  decided. That single check is what makes a double reply and a double click
  both harmless — the second one finds a booking that is no longer pending and
  returns "skipped" without touching anything or re-notifying the lead.

Cancelling frees the seat with no Calendar call at all: get_available_slots()
sizes a slot with bookings.count_active_bookings(), which counts
status != 'cancelled', so the seat is back the moment the row flips. The event's
description keeps the cancelled student's line — cosmetic, and documented in
CLAUDE.md's Known Issues.

The notice is a single transactional message. It never calls the AI, and it
never unpauses a conversation: a lead being handled by a human still deserves to
hear that their class was confirmed, and the operator stays in control.
"""

import logging

import bot.bookings as bookings
import bot.messages as messages
import whatsapp.whatsapp_service as whatsapp_service
import bot.class_types as class_types
from bot.scheduling import TIMEZONE

logger = logging.getLogger(__name__)

# The two outcomes a pending booking can be decided into. A subset of
# bookings.valid_booking_statuses on purpose: 'pending_confirmation' is where a
# booking starts, never something the owner can move it back to.
valid_decisions: set[str] = {
    "confirmed",
    "cancelled",
}

# The only status a booking can be decided from (guard 5A).
DECIDABLE_STATUS: str = "pending_confirmation"


def confirm_or_cancel_booking(booking_id: str, decision: str) -> dict:
    """Close a trial-class booking out, and tell the lead what happened.

    Args:
        booking_id (str): trial_bookings.id the owner decided on.
        decision (str): One of valid_decisions.

    Returns:
        dict: One of
            {"result": "not_found"} — no booking has this id;
            {"result": "skipped", "status": str} — the booking was already
                decided; nothing was written and the lead was not notified;
            {"result": "applied", "decision": str, "lead_notified": bool} — the
                status was updated. lead_notified is False when the WhatsApp
                notice failed, which does not undo the decision.

    Raises:
        ValueError: If decision is not one of valid_decisions.
    """
    if decision not in valid_decisions:
        raise ValueError(f"Invalid booking decision: {decision!r}")

    booking = bookings.get_booking(booking_id)
    if booking is None:
        logger.warning("Booking %s not found; nothing to decide.", booking_id)
        return {"result": "not_found"}

    # Guard 5A. Both channels land here, so neither has to remember the rule.
    if booking["status"] != DECIDABLE_STATUS:
        logger.info(
            "Booking %s is already '%s'; ignoring the '%s' decision.",
            booking_id, booking["status"], decision,
        )
        return {"result": "skipped", "status": booking["status"]}

    bookings.update_booking_status(booking_id, decision)

    lead_notified = _notify_lead(booking, decision)
    return {"result": "applied", "decision": decision, "lead_notified": lead_notified}


def _notify_lead(booking: dict, decision: str) -> bool:
    """Send the lead the outcome of their trial class and record it.

    Isolated so a delivery failure can never reach the caller: the booking is
    already decided by the time this runs, and the owner's webhook must answer
    Twilio with a 200 either way.

    Args:
        booking (dict): The booking row, as it was BEFORE the status update —
            only the descriptive columns are read, which the update never
            touches.
        decision (str): One of valid_decisions.

    Returns:
        bool: True if the message was sent and recorded, False if it failed.
    """
    try:
        text = _compose_lead_message(booking, decision)
        whatsapp_service.send_message(booking["sender"], text)
        # is_read=True: the operator's unread count is for what the LEAD says,
        # and this is the attendant talking (see bot/messages.py).
        messages.add_message(booking["sender"], "ai", text, is_read=True)
    except Exception:
        # Never log the text itself (public repo): the booking id is enough to
        # find the row and retry by hand.
        logger.exception("Could not notify the lead about booking %s.", booking["id"])
        return False

    logger.info("Lead notified about booking %s (%s).", booking["id"], decision)
    return True


def _compose_lead_message(booking: dict, decision: str) -> str:
    """Build the Portuguese WhatsApp text telling the lead the outcome.

    Args:
        booking (dict): The booking row.
        decision (str): One of valid_decisions.

    Returns:
        str: The message to send to the lead.
    """
    # One read per decision — this is a single booking, not a loop.
    class_labels: dict[str, str] = class_types.load_class_types()["labels"]
    class_label = class_labels.get(booking["class_type"], booking["class_type"])
    when = booking["slot_start"].astimezone(TIMEZONE).strftime("%d/%m às %H:%M")
    lead_name = booking["lead_name"]
    child_name = booking.get("child_name")

    if decision == "confirmed":
        headline = (
            f"✅ {lead_name}, a aula experimental de {child_name} está confirmada!"
            if child_name
            else f"✅ {lead_name}, sua aula experimental está confirmada!"
        )
        return (
            f"{headline}\n"
            f"Turma: {class_label}\n"
            f"Quando: {when}\n"
            "Chegue uns 10 minutinhos antes. Qualquer imprevisto, é só me avisar por aqui!"
        )

    headline = (
        f"😕 {lead_name}, infelizmente a aula experimental de {child_name} de {when} foi cancelada."
        if child_name
        else f"😕 {lead_name}, infelizmente sua aula experimental de {when} foi cancelada."
    )
    return (
        f"{headline}\n"
        f"Turma: {class_label}\n"
        "Me chama por aqui que eu já vejo outro horário para você. 💪"
    )
