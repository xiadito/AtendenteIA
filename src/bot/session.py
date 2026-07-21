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
   

def get_session(sender: str) -> dict:
    """
        This function is responsible for getting the session of a client.
        If the session doesn't exist, it creates a default session for the client.
    Args:
        sender (str): Customer number in the format "5521999999999"
    Returns:
        dict: Session data for the client, including state, cart, current category, etc.
    """
    
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT history FROM sessions WHERE sender = %s", (sender,))
            row = cur.fetchone()
            
            if row is None: 
                cur.execute("INSERT INTO sessions (sender, history) VALUES (%s, %s)", (sender, json.dumps([])))
                conn.commit()
                logger.info(f"New session created for sender: {sender} in database.")
                return {"history": []}
            
            return {"history": row["history"]}            
    

def save_session(sender: str, session: dict) -> None:
    """_summary_
        This function is responsible for saving the session of a client. 
        It updates the session data in the in-memory store.
    Args:
        sender (str): Customer number in the format "5521999999999"
        session_data (dict): Updated session data to be saved
    """
    history = session.get("history", [])
    
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE sessions
                SET history = %s::jsonb,
                    updated_at = NOW()
                WHERE sender = %s""", (json.dumps(history), sender)
            )
            conn.commit()
            logger.info("Session updated for sender: %s in database. history: %s", sender, json.dumps(history))

def clear_session(sender: str) -> None:
    """_summary_
        This function is responsible for clearing the session of a client. 
        It removes the session data from the in-memory store.
    Args:
        sender (str): Customer number in the format "5521999999999"
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE sender = %s", (sender,))
            cur.execute("DELETE FROM orders WHERE sender = %s", (sender,))
        
        conn.commit()
            
        logger.info("Session cleared for sender: %s in database.", sender)
        logger.info("Current sessions after clearing: %s", get_all_sessions())

def get_all_sessions() -> dict:
    """_summary_
        This function is responsible for getting all the sessions. 
        It returns the entire in-memory session store.
    Returns:
        dict: All sessions stored in memory, with client numbers as keys and session data as values.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT sender, history FROM sessions")
            rows = cur.fetchall()
            
            sessions = {row["sender"]: {"history": row["history"]} for row in rows}
            logger.info("All sessions retrieved from database: %s", sessions)
    
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
    


