import logging
import uuid
import json

from database.db import get_connection

logger = logging.getLogger(__name__)

valid_order_statuses: set[str] = {
    "pending",    # Order confirmed by customer, awaiting store acknowledgment
    "confirmed",  # Store owner acknowledged the order
    "preparing",  # Order is being prepared
    "ready",      # Ready for pickup or out for delivery
    "delivered",  # Successfully delivered or picked up
    "cancelled",  # Cancelled by customer or store
}

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

# Column names carried on the in-memory session dict, beyond "history". Kept in
# one place so get_session/save_session/get_all_sessions never drift apart: a
# column written by save_session but not read by get_session (or vice versa)
# would make state silently vanish next turn.
_STATE_COLUMNS: tuple[str, ...] = ("stage", "lead_name", "child_name", "qualification", "is_paused")


def _row_to_session(row: dict) -> dict:
    """Shape a sessions DB row into the session dict the app passes around.

    Args:
        row (dict): A RealDictCursor row with history + the state columns + updated_at.

    Returns:
        dict: {"history", "stage", "lead_name", "child_name", "qualification",
        "is_paused", "updated_at"}.
    """
    session: dict = {"history": row["history"], "updated_at": row["updated_at"]}
    for column in _STATE_COLUMNS:
        session[column] = row[column]
    return session


def get_session(sender: str) -> dict:
    """Get a client's session, creating a default one if it doesn't exist.

    Args:
        sender (str): Customer number in the format "5521999999999".

    Returns:
        dict: Session data — history plus the conversation-state columns
        (stage, lead_name, child_name, qualification, is_paused) and updated_at.
    """
    select_columns = "history, " + ", ".join(_STATE_COLUMNS) + ", updated_at"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {select_columns} FROM sessions WHERE sender = %s", (sender,))
            row = cur.fetchone()

            if row is None:
                cur.execute(
                    f"""
                    INSERT INTO sessions (sender, history) VALUES (%s, %s::jsonb)
                    RETURNING {select_columns}
                    """,
                    (sender, json.dumps([])),
                )
                row = cur.fetchone()
                conn.commit()
                logger.info("New session created for sender: %s in database.", sender)

            return _row_to_session(row)


def save_session(sender: str, session: dict) -> None:
    """Persist a client's session — history and all conversation-state columns.

    Every state column is written here. It must stay in sync with the columns
    get_session reads back, or state written one turn disappears the next.

    Args:
        sender (str): Customer number in the format "5521999999999".
        session (dict): Session data to save. Missing keys fall back to their
            column defaults so a partial dict never crashes the update.
    """
    history = session.get("history", [])

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sessions
                SET history = %s::jsonb,
                    stage = %s,
                    lead_name = %s,
                    child_name = %s,
                    qualification = %s,
                    is_paused = %s,
                    updated_at = NOW()
                WHERE sender = %s
                """,
                (
                    json.dumps(history),
                    session.get("stage", "greeting"),
                    session.get("lead_name"),
                    session.get("child_name"),
                    session.get("qualification", "unknown"),
                    session.get("is_paused", False),
                    sender,
                ),
            )
            conn.commit()
            # Never log history/PII (public repo): sender + stage only.
            logger.info("Session saved for sender: %s (stage=%s).", sender, session.get("stage", "greeting"))


def clear_session(sender: str) -> None:
    """Delete a client's session and their orders.

    Args:
        sender (str): Customer number in the format "5521999999999".
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE sender = %s", (sender,))
            cur.execute("DELETE FROM orders WHERE sender = %s", (sender,))

        conn.commit()

        logger.info("Session cleared for sender: %s in database.", sender)


def get_all_sessions() -> dict:
    """Return every session keyed by sender.

    Returns:
        dict: {sender: session_dict}. Does not log its contents (avoids dumping
        lead history/PII into logs on a public repo).
    """
    select_columns = "sender, history, " + ", ".join(_STATE_COLUMNS) + ", updated_at"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {select_columns} FROM sessions")
            rows = cur.fetchall()

    sessions = {row["sender"]: _row_to_session(row) for row in rows}
    logger.info("Retrieved %d session(s) from database.", len(sessions))
    return sessions

def save_order(sender: str, order: dict) -> str:
    """ 
    Save a new order or update an existing one. Must be confirmed by the user for the AI.
    
    Args:
        sender (str): number of the client
        order (dict): dict that contains the 'items' and 'total'
    """
    get_session(sender) #ensure session exists, creates if not
    order_id = str(uuid.uuid4())

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                    INSERT INTO orders (id, sender, items, total, client_address, current_status, created_at)
                    VALUES (%s, %s, %s::jsonb, %s, %s, %s, NOW())
            """, (
                order_id,
                sender,
                json.dumps(order.get("items", [])),
                order.get("total", 0.0),
                order.get("address", "Endereço não fornecido/identificado"),
                order.get("status", "pending"),
            ))
            conn.commit()
            
    logger.info("Order %s saved for sender: %s in database. Items: %s, Total: %.2f", order_id, sender, json.dumps(order.get("items", [])), order.get("total", 0.0))
    return order_id

def get_all_orders() -> list[dict]:
    """Return all orders from all active sessions, sorted newest first.

    Returns:
        list[dict]: All orders, each containing sender, items, total, status and created_at.
    """

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, 
                    sender, 
                    items, 
                    total, 
                    client_address, 
                    current_status, 
                    created_at 
                FROM orders 
                ORDER BY created_at DESC
            """)
            rows = cur.fetchall()
            
    return [
        {
            "id": row["id"],
            "sender": row["sender"],
            "items": row["items"],
            "total": row["total"],
            "address": row["client_address"],
            "status": row["current_status"], # normalize column name
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]
    
def update_order_status(order_id: str, status: str) -> bool:
    """Update the status of an order identified by its order ID.

    Searches across all sessions for the order with the matching id.
    When migrating to PostgreSQL, this becomes a single SQL UPDATE —
    the public signature of the function stays exactly the same.

    Args:
        sender (str): The client number associated with the order.
        order_id (str): The unique ID of the order to update.
        status (str): The new status to set for the order.

    Returns:
        bool: True if the order was found and updated, False otherwise.
    """
    
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE orders
                SET current_status = %s
                WHERE id = %s
            """, (status, order_id))
            
            updated: bool = cur.rowcount > 0
        
        conn.commit()
        
        if updated:
            logger.info("Order %s status updated to '%s' in database.", order_id, status)
        else:
            logger.warning("Order %s not found for status update in database.", order_id)
        
        return updated
    


