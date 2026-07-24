"""Read access to ai_configs, the per-tenant customizable prompt layer.

The values returned here are UNTRUSTED client input (edited by the gym owner via
SQL). bot/ai_context.py::build_system_prompt() decides where they may be
injected into the prompt — this module only reads them, it never builds prompt
text and never lets the client's text reach an unbounded position in the prompt.
"""

import logging

from database.db import get_connection
from integrations.store import DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)

# Returned when a tenant has no ai_configs row. The conversation must still work
# (degrade, never crash), so the prompt builder gets safe, obviously-empty text
# instead of a missing key. Seeding (migration 007) normally makes this unused.
_FALLBACK_CONFIG: dict[str, str] = {
    "tenant_id": DEFAULT_TENANT_ID,
    "academy_name": "a academia",
    "assistant_name": "a atendente",
    "tone": "simpática, clara e objetiva",
    "business_info": "",
    "flow_emphasis": "",
}


def get_ai_config(tenant_id: str = DEFAULT_TENANT_ID) -> dict[str, str]:
    """Load the customizable prompt config for a tenant.

    Args:
        tenant_id (str): Tenant identifier. Fixed to DEFAULT_TENANT_ID for the pilot.

    Returns:
        dict[str, str]: The ai_configs row as a dict, or a safe fallback config
        (see _FALLBACK_CONFIG) if the tenant has no row.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tenant_id, academy_name, assistant_name, tone, business_info, flow_emphasis
                FROM ai_configs
                WHERE tenant_id = %s
                """,
                (tenant_id,),
            )
            row = cur.fetchone()

    if row is None:
        logger.warning("No ai_configs row for tenant '%s'; using fallback config.", tenant_id)
        return dict(_FALLBACK_CONFIG)

    return dict(row)
