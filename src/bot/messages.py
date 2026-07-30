"""Data access for `messages`, the single source of truth for a conversation.

Every message any of the three authors produces lands in this table: the lead's
incoming WhatsApp text, the AI's reply, and the operator's reply typed in the
dashboard inbox. Nothing keeps a second copy — the LLM payload is a WINDOW over
this table (get_recent_messages), and the operator inbox is a full read of it
(get_conversation / list_conversations).

That single-record rule is what makes a human takeover possible: a paused
conversation still writes the lead's messages here (bot/handlers.py stores and
returns without calling the AI), so the operator can read what was said while
the bot was silent.

PII: this module never logs message content — only the sender, the author and
row counts. The repository is public and these rows hold whole conversations.

ORDERING TRAP: every query sorts by (created_at, id), never created_at alone.
The column defaults to NOW(), which in Postgres is transaction_timestamp(), so
rows written inside one transaction share an instant and would come back in an
arbitrary order.
"""

import logging
from datetime import datetime

from database.db import get_connection
from integrations.store import DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)

# Who produced a message. Single source of truth (no DB CHECK), the same pattern
# as session.valid_stages and bookings.valid_booking_statuses.
valid_authors: set[str] = {
    "lead",      # The lead, over WhatsApp
    "ai",        # The Corujai attendant
    "operator",  # A human answering from the dashboard inbox
}

# Author -> the role bot/ai_service.py expects in the LLM payload. The operator
# is indistinguishable from the AI to the model on purpose: both are "the
# attendant" as far as the lead's thread is concerned, so the model reads a
# human takeover as its own earlier turns and picks the conversation back up.
_AUTHOR_ROLES: dict[str, str] = {
    "lead": "user",
    "ai": "assistant",
    "operator": "assistant",
}


def author_to_role(author: str) -> str:
    """Map a message author to the LLM chat role.

    Args:
        author (str): One of valid_authors.

    Returns:
        str: "user" for the lead, "assistant" for the AI and the operator.
        Unknown authors fall back to "assistant", which is the safe side: an
        unrecognized message is never replayed to the model as the lead talking.
    """
    return _AUTHOR_ROLES.get(author, "assistant")


def add_message(
    sender: str,
    author: str,
    content: str,
    is_read: bool = False,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> None:
    """Append one message to a conversation.

    Callers are trusted internal code, so an invalid author raises instead of
    degrading silently — a message filed under a bogus author would be invisible
    to both the payload builder and the unread count.

    Args:
        sender (str): The lead's number, e.g. "5521999999999". Must already have
            a sessions row (the FK requires it).
        author (str): One of valid_authors.
        content (str): The message text, stored verbatim. For the AI this is the
            OUTGOING text, with the action block already stripped.
        is_read (bool): Whether the operator has already seen this. Only
            meaningful for author="lead"; pass True for the AI and the operator,
            whose own messages are nothing to catch up on.
        tenant_id (str): Tenant identifier. Fixed to DEFAULT_TENANT_ID for the pilot.

    Raises:
        ValueError: If author is not one of valid_authors.
    """
    if author not in valid_authors:
        raise ValueError(f"Invalid message author: {author!r}")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO messages (tenant_id, sender, author, content, is_read)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (tenant_id, sender, author, content, is_read),
            )
            conn.commit()

    # Never log content (public repo): sender + author only.
    logger.info("Message stored for %s (author=%s).", sender, author)


def get_recent_messages(
    sender: str,
    limit: int,
    since: datetime | None = None,
) -> list[dict]:
    """Return the tail of a conversation, oldest first, for the LLM payload.

    The rows are fetched newest-first so the LIMIT keeps the MOST RECENT ones,
    then reversed into chronological order — which is what the model needs.
    Fetching oldest-first with a LIMIT would hand the model the beginning of the
    conversation and drop everything that just happened.

    Args:
        sender (str): The lead's number.
        limit (int): How many messages to keep, counted in messages (not turns).
        since (datetime | None): When set, only messages created at or after
            this instant are returned. bot/handlers.py passes the session's
            conversation_started_at, so a conversation restarted by the 1h
            inactivity timeout does not replay the previous one to the model.

    Returns:
        list[dict]: Message rows in chronological order (possibly empty).
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, sender, author, content, is_read, created_at
                FROM messages
                WHERE sender = %s AND (%s::timestamptz IS NULL OR created_at >= %s)
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (sender, since, since, limit),
            )
            rows = cur.fetchall()

    # Reverse the newest-first window back into reading order.
    return [dict(row) for row in reversed(rows)]


def get_conversation(sender: str) -> list[dict]:
    """Return a lead's ENTIRE conversation, oldest first, for the inbox screen.

    Deliberately unfiltered and uncapped: the operator taking over needs the
    whole thread, including whatever came before an inactivity timeout reset the
    AI's window.

    Args:
        sender (str): The lead's number.

    Returns:
        list[dict]: Every message for this sender, in chronological order.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, sender, author, content, is_read, created_at
                FROM messages
                WHERE sender = %s
                ORDER BY created_at, id
                """,
                (sender,),
            )
            rows = cur.fetchall()

    return [dict(row) for row in rows]


def mark_conversation_read(sender: str) -> int:
    """Clear the unread flag on a lead's messages in one conversation.

    Only touches author = 'lead' rows: the AI's and the operator's messages are
    already read by definition, and flipping them would be a no-op that still
    costs writes.

    Args:
        sender (str): The lead's number.

    Returns:
        int: How many messages were marked read (0 when there was no backlog).
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE messages
                SET is_read = TRUE
                WHERE sender = %s AND author = 'lead' AND is_read = FALSE
                """,
                (sender,),
            )
            marked: int = cur.rowcount
            conn.commit()

    if marked:
        logger.info("Marked %d message(s) as read for %s.", marked, sender)

    return marked


def count_unread(sender: str) -> int:
    """Count a lead's messages the operator has not read yet.

    Args:
        sender (str): The lead's number.

    Returns:
        int: Number of unread messages from the lead.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS unread_count
                FROM messages
                WHERE sender = %s AND author = 'lead' AND is_read = FALSE
                """,
                (sender,),
            )
            unread_count: int = cur.fetchone()["unread_count"]

    return unread_count


def list_conversations() -> list[dict]:
    """Return one row per conversation for the inbox list.

    Driven by `sessions`, not by `messages`, so a lead whose session exists but
    who has not said anything yet still shows up instead of vanishing from the
    operator's view.

    Ordering answers "what needs me?" first: anything paused (a human holds it)
    or carrying unread messages floats to the top, and the rest follows by
    recency. Paused and unread share one rank rather than nesting, because a
    paused thread with nothing new still needs to be handed back.

    Returns:
        list[dict]: Rows with sender, is_paused, stage, lead_name, unread_count,
        and the last message's author/content/created_at (all None when the
        conversation has no messages yet).
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    s.sender,
                    s.is_paused,
                    s.stage,
                    s.lead_name,
                    s.updated_at,
                    last.author     AS last_author,
                    last.content    AS last_content,
                    last.created_at AS last_created_at,
                    COALESCE(unread.unread_count, 0) AS unread_count
                FROM sessions s
                LEFT JOIN LATERAL (
                    SELECT m.author, m.content, m.created_at
                    FROM messages m
                    WHERE m.sender = s.sender
                    ORDER BY m.created_at DESC, m.id DESC
                    LIMIT 1
                ) last ON TRUE
                LEFT JOIN LATERAL (
                    SELECT COUNT(*) AS unread_count
                    FROM messages m
                    WHERE m.sender = s.sender
                      AND m.author = 'lead'
                      AND m.is_read = FALSE
                ) unread ON TRUE
                ORDER BY
                    (s.is_paused OR COALESCE(unread.unread_count, 0) > 0) DESC,
                    last.created_at DESC NULLS LAST,
                    s.updated_at DESC
                """
            )
            rows = cur.fetchall()

    conversations = [dict(row) for row in rows]
    # Counts only — never the previews, which are message content.
    logger.info("Inbox listed %d conversation(s).", len(conversations))
    return conversations
