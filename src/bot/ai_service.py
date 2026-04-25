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
from bot.ai_context import system_prompt

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

def get_ai_response(conversation_history: list[dict[str, str]]) -> str:
    """
    Sends the conversation history to the LLM and returns the attendant reply.

    Args:
        conversation_history (list[dict[str, str]]): 
        List of messsages in the conversation.
        the keys are "role" and "content".
        "role" is either "user or "attendant".
    Returns:
        str: The attendant reply as a string.
    """
    
    model: str = Config.AI_MODEL
    client: OpenAI = _get_client()
    
    # def exemplo_visual()
        # messages = list[dict[str, str]] = [
        #     {},
        #     {},
        #     {},
        #     *conversation_history = [{}, {}, {},] -> {}, {}, {},
        
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}, *conversation_history]
    logger.info("Calling AI model '%s' with %d message(s).", model, len(messages))
    
    try:
        response = client.chat.completions.create(
            model = model,
            messages = messages,
            temperature = 0.7,
            max_tokens = 512,
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