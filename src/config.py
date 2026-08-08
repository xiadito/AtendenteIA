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
    
    # Fallback mirrors the documented local setup (see CLAUDE.md "Local database
    # setup"): the app role is corujai_app, never the postgres superuser.
    DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://corujai_app:senha_faltando@localhost:5432/corujai")
    
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

    # Public self-service signup (Module S3c). DEFAULTS TO FALSE, and the default
    # is the whole safety story: whoever forgets to set this is closed, never
    # open. Turning it on creates a SECOND TENANT the moment somebody signs up.
    #
    # Module S3b made that safe — the reads filter by tenant now — so this is no
    # longer blocked on anything technical. It stays false by default because
    # WHEN to open signup is a business call: every new gym still needs a Twilio
    # Sender the founder arranges by hand, and the onboarding screen tells them
    # so. Flip it deliberately, not by inheriting someone else's .env.
    SIGNUP_ENABLED = os.environ.get("SIGNUP_ENABLED", "false").strip().lower() == "true"

    #google calendar
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
    GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI")