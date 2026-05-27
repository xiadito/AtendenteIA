"""
Firebird reader for Sync Agent.

Read-only access to the POS database. This modulo never writes, deletes, or otherwise modifies data on the source system.    
"""

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterator, List, Optional

import firebird.driver as fb

logger: logging.Logger = logging.getLogger(__name__)

# ============================================================
# PLACEHOLDER QUERY — UPDATE AFTER SCHEMA DISCOVERY
# ============================================================
# Expected output columns (in this order):
#   1. external_id     — stable unique identifier from the POS
#   2. code            — SKU or barcode (may be NULL)
#   3. name            — product display name
#   4. price           — current sale price (NUMERIC)
#   5. stock_quantity  — available stock (NUMERIC; may be fractional for
#                        items sold by weight)
#   6. category        — product category name (may be NULL)
#
# Once the schema is mapped, replace the placeholder table/column names
# with the real ones from PDV Rio's Firebird database.
# ============================================================

products_query: str = """
SELECT 
    ID AS EXTERNAL_ID,
    CODIGO AS CODE,
    DESCRICAO AS NAME,
    PRECO AS PRICE,
    ESTOQUE AS STOCK_QUANTITY,
    CATEGORIA AS CATEGORY
FROM PRODUTO
"""

@dataclass(frozen=True)
class ProductRecord:
    """Imutable representation of a single product read from the POS.
    """
    
    external_id: int
    code: Optional[str]
    name: str
    price: Decimal
    stock_quantity: Decimal
    category: Optional[str]
    
class FirebirdReader:
    """ Read-only client for the POS Firebird database."""
    
    def __init__(
            self,
            host: str,
            port: int,
            database: str,
            user: str,
            password: str,
            charset: str = "WIN1252",
    ):
        # DSN format expected by firebird-driver: "host/port:database_path"
        self.dsn = f"{host}/{port}:{database}"
        self.user = user
        self.password = password
        self.charset = charset
        
    @contextmanager
    def _connect(self) -> Iterator[fb.Connection]:
        """Open a Firebird connection bound to a 'with' block."""
        
        conn = fb.Connection = fb.connect(
            self.dsn,
            user=self.user,
            password=self.password,
            charset=self.charset,
        )
        try:
            yield conn
        finally:
            conn.close()
            
    def fetch_products(self) -> List[ProductRecord]:
        """Fetch every product from the POS database.

        Returns:
            List[ProductRecord]: A list of ProductRecord objects.
        """
        
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(products_query)
            rows: list[tuple] = cur.fetchall()
            
        products: list[ProductRecord] = [self._row_to_product(row) for row in rows]
        logger.info("Fetched %d products from POS database", len(products))
        
        return products
    
    @staticmethod
    def _row_to_product(row: tuple) -> ProductRecord:
        """Map a raw cursor row to a type ProductRecord."""
        
        raw_external_id, raw_code, raw_name, raw_price, raw_stock_quantity, raw_category = row
        
        return ProductRecord(
            external_id = str(raw_external_id),
            code = str(raw_code).strip() if raw_code is not None else None,
            name = str(raw_name).strip(),
            price = Decimal(str(raw_price)) if raw_price is not None else Decimal("0.00"),
            stock_quantity = Decimal(str(raw_stock_quantity)) if raw_stock_quantity is not None else Decimal("0.00"),
            category = str(raw_category) if raw_category is not None else None,
        )
        
        
        
            