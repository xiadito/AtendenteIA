from twilio.rest import Client
from config import Config
import logging

logger  = logging.getLogger(__name__)

def get_client() -> Client:
    """
    Creates the client for twilio using the account SID and auth token from the config.
    Makes it to simulate the client in future tests.
    Returns:
        Client: client from twilio
    """
    return Client(Config.TWILIO_ACCOUNT_SID, Config.TWILIO_AUTH_TOKEN)

def send_message(to: str, text: str) -> str:
    """

    Args:
        to (str): The phone number to which the message will be sent.
        text (str): The text of the message to be sent.

    Returns:
        str: The SID (UNIQUE ID) of the generated message from twilio.
    """
    try:
        client = get_client()
        
        message = client.messages.create(
            body = text,
            from_ = Config.TWILIO_SANDBOX_NUMBER,
            to = f"whatsapp:+{to}"
        )
        logger.info(f"Message sent to {to}: {text} | SID: {message.sid}")
        return message.sid
    except Exception as e:
        #All the erros of twilio will be catched here, and we can log them for future debugging.
        #We will need to solve this in webhooks. 
        logger.error(f"Error sending message para {to}: {e}")
        
        #literally re-raise the exception to be handled in the webhooks.
        raise

