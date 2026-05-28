"""
Sync agent entry point.

Runs an infinite loop that periodically reads from the POS database and
syncs the product catalog to the central PostgreSQL.

This process is meant to run on the client's PC (where the POS database is located)
, connecting outward to the central PostgreSQL on Railway.    
"""

import logging
import sys
import time 
from logging.handlers import RotatingFileHandler
from typing import Final

from firebird_reader import FirebirdReader
from postgres_writer import PostgresWriter
from config import Config

logger: logging.Logger = logging.getLogger(__name__)

default_sync_interval_sec: Final[int] = Config.SYNC_INTERVAL_SECONDS or 300
default_log_file: Final[str] = Config.LOG_FILE or "sync_agent.log"
default_log_level: Final[str] = Config.LOG_LEVEL or "INFO"
default_firebird_charset: Final[str] = "WIN1252"

log_file_max_bytes: Final[int] = 5 * 1024 * 1024  # 5 MB
log_file_backup_count: Final[int] = 2

def setup_logging(log_file: str, log_level: int) -> None:
    """Configure rotating file logging plus stdout output."""

    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            log_file,
            maxBytes=log_file_max_bytes,
            backupCount=log_file_backup_count,
            encoding="utf-8"
        ),
        logging.StreamHandler(sys.stdout)
    ]
    
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers
    )
    
def run_one_cycle(reader: FirebirdReader, writer: PostgresWriter) -> None:
    """Run one sync cycle: read from Firebird and write to PostgreSQL."""
    
    logger.info("Starting sync cycle")
    
    try:
        products = reader.fetch_products()
        writer.sync_products(products)
        logger.info("Sync cycle completed successfully")
        
    except Exception as e:
        logger.exception("Error during sync cycle: %s", e)
        
def build_reader() -> FirebirdReader:
    """Construct the FirebirdReader from environment variables."""
    
    return FirebirdReader(
        host=Config.FIREBIRD_HOST,
        port=int(Config.FIREBIRD_PORT),
        database=Config.FIREBIRD_DATABASE,
        user=Config.FIREBIRD_USER,
        password=Config.FIREBIRD_PASSWORD,
        charset=Config.FIREBIRD_CHARSET,
    )
    
def build_writer() -> PostgresWriter:
    """Construct the PostgresWriter from environment variables."""
    
    return PostgresWriter(
        database_url=Config.DATABASE_URL
    )
    
def main() -> None:
    """Bootstrap and run the infinite sync loop."""
    log_file = default_log_file
    log_level = default_log_level
    setup_logging(log_file, log_level)
    logger.info("Sync agent starting up")
    
    reader: FirebirdReader = build_reader()
    writer: PostgresWriter = build_writer()
    
    interval_sec: int = default_sync_interval_sec
    
    try: 
        while True:
            run_one_cycle(reader, writer)
            logger.info("Sleeping for %d seconds before next cycle", interval_sec)
            time.sleep(interval_sec)
            
    except KeyboardInterrupt:
        logger.info("Sync agent shutting down due to keyboard interrupt")

if __name__ == "__main__":
    main()