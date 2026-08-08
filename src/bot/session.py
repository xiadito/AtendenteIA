"""Data access for `sessions`: one row per lead PER TENANT, holding conversation STATE.

The conversation itself is NOT here — it lives in `messages` (bot/messages.py),
the single source of truth since Module 5. This module owns the typed state
columns the AI reports in its action block, plus the two transient markers the
operator takeover relies on (needs_resume_note, conversation_started_at).

THE KEY IS (tenant_id, sender) SINCE MODULE S3b, not `sender` alone. That is the
whole point of migration 011: the same person can be a lead at two gyms, and
before the composite primary key the second gym simply could not record them.
Every function here therefore takes a tenant and filters by the pair — a query
that matched on `sender` alone would answer with another gym's conversation.

The parameter is explicit and never read from flask.g or current_user (decision
14A): the webhook passes the tenant it resolved from Twilio's "To", the dashboard
passes current_user.tenant_id, and the cron passes the tenant of the row it is
working on. The DEFAULT_TENANT_ID default keeps the pilot's callers unchanged.
"""

import logging

from database.db import get_connection
from integrations.store import DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)

# Conversation stages the AI can report in its action block. Single source of
# truth (no DB CHECK): widening this is a code change with no migration, the
# same pattern as bookings.valid_booking_statuses.
valid_stages: set[str] = {
    "greeting",            # First contact, presenting the academy
    "interest",           # Understanding the interest and the class type
    "objection",          # Handling an objection (price, schedule, insecurity)
    "availability",       # Collecting the lead's availability
    "proposal",           # A slot was proposed, waiting for acceptance
    "booked",             # Trial class scheduled
    "handoff_requested",  # Lead asked for a human attendant
    "closed_no_booking",  # Conversation closed without a booking
}

# Lead qualification. Three values, not a boolean: at the start "don't know
# yet" is a real state a boolean can't express without lying.
valid_qualifications: set[str] = {
    "unknown",
    "qualified",
    "unqualified",
}

# Column names carried on the in-memory session dict. Kept in one place so
# get_session/save_session/get_all_sessions never drift apart: a column written
# by save_session but not read by get_session (or vice versa) would make state
# silently vanish next turn. save_session lists these by hand in its UPDATE, so
# adding one here means adding it there too.
_STATE_COLUMNS: tuple[str, ...] = (
    "stage",
    "lead_name",
    "child_name",
    "qualification",
    "is_paused",
    "needs_resume_note",
    "conversation_started_at",
)


def _row_to_session(row: dict) -> dict:
    """Shape a sessions DB row into the session dict the app passes around.

    Args:
        row (dict): A RealDictCursor row with the state columns + updated_at.

    Returns:
        dict: Every column in _STATE_COLUMNS, plus "updated_at". The
        conversation itself is not here — bot/messages.py owns it.
    """
    session: dict = {"updated_at": row["updated_at"]}
    for column in _STATE_COLUMNS:
        session[column] = row[column]
    return session


def get_session(sender: str, tenant_id: str = DEFAULT_TENANT_ID) -> dict:
    """Get a lead's session at one gym, creating a default one if it doesn't exist.

    Args:
        sender (str): Customer number in the format "5521999999999".
        tenant_id (str): The gym this conversation belongs to. Defaults to the
            pilot tenant.

    Returns:
        dict: Session data — the conversation-state columns (stage, lead_name,
        child_name, qualification, is_paused, needs_resume_note,
        conversation_started_at) plus updated_at.
    """
    select_columns = ", ".join(_STATE_COLUMNS) + ", updated_at"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {select_columns} FROM sessions WHERE tenant_id = %s AND sender = %s",
                (tenant_id, sender),
            )
            row = cur.fetchone()

            if row is None:
                # Only the primary key is supplied: every other column has a
                # column default, including the conversation_started_at that
                # bounds the AI's window from this moment on. Since S3b the key
                # is the PAIR, so both halves are written here.
                cur.execute(
                    f"""
                    INSERT INTO sessions (tenant_id, sender) VALUES (%s, %s)
                    RETURNING {select_columns}
                    """,
                    (tenant_id, sender),
                )
                row = cur.fetchone()
                conn.commit()
                logger.info(
                    "New session created for sender: %s (tenant %s) in database.", sender, tenant_id
                )

            return _row_to_session(row)


def session_exists(sender: str, tenant_id: str = DEFAULT_TENANT_ID) -> bool:
    """Check whether a lead has a session at one gym, WITHOUT creating one.

    get_session() creates on miss, which is right for an incoming WhatsApp
    message but wrong for the dashboard: the inbox routes take the sender from
    the URL, so calling get_session there would mint a phantom session for any
    number someone typed. This is the read-only lookup those routes use to 404
    instead.

    Since S3b it is also what stops one gym from reaching another's conversation
    by typing the number into the URL: a lead of gym B is simply not found under
    gym A's tenant, and the route 404s.

    Args:
        sender (str): Customer number in the format "5521999999999".
        tenant_id (str): The gym asking.

    Returns:
        bool: True if a sessions row exists for this (tenant, sender) pair.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM sessions WHERE tenant_id = %s AND sender = %s",
                (tenant_id, sender),
            )
            return cur.fetchone() is not None


def save_session(sender: str, session: dict, tenant_id: str = DEFAULT_TENANT_ID) -> None:
    """Persist a lead's session at one gym — every conversation-state column.

    Every state column is written here. It must stay in sync with the columns
    get_session reads back, or state written one turn disappears the next.

    conversation_started_at is only overwritten when the caller supplies it
    (the 1h inactivity timeout does); COALESCE keeps the stored boundary
    otherwise, so an ordinary turn never silently restarts the AI's window.

    Args:
        sender (str): Customer number in the format "5521999999999".
        session (dict): Session data to save. Missing keys fall back to their
            column defaults so a partial dict never crashes the update.
        tenant_id (str): The gym this conversation belongs to. Must match the
            tenant get_session() read, or the UPDATE matches nothing and the
            turn's state is silently lost.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sessions
                SET stage = %s,
                    lead_name = %s,
                    child_name = %s,
                    qualification = %s,
                    is_paused = %s,
                    needs_resume_note = %s,
                    conversation_started_at = COALESCE(%s, conversation_started_at),
                    updated_at = NOW()
                WHERE tenant_id = %s AND sender = %s
                """,
                (
                    session.get("stage", "greeting"),
                    session.get("lead_name"),
                    session.get("child_name"),
                    session.get("qualification", "unknown"),
                    session.get("is_paused", False),
                    session.get("needs_resume_note", False),
                    session.get("conversation_started_at"),
                    tenant_id,
                    sender,
                ),
            )
            conn.commit()
            # Never log message content/PII (public repo): sender + stage only.
            logger.info("Session saved for sender: %s (stage=%s).", sender, session.get("stage", "greeting"))


def clear_session(sender: str, tenant_id: str = DEFAULT_TENANT_ID) -> None:
    """Delete a lead's session at one gym, resetting the conversation.

    The lead's messages go with it: the composite foreign key on messages
    (tenant_id, sender) carries ON DELETE CASCADE, so this wipes the thread too.
    That is the intent — forgetting a lead should not leave an orphan
    conversation nobody can continue — but it means this is the one call that
    destroys inbox history, not just state.

    Since S3b the cascade is tenant-scoped as well: clearing a lead at gym A
    leaves the SAME person's conversation at gym B untouched.

    Args:
        sender (str): Customer number in the format "5521999999999".
        tenant_id (str): The gym whose copy of this lead is being forgotten.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM sessions WHERE tenant_id = %s AND sender = %s",
                (tenant_id, sender),
            )

        conn.commit()

        logger.info("Session cleared for sender: %s (tenant %s) in database.", sender, tenant_id)


def get_all_sessions(tenant_id: str = DEFAULT_TENANT_ID) -> dict:
    """Return one gym's sessions, keyed by sender.

    Args:
        tenant_id (str): The gym whose sessions to return.

    Returns:
        dict: {sender: session_dict}. Keyed by sender alone, which is
        unambiguous because the result is already one tenant's. Does not log its
        contents (avoids dumping lead state/PII into logs on a public repo).
    """
    select_columns = "sender, " + ", ".join(_STATE_COLUMNS) + ", updated_at"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {select_columns} FROM sessions WHERE tenant_id = %s",
                (tenant_id,),
            )
            rows = cur.fetchall()

    sessions = {row["sender"]: _row_to_session(row) for row in rows}
    logger.info("Retrieved %d session(s) from database.", len(sessions))
    return sessions
