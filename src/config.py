import os 
from dotenv import load_dotenv

load_dotenv()  # lê o arquivo .env e injeta no os.environ

class Config:
    # WhatsApp API
    WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
    PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
    
    #flask
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-fallback-key")
    FLASK_ENV = os.environ.get("FLASK_ENV", "development")
    
    DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:senha_faltando@localhost:5432/mercadinho_dev")
    
    #twilio 
    TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
    TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
    TWILIO_SANDBOX_NUMBER = os.environ.get("TWILIO_SANDBOX_NUMBER", "whatsapp:+14155238886")
    
    #AI Service
    AI_BASE_URL = os.environ.get("AI_BASE_URL")
    AI_MODEL = os.environ.get("AI_MODEL")
    AI_API_KEY = os.environ.get("AI_API_KEY")

    #dashboard
    DASHBOARD_USER = os.environ.get("DASHBOARD_USER")
    DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD")

    #google calendar
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
    GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI")