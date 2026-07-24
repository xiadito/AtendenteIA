"""Incoming-message orchestration for the goal-driven scheduling AI (Module 3).

handle_text_message() is the single entry point. Its step order matters:

1. Pause check FIRST — a handoff-paused lead gets no reply and costs no tokens,
   and the pause is structurally exempt from the timeout (we never reach the
   timeout code for a paused session).
2. 1h inactivity timeout — lazy, evaluated on message arrival from
   sessions.updated_at. No scheduler/cron/thread.
3. Build the per-turn context (cached slots + the lead's active bookings) and
   assemble the two-layer system prompt.
4. Call the LLM, parse its <corujai_action> block defensively, apply state
   leniently and the action strictly, persist, and send the cleaned message.

Invariant: no parsing or action failure may stop the message from reaching the
lead. Everything degrades; nothing crashes the send.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone

import bot.ai_configs as ai_configs
import bot.bookings as bookings
import bot.scheduling as scheduling
import bot.session as session
import whatsapp.whatsapp_service as whatsapp_service
from bot.ai_context import ACTION_TAG, build_system_prompt, get_cached_slots
from bot.ai_service import get_ai_response

logger = logging.getLogger(__name__)

# Maximum conversation turns kept per session (1 turn = user + assistant = 2 items).
max_history_turns: int = 10

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


def handle_text_message(sender: str, body: str) -> None:
    """Entry point for an incoming WhatsApp text message.

    Args:
        sender (str): The lead's number, e.g. "5521999999999".
        body (str): The text the lead sent.
    """
    text = body
    logger.info("Handling text message from %s: %.80s", sender, text)

    state: dict = session.get_session(sender)

    # 1. Pause check FIRST. A handoff-paused lead is not answered, and this must
    #    come before any token cost and before the timeout (so the pause never
    #    expires on its own).
    if state.get("is_paused"):
        logger.info("Session for %s is paused (handoff); not replying.", sender)
        return

    # 2. Lazy 1h inactivity timeout (only reached when not paused).
    updated_at = state.get("updated_at")
    if updated_at is not None and datetime.now(timezone.utc) - updated_at > INACTIVITY_TIMEOUT:
        _reset_timed_out_session(state, sender)

    # 3. Build this turn's context. Active bookings are injected ALWAYS (not just
    #    after a timeout) so the AI always knows what the lead already booked.
    config = ai_configs.get_ai_config()
    slots = get_cached_slots()
    active_bookings = bookings.list_active_bookings_by_sender(sender)
    system_prompt = build_system_prompt(config, slots, active_bookings)

    # 4. Append the user turn and call the AI.
    history: list[dict[str, str]] = state["history"]
    _add_to_history(history, "user", text)

    try:
        raw_response: str = get_ai_response(_trim_history(history), system_prompt)
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
    #    the AI's text or a handler-composed recovery message.
    outgoing = ai_message
    if action_data is not None:
        _apply_lenient_state(state, action_data)
        try:
            outgoing = _execute_action(state, action_data, slots, sender, ai_message)
        except Exception:
            # Errors here (e.g. a Calendar network blip inside book_slot's event
            # fetch) happen before anything is written, so no booking stands.
            # Honor the send invariant with a neutral message, never a false
            # "agendado".
            logger.exception("Action execution failed for sender %s; sending a safe message.", sender)
            state["stage"] = "proposal"
            outgoing = "Tive um probleminha pra processar isso agora. Pode tentar de novo? 🙏"

    if not outgoing.strip():
        outgoing = "Desculpe, pode repetir, por favor? 🙂"

    # 7. Persist. History stores the OUTGOING text (block already stripped): it
    #    is what the lead actually saw and keeps the next turn coherent, while
    #    keeping the (now larger) action block out of the token budget.
    _add_to_history(history, "assistant", outgoing)
    state["history"] = _trim_history(history)
    session.save_session(sender, state)

    # 8. Send.
    whatsapp_service.send_message(sender, outgoing)


#
# TIMEOUT
#

def _reset_timed_out_session(state: dict, sender: str) -> None:
    """Close a timed-out conversation and reset the session to a fresh start.

    Only called for non-paused sessions. If the previous conversation had not
    reached 'booked', it is recorded as closed_no_booking — via log only, since
    Module 3 deliberately has no conversation_events table (the data is
    discardable during the build).

    Args:
        state (dict): The session dict to reset in place.
        sender (str): The lead's number, for logging.
    """
    previous_stage = state.get("stage")
    if previous_stage != "booked":
        logger.info("Session for %s timed out at stage %r -> closed_no_booking; restarting.", sender, previous_stage)
    else:
        logger.info("Session for %s timed out after a booking; restarting conversation.", sender)

    state["history"] = []
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


def _execute_action(state: dict, data: dict, slots: list[dict], sender: str, ai_message: str) -> str:
    """Execute the strict action and set the final stage. Returns the outgoing text.

    Args:
        state (dict): The session dict, mutated in place.
        data (dict): The parsed action object.
        slots (list[dict]): The slots injected this turn (for event_id validation).
        sender (str): The lead's number.
        ai_message (str): The AI's lead-facing text (block already stripped).

    Returns:
        str: The message to send — the AI's text, or a handler-composed recovery
        message when the action could not complete as the model assumed.
    """
    action = data.get("action")
    if action not in valid_actions:
        logger.warning("Unknown action %r; treating as 'none'.", action)
        action = "none"

    if action == "handoff":
        state["is_paused"] = True
        state["stage"] = "handoff_requested"
        logger.info("Handoff requested by %s; session paused.", sender)
        return ai_message

    if action == "book":
        return _execute_booking(state, data, slots, sender, ai_message)

    # action == "none": pure registry update.
    state["stage"] = _coerce_stage(data, state.get("stage", "greeting"))
    return ai_message


def _execute_booking(state: dict, data: dict, slots: list[dict], sender: str, ai_message: str) -> str:
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

    Returns:
        str: The message to send.
    """
    valid_event_ids = {slot["event_id"] for slot in slots}
    event_id = data.get("event_id")

    if not event_id or event_id not in valid_event_ids:
        logger.warning("Booking refused for %s: event_id %r is not in the injected slots.", sender, event_id)
        state["stage"] = "proposal"
        return _reoffer_message(slots)

    lead_name = state.get("lead_name")
    if not lead_name:
        state["stage"] = "proposal"
        return "Antes de eu confirmar, como você se chama? 🙂"

    lead = {"sender": sender, "name": lead_name, "child_name": state.get("child_name")}
    result = scheduling.book_slot(event_id, lead)
    status = result.get("status")

    if status == "created":
        state["stage"] = "booked"
        logger.info("Booking created for %s (synced=%s).", sender, result.get("calendar_synced"))
        return ai_message

    if status == "missing_child_name":
        state["stage"] = "proposal"
        return "Pra confirmar a aula experimental, me diz o nome da criança que vai participar? 🙂"

    if status == "full":
        state["stage"] = "proposal"
        options = _format_slot_options(slots, exclude_event_id=event_id)
        if options:
            return f"Poxa, esse horário acabou de lotar! 😕 Mas ainda temos estes:\n{options}\nQual fica melhor pra você?"
        return "Poxa, esse horário acabou de lotar. Vou verificar outros horários e já te retorno! 🙏"

    if status == "duplicate":
        state["stage"] = "booked"
        return "Você já tem esse horário reservado com a gente! 😄 Posso te ajudar com mais alguma coisa?"

    if status in {"integration_not_connected", "needs_reconnect"}:
        logger.warning("Booking for %s could not proceed: integration status %r.", sender, status)
        state["stage"] = "proposal"
        return "Tivemos um probleminha técnico pra confirmar o horário agora. Já já retorno pra fechar com você, tá? 🙏"

    logger.warning("Unexpected book_slot status %r for %s.", status, sender)
    state["stage"] = "proposal"
    return _reoffer_message(slots)


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
# HISTORY HELPERS
#

def _trim_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep only the most recent max_history_turns turns (2 items each).

    Args:
        history (list[dict[str, str]]): The conversation history.

    Returns:
        list[dict[str, str]]: The trimmed history, most recent items kept.
    """
    max_items: int = max_history_turns * 2
    if len(history) > max_items:
        return history[-max_items:]
    return history


def _add_to_history(history: list, role: str, content: str) -> None:
    """Append a message to the history in {"role", "content"} form.

    Args:
        history (list): The history list to append to.
        role (str): "user" or "assistant".
        content (str): The message text.

    Raises:
        ValueError: If role is not "user" or "assistant".
    """
    allowed_roles: set = {"user", "assistant"}
    if role not in allowed_roles:
        raise ValueError(f"Invalid role: {role!r}")

    history.append({"role": role, "content": content})
