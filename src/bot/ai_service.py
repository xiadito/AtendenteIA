"""
ai_service.py

Handles all communication with the language model.
In development: Ollama local (llama3.2 / mistral) via OpenAI-compatible endpoint.
In production:  Claude Haiku 4.5 via Anthropic API.

The switch between environments is controlled exclusively by environment variables:
    AI_BASE_URL, AI_MODEL, AI_API_KEY

No code changes are required between dev and prod.
"""
import logging
from config import Config
from openai import OpenAI

logger = logging.getLogger(__name__)

def _get_client() -> OpenAI:
    """
    Builds and returns an OpenAI client

    The base_url determines wheter requests go to Ollama (dev) 
    or go to the Anthropic-compatible endpoint (prod)
    Raises:
        ValueError: If the .env is missing a variable an error is raised
    Returns:
        OpenAI: API Client 
    """
    
    if not Config.AI_BASE_URL or not Config.AI_MODEL or not Config.AI_API_KEY:
        raise ValueError("AI configuration is incomplete. Please set AI_BASE_URL, AI_MODEL, and AI_API_KEY in environment variables.")
    
    client = OpenAI(base_url=Config.AI_BASE_URL, api_key=Config.AI_API_KEY)
    
    return client

def get_ai_response(conversation_history: list[dict[str, str]], system_prompt: str) -> str:
    """
    Sends the conversation history to the LLM and returns the attendant reply.

    The system prompt is passed in per turn (not imported) because Module 3
    rebuilds it every message: it mixes the protected layer with per-tenant
    config, the currently available slots and the lead's active bookings.

    Args:
        conversation_history (list[dict[str, str]]):
            List of messages in the conversation. The keys are "role" and
            "content"; "role" is either "user" or "assistant".
        system_prompt (str): The fully assembled system prompt for this turn,
            built by bot.ai_context.build_system_prompt().
    Returns:
        str: The attendant reply as a string. May contain a <corujai_action>
        block, which the handler parses and strips before sending to the lead.
    """

    model: str = Config.AI_MODEL
    client: OpenAI = _get_client()

    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}, *conversation_history]
    logger.info("Calling AI model '%s' with %d message(s).", model, len(messages))

    try:
        response = client.chat.completions.create(
            model = model,
            messages = messages,
            temperature = 0.7,
            # Headroom for the Portuguese reply AND the appended action block;
            # a too-small limit truncates the block and breaks the parse.
            max_tokens = 900,
        )
    except Exception as exc:
        logger.error("AI API call failed: %s", exc)
        raise RuntimeError(f"AI service unavailable: {exc}") from exc
    
    reply: str = response.choices[0].message.content or ""
    
    if not reply.strip():
        logger.warning("AI returned an empty respose.")
        return "Desculpe, não consegui entendi. Poderia enviar novamente?"

    logger.info("AI replied with %d", len(reply))
    return reply.strip()

def update_order_status(sender: str, order_index: int, status: str) -> bool:
    """
    Updates the status of an order in the database.

    Args:
        sender (str): The user who sent the message.
        order_index (int): The index of the order to update.
        status (str): The new status to set for the order.

    Returns:
        bool: True if the update was successful, False otherwise.
    """