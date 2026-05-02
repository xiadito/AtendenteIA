import logging

logger = logging.getLogger(__name__)

#In-memory session store
#Key: client number, Value: session data (dict)
# this will be replaced by a database
_sessions: dict[str, dict] = {}
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

def get_all_orders() -> list[dict]:
    """Return all orders from all active sessions, sorted by creation time (newest first).
    
    Each order dict shape: {"sender": str, "items": list, "total": float,
                            "status": str, "created_at": str}
    """

    all_orders: list[dict] = []


