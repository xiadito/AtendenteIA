from twilio.rest import Client
from config import Config

def send_message(to: str, text: str):
    client = Client(Config.TWILIO_ACCOUNT_SID, Config.TWILIO_AUTH_TOKEN)
    
    message = client.messages.create(
        body = text,
        from_ = Config.TWILIO_SANDBOX_NUMBER,
        to = f"whatsapp:+{to}"
    )
    return message.sid  