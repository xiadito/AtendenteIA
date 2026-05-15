import logging
import uuid

logger = logging.getLogger(__name__)

#In-memory session store
#Key: client number, Value: session data (dict)
# this will be replaced by a database
_sessions: dict[str, dict] = {}

#EXAMPLE SESSION
#_sessions = {
#     "5521999887766": {
#         "history": [{"role": "user/assistant", "content": "message"}],
#         "order": [
#             {
#                 "id":         "123e4567-e89b-12d3-a456-426614174000", #UUID4 string
#                 "sender":     "5521999887766",
#                 "status":     "pending",
#                 "created_at": "2026-05-07T10:00:00",
#                 "items":      [{"name": "Arroz", "price": 5.99, "quantity": 2}],
#                 "total":      11.98,
#             }
#         ],
#     }
# }

valid_order_statuses: set[str] = {
    "pending",    # Order confirmed by customer, awaiting store acknowledgment
    "confirmed",  # Store owner acknowledged the order
    "preparing",  # Order is being prepared
    "ready",      # Ready for pickup or out for delivery
    "delivered",  # Successfully delivered or picked up
    "cancelled",  # Cancelled by customer or store
}
   

def get_session(sender: str) -> dict:
    """_summary_
        This function is responsible for getting the session of a client.
        If the session doesn't exist, it creates a default session for the client.
    Args:
        sender (str): Customer number in the format "5521999999999"
    Returns:
        dict: Session data for the client, including state, cart, current category, etc.
    """

    if sender not in _sessions:  
        logger.info(f"New session created for sender: {sender}")
        _sessions[sender]: dict = {"history": []}  # fresh list per user
    
    return _sessions[sender]

def save_session(sender: str, session: dict) -> None:
    """_summary_
        This function is responsible for saving the session of a client. 
        It updates the session data in the in-memory store.
    Args:
        sender (str): Customer number in the format "5521999999999"
        session_data (dict): Updated session data to be saved
    """
    logger.info(f"Session updated for sender: {sender} | New state: {session.get('state')}")
    _sessions[sender] = session
    
def clear_session(sender: str) -> None:
    """_summary_
        This function is responsible for clearing the session of a client. 
        It removes the session data from the in-memory store.
    Args:
        sender (str): Customer number in the format "5521999999999"
    """
    if sender in _sessions:
        del _sessions[sender]
        logger.info(f"Session cleared for sender: {sender}")
        logger.info(f"Current sessions after clearing: {get_all_sessions()}")
        
def get_all_sessions() -> dict:
    """_summary_
        This function is responsible for getting all the sessions. 
        It returns the entire in-memory session store.
    Returns:
        dict: All sessions stored in memory, with client numbers as keys and session data as values.
    """
    return _sessions

def save_order(sender: str, order: dict) -> str:
    """ 
    Save a new order or update an existing one. Must be confirmed by the user for the AI.
    
    Args:
        sender (str): number of the client
        order (dict): dict that contains the 'items' and 'total'
    """

    from datetime import datetime

    _session: dict = _sessions.get(sender, get_session(sender))

    if "order" not in _session:
        _session["order"] = [] #inside this have a dict with data.
    
    order_id = str(uuid.uuid4())
    order["id"] = order_id
    order["sender"] = sender
    order["status"] = "pending"
    order["created_at"] = datetime.now().isoformat()

    _session["order"].append(order) #save the order
    logger.info("Order %s saved for sender %s | Total: R$ %.2f",
                order_id, sender, order.get("total", 0))
    
    return order_id

def get_all_orders() -> list[dict]:
    """Return all orders from all active sessions, sorted newest first.

    Returns:
        list[dict]: All orders, each containing sender, items, total, status and created_at.
    """

    all_orders: list[dict] = []

    for session_data in _sessions.values():
        #Some sessions may not have any orders yet - skip them safely
        orders: list[dict] = session_data.get("order", [])
        all_orders.extend(orders)

    all_orders.sort(key=lambda o: o.get("created_at", ""), reverse=True)

    return all_orders

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
    
    if status not in valid_order_statuses:
        raise ValueError(f"Invalid order status: {status}. Must be one of {valid_order_statuses}")
    
    for session_data in _sessions.values():
        orders: list[dict] = session_data.get("order", [])
        for order in orders:
            if order.get("id") == order_id:
                order["status"] = status
                logger.info("Order %s status updated to '%s'", order_id, status)
                return True
    
    logger.warning("Order %s not found for status update.", order_id)
    return False


