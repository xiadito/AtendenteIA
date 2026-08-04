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

# Module 5 — operator inbox and takeover (LLM and WhatsApp both stubbed; fully deterministic)
python tests/test_inbox/test_inbox_suite.py

# Module 6 — booking confirmation by the owner (WhatsApp stubbed; fully deterministic)
python tests/test_confirmation/test_confirmation_suite.py
```

Each suite prints a PASS/FAIL report and exits non-zero on failure; SKIPs don't fail the run.
Each module also has a manual CLI (`test_scheduling.py`, `test_ai_action.py`,
`test_owner_notifications.py`, `test_inbox.py`, `test_confirmation.py`) and a testing roteiro
(`SCHEDULING_ENGINE_TESTING.md`, `AI_ACTION_TESTING.md`, `OWNER_NOTIFICATIONS_TESTING.md`,
`INBOX_TESTING.md`, `CONFIRMATION_TESTING.md`).

Each suite owns a sender prefix so their teardowns can never collide: `5521000...` (scheduling),
`5522000...` (AI action), `5523000...` (owner notifications), `5524000...` (inbox),
`5525000...` (confirmation).

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
-- Migration history: 001-007, all seven present
SELECT version FROM schema_migrations ORDER BY version;

-- Funnel state per lead (why the state lives in columns, not JSONB)
SELECT sender, stage, lead_name, child_name, qualification, is_paused, updated_at
FROM sessions ORDER BY updated_at DESC;

-- One conversation, in the order the inbox shows it (tie-break by id — see Session Storage)
SELECT author, is_read, created_at, content
FROM messages WHERE sender = 'whatsapp:+55...' ORDER BY created_at, id;

-- What the operator still has to read
SELECT sender, COUNT(*) AS unread FROM messages
WHERE author = 'lead' AND is_read = FALSE GROUP BY sender ORDER BY unread DESC;

-- Conversations someone took over and may not have handed back
SELECT sender, stage, updated_at FROM sessions WHERE is_paused = TRUE;

-- Bookings the AI closed, newest first
SELECT sender, lead_name, child_name, class_type, slot_start, status
FROM trial_bookings ORDER BY created_at DESC;

-- What the owner still has to decide — the same ordering the /dashboard/bookings screen uses
SELECT id, lead_name, child_name, class_type, slot_start, status
FROM trial_bookings ORDER BY (status = 'pending_confirmation') DESC, slot_start;

-- The two records of one decision side by side: what the owner replied (owner_response) vs.
-- what became of the class (status). They should never disagree — see Booking Confirmation.
SELECT b.id, b.lead_name, b.status, n.owner_response, n.sent_at
FROM trial_bookings b
LEFT JOIN owner_notifications n ON n.booking_id = b.id AND n.event_type = 'booking'
ORDER BY b.slot_start DESC;

-- Set the owner's WhatsApp number (plain digits, no "whatsapp:+") — no UI yet
UPDATE owners SET owner_phone = '5521999999999' WHERE tenant_id = 'default';

-- Notifications queued for the owner, newest first
SELECT event_type, lead_sender, booking_id, status, attempts, owner_response, created_at
FROM owner_notifications ORDER BY created_at DESC;

-- Un-pause a lead by hand. Normally the inbox's "Devolver para a IA" button does this, and it
-- also resets the stage and arms the resume note — this bare UPDATE does neither.
UPDATE sessions SET is_paused = FALSE WHERE sender = 'whatsapp:+55...';

-- Reset one lead to a fresh greeting without touching the others.
-- CAREFUL: this deletes their messages too (ON DELETE CASCADE).
DELETE FROM sessions WHERE sender = 'whatsapp:+55...';
```

**Don't `TRUNCATE owners`** when clearing test data — it holds the Google Calendar tokens, and
wiping it means redoing the whole OAuth flow. `sessions`, `messages` and `trial_bookings` are
safe to empty (emptying `sessions` empties `messages` with it).

`messages.content` is plain `TEXT`, so the conversation reads straight out of the grid — sort by
`created_at, id` and it is exactly what the operator sees in the inbox. It is also the readable
way to confirm the AI's stored text is **block-stripped**: no `<corujai_action>` should ever
appear in a row.

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
      → bot/session.py           (Postgres-backed conversation state)
      → bot/messages.py          (records the lead's message — ALSO when paused, then returns)
      → bot/ai_configs.py        (per-tenant customizable prompt layer)
      → bot/ai_context.py        (cached slots + build_system_prompt)
      → bot/ai_service.py::get_ai_response(payload, system_prompt)  (payload = last N of messages)
      → bot/scheduling.py::book_slot()  (executes a booking, if the AI asked)
      → bot/messages.py          (records the AI's outgoing, block-stripped text)
      → bot/owner_notifications.py::enqueue_notification()  (after save_session, only logs on failure)
      → whatsapp/whatsapp_service.py::send_message()  (sends reply via Twilio) -> Future migration to Whatsapp API
```

There is a second way into a conversation, and it does not come from Twilio — the **operator
inbox** (Module 5). A human answers from the dashboard:

```
Operator POST /dashboard/inbox/<sender>/reply
  → webhook/routes.py::inbox_reply()
    → whatsapp/whatsapp_service.py::send_message()   (FIRST — see Operator Inbox)
    → bot/messages.py::add_message(author="operator") (only if the send succeeded)

Operator POST /dashboard/inbox/<sender>/resume
  → webhook/routes.py::inbox_resume()
    → bot/session.py::save_session()  (is_paused=False, stage="interest", needs_resume_note=True)
```

And a third way, the **owner** deciding whether a trial class actually happens (Module 6). It
has two entrances and one exit — both channels converge on the same coordinator:

```
Owner replies "1"/"2" over WhatsApp        Owner clicks on the dashboard
  → webhook/routes.py::receive_twilio_owner()   → webhook/routes.py::bookings_confirm/_cancel()
    → owner_notifications.register_owner_response()  → _booking_decision_response()
       (returns the stamped row: event_type, booking_id)
                    ↓                                          ↓
            bot/confirmations.py::confirm_or_cancel_booking(booking_id, decision)
              1. guard: only from status 'pending_confirmation' (else "skipped")
              2. bookings.update_booking_status()      ← the authoritative fact
              3. notify the lead, in its own try/except that only logs
                 → whatsapp_service.send_message() + messages.add_message(author="ai")
                                                           ↓
                                    (dashboard only) owner_notifications
                                      .register_response_for_booking()
```

**Three roles, do not conflate them.** The **lead** writes over WhatsApp and is always routed to
`handle_text_message()`, even while paused. The **owner** replies `1`/`2` to notifications over
WhatsApp and is routed to `receive_twilio_owner()`. The **operator** works only from the
dashboard and never over WhatsApp at all.

A closed booking or a handoff does not notify the owner synchronously: `handle_text_message()`
only enqueues a row in `owner_notifications` (isolated in its own `try/except` that never blocks
the reply to the lead). A separate Railway cron service, `jobs/drain_notifications.py`, drains
pending rows on its own schedule and does the actual WhatsApp send, with retries — see
Deployment. When the owner replies `1`/`2` to a notification, `receive_twilio_owner()` records
the response via `owner_notifications.register_owner_response()` — which since Module 6 returns
the **stamped row** (`dict | None`, carrying `event_type` and `booking_id`) instead of a bare
`bool` — and then hands a `booking` event to `bot/confirmations.py`, which closes the booking
out. **Module 4's deferral is resolved**: the reply now reaches `trial_bookings.status`.

`register_owner_response()` itself still only writes to `owner_notifications`; what changed is
that its **caller** acts on the result. A `handoff` notification carries `booking_id = NULL` and
stays recorded-only — the `event_type` check in `receive_twilio_owner()` is what keeps
`update_booking_status(None, ...)` from ever being reachable.

### AI-Driven Conversation (Module 3)

Conversations are fully driven by an LLM — there is no state machine. Since Module 3 the AI
is a **goal-driven scheduling attendant**: it guides the lead to book a free trial class and,
on every reply, appends a `<corujai_action>{...}</corujai_action>` block that the handler
parses to update conversation state and execute actions. The flow in
`bot/handlers.py::handle_text_message()` — **order matters**:

1. Load the conversation-state columns from `bot/session.py`.
2. **Pause check FIRST**: if `is_paused` (a handoff happened, or the operator took over),
   **record the lead's message** in `messages` and return without answering — no token cost,
   and the pause is structurally exempt from the timeout. The recording is the whole point:
   an operator who cannot read what the lead said during the takeover has nothing to answer.
3. **Lazy 1h inactivity timeout** from `sessions.updated_at` (no scheduler): a non-`booked`
   conversation is recorded as `closed_no_booking` (log only) and reset to a fresh greeting.
   It stamps `conversation_started_at = NOW()` instead of deleting anything — see Session Storage.
4. Build the per-turn context: cached available slots (`ai_context.get_cached_slots()`) +
   the lead's active bookings (`bookings.list_active_bookings_by_sender()`, injected always) →
   `ai_context.build_system_prompt()`. If `needs_resume_note` is set, the resume note rides
   along and the marker is cleared in the same pass, so it is delivered exactly once.
5. Record the lead's message, then call `get_ai_response(payload, system_prompt)` where the
   payload is the last `MAX_PAYLOAD_MESSAGES` (20) rows of `messages` since
   `conversation_started_at`, mapped to roles by `_to_llm_payload()`.
6. Parse the action block defensively (`_extract_action`): tolerates markdown fences, uses the
   **last** of multiple blocks, degrades to no-action on malformed/absent/unclosed.
7. Apply state **leniently** (invalid `stage`/`qualification` keep the previous value) and the
   `book`/`handoff` action **strictly** (a missing or hallucinated `event_id` — one not among
   the injected slots — is refused in Python). The final `stage` follows the real `book_slot()`
   outcome, not the model's optimistic claim.
8. Persist the state, record the **outgoing** (block-stripped) text in `messages`, and send it.

**Invariant:** no parse or action failure may stop the reply from reaching the lead. The stored
text is the message **without** the action block (the state lives in columns, so the block would
only waste tokens — and the operator would see it in the inbox).

**Payload assembly (`_to_llm_payload`).** `lead` → `user`; `ai` **and** `operator` →
`assistant`, because to the lead they are one continuous attendant. Leading `assistant`
messages are dropped: the window is a fixed-size tail, so it can open in the middle of an
operator's burst, and the Anthropic-compatible endpoint rejects a payload starting on
`assistant`.

The grocery-store `ORDER_CONFIRMED:` path and the whole `orders` feature behind it are gone
(see Session Storage).

### AI Service

`bot/ai_service.py` uses the **OpenAI SDK** pointed at a configurable base URL
(`AI_BASE_URL`). Ollama has been abandoned — both dev and prod run against the
Anthropic-compatible endpoint (`https://api.anthropic.com/v1/`) with Claude Haiku 4.5
(`AI_MODEL=claude-haiku-4-5-20251001`). Switching providers, if ever needed again, is
still just an env var change — no code changes required.

`get_ai_response(payload, system_prompt)` takes the system prompt **per turn** (it is no
longer imported): Module 3 rebuilds it every message from the protected layer + tenant config
+ slots + the lead's bookings. Since Module 5 the payload is a window over `messages`, not a
JSONB blob carried on the session.

### Session Storage

`bot/session.py` persists sessions in **Postgres** via `database/db.py::get_connection()`
(psycopg2 with `RealDictCursor`, so rows come back as dicts). Three tables matter:

- `messages` — **the single source of truth for a conversation** (migration 007, Module 5),
  owned by `bot/messages.py`. One row per message, `author` ∈ `lead | ai | operator`
  (validated in Python, no DB `CHECK`). The operator inbox reads it whole; the AI reads a
  window of it. `is_read` only carries meaning for `author = 'lead'` — it answers "has the
  **operator** seen this?" — so AI and operator messages are born read.
- `sessions` — one row per `sender`, created complete by migration 001, holding **state only**.
  The Module 3 **conversation state** lives in discrete typed columns (`stage`, `lead_name`,
  `child_name`, `qualification`, `is_paused`), not JSONB, so the funnel is explorable with
  plain SQL. Module 5 adds two transient markers, `needs_resume_note` and
  `conversation_started_at`. Indexed by `stage` (`idx_sessions_stage`).
- `trial_bookings` — one row per trial-class booking (Module 2), with `child_name` for
  `[BABY]`/`[CRIANCAS]` classes (Module 3 preliminary step), also created complete by
  migration 004.

**`sessions.history` is gone.** Until Module 5 the conversation lived in a `jsonb` column
capped at 10 turns and readable by nothing but the AI. Keeping it alongside `messages` would
mean two records of one conversation, which would inevitably drift, so it was removed from
migration 001 rather than left in place.

**`conversation_started_at` is the AI's window boundary.** The 1h timeout used to blank
`history`; it now stamps this column instead. `messages.get_recent_messages(sender, n, since=)`
honours it, so the AI genuinely restarts while `get_conversation()` still returns the whole
thread to the operator — deleting the messages would blind the human to everything that came
before.

Other tables exist but are not touched by `session.py`: `owners` (migration 003, Google
Calendar credentials + `owner_phone`) and `ai_configs` (migration 005, the customizable prompt
layer). `products` (migration 002) is grocery-store legacy kept alive only because `sync_agent/`
reads it. `owner_notifications` (migration 006) is the owner-notification queue — see
Request Flow and `src/tests/test_owner_notifications/OWNER_NOTIFICATIONS_TESTING.md`.

**The `orders` feature was removed entirely.** It went orphan at Module 3 (the AI closes
bookings, not orders), and was then deleted end to end: the `orders` table, `save_order()`,
`get_all_orders()`, `update_order_status()`, `valid_order_statuses`, `database/seed.py`, and the
`/dashboard/index` + `/dashboard/update-order-status` routes with their template and stylesheet.
Nothing in the codebase references `orders`.

**Trap:** `get_session()`, `save_session()` and `get_all_sessions()` must read/write the *same*
column set (they share `_STATE_COLUMNS`/`_row_to_session`) — a column written by one but not
read by another makes state silently vanish next turn. `save_session()` also lists its columns
by hand in the `UPDATE`, so a new state column needs adding in **both** places.

**Trap:** `get_session()` **creates** a row on miss, which is right for an incoming WhatsApp
message and wrong for the dashboard, where the sender comes from the URL. `session_exists()`
is the read-only lookup the inbox routes use to `404` instead of minting a phantom conversation.

**Trap:** `clear_session()` now deletes the lead's **messages** too — `messages.sender` carries
`ON DELETE CASCADE`. That cascade is load-bearing: without it every `DELETE FROM sessions`
(the Module 3 test CLI's `reset`, both suites' teardown) would fail on a foreign-key violation.

**Trap:** never order `messages` by `created_at` alone. The column defaults to `NOW()`, which is
`transaction_timestamp()` — rows written in one transaction share an instant. Every query in
`bot/messages.py` sorts by `(created_at, id)`.

`valid_stages` and `valid_qualifications` (module-level `set`s in
`session.py`) are the single source of truth for their allowed values — validated in Python,
with **no DB `CHECK`**, so widening an enum is a code change with no migration (same pattern as
`bookings.valid_booking_statuses`).

### Database & Migrations

`database/db.py::init_db()` is a small hand-rolled migration runner, called once from
`create_app()`. It creates a `schema_migrations` table, then applies every `.sql` file in
`database/migrations/` in filename order, recording each version so it never re-runs.
There is no ORM — SQLAlchemy/Alembic are deliberately *not* dependencies.

The sequence is **contiguous, 001–007**, and each table is created complete by a single
migration — there are no `ALTER TABLE` follow-ups:

| Version | Creates |
|---|---|
| `001_create_sessions.sql` | `sessions` (state columns only — **no `history`** — incl. `needs_resume_note` and `conversation_started_at`) + `idx_sessions_stage` |
| `002_create_products.sql` | `products` + indexes on `external_id`, `is_active`, `category` |
| `003_create_owners.sql` | `owners` (Google Calendar credentials + `owner_phone`) + seeds the `default` tenant row |
| `004_create_trial_bookings.sql` | `trial_bookings` (incl. `child_name`) + indexes on `sender`, `slot_start`, `calendar_event_id` |
| `005_create_ai_configs.sql` | `ai_configs` + seeds the `default` tenant config |
| `006_create_owner_notifications.sql` | `owner_notifications` (the owner-notification queue) + two partial unique indexes (idempotency per `event_type`) + indexes on `status` and `owner_phone` |
| `007_create_messages.sql` | `messages` (the conversation, FK to `sessions` `ON DELETE CASCADE`) + `(sender, created_at)` and a partial index for the unread count |

**The "never edit an applied migration" rule was suspended while the project was pre-deploy —
and Module 5 was the last time.** With no database created anywhere, there was no applied
history to protect, so late schema decisions were *folded back into the base migration* rather
than appended as `ALTER`s: `child_name` into 004 (`c44494e`), the conversation-state columns
into 001 (`7ef92cd`), `owner_phone` into 003, and finally the `history` removal plus the two
takeover markers into 001 again. The payoff is that each migration reads as one coherent table
definition with its whole rationale in the header, instead of a design spread over several files.

> **The window is now closed.** Once the database is created from these seven migrations, the
> normal rule is back: add a new numbered `.sql` file, never edit an applied one.

Editing 001 for Module 5 means the local database (and, at the first real deploy, the
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

With the database created from 001–007 this reverts to the normal rule: add a new numbered
`.sql` file; never edit an applied one.

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

`build_system_prompt(config, slots, active_bookings, resume_note=False)` assembles protected +
customizable + available slots + the lead's active bookings. With `resume_note=True` it also
appends `RESUME_NOTE` **inside the protected region**, where the untrusted tenant config can
never reach it — see Operator Inbox. `get_cached_slots()` caches
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
| `/dashboard/menu` | GET | Post-login navigation hub (inbox, integrations, future features) |
| `/dashboard/inbox` | GET | Conversation list — paused and unread first |
| `/dashboard/inbox/conversations` | GET | Partial of the list, HTMX polling target |
| `/dashboard/inbox/<sender>` | GET | One conversation; marks the lead's messages read |
| `/dashboard/inbox/<sender>/messages` | GET | Partial of the messages, HTMX polling target |
| `/dashboard/inbox/<sender>/reply` | POST | Operator's reply — sends, then records |
| `/dashboard/inbox/<sender>/resume` | POST | Hands the conversation back to the AI |
| `/dashboard/bookings` | GET | Booking list — pending confirmation first, then by `slot_start` |
| `/dashboard/bookings/list` | GET | Partial of the list, target of the decision swap |
| `/dashboard/bookings/<booking_id>/confirm` | POST | Confirms a trial class |
| `/dashboard/bookings/<booking_id>/cancel` | POST | Cancels a trial class |

Every route above is behind `@_require_auth`. The four per-sender ones `404` on a number with
no session (`session.session_exists()`), so a hand-typed URL cannot mint a phantom conversation.
The two booking POSTs need no such guard: `bookings.get_booking()` returns `None` rather than
creating, so an unknown id is answered with a Portuguese notice inside the list.

`GET /` (in `webhook_bp`) simply redirects to `dashboard.menu` — there is no separate landing
page. Login redirects to the menu too, so the menu is the single entry point to the UI.

**The dashboard has two data screens**: the inbox (Module 5) and the bookings review (Module 6),
the first ones since the order list went away with the `orders` feature.

**Trap:** the bookings page endpoint is `dashboard.bookings_review`, not `dashboard.bookings`.
`routes.py` does `import bot.bookings as bookings` at the top, and a view function named
`bookings` would shadow the module for every route in the file.

### Operator Inbox (Module 5)

The inbox is where a human takes a conversation over and gives it back. Replies go out through
the gym's own Twilio number, so the lead sees one continuous thread and never learns whether a
human or the AI answered.

**`is_paused` is the whole control surface.** There is no second status column: paused means a
human holds the conversation, and the AI stays silent. Before Module 5 nothing ever cleared it —
a handoff paused a lead permanently. `inbox_resume` is the only exit.

**Handing back (`inbox_resume`)** does three things: clears `is_paused`, resets `stage` to
`interest`, and sets `needs_resume_note`. The stage is reused, not invented, on purpose — the
stage list exists in **two** places that must agree, the `session.valid_stages` set **and** the
milestone list inside `PROTECTED_LAYER`. Adding a value to only the set would hand the model a
stage it has never seen described.

**The resume note exists because the operator is invisible to the model.** Operator messages
reach it with role `assistant`, indistinguishable from its own turns, so without being told it
reads a takeover as its own history and typically re-introduces itself to a lead already deep in
the funnel. The note (`ai_context.RESUME_NOTE`) rides on exactly one turn: the handler reads
`needs_resume_note` and clears it in the same pass.

**Reply order is inverted on purpose — send FIRST, record only on success.** The AI route does
the opposite (record, then send). The operator is a person watching the screen who can resend
immediately, and the record has to reflect what the lead actually received: recording first
would leave a phantom message that the AI's next turn replays as something it said. A send
failure answers **200** with a warning inside the HTML, never a bare 500 — HTMX does not swap
content on 4xx/5xx, so a 500 would leave the operator staring at a mute screen. The reply box is
cleared by an `HX-Trigger: reply-sent` header the route only sends on success, so a failure
leaves the text right where the operator needs it.

**PII:** nothing in this path logs message content — only `sender`, `author` and counts. The
repository is public and these rows are whole conversations.

Testing roteiro: `src/tests/test_inbox/INBOX_TESTING.md`.

### Booking Confirmation (Module 6)

The last piece of the core: the owner decides whether a booked trial class actually happens, and
the lead is told either way. It closes the loop lead → AI → booking → notification → takeover →
**confirmation**.

**`bot/confirmations.py` is the single source of the closing rule.** The owner can decide in two
places — replying `1`/`2` to the WhatsApp notification, or clicking on `/dashboard/bookings` —
and both call `confirm_or_cancel_booking(booking_id, decision)`. Nothing about closing a booking
lives in the routes; they translate a click into a call and a result into Portuguese. That is
also why the transition guard lives in the coordinator and not in each channel: written twice, it
would eventually be right in only one of them.

**Guard: only `pending_confirmation` can be decided.** Anything else returns
`{"result": "skipped", "status": ...}` without writing or notifying. This one check is what makes
both duplicates harmless — the owner replying `1` twice (the second reply finds no open
notification and `register_owner_response()` returns `None`) and the owner double-clicking the
button (the second click finds a booking that is no longer pending). The UI hides the buttons on
a decided booking, but that is convenience, not the gate.

**Order: status first, lead second.** `update_booking_status()` is the authoritative fact; the
WhatsApp notice is a courtesy on top of it, isolated in a `try/except` that only logs.
`send_message()` re-raises, and a Twilio blip must neither roll the status back nor escape into
the owner's webhook — an exception there would make Twilio retry the owner's `1`. The return
value carries `lead_notified` so the dashboard can warn the owner to reach the lead another way.
The notice is one message: it never calls the AI, and it never unpauses a conversation — a lead
being handled by a human still deserves to hear their class was confirmed.

**Cancelling frees the seat with no Calendar call.** `get_available_slots()` sizes a slot with
`bookings.count_active_bookings()`, which counts `status != 'cancelled'`, so flipping the row is
the whole release. See Known Issues for the cosmetic debt this leaves behind.

**Two records, two questions.** `owner_notifications.owner_response` answers "what did the owner
reply?"; `trial_bookings.status` answers "what became of the class?". Deciding from the dashboard
writes both (`register_response_for_booking()`), so a booking resolved on screen doesn't sit in
the queue as unanswered forever.

Testing roteiro: `src/tests/test_confirmation/CONFIRMATION_TESTING.md`.

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
│   ├── integrations.css  ← Google Calendar connection status page
│   ├── inbox.css         ← inbox conversation list
│   ├── conversation.css  ← one conversation + reply box
│   └── bookings.css      ← bookings review list + decision buttons
└── js/
    └── theme.js          ← shared dark/light theme toggle (all pages)
```

Every stylesheet is paired with exactly one **page** template in `src/templates/`
(`login.html`, `menu.html`, `integrations_google.html`, `inbox.html`, `conversation.html`), all
of which are rendered by a route. Deleting a template means deleting its stylesheet too — that
is why removing the order list took `dashboard.html` and `dashboard.css` with it.

Templates whose name starts with `_` are **HTMX partials** (`_inbox_list.html`,
`_conversation_messages.html`) and get no stylesheet of their own: they are rendered both
inside their parent page (via `{% include %}` on first load) and alone by the polling route,
so they inherit the parent's CSS and must never carry `<html>`/`<head>`.

**HTMX is loaded from a CDN** (`unpkg.com/htmx.org@2.0.4`) in the two inbox pages. There is no
build pipeline and no bundler in this project, deliberately — a script tag is the whole setup.
Polling is `hx-trigger="every 5s"`, and each page swaps only its partial so the head, the theme
and the toggle button are never reloaded.

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
  `register_owner_response()` — which, since Module 6, also closes the booking out. See
  `src/tests/test_owner_notifications/OWNER_NOTIFICATIONS_TESTING.md`.
- **Module 5 (done)** — Operator inbox with takeover (`bot/messages.py`, the `dashboard_bp`
  inbox routes, `inbox.html` / `conversation.html`). `messages` becomes the single source of
  truth for a conversation and `sessions.history` is retired; the LLM payload is a window over
  it. A paused conversation still records what the lead says, the operator answers from the
  dashboard through the gym's own number, and **the handoff is finally reversible** —
  `inbox_resume` clears `is_paused`, resets the stage and arms the resume note. First dynamic
  screen in the project (HTMX + polling). See `src/tests/test_inbox/INBOX_TESTING.md`.
- **Module 6 (done)** — Booking confirmation by the owner (`bot/confirmations.py`, the
  `dashboard_bp` bookings routes, `bookings.html`). The owner's `1`/`2` finally reaches
  `trial_bookings.status`, and the same decision is available as a button on the dashboard —
  both channels go through one coordinator, which guards the transition and tells the lead the
  outcome. Cancelling frees the seat through `count_active_bookings()` alone. **With it the core
  is complete**: lead → AI → booking → notification → takeover → confirmation. See
  `src/tests/test_confirmation/CONFIRMATION_TESTING.md`.
- **Future** — beyond the core: reminders before the class, a funnel/metrics screen, multi-tenant
  onboarding (every tenant is still hardcoded to `default`), and a real user model so the
  dashboard stops being single-password.

## Known Issues / TODOs

- **Neither data screen paginates or searches.** `list_conversations()` returns every session,
  `get_conversation()` every message, and `list_bookings_for_review()` every booking ever made —
  all uncapped. Fine for a pilot with one gym; the first busy tenant will need a limit, and the
  bookings screen will need a date filter before it grows past a scroll.
- **Polling, not push.** The inbox refreshes on a 5s timer (Module 5, decision 5B), so a new
  message takes up to 5s to appear and every open tab costs two requests per cycle. Websockets
  or SSE would be the upgrade, at the cost of the "no build pipeline" simplicity.
- **The operator inbox is single-user.** Authentication is the one shared
  `DASHBOARD_PASSWORD`, so `author = 'operator'` cannot say *which* human replied, and nothing
  stops two operators from answering the same lead at once.
- **Owner notifications are at-least-once, not exactly-once.** `jobs/drain_notifications.py`
  retries a failed send every cron cycle (bounded by `MAX_ATTEMPTS = 5`) with no advisory lock —
  Railway's cron already skips overlapping runs, so a second lock would be redundant, not
  additive safety. A notification that hits the cap sits as `status = 'failed'` with no
  automatic escalation yet.
- **A cancelled booking leaves a stale line on the Calendar event.** Module 6, decision 1A:
  cancelling only flips `trial_bookings.status`, because that is all
  `count_active_bookings()` reads and therefore all it takes to free the seat. The event's
  description still lists the cancelled student under `--- Reservas Corujai ---`. **Cosmetic** —
  the seat is genuinely available and will be re-offered — but an owner reading the calendar
  directly sees a name that no longer counts. Patching the event on cancel would mean a Calendar
  round-trip that can fail after the status is already committed, which is the trade that was
  refused.
- **The lead notice on a decision is fire-and-forget.** `confirmations._notify_lead()` swallows a
  send failure into a log and `lead_notified: False`; there is no retry and no queue (unlike the
  owner notifications, which have a cron). The dashboard warns the owner to reach the lead
  another way; the WhatsApp channel has nowhere to show that warning at all.
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
  `reset` command, `src/tests/test_ai_action/test_ai_action.py`. Since Module 5 it also wipes
  the lead's `messages`, via `ON DELETE CASCADE`.)
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
