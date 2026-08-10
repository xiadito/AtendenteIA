"""Incoming-message orchestration for the goal-driven scheduling AI (Module 3).

handle_text_message() is the single entry point. Its step order matters:

1. Pause check FIRST — a handoff-paused lead gets no reply and costs no tokens,
   and the pause is structurally exempt from the timeout (we never reach the
   timeout code for a paused session). It still STORES the lead's message: a
   human holds the conversation in the dashboard inbox, and an operator who
   cannot see what the lead said during the takeover is blind.
2. 1h inactivity timeout — lazy, evaluated on message arrival from
   sessions.updated_at. No scheduler/cron/thread.
3. Build the per-turn context (cached slots + the lead's active bookings) and
   assemble the two-layer system prompt, plus the resume note when the operator
   just handed this conversation back.
4. Call the LLM, parse its <corujai_action> block defensively, apply state
   leniently and the action strictly, persist, and send the cleaned message.

The conversation is NOT stored here: bot/messages.py owns it, and the LLM
payload is a window over that table (see _to_llm_payload).

Invariant: no parsing or action failure may stop the message from reaching the
lead. Everything degrades; nothing crashes the send.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone

import bot.ai_configs as ai_configs
import bot.bookings as bookings
import bot.messages as messages
import bot.owner_notifications as owner_notifications
import bot.scheduling as scheduling
import bot.session as session
import integrations.store as store
import whatsapp.whatsapp_service as whatsapp_service
from bot.ai_context import ACTION_TAG, build_system_prompt, get_cached_slots
from bot.ai_service import get_ai_response

logger = logging.getLogger(__name__)

# How many messages of the conversation are replayed to the model each turn.
# Counted in MESSAGES, not turns: 20 is the same window the old 10-turn history
# cap gave, now taken from the messages table. An operator burst makes a turn
# stop being exactly two messages, which is why the unit changed.
MAX_PAYLOAD_MESSAGES: int = 20

# A conversation with no activity for this long is closed and restarted from
# scratch on the next message. Evaluated lazily (see handle_text_message):
# a lead who never writes again keeps stale state in the DB forever, which is
# accepted for the build phase (no dashboard funnel exists yet).
INACTIVITY_TIMEOUT: timedelta = timedelta(hours=1)

valid_actions: set[str] = {"none", "book", "handoff"}

# Matches one action block. DOTALL so it spans newlines; the last match wins if
# the model emits two (it correcting itself). Shares ACTION_TAG with the prompt.
_ACTION_BLOCK_PATTERN: re.Pattern[str] = re.compile(
    rf"<{ACTION_TAG}>(.*?)</{ACTION_TAG}>", re.DOTALL | re.IGNORECASE
)


def handle_text_message(
    sender: str,
    body: str,
    tenant_id: str = store.DEFAULT_TENANT_ID,
) -> None:
    """Entry point for an incoming WhatsApp text message.

    `tenant_id` arrives already resolved from Twilio's "To" field
    (webhook/routes.py::receive_twilio) and, since Module S3b, travels through
    EVERY read and write below: get_session(), save_session(), add_message(),
    get_recent_messages(), get_ai_config(), get_cached_slots(),
    list_active_bookings_by_sender(), book_slot(), get_owner_for_notification()
    and enqueue_notification(). Nothing in this path reads flask.g or
    current_user — the tenant is an argument, always (decision 14A).

    That closes the seam Module S3a left open. `sessions` is now keyed by
    (tenant_id, sender), `messages` has a composite foreign key onto it, and
    `trial_bookings`' UNIQUE includes the tenant, so a second gym is genuinely
    isolated rather than reading the pilot's rows.

    Args:
        sender (str): The lead's number, e.g. "5521999999999".
        body (str): The text the lead sent.
        tenant_id (str): The gym this message was written to. Defaults to the
            pilot tenant, which keeps every existing caller (the manual CLIs and
            the test suites) working with two arguments.
    """
    text = body
    # Length, never the text: these messages are whole conversations and the
    # repository is public (see bot/messages.py).
    logger.info(
        "Handling text message from %s (%d chars) for tenant %s.", sender, len(text), tenant_id
    )

    state: dict = session.get_session(sender, tenant_id=tenant_id)

    # 1. Pause check FIRST. A handoff-paused lead is not answered, and this must
    #    come before any token cost and before the timeout (so the pause never
    #    expires on its own). The message is STILL recorded, unread: a human is
    #    holding this conversation in the inbox, and everything the lead says
    #    while the bot is silent is exactly what that operator needs to read.
    if state.get("is_paused"):
        messages.add_message(sender, "lead", text, tenant_id=tenant_id)
        logger.info("Session for %s is paused (handoff); message stored, AI not called.", sender)
        return

    # 2. Lazy 1h inactivity timeout (only reached when not paused).
    updated_at = state.get("updated_at")
    if updated_at is not None and datetime.now(timezone.utc) - updated_at > INACTIVITY_TIMEOUT:
        _reset_timed_out_session(state, sender)

    # 3. Build this turn's context. Active bookings are injected ALWAYS (not just
    #    after a timeout) so the AI always knows what the lead already booked.
    #    The resume note rides along on the single turn after the operator hands
    #    the conversation back, and the marker is cleared right here so it is
    #    delivered exactly once (persisted with the rest of the state in step 7).
    #    get_cached_slots() is the delicate one: its ~60s cache is keyed by
    #    (tenant_id, days_ahead), so passing the tenant is what keeps gym A from
    #    being offered the times cached for gym B.
    config = ai_configs.get_ai_config(tenant_id)
    slots = get_cached_slots(tenant_id=tenant_id)
    active_bookings = bookings.list_active_bookings_by_sender(sender, tenant_id=tenant_id)
    resume_note: bool = bool(state.get("needs_resume_note"))
    system_prompt = build_system_prompt(config, slots, active_bookings, resume_note=resume_note)
    state["needs_resume_note"] = False

    # 4. Record the lead's message, then replay the recent window to the AI.
    #    The window is bounded by conversation_started_at, so a conversation the
    #    timeout restarted does not get the previous one replayed into it.
    messages.add_message(sender, "lead", text, tenant_id=tenant_id)
    recent = messages.get_recent_messages(
        sender,
        MAX_PAYLOAD_MESSAGES,
        since=state.get("conversation_started_at"),
        tenant_id=tenant_id,
    )

    try:
        raw_response: str = get_ai_response(_to_llm_payload(recent), system_prompt)
    except RuntimeError as exc:
        logger.error("AI service error for sender %s: %s", sender, exc)
        raw_response = (
            "Perdão, tivemos uma instabilidade agora. "
            "Pode reenviar a mensagem, por favor?"
        )

    # 5. Parse defensively. A missing block is normal (no warning); anything
    #    malformed degrades to "no action" with a warning.
    action_data = _extract_action(raw_response)
    ai_message = _strip_action_block(raw_response)

    # 6. Apply state (lenient) and the action (strict). The final message may be
    #    the AI's text or a handler-composed recovery message. notification_event
    #    is None unless a booking or a handoff just happened.
    outgoing = ai_message
    notification_event: dict | None = None
    if action_data is not None:
        _apply_lenient_state(state, action_data)
        try:
            outgoing, notification_event = _execute_action(
                state, action_data, slots, sender, ai_message, tenant_id
            )
        except Exception:
            # Errors here (e.g. a Calendar network blip inside book_slot's event
            # fetch) happen before anything is written, so no booking stands.
            # Honor the send invariant with a neutral message, never a false
            # "agendado".
            logger.exception("Action execution failed for sender %s; sending a safe message.", sender)
            state["stage"] = "proposal"
            outgoing = "Tive um probleminha pra processar isso agora. Pode tentar de novo? 🙏"
            notification_event = None

    if not outgoing.strip():
        outgoing = "Desculpe, pode repetir, por favor? 🙂"

    # 7. Persist the state, then the message. The stored text is the OUTGOING one
    #    (action block already stripped): it is what the lead actually saw, what
    #    the operator will read in the inbox, and it keeps the action block out
    #    of the next turn's token budget. Born read — an outgoing message is
    #    nothing for the operator to catch up on.
    session.save_session(sender, state, tenant_id=tenant_id)
    messages.add_message(sender, "ai", outgoing, is_read=True, tenant_id=tenant_id)

    # 7b. Enqueue an owner notification, if this turn closed a booking or
    #     triggered a handoff. Isolated on purpose: this runs after the
    #     session is already saved, in its own try/except that only logs.
    #     A failure here must never stop the reply from reaching the lead.
    if notification_event is not None:
        try:
            owner = store.get_owner_for_notification(tenant_id)
            if owner is None or not owner.get("owner_phone"):
                logger.warning("Owner has no owner_phone configured; skipping notification for %s.", sender)
            else:
                owner_notifications.enqueue_notification(
                    owner_id=owner["id"],
                    owner_phone=owner["owner_phone"],
                    event_type=notification_event["event_type"],
                    lead_sender=sender,
                    booking_id=notification_event.get("booking_id"),
                    tenant_id=tenant_id,
                )
        except Exception:
            logger.exception("Failed to enqueue owner notification for %s; message will still be sent.", sender)

    # 8. Send, from THIS gym's own WhatsApp number (Module S3d).
    #
    #    Unguarded, as it always was: a Twilio failure here answers Twilio with a
    #    500 and Twilio retries, which is the behaviour this route has had since
    #    Module 3. S3d's new failure — a tenant with no whatsapp_number — cannot
    #    reach this line: without a registered number nothing routes to that
    #    tenant in the first place (receive_twilio falls back to 'default'), so
    #    the only tenants that get here are the pilot and gyms that have a number.
    whatsapp_service.send_message(sender, outgoing, tenant_id=tenant_id)


#
# TIMEOUT
#

def _reset_timed_out_session(state: dict, sender: str) -> None:
    """Close a timed-out conversation and reset the session to a fresh start.

    Only called for non-paused sessions. If the previous conversation had not
    reached 'booked', it is recorded as closed_no_booking — via log only, since
    Module 3 deliberately has no conversation_events table (the data is
    discardable during the build).

    Messages are NOT deleted. Since Module 5 they are the operator inbox's
    record of the lead, and wiping them would blind the human to everything that
    came before. The reset instead moves conversation_started_at to now, which
    is the boundary get_recent_messages honours: the AI restarts from nothing
    while the inbox still shows the whole relationship.

    Args:
        state (dict): The session dict to reset in place.
        sender (str): The lead's number, for logging.
    """
    previous_stage = state.get("stage")
    if previous_stage != "booked":
        logger.info("Session for %s timed out at stage %r -> closed_no_booking; restarting.", sender, previous_stage)
    else:
        logger.info("Session for %s timed out after a booking; restarting conversation.", sender)

    state["conversation_started_at"] = datetime.now(timezone.utc)
    state["stage"] = "greeting"
    state["lead_name"] = None
    state["child_name"] = None
    state["qualification"] = "unknown"
    # is_paused is left as-is (False here): a timeout never touches the pause.


#
# ACTION PARSING
#

def _extract_action(raw_response: str) -> dict | None:
    """Extract the action block from an AI response, tolerantly.

    Args:
        raw_response (str): The raw LLM output, which may contain a
            <corujai_action> block anywhere in the text.

    Returns:
        dict | None: The parsed action object, or None when there is no usable
        block (absent, unclosed, malformed, or not a JSON object). None means
        "take no action"; only a truly absent block is silent, the rest warn.
    """
    matches = _ACTION_BLOCK_PATTERN.findall(raw_response)

    if not matches:
        # No closed block. A dangling "<corujai_action>" with no closing tag is
        # a malformed emission, not a clean "no block", so warn on that only.
        if f"<{ACTION_TAG}>".lower() in raw_response.lower():
            logger.warning("Unclosed action tag from the model; treating as no action.")
        return None

    if len(matches) > 1:
        logger.warning("Model emitted %d action blocks; using the last.", len(matches))

    payload = _strip_code_fences(matches[-1].strip())

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        logger.warning("Malformed action JSON; sending the message with no action.")
        return None

    if not isinstance(data, dict):
        logger.warning("Action block was not a JSON object; no action taken.")
        return None

    return data


def _strip_code_fences(text: str) -> str:
    """Strip a surrounding ```json ... ``` fence the model sometimes adds unasked.

    Args:
        text (str): The action-block payload.

    Returns:
        str: The payload without an enclosing markdown code fence.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped)
    return stripped.strip()


def _strip_action_block(raw_response: str) -> str:
    """Remove every action block (and any dangling open tag) from the response.

    Removing from wherever the block sits concatenates the surrounding text, so
    a block in the middle of the message is handled too.

    Args:
        raw_response (str): The raw LLM output.

    Returns:
        str: The lead-facing text, with all action markup removed.
    """
    cleaned = _ACTION_BLOCK_PATTERN.sub("", raw_response)
    # Drop an unclosed trailing "<corujai_action> ..." with no closing tag.
    cleaned = re.sub(rf"<{ACTION_TAG}>.*$", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()


#
# STATE (LENIENT) AND ACTION (STRICT)
#

def _apply_lenient_state(state: dict, data: dict) -> None:
    """Apply the registry-only fields. Invalid values keep the previous state.

    stage is NOT applied here: its final value depends on the action outcome and
    is decided in _execute_action. Unknown fields are ignored silently (they
    have no column — decision 1).

    Args:
        state (dict): The session dict, mutated in place.
        data (dict): The parsed action object.
    """
    lead_name = data.get("lead_name")
    if isinstance(lead_name, str) and lead_name.strip():
        state["lead_name"] = lead_name.strip()

    child_name = data.get("child_name")
    if isinstance(child_name, str) and child_name.strip():
        state["child_name"] = child_name.strip()

    qualification = data.get("qualification")
    if qualification in session.valid_qualifications:
        state["qualification"] = qualification
    elif qualification is not None:
        logger.warning("Invalid qualification %r; keeping the previous value.", qualification)


def _coerce_stage(data: dict, current: str) -> str:
    """Return the model's stage if valid, else keep the current one (with a warning).

    Args:
        data (dict): The parsed action object.
        current (str): The stage to fall back to.

    Returns:
        str: A valid stage value.
    """
    stage = data.get("stage")
    if stage in session.valid_stages:
        return stage
    if stage is not None:
        logger.warning("Invalid stage %r; keeping %r.", stage, current)
    return current


def _execute_action(
    state: dict, data: dict, slots: list[dict], sender: str, ai_message: str,
    tenant_id: str = store.DEFAULT_TENANT_ID,
) -> tuple[str, dict | None]:
    """Execute the strict action and set the final stage. Returns the outgoing text.

    Args:
        state (dict): The session dict, mutated in place.
        data (dict): The parsed action object.
        slots (list[dict]): The slots injected this turn (for event_id validation).
        sender (str): The lead's number.
        ai_message (str): The AI's lead-facing text (block already stripped).
        tenant_id (str): The gym this conversation belongs to, carried down to
            book_slot() so the reservation lands in the right ledger.

    Returns:
        tuple[str, dict | None]: The message to send — the AI's text, or a
        handler-composed recovery message when the action could not complete
        as the model assumed — and a notification_event ({"event_type": ...,
        "booking_id": ...} or {"event_type": "handoff"}) when this turn
        closed a booking or triggered a handoff, else None.
    """
    action = data.get("action")
    if action not in valid_actions:
        logger.warning("Unknown action %r; treating as 'none'.", action)
        action = "none"

    if action == "handoff":
        state["is_paused"] = True
        state["stage"] = "handoff_requested"
        logger.info("Handoff requested by %s; session paused.", sender)
        return ai_message, {"event_type": "handoff"}

    if action == "book":
        return _execute_booking(state, data, slots, sender, ai_message, tenant_id)

    # action == "none": pure registry update.
    state["stage"] = _coerce_stage(data, state.get("stage", "greeting"))
    return ai_message, None


def _execute_booking(
    state: dict, data: dict, slots: list[dict], sender: str, ai_message: str,
    tenant_id: str = store.DEFAULT_TENANT_ID,
) -> tuple[str, dict | None]:
    """Validate and perform a booking, returning the message to send.

    event_id is validated against the injected slots in Python: the AI never
    invents a time, and the code never trusts that it didn't. The final stage
    reflects the real outcome, not the model's optimistic claim.

    Args:
        state (dict): The session dict, mutated in place.
        data (dict): The parsed action object.
        slots (list[dict]): The slots injected this turn.
        sender (str): The lead's number.
        ai_message (str): The AI's lead-facing text.
        tenant_id (str): The gym whose class types and ledger apply.

    Returns:
        tuple[str, dict | None]: The message to send, and a notification_event
        ({"event_type": "booking", "booking_id": ...}) only when book_slot
        actually created a new booking, else None.
    """
    valid_event_ids = {slot["event_id"] for slot in slots}
    event_id = data.get("event_id")

    if not event_id or event_id not in valid_event_ids:
        logger.warning("Booking refused for %s: event_id %r is not in the injected slots.", sender, event_id)
        state["stage"] = "proposal"
        return _reoffer_message(slots), None

    lead_name = state.get("lead_name")
    if not lead_name:
        state["stage"] = "proposal"
        return "Antes de eu confirmar, como você se chama? 🙂", None

    lead = {"sender": sender, "name": lead_name, "child_name": state.get("child_name")}
    result = scheduling.book_slot(event_id, lead, tenant_id=tenant_id)
    status = result.get("status")

    if status == "created":
        state["stage"] = "booked"
        logger.info("Booking created for %s (synced=%s).", sender, result.get("calendar_synced"))
        notification_event = {"event_type": "booking", "booking_id": result["booking_id"]}
        return ai_message, notification_event

    if status == "missing_child_name":
        state["stage"] = "proposal"
        return "Pra confirmar a aula experimental, me diz o nome da criança que vai participar? 🙂", None

    if status == "full":
        state["stage"] = "proposal"
        options = _format_slot_options(slots, exclude_event_id=event_id)
        if options:
            return f"Poxa, esse horário acabou de lotar! 😕 Mas ainda temos estes:\n{options}\nQual fica melhor pra você?", None
        return "Poxa, esse horário acabou de lotar. Vou verificar outros horários e já te retorno! 🙏", None

    if status == "duplicate":
        state["stage"] = "booked"
        return "Você já tem esse horário reservado com a gente! 😄 Posso te ajudar com mais alguma coisa?", None

    if status in {"integration_not_connected", "needs_reconnect"}:
        logger.warning("Booking for %s could not proceed: integration status %r.", sender, status)
        state["stage"] = "proposal"
        return "Tivemos um probleminha técnico pra confirmar o horário agora. Já já retorno pra fechar com você, tá? 🙏", None

    logger.warning("Unexpected book_slot status %r for %s.", status, sender)
    state["stage"] = "proposal"
    return _reoffer_message(slots), None


def _format_slot_options(slots: list[dict], exclude_event_id: str | None = None, limit: int = 6) -> str:
    """Render up to `limit` slot labels as a hyphen list, optionally excluding one.

    Args:
        slots (list[dict]): The injected slots.
        exclude_event_id (str | None): A slot to leave out (e.g. the one that filled).
        limit (int): Maximum number of options to list.

    Returns:
        str: A hyphen-bulleted list of labels, or "" when there is nothing to offer.
    """
    labels = [slot["label"] for slot in slots if slot["event_id"] != exclude_event_id]
    if not labels:
        return ""
    return "\n".join(f"- {label}" for label in labels[:limit])


def _reoffer_message(slots: list[dict]) -> str:
    """Message used when a booking is refused and the lead should pick a real slot.

    Args:
        slots (list[dict]): The injected slots.

    Returns:
        str: A Portuguese message re-offering the available times.
    """
    options = _format_slot_options(slots)
    if options:
        return f"Deixa eu confirmar os horários disponíveis certinho pra você:\n{options}\nQual você prefere?"
    return "No momento estou sem horários disponíveis, mas já verifico e te retorno! 🙏"


#
# LLM PAYLOAD
#

def _to_llm_payload(recent: list[dict]) -> list[dict[str, str]]:
    """Turn message rows into the {"role", "content"} list the LLM expects.

    The lead becomes "user"; the AI and the operator both become "assistant",
    since to the lead they are one continuous attendant (bot.messages
    .author_to_role owns that mapping).

    Leading assistant messages are dropped. The window is a fixed-size tail of
    the conversation, so it can easily open in the middle of an operator's burst
    of replies — and a payload whose first message is "assistant" is rejected by
    the Anthropic-compatible endpoint. Trimming from the front costs the model a
    little context; not trimming costs the lead their answer.

    Args:
        recent (list[dict]): Rows from messages.get_recent_messages(), already
            in chronological order.

    Returns:
        list[dict[str, str]]: The conversation payload, starting at a user message.
    """
    payload: list[dict[str, str]] = [
        {"role": messages.author_to_role(row["author"]), "content": row["content"]}
        for row in recent
    ]

    first_user = next((i for i, item in enumerate(payload) if item["role"] == "user"), None)
    if first_user is None:
        # Nothing from the lead in the window: there is no conversation to
        # replay, and the caller's message is always appended before this runs.
        return []
    if first_user > 0:
        logger.info("Dropped %d leading assistant message(s) from the LLM payload.", first_user)

    return payload[first_user:]
