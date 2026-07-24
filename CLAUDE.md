# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

The app runs from the `src/` directory, but `requirements.txt` lives at the **repo root**:

```bash
# Activate virtualenv (Arch Linux / any Linux)
source venv/bin/activate

# deactivate virtualenv
deactivate

# Install dependencies (from the repo root)
pip install -r requirements.txt

# Run locally (dev)
cd src && python app.py          # Flask dev server on port 5000

# Run as production (gunicorn)
cd src && gunicorn "app:create_app()"
```

### Tests

There is no unittest/pytest wiring; each module ships a standalone runnable script under
`src/tests/`, located from `src/` **by name** (`next(p for p in ... if p.name == "src")`),
never by counting `.parent` hops. Run from `src/`:

```bash
# Module 2 — scheduling engine (writes to real Calendar + Postgres)
python tests/test_scheduling/test_scheduling_suite.py

# Module 3 — AI action layer (LLM stubbed for determinism; --skip-live avoids Calendar writes)
python tests/test_ai_action/test_ai_action_suite.py --skip-live
```

Each suite prints a PASS/FAIL report and exits non-zero on failure; SKIPs don't fail the run.
Each module also has a manual CLI (`test_scheduling.py`, `test_ai_action.py`) and a testing
roteiro (`SCHEDULING_ENGINE_TESTING.md`, `AI_ACTION_TESTING.md`).

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

This is **Corujai**, a **WhatsApp chatbot focused on closing leads** for gyms (Jiu-Jitsu, CrossFit, weightlifting) built with Flask and deployed on Railway. It uses **Twilio Sandbox** for WhatsApp messaging (not the Meta Cloud API directly — the Meta webhook code exists but is commented out in `routes.py`).

### Request Flow

```
Twilio POST /webhook
  → webhook/routes.py::receive_twilio()
    → bot/handlers.py::handle_text_message()
      → bot/session.py           (Postgres-backed session + conversation state)
      → bot/ai_configs.py        (per-tenant customizable prompt layer)
      → bot/ai_context.py        (cached slots + build_system_prompt)
      → bot/ai_service.py::get_ai_response(history, system_prompt)  (calls LLM)
      → bot/scheduling.py::book_slot()  (executes a booking, if the AI asked)
      → whatsapp/whatsapp_service.py::send_message()  (sends reply via Twilio) -> Future migration to Whatsapp API
```

### AI-Driven Conversation (Module 3)

Conversations are fully driven by an LLM — there is no state machine. Since Module 3 the AI
is a **goal-driven scheduling attendant**: it guides the lead to book a free trial class and,
on every reply, appends a `<corujai_action>{...}</corujai_action>` block that the handler
parses to update conversation state and execute actions. The flow in
`bot/handlers.py::handle_text_message()` — **order matters**:

1. Load the session (history **and** the conversation-state columns) from `bot/session.py`.
2. **Pause check FIRST**: if `is_paused` (a handoff happened), return without answering — no
   token cost, and the pause is structurally exempt from the timeout.
3. **Lazy 1h inactivity timeout** from `sessions.updated_at` (no scheduler): a non-`booked`
   conversation is recorded as `closed_no_booking` (log only) and reset to a fresh greeting.
4. Build the per-turn context: cached available slots (`ai_context.get_cached_slots()`) +
   the lead's active bookings (`bookings.list_active_bookings_by_sender()`, injected always) →
   `ai_context.build_system_prompt()`.
5. Append the user turn, call `get_ai_response(history, system_prompt)`.
6. Parse the action block defensively (`_extract_action`): tolerates markdown fences, uses the
   **last** of multiple blocks, degrades to no-action on malformed/absent/unclosed.
7. Apply state **leniently** (invalid `stage`/`qualification` keep the previous value) and the
   `book`/`handoff` action **strictly** (a missing or hallucinated `event_id` — one not among
   the injected slots — is refused in Python). The final `stage` follows the real `book_slot()`
   outcome, not the model's optimistic claim.
8. Persist the state; store the **outgoing** (block-stripped) text in history; send it.

**Invariant:** no parse or action failure may stop the reply from reaching the lead. History
is capped at the last 10 turns (`max_history_turns = 10`), and stores the message **without**
the action block (the state lives in columns, so the block would only waste tokens).

The grocery-store `ORDER_CONFIRMED:` path is gone; `orders`/`save_order()` still exist but go
orphan (see Known Issues).

### AI Service

`bot/ai_service.py` uses the **OpenAI SDK** pointed at a configurable base URL
(`AI_BASE_URL`). Ollama has been abandoned — both dev and prod run against the
Anthropic-compatible endpoint (`https://api.anthropic.com/v1/`) with Claude Haiku 4.5
(`AI_MODEL=claude-haiku-4-5-20251001`). Switching providers, if ever needed again, is
still just an env var change — no code changes required.

`get_ai_response(history, system_prompt)` takes the system prompt **per turn** (it is no
longer imported): Module 3 rebuilds it every message from the protected layer + tenant config
+ slots + the lead's bookings.

### Session Storage

`bot/session.py` persists sessions and orders in **Postgres** via `database/db.py::get_connection()`
(psycopg2 with `RealDictCursor`, so rows come back as dicts). Three tables are involved:

- `sessions` — one row per `sender`. `history` is `jsonb`; the Module 3 **conversation state**
  lives in discrete typed columns (`stage`, `lead_name`, `child_name`, `qualification`,
  `is_paused`), not JSONB, so the funnel is explorable with plain SQL.
- `orders` — one row per order, keyed by a UUID4 `id` generated in `save_order()`. Orphan since
  Module 3 (see Known Issues) but still read by the dashboard.
- `trial_bookings` — one row per trial-class booking (Module 2), with `child_name` for
  `[BABY]`/`[CRIANCAS]` classes (Module 3 preliminary step).

**Trap:** `get_session()`, `save_session()` and `get_all_sessions()` must read/write the *same*
column set (they share `_STATE_COLUMNS`/`_row_to_session`) — a column written by one but not
read by another makes state silently vanish next turn.

`get_all_orders()` normalizes the DB column `current_status` to the key `status` and
`client_address` to `address` — templates and the dashboard route use the normalized names.

`valid_order_statuses`, `valid_stages` and `valid_qualifications` (module-level `set`s in
`session.py`) are the single source of truth for their allowed values — validated in Python,
with **no DB `CHECK`**, so widening an enum is a code change with no migration (same pattern as
`bookings.valid_booking_statuses`).

### Database & Migrations

`database/db.py::init_db()` is a small hand-rolled migration runner, called once from
`create_app()`. It creates a `schema_migrations` table, then applies every `.sql` file in
`database/migrations/` in filename order, recording each version so it never re-runs.
There is no ORM — SQLAlchemy/Alembic are deliberately *not* dependencies. To change the
schema, add a new numbered `.sql` file; never edit an applied one.

### Two-Layer System Prompt (Module 3)

`bot/ai_context.py` builds the system prompt in two layers every turn:

- **Protected layer** (`PROTECTED_LAYER`, immutable, in code): mission, conversation
  milestones (the 8 stages), the `<corujai_action>` block contract, scheduling rules (never
  offer a time outside the injected list; child classes require `child_name`), the first-message
  1h timeout notice, and safeguards. It is a **plain string, not an f-string** — the action
  block is full of literal JSON braces.
- **Customizable layer** (`bot/ai_configs.py` → the `ai_configs` table, per `tenant_id`): gym
  name, attendant name, tone, business info, flow emphasis. **Untrusted input** — framed as
  data, injected only at fixed points, never allowed to rewrite the prompt. Edited by SQL (no UI).

`build_system_prompt(config, slots, active_bookings)` assembles protected + customizable +
available slots + the lead's active bookings. `get_cached_slots()` caches
`scheduling.get_available_slots()` for ~60s **per gunicorn worker** (a stale slot is safe — the
Module 2 advisory lock is the real arbiter, and a filled slot returns `"full"`), and turns the
integration exceptions into an empty list so a disconnected calendar never breaks the chat.
`ACTION_TAG` is defined here and imported by the handler's parser so the tag literal can't drift.

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
to `"default"` for the pilot. `get_calendar_service()`, `mark_needs_reconnect()` and
`NeedsReconnectError` are now live: `bot/scheduling.py` (Module 2) is their first caller, and
Module 3's conversation flow exercises the whole path.

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
| `AI_BASE_URL` | LLM endpoint — Anthropic-compatible (e.g. `https://api.anthropic.com/v1/`) |
| `AI_MODEL` | Model name (e.g. `claude-haiku-4-5-20251001`) |
| `AI_API_KEY` | API key for the LLM provider |
| `DATABASE_URL` | Postgres URL — required; `init_db()` and every session/order query use it |
| `GOOGLE_CLIENT_ID` | Google Cloud OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Google Cloud OAuth client secret |
| `GOOGLE_REDIRECT_URI` | Must match the redirect URI registered in Google Cloud Console exactly |
| `FLASK_ENV` | Defaults to `development`; gates `seed_fake_orders()` |
| `DASHBOARD_USER` | Read by `config.py` but **never used** — login checks the password only |
| `WHATSAPP_TOKEN` | Meta Cloud API token (currently unused) |
| `WHATSAPP_PHONE_NUMBER_ID` | Meta Cloud API phone ID (currently unused) |

## Roadmap

- **Module 1 (done)** — Google Calendar OAuth onboarding (`integrations/`): connect flow,
  token storage in `owners`, PKCE. See `src/tests/GOOGLE_CALENDAR_OAUTH_TESTING.md`.
- **Module 2 (done)** — Scheduling engine (`bot/scheduling.py`, `bot/bookings.py`):
  pure functions that read free slots from the owner's calendar and book a trial class
  into Postgres. See `src/tests/test_scheduling/SCHEDULING_ENGINE_TESTING.md`.
- **Module 3 (done)** — Wires the scheduling engine into the AI conversation. The AI returns a
  `<corujai_action>` block per turn; the handler parses it, updates discrete state columns on
  `sessions`, and calls `book_slot()`/handoff mid-conversation. Two-layer prompt in
  `bot/ai_context.py` + per-tenant `ai_configs`. See
  `src/tests/test_ai_action/AI_ACTION_TESTING.md`.
- **Modules 4 & 5 (future)** — owner notification / inbox / takeover and un-pause after a
  handoff. Module 3 only *pauses* on handoff (`is_paused`); nothing un-pauses it yet.

## Known Issues / TODOs

- `database/seed.py::seed_fake_orders()` is imported in `app.py` but its call is commented out. It is now safe to re-enable (it guards on `Config.FLASK_ENV == "development"` and skips when orders already exist), but it writes real rows to Postgres — keep it commented in production.
- **The `orders` code is orphan since Module 3** — `save_order()`, `update_order_status()`,
  `valid_order_statuses`, the `orders` table and the dashboard that reads it no longer receive
  new data (the AI closes bookings, not orders). It is left **working on purpose**: the
  dashboard must keep opening without error. Its fate is a later module's call — don't remove it.
- **The 1h timeout is lazy** (evaluated only when a message arrives): a lead who never writes
  again keeps stale state in `sessions` forever. Accepted for the build phase — there is no
  dashboard funnel to distort yet.
- Dead code still present: `bot/ai_service.py::update_order_status()` (a body-less stub that
  shadows the real one in `session.py`) and the commented-out Meta `receive()` route in
  `webhook/routes.py`. (`bot/session.py::clear_session()` is now used — the Module 3 test CLI's
  `reset` command and manual un-pause both call it.)
- `VERIFY_TOKEN` and `GET /webhook` exist only for the Meta Cloud API, which is not in use.
- `config.py` and `.env.example` still default `DATABASE_URL` to a `mercadinho_dev` database name — a naming leftover from the pre-pivot product, harmless but stale.
- `sync_agent/schedule/sync_agent.log` is committed to git — a runtime log file that shouldn't be tracked.
- `integrations/routes.py::google_callback` is guarded by `@_require_auth`. If the dashboard session expires between `/connect` and `/callback` (two separate HTTP requests), Google's `code` is lost on the redirect to login. Rare in practice, but real.

## Deployment

Hosted on **Railway** via Nixpacks. Entry point: `gunicorn "app:create_app()"` (defined in both `src/Procfile` and `src/railway.json`). The working directory for Railway must be `src/` since all imports are relative to that folder (e.g., `from config import Config`).