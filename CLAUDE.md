# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

The app runs from the `src/` directory, but `requirements.txt` lives at the **repo root**:

```bash
# Activate virtualenv (Windows)
venv\Scripts\activate

# deactivate virtualenv (Windows)
deactivate

# Install dependencies (from the repo root)
pip install -r requirements.txt

# Run locally (dev)
cd src && python app.py          # Flask dev server on port 5000

# Run as production (gunicorn)
cd src && gunicorn "app:create_app()"
```

There are no automated tests currently.

### Dependencies

`requirements.txt` is a **curated** pinned list: the 10 packages the code actually imports
(Flask, gunicorn, python-dotenv, openai, twilio, psycopg2-binary, requests,
google-api-python-client, google-auth-oauthlib, google-auth) plus their transitive closure.
Do **not** regenerate it with a bare `pip freeze > requirements.txt` — that pulls back in
every experiment left in the venv. When adding a dependency, append the pin plus whatever
it drags in.

`sync_agent/` is a separate program with its own `sync_agent/requirements.txt`; the two lists
are independent.

## Architecture Overview

This is a **WhatsApp chatbot focused on closing leads** for Jiu-Jitsu academys built with Flask and deployed on Railway. It uses **Twilio Sandbox** for WhatsApp messaging (not the Meta Cloud API directly — the Meta webhook code exists but is commented out in `routes.py`).

### Request Flow

```
Twilio POST /webhook
  → webhook/routes.py::receive_twilio()
    → bot/handlers.py::handle_text_message()
      → bot/session.py  (Postgres-backed session store)
      → bot/ai_service.py::get_ai_response()  (calls LLM)
      → whatsapp/whatsapp_service.py::send_message()  (sends reply via Twilio) -> Future migration to Whatsapp API
```

### AI-Driven Conversation

Conversations are fully driven by an LLM — there is no state machine. The flow in `bot/handlers.py::handle_text_message()` is:

1. Load session and conversation history from `bot/session.py`
2. Append the new user message to history
3. Call `bot/ai_service.py::get_ai_response()` with the trimmed history
4. Parse the AI response for an `ORDER_CONFIRMED:` JSON block using `_extract_order()`
5. If an order block is found, strip it from the response and save it via `session.save_order()`
6. Send the cleaned response to the user via Twilio

History is capped at the last 10 turns (`max_history_turns = 10`) to control token usage.

### AI Service

`bot/ai_service.py` uses the **OpenAI SDK** pointed at a configurable base URL, making it compatible with both:
- **Dev**: Ollama local server (e.g. `llama3.2`, `mistral`) via `AI_BASE_URL=http://localhost:11434/v1`
- **Prod**: Anthropic-compatible endpoint (e.g. Claude Haiku 4.5) via `AI_BASE_URL=https://api.anthropic.com/v1`

Switch between environments exclusively via env vars — no code changes required.

### Session Storage

`bot/session.py` persists sessions and orders in **Postgres** via `database/db.py::get_connection()`
(psycopg2 with `RealDictCursor`, so rows come back as dicts). Two tables are involved:

- `sessions` — one row per `sender`, with `history` stored as `jsonb`
- `orders` — one row per order, keyed by a UUID4 `id` generated in `save_order()`

`get_all_orders()` normalizes the DB column `current_status` to the key `status` and
`client_address` to `address` — templates and the dashboard route use the normalized names.

`valid_order_statuses` (module-level `set` in `session.py`) is the single source of truth for
allowed status values — both the dashboard route and `update_order_status()` validate against it.

### Database & Migrations

`database/db.py::init_db()` is a small hand-rolled migration runner, called once from
`create_app()`. It creates a `schema_migrations` table, then applies every `.sql` file in
`database/migrations/` in filename order, recording each version so it never re-runs.
There is no ORM — SQLAlchemy/Alembic are deliberately *not* dependencies. To change the
schema, add a new numbered `.sql` file; never edit an applied one.

### Product Catalog & System Prompt

`bot/ai_context.py` holds:
- `categories` — hardcoded product catalog (marked as temporary; will be loaded from DB)
- `write_categories()` — renders `categories` into a flat text list for the prompt
- `store_context` — business hours, product catalog, delivery and payment, as one block
- `system_prompt` — full system prompt injected at the start of every LLM call

The three compose in one direction: `categories` → `store_context` → `system_prompt`.
`store_context` is interpolated at the **end** of `system_prompt`, which is why the rules
above it can say "the catalog below". Only `system_prompt` is imported (by `ai_service.py`) —
adding a fact about the store means editing `store_context`, never the prompt body, so the
catalog is never duplicated inside the prompt.

### Dashboard

A password-protected web dashboard is available at `/dashboard/index`. Routes are defined in `webhook/routes.py` under `dashboard_bp`:

| Route | Method | Description |
|---|---|---|
| `/dashboard/login` | GET/POST | Password login form |
| `/dashboard/logout` | GET | Clears session, redirects to login |
| `/dashboard/menu` | GET | Post-login navigation hub (orders, integrations, future features) |
| `/dashboard/index` | GET | Order list (requires auth); accepts `?status=` query param to filter by status |
| `/dashboard/update-order-status` | POST | Advance an order's status; expects `order_id` and `status` form fields |

`GET /` (in `webhook_bp`) simply redirects to `dashboard.menu` — there is no separate landing
page. Login redirects to the menu too, so the menu is the single entry point to the UI.

### Google Calendar Integration

`integrations/` implements OAuth 2.0 onboarding for Google Calendar (Module 1). Routes are
registered under the `/integrations` prefix:

| Route | Method | Description |
|---|---|---|
| `/integrations/google` | GET | Connection status page |
| `/integrations/google/connect` | GET | Generates CSRF `state` + PKCE `code_verifier`, redirects to Google |
| `/integrations/google/callback` | GET | Validates state, exchanges the code, stores credentials |
| `/integrations/google/disconnect` | POST | Best-effort token revoke, then clears stored credentials |

**PKCE is mandatory.** `google-auth-oauthlib` enables `autogenerate_code_verifier` by default,
so `authorization_url()` generates a `code_verifier` and sends only its SHA-256 hash to Google.
Because connect and callback are two separate HTTP requests (possibly two gunicorn workers),
**both** `oauth_state` and `oauth_code_verifier` must be persisted in the Flask session and
handed back to `exchange_code_for_tokens()`. Building a fresh `Flow` in the callback without
the verifier fails with `invalid_grant: Missing code verifier`.

Credentials live in the `owners` table (`integrations/store.py`), keyed by `tenant_id`, fixed
to `"default"` for the pilot. `mark_needs_reconnect()` and `NeedsReconnectError` are wired but
never called/caught yet — that belongs to Module 2, along with `get_calendar_service()`.

### Static Assets

All front-end assets live in `src/static/`:

```
src/static/
├── css/
│   ├── theme.css         ← CSS variables, dark mode override, .theme-toggle button
│   ├── login.css         ← login card + form styles
│   ├── menu.css          ← post-login navigation hub
│   ├── dashboard.css     ← table, badges, summary bar, status filter
│   └── integrations.css  ← Google Calendar connection status page
└── js/
    └── theme.js          ← shared dark/light theme toggle (all pages)
```

Every stylesheet is paired with exactly one template in `src/templates/`
(`login.html`, `menu.html`, `dashboard.html`, `integrations_google.html`), all of which are
rendered by a route. Deleting a template means deleting its stylesheet too.

Theme preference is persisted in `localStorage` and falls back to the OS `prefers-color-scheme` setting.

## Environment Variables

Defined in `src/.env` and loaded via `config.py`:

| Variable | Purpose |
|---|---|
| `TWILIO_ACCOUNT_SID` | Twilio credentials |
| `TWILIO_AUTH_TOKEN` | Twilio credentials |
| `TWILIO_SANDBOX_NUMBER` | Twilio sandbox number (default: `whatsapp:+14155238886`) |
| `VERIFY_TOKEN` | Meta webhook verification token (GET /webhook) |
| `FLASK_SECRET_KEY` | Flask session secret (required for dashboard auth) |
| `DASHBOARD_PASSWORD` | Plain-text password for the dashboard login |
| `AI_BASE_URL` | LLM endpoint — Ollama or Anthropic-compatible |
| `AI_MODEL` | Model name (e.g. `llama3.2`, `claude-haiku-4-5-20251001`) |
| `AI_API_KEY` | API key for the LLM provider |
| `DATABASE_URL` | Postgres URL — required; `init_db()` and every session/order query use it |
| `GOOGLE_CLIENT_ID` | Google Cloud OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Google Cloud OAuth client secret |
| `GOOGLE_REDIRECT_URI` | Must match the redirect URI registered in Google Cloud Console exactly |
| `FLASK_ENV` | Defaults to `development`; gates `seed_fake_orders()` |
| `DASHBOARD_USER` | Read by `config.py` but **never used** — login checks the password only |
| `WHATSAPP_TOKEN` | Meta Cloud API token (currently unused) |
| `WHATSAPP_PHONE_NUMBER_ID` | Meta Cloud API phone ID (currently unused) |

## Known Issues / TODOs

- `database/seed.py::seed_fake_orders()` is imported in `app.py` but its call is commented out. It is now safe to re-enable (it guards on `Config.FLASK_ENV == "development"` and skips when orders already exist), but it writes real rows to Postgres — keep it commented in production.
- Dead code still present from the pre-AI state machine: `bot/session.py::clear_session()` / `get_all_sessions()` (the latter is only reachable from the former), `bot/ai_service.py::update_order_status()` (a body-less stub that shadows the real one in `session.py`), and the commented-out Meta `receive()` route in `webhook/routes.py`.
- `VERIFY_TOKEN` and `GET /webhook` exist only for the Meta Cloud API, which is not in use.
- Module 2 scaffolding in `integrations/` is intentionally unreachable: `get_calendar_service()` is never called, and `NeedsReconnectError` is raised but never caught (so `mark_needs_reconnect()` never fires).

## Deployment

Hosted on **Railway** via Nixpacks. Entry point: `gunicorn "app:create_app()"` (defined in both `src/Procfile` and `src/railway.json`). The working directory for Railway must be `src/` since all imports are relative to that folder (e.g., `from config import Config`).