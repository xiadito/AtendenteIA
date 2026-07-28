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

### Local database setup

One-time, before the first run. The app never creates its own database or role — it only
applies migrations to a database that already exists:

```bash
# 1. Create the database and a dedicated non-superuser role
psql -U postgres -c "CREATE DATABASE corujai;"
psql -U postgres -c "CREATE ROLE corujai_app WITH LOGIN PASSWORD 'sua_senha';"
psql -U postgres -c "ALTER DATABASE corujai OWNER TO corujai_app;"

# 2. Point src/.env at it
#    DATABASE_URL="postgresql://corujai_app:sua_senha@localhost:5432/corujai"

# 3. First `python app.py` applies the migrations; verify:
psql "$DATABASE_URL" -c "SELECT version FROM schema_migrations ORDER BY version;"
```

**Run as `corujai_app`, not as `postgres`.** Railway hands the app a plain role, and a
superuser bypasses every permission check — connecting as `postgres` in dev hides
`permission denied` errors until deploy.

`ALTER DATABASE ... OWNER TO` is the whole grant story on PostgreSQL 15+ (Arch ships 18):
the `public` schema is owned by `pg_database_owner`, which resolves to whoever owns the
database, so the role gets `CREATE TABLE` with no explicit `GRANT`. Older tutorials tell you
to `GRANT ALL ON SCHEMA public` — that was needed pre-15 and is redundant here. No migration
uses an extension or any other superuser-only operation.

### Tests

There is no unittest/pytest wiring; each module ships a standalone runnable script under
`src/tests/`, located from `src/` **by name** (`next(p for p in ... if p.name == "src")`),
never by counting `.parent` hops. Run from `src/`:

```bash
# Module 2 — scheduling engine (writes to real Calendar + Postgres)
python tests/test_scheduling/test_scheduling_suite.py

# Module 3 — AI action layer (LLM stubbed for determinism; --skip-live avoids Calendar writes)
python tests/test_ai_action/test_ai_action_suite.py --skip-live

# Owner notifications — enqueue, cron drain, and owner-reply recording (fully deterministic)
python tests/test_owner_notifications/test_owner_notifications_suite.py
```

Each suite prints a PASS/FAIL report and exits non-zero on failure; SKIPs don't fail the run.
Each module also has a manual CLI (`test_scheduling.py`, `test_ai_action.py`,
`test_owner_notifications.py`) and a testing roteiro (`SCHEDULING_ENGINE_TESTING.md`,
`AI_ACTION_TESTING.md`, `OWNER_NOTIFICATIONS_TESTING.md`).

### Inspecting the database with DBeaver

The suites assert; DBeaver is for **seeing** what a conversation actually wrote. It pairs with
the manual CLIs — send a turn through `test_ai_action.py`, then read the row back — which is the
only way to check that the state columns hold what the `<corujai_action>` block claimed.

Connection: `Database → New Database Connection → PostgreSQL`, host `localhost`, port `5432`,
database `corujai`, user `corujai_app` + its password. DBeaver is Java and speaks **JDBC**, so
its URL is `jdbc:postgresql://localhost:5432/corujai` — no credentials embedded, unlike the
libpq `DATABASE_URL` in `src/.env`. On first connect it offers to download the driver; accept.

Three settings that save real time:

- **`Navigator View → Simple`** (right-click the connection) collapses
  `Databases → corujai → Schemas → public → Tables` down to `corujai → Tables`.
- **Auto-commit on.** In `Manual Commit` a `CREATE`/`UPDATE` sits in an open transaction, and
  the metadata tree — which uses a *separate* connection — cannot see it.
- **`F5` refreshes the selected node only.** After `python app.py` applies a migration, select
  the `Tables` folder and refresh; pressing `F5` on a single table won't reveal a new one.
  If the tree stays stale, right-click the connection → `Invalidate/Reconnect`.

Queries worth keeping in a saved SQL editor:

```sql
-- Migration history: 001-006, all six present
SELECT version FROM schema_migrations ORDER BY version;

-- Funnel state per lead (why the state lives in columns, not JSONB)
SELECT sender, stage, lead_name, child_name, qualification, is_paused, updated_at
FROM sessions ORDER BY updated_at DESC;

-- Bookings the AI closed, newest first
SELECT sender, lead_name, child_name, class_type, slot_start, status
FROM trial_bookings ORDER BY created_at DESC;

-- Set the owner's WhatsApp number (plain digits, no "whatsapp:+") — no UI yet
UPDATE owners SET owner_phone = '5521999999999' WHERE tenant_id = 'default';

-- Notifications queued for the owner, newest first
SELECT event_type, lead_sender, booking_id, status, attempts, owner_response, created_at
FROM owner_notifications ORDER BY created_at DESC;

-- Un-pause a lead parked by a handoff (nothing does this automatically until a future feature)
UPDATE sessions SET is_paused = FALSE WHERE sender = 'whatsapp:+55...';

-- Reset one lead to a fresh greeting without touching the others
DELETE FROM sessions WHERE sender = 'whatsapp:+55...';
```

**Don't `TRUNCATE owners`** when clearing test data — it holds the Google Calendar tokens, and
wiping it means redoing the whole OAuth flow. `sessions` and `trial_bookings` are safe to empty.

`history` is `jsonb`; double-clicking the cell opens DBeaver's JSON viewer, which is the
readable way to confirm the 10-turn cap and that the stored text is **block-stripped**.

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
    → integrations/store.py::get_owner_by_phone()  (owner reply? route to receive_twilio_owner() instead)
    → bot/handlers.py::handle_text_message()
      → bot/session.py           (Postgres-backed session + conversation state)
      → bot/ai_configs.py        (per-tenant customizable prompt layer)
      → bot/ai_context.py        (cached slots + build_system_prompt)
      → bot/ai_service.py::get_ai_response(history, system_prompt)  (calls LLM)
      → bot/scheduling.py::book_slot()  (executes a booking, if the AI asked)
      → bot/owner_notifications.py::enqueue_notification()  (after save_session, only logs on failure)
      → whatsapp/whatsapp_service.py::send_message()  (sends reply via Twilio) -> Future migration to Whatsapp API
```

A closed booking or a handoff does not notify the owner synchronously: `handle_text_message()`
only enqueues a row in `owner_notifications` (isolated in its own `try/except` that never blocks
the reply to the lead). A separate Railway cron service, `jobs/drain_notifications.py`, drains
pending rows on its own schedule and does the actual WhatsApp send, with retries — see
Deployment. When the owner replies `1`/`2` to a notification, `receive_twilio_owner()` records
the response via `owner_notifications.register_owner_response()` only; it does not update
`trial_bookings` — closing out the booking based on that reply is still a future feature.

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

The grocery-store `ORDER_CONFIRMED:` path and the whole `orders` feature behind it are gone
(see Session Storage).

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

`bot/session.py` persists sessions in **Postgres** via `database/db.py::get_connection()`
(psycopg2 with `RealDictCursor`, so rows come back as dicts). Two tables are involved:

- `sessions` — one row per `sender`, created complete by migration 001. `history` is `jsonb`;
  the Module 3 **conversation state** lives in discrete typed columns (`stage`, `lead_name`,
  `child_name`, `qualification`, `is_paused`), not JSONB, so the funnel is explorable with
  plain SQL. Indexed by `stage` (`idx_sessions_stage`).
- `trial_bookings` — one row per trial-class booking (Module 2), with `child_name` for
  `[BABY]`/`[CRIANCAS]` classes (Module 3 preliminary step), also created complete by
  migration 004.

Other tables exist but are not touched by `session.py`: `owners` (migration 003, Google
Calendar credentials + `owner_phone`) and `ai_configs` (migration 005, the customizable prompt
layer). `products` (migration 002) is grocery-store legacy kept alive only because `sync_agent/`
reads it. `owner_notifications` (migration 006) is the owner-notification queue — see
Request Flow and `src/tests/test_owner_notifications/OWNER_NOTIFICATIONS_TESTING.md`.

**The `orders` feature was removed entirely.** It went orphan at Module 3 (the AI closes
bookings, not orders), and was then deleted end to end: the `orders` table, `save_order()`,
`get_all_orders()`, `update_order_status()`, `valid_order_statuses`, `database/seed.py`, and the
`/dashboard/index` + `/dashboard/update-order-status` routes with their template and stylesheet.
`clear_session()` now deletes only the session row. Nothing in the codebase references `orders`.

**Trap:** `get_session()`, `save_session()` and `get_all_sessions()` must read/write the *same*
column set (they share `_STATE_COLUMNS`/`_row_to_session`) — a column written by one but not
read by another makes state silently vanish next turn.

`valid_stages` and `valid_qualifications` (module-level `set`s in
`session.py`) are the single source of truth for their allowed values — validated in Python,
with **no DB `CHECK`**, so widening an enum is a code change with no migration (same pattern as
`bookings.valid_booking_statuses`).

### Database & Migrations

`database/db.py::init_db()` is a small hand-rolled migration runner, called once from
`create_app()`. It creates a `schema_migrations` table, then applies every `.sql` file in
`database/migrations/` in filename order, recording each version so it never re-runs.
There is no ORM — SQLAlchemy/Alembic are deliberately *not* dependencies.

The sequence is **contiguous, 001–006**, and each table is created complete by a single
migration — there are no `ALTER TABLE` follow-ups:

| Version | Creates |
|---|---|
| `001_create_sessions.sql` | `sessions` (incl. all conversation-state columns) + `idx_sessions_stage` |
| `002_create_products.sql` | `products` + indexes on `external_id`, `is_active`, `category` |
| `003_create_owners.sql` | `owners` (Google Calendar credentials + `owner_phone`) + seeds the `default` tenant row |
| `004_create_trial_bookings.sql` | `trial_bookings` (incl. `child_name`) + indexes on `sender`, `slot_start`, `calendar_event_id` |
| `005_create_ai_configs.sql` | `ai_configs` + seeds the `default` tenant config |
| `006_create_owner_notifications.sql` | `owner_notifications` (the owner-notification queue) + two partial unique indexes (idempotency per `event_type`) + indexes on `status` and `owner_phone` |

**The "never edit an applied migration" rule is currently suspended, on purpose.** While the
project is pre-deploy the only database is the local `corujai`, recreated from empty at will,
so there is no applied history to protect. Late schema decisions were therefore *folded back
into the base migration* rather than appended as `ALTER`s — `child_name` into 004 (`c44494e`),
the conversation-state columns into 001 (`7ef92cd`), `owner_phone` into 003 — and the leftover
numbers were closed up (`8e4ea54`). The payoff is that each migration reads as one coherent
table definition with its whole rationale in the header, instead of a design spread over
several files.

Editing 003 to add `owner_phone` means the local database (and, at the first real deploy, the
Railway one too) must be dropped and recreated, not migrated — see the cost below. After
recreating, set the pilot's number by hand (no UI exists yet):

```sql
UPDATE owners SET owner_phone = '5521999999999' WHERE tenant_id = 'default';
```

The cost, in two parts:

- **Editing a migration in place is invisible to `init_db()`** — it only checks whether a
  version string is in `schema_migrations`, never whether the file changed. A database that
  applied the old 001 keeps the old `sessions` forever. After any such edit the local database
  must be dropped and recreated, not migrated.
- **Renaming one silently re-runs it**, because `version` is the filename stem
  (`sql_file.stem`). That is only safe because every migration here is idempotent
  (`CREATE TABLE/INDEX IF NOT EXISTS`, `INSERT ... ON CONFLICT DO NOTHING`) — treat that as a
  hard requirement, not a style. A rename also strands the old version string in
  `schema_migrations`; delete it by hand.

Once the first real deploy exists this reverts to the normal rule: add a new numbered `.sql`
file; never edit an applied one.

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

A password-protected web dashboard is available at `/dashboard/menu`. Routes are defined in `webhook/routes.py` under `dashboard_bp`:

| Route | Method | Description |
|---|---|---|
| `/dashboard/login` | GET/POST | Password login form |
| `/dashboard/logout` | GET | Clears session, redirects to login |
| `/dashboard/menu` | GET | Post-login navigation hub (integrations, future features) |

`GET /` (in `webhook_bp`) simply redirects to `dashboard.menu` — there is no separate landing
page. Login redirects to the menu too, so the menu is the single entry point to the UI.

**The dashboard currently has no data screen.** Its only one was the order list, removed with
the `orders` feature; the menu links to the Google Calendar integration page and nothing else.
A `trial_bookings` screen (so the owner can confirm trial classes) belongs to a later module.

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
│   └── integrations.css  ← Google Calendar connection status page
└── js/
    └── theme.js          ← shared dark/light theme toggle (all pages)
```

Every stylesheet is paired with exactly one template in `src/templates/`
(`login.html`, `menu.html`, `integrations_google.html`), all of which are
rendered by a route. Deleting a template means deleting its stylesheet too — that is why
removing the order list took `dashboard.html` and `dashboard.css` with it.

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
| `DATABASE_URL` | Postgres URL — required; `init_db()` and every session/booking query use it. Connect as `corujai_app`, not `postgres` (see Local database setup) |
| `GOOGLE_CLIENT_ID` | Google Cloud OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Google Cloud OAuth client secret |
| `GOOGLE_REDIRECT_URI` | Must match the redirect URI registered in Google Cloud Console exactly |
| `FLASK_ENV` | Defaults to `development`; not currently gating anything since `seed.py` was removed |
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
- **Owner notifications (done)** — A closed booking or a handoff enqueues a row in
  `owner_notifications` (`bot/owner_notifications.py`); a separate Railway cron service
  (`jobs/drain_notifications.py`) drains and delivers it via WhatsApp with retries. The owner's
  `1`/`2` reply is routed by `webhook/routes.py::receive_twilio_owner()` and recorded via
  `register_owner_response()`. See `src/tests/test_owner_notifications/OWNER_NOTIFICATIONS_TESTING.md`.
- **Future** — an inbox/takeover screen in the dashboard, and un-pausing a session after a
  handoff (today `is_paused` only ever gets set, never cleared) and actually closing out a
  booking (`trial_bookings.status`) based on the owner's recorded response.

## Known Issues / TODOs

- **The dashboard has no data screen** since the `orders` removal — only login, logout and a
  menu pointing at the Google Calendar page. The screen that replaces it (a `trial_bookings`
  list so the owner can confirm trial classes) is still future work.
- **Owner notifications are at-least-once, not exactly-once.** `jobs/drain_notifications.py`
  retries a failed send every cron cycle (bounded by `MAX_ATTEMPTS = 5`) with no advisory lock —
  Railway's cron already skips overlapping runs, so a second lock would be redundant, not
  additive safety. A notification that hits the cap sits as `status = 'failed'` with no
  automatic escalation yet.
- **The owner's `1`/`2` reply is recorded, not acted on.** `register_owner_response()` only sets
  `owner_notifications.owner_response`; nothing yet flips `trial_bookings.status` based on it,
  and un-pausing a session after a handoff (`sessions.is_paused`) is also still unimplemented —
  both are future work.
- `products` (migration 002) is grocery-store legacy with no reader inside `src/` — only
  `sync_agent/` uses it. It survives the rebrand for that reason alone.
- `sync_agent/schedule/README.md` still documents the pre-rebrand names: the `mercadinho_dev`
  database (line 35) and the `MercadinhoSyncAgent` Windows service (lines 86, 90). Out of scope
  for the rebrand commits — `sync_agent/` is a separate program with its own requirements.
- **The 1h timeout is lazy** (evaluated only when a message arrives): a lead who never writes
  again keeps stale state in `sessions` forever. Accepted for the build phase — there is no
  dashboard funnel to distort yet.
- Dead code still present: the commented-out Meta `receive()` route in `webhook/routes.py`.
  (`bot/session.py::clear_session()` is live — its one call site is the Module 3 test CLI's
  `reset` command, `src/tests/test_ai_action/test_ai_action.py`.)
- `VERIFY_TOKEN` and `GET /webhook` exist only for the Meta Cloud API, which is not in use.
- `sync_agent/schedule/sync_agent.log` is committed to git — a runtime log file that shouldn't be tracked.
- `integrations/routes.py::google_callback` is guarded by `@_require_auth`. If the dashboard session expires between `/connect` and `/callback` (two separate HTTP requests), Google's `code` is lost on the redirect to login. Rare in practice, but real.

## Deployment

Hosted on **Railway** via Nixpacks, as **two services from the same repository** (not two
repos) — both with Root Directory `src/`, since all imports are relative to that folder (e.g.,
`from config import Config`):

1. **Web** — entry point `gunicorn "app:create_app()"` (defined in both `src/Procfile` and
   `src/railway.json`). Public domain, handles the Twilio webhook and the dashboard. Unchanged
   by the owner-notifications work.
2. **Cron** — start command `python -m jobs.drain_notifications`, Cron Schedule `* * * * *`, no
   public domain. Railway starts the service on schedule, runs `jobs/drain_notifications.py::main()`
   to completion, and shuts it down — overlapping runs are skipped natively, so the script
   itself uses no advisory lock. It shares the same `DATABASE_URL` and Twilio env vars as the
   web service. The script **must exit** (`sys.exit(...)`) without leaving a server, a thread,
   or an open connection running, or a slow run could get killed mid-way by the next schedule
   tick instead of being skipped cleanly.
