import os 
from dotenv import load_dotenv

load_dotenv()  # lê o arquivo .env e injeta no os.environ

class Config:
    WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
    PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-fallback-key")
    DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///bot.db")