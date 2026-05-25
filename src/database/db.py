import logging 
from config import Config
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

def get_connection() -> psycopg2.extensions.connection:
    """Return a ne database connection using DATABASE_URL

    Cursors created from this connection will return rows as dictionaries,
    (via RealDictCursor) for egonomic row[column] acess.
    
    Returns:
        psycopg2 connection object
    Raises:
        RuntimeError: if DATABASE_URL is not set in environment variables
    """
    
    database_url = Config.DATABASE_URL
    
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set in environment variables.")
    
    return psycopg2.connect(database_url, cursor_factory=RealDictCursor)

def init_db():
    
    with get_connection() as conn:
        with conn.cursor() as cur:
            # The schema_migrations table is the only DDL hardcoded in Python
            # Every other schema change lives in a versioned .sql file.
            
            cur.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version VARCHAR(255) PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            
            conn.commit()
            
            #discover all migration files, sorted by filename
            
            migrations_dir: Path = Path(__file__).parent / "migrations"
            sql_files: list[Path] = sorted(migrations_dir.glob("*.sql"))
            
            for sql_file in sql_files:
                # the version is the filename without extension, eg "001_initial"
                version: str = sql_file.stem
                
                # check whether this migration has already been applied
                cur.execute(
                    "SELECT version FROM schema_migrations WHERE version = %s", 
                    (version,)
                )
                
                already_applied: bool = cur.fetchone() is not None
                
                if already_applied:
                    logger.info("Migration %s already applied, skipping.", version)
                    continue
                
                # read and execute the sql file
                sql: str = sql_file.read_text(encoding="utf-8")
                cur.execute(sql)
                
                #recorde the migraton as applied so it never runs again
                cur.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)", 
                    (version,)
                )
                
                conn.commit()
                logger.info("Migration %s applied successfully.", version)


