# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

All commands run from the `src/` directory with the virtualenv activated:

```bash
# Activate virtualenv (Windows)
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run locally (dev)
cd src && python app.py          # Flask dev server on port 5000

# Run as production (gunicorn)
cd src && gunicorn "app:create_app()"
```

There are no automated tests currently.

## Architecture Overview

This is a **WhatsApp chatbot** for a grocery store ("Mercadinho da Vila") built with Flask and deployed on Railway. It uses **Twilio Sandbox** for WhatsApp messaging (not the Meta Cloud API directly — the Meta webhook code exists but is commented out in `routes.py`).

### Request Flow

```
Twilio POST /webhook
  → webhook/routes.py::receive_twilio()
    → bot/handlers.py::handle_message()
      → bot/session.py  (in-memory session store)
      → whatsapp/whatsapp_service.py  (sends replies via Twilio)
```

### State Machine

Conversations are driven by `bot/states.py::State` enum. Each incoming message is dispatched to a handler based on the session's current state:

| State | Handler |
|---|---|
| `initial` | `_handle_initial` — sends main menu |
| `main_menu` | `_handle_main_menu` — routes to category |
| `chosing_product` | `_handle_choosing_product` — adds item to cart |
| `waiting_action` | `_handle_waiting_action` — continue/view cart/checkout/attendant |
| `attendant` | `_handle_attendant` — notifies human handoff |

### Session Storage

`bot/session.py` keeps sessions in a module-level `_sessions` dict (in-memory, lost on restart). The session schema per user:

```python
{
    "state": str,           # State enum value
    "cart": list[dict],     # [{"name", "price", "quantity"}]
    "current_category": str,
    "customer_name": str,
    "last_text": str,
}
```

### Product Catalog

`bot/Context.py` holds the hardcoded product catalog (`categories` dict) and the `STORE_CONTEXT` string used by the AI service. `bot/catalog.py` (deleted, still referenced by `handlers.py`) previously exported `Categories` — this import is currently broken.

### AI Service

`bot/ai_service.py` is a stub. The plan is to support Ollama locally and Claude Haiku in production, controlled by env vars `AI_BASE_URL`, `AI_MODEL`, `AI_API_KEY`.

## Environment Variables

Defined in `src/.env` and loaded via `config.py`:

| Variable | Purpose |
|---|---|
| `WHATSAPP_TOKEN` | Meta Cloud API token (currently unused) |
| `WHATSAPP_PHONE_NUMBER_ID` | Meta Cloud API phone ID (currently unused) |
| `VERIFY_TOKEN` | Meta webhook verification token |
| `FLASK_SECRET_KEY` | Flask session secret |
| `DATABASE_URL` | SQLite or Postgres URL (DB layer not yet active) |
| `TWILIO_ACCOUNT_SID` | Twilio credentials |
| `TWILIO_AUTH_TOKEN` | Twilio credentials |
| `TWILIO_SANDBOX_NUMBER` | Twilio sandbox number (default: `whatsapp:+14155238886`) |

## Deployment

Hosted on **Railway** via Nixpacks. Entry point: `gunicorn "app:create_app()"` (defined in both `src/Procfile` and `src/railway.json`). The working directory for Railway must be `src/` since all imports are relative to that folder (e.g., `from config import Config`).
