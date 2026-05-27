"""
PostgreSQL writer

Persists the synced products catalog to the central database used by me.

Idempotency strategy:
- Each product row is UPSERTed by 'external_id' (the POS product ID).
- 'last_synced_at' is set to 'clock_timestamp()' on every write so each row gets
a distinct timestamp inside the transaction.
- A sync-start timestamp is captured from the database before the upserts. Any row whose 
'last_synced_at' is older than this timestamp is considered absent from the lastest POS read
and is marked as inactive.    
"""

import logging 
from contextlib import contextmanager
from typing import Iterator, List

import psycopg2
from psycopg2.extensions import connection as PgConnection

from firebird_reader import ProductRecord

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS: int = 10

class PostgresWriter:
    """Write for the  central PostgreSQL products database."""
    
    def __init__(self, database_url: str):
        self.database_url = database_url
        
    @contextmanager
    def _connect(self) -> Iterator[PgConnection]:
        """Open a PostgreSQL connection bound to a 'with' block."""
        
        conn = psycopg2.connect(self.database_url)
        try:
            yield conn
        finally:
            conn.close()
            
            
    def sync_product(self, products: List[ProductRecord]) -> dict[str, int]:
        """Persist the product catalog: upsert all incoming products and 
        soft-delete any product missing from the current snapshot.

        If the incoming list is empty the sync is aborted to avoid accidentally deactivating
        the whole catalog (e.g. due to a failed query against the source).
        
    
        Args:
            products (List[ProductRecord]): the list of the products will be inserted/updated.
        Returns:
            dict[str, int]: a dictionary containing the number of products upserted and deactivated.
        """
        
        if not products:
            logger.warning("Received empty product list, aborting sync to avoid mass deactivation.")
            return {"upserted": 0, "deactivated": 0}
        
        with self._connect() as conn:
            with conn.cursor() as cur:
                # read the DB clock before any write so it cannot
                # accindentally fall after the upsert timestamps
                cur.execute("SELECT clock_timestamp()")
                sync_start_time = cur.fetchone()[0]
                
                # Upserts - each row gets a fresh clock_timestamp() in SQL
                for product in products:
                    cur.execute(
                        """
                        INSERT INTO products (
                            external_id, code, name, price, stock_quantity, category, is_active, last_synced_at
                        )
                        Values (%s, %s, %s, %s, %s, %s, true, clock_timestamp())
                        ON CONFLICT (external_id) DO UPDATE SET
                            code = EXCLUDED.code,
                            name = EXCLUDED.name,
                            price = EXCLUDED.price,
                            stock_quantity = EXCLUDED.stock_quantity,
                            category = EXCLUDED.category,
                            is_active = true,
                            last_synced_at = clock_timestamp()
                        """,
                        (
                            product.external_id,
                            product.code,
                            product.name,
                            product.price,
                            product.stock_quantity,
                            product.category,
                        )
                    )
                
                cur.execute(
                    """
                    UPDATE products
                    SET is_active = false
                    WHERE last_synced_at < %s
                    AND is_active = true
                    """,
                    (sync_start_time,)
                )
                
                deactivated: int = cur.rowcount
            
            conn.commit()
            
            upserted: int = len(products)
            logger.info("Sync completed: %d upserted, %d deactivated", upserted, deactivated)
            
            return {"upserted": upserted, "deactivated": deactivated}
                
                
            
    