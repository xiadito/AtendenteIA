import os
from dotenv import load_dotenv

load_dotenv()  # Load environment variables from .env file

class Config:
    FIREBIRD_HOST = os.getenv("FIREBIRD_HOST", "localhost")
    FIREBIRD_PORT = int(os.getenv("FIREBIRD_PORT", "3050"))
    FIREBIRD_DATABASE = os.getenv("FIREBIRD_DATABASE", "pos.fdb")
    FIREBIRD_USER = os.getenv("FIREBIRD_USER", "sysdba")
    FIREBIRD_PASSWORD = os.getenv("FIREBIRD_PASSWORD", "masterkey")
    FIREBIRD_CHARSET = os.getenv("FIREBIRD_CHARSET", "WIN1252")
    
    DATABASE_URL = os.getenv("DATABASE_URL")
    
    SYNC_INTERVAL_SECONDS = int(os.getenv("SYNC_INTERVAL_SECONDS", "300"))
    
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE = os.getenv("LOG_FILE", "sync_agent.log")