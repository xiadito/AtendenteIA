import logging
import whatsapp.whatsapp_service as whatsapp_service
import bot.session as session
from bot.ai_service import get_ai_response

logger = logging.getLogger(__name__)

# Maximum number of conversation turns kept in memory per session.
# Each "turn" = 1 user message + 1 assistant reply = 2 list items.
# Keeping the window short reduces token usage and avoids stale context
max_history_turns: int = 10

def _trim_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """
    Keeps only the most recent max_history_turns turns, for short token usage porpouses.
    
    Each turns is a user+assistant pair (2 items). We calculate the max number of list items and slice from the end to preserve recency
    
    Args:
        history (list[dict[str, str]]): conversation history list

    Returns:
        list[dict[str, str]]: trimmed list with the most recent items. Minimum of 2 items returning.
    """
    
    max_items: int = max_history_turns * 2
    if len(history) > max_items:
        return history[-max_items:] #here we use - to trim from the end of the list and keep only the recent items
    
    return history
    
def handle_text_message(sender: str, body: str) -> dict:
    """
    Main entry point for incoming messages.
    Fetches the session, checks for global comands, and delegates to the apropriate handler.
    Args:
        sender (str): number of the user that sent the message.
        body (str): text that the client sent.
    Returns:
        dict: the dict of the section uptaded with the new state
    """
    text = body
    logger.info("Handling text message from %s: %.80s", sender, text)
    
    # load the existing session
    _session: dict = session.get_session(sender) # get the session of the client
    history: list[dict[str, str]] = _session.get("history", [])
    
    # Append the new user turn to the history
    _add_to_history(history, "user", text)

    # call the AI to give a response
    try:
        response: str = get_ai_response(_trim_history(history))
    except RuntimeError as exc:
        logger.error("AI service error for sender %s: %s", sender, exc)
        response = (
            "Perdão, aconteceu uma instabilidade agora."
            "Poderia reenviar a mensagem ou chamar outro atendente?"
        )

    # Append the assistant reply to history
    _add_to_history(history, "assistant", response)
    _session["history"] = _trim_history(history)
    
    whatsapp_service.send_message(sender, response)
    
def _add_to_history(history: list, role: str, content: str) -> None:
    """
    Add to the history in the correct format.
    Minimizes rewriting the same thing.

    Args:
        history (list): the conversation history list to append to.
        role (str): is either user ou attendant
        content (str): response from user, attendant or system.
    """
    allowed_roles: set = {"user", "assistant"}

    if role not in allowed_roles:
        raise ValueError(f"Role inválida: {role!r}")

    history.append({"role": role, "content": content})  