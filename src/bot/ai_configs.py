"""Read and write access to ai_configs, the per-tenant customizable prompt layer.

The values here are UNTRUSTED client input: the gym owner edits them from the
settings screen (and, before that screen existed, by hand via SQL).
bot/ai_context.py::build_system_prompt() decides where they may be injected into
the prompt — this module only stores and returns them, it never builds prompt
text and never lets the client's text reach an unbounded position in the prompt.

There is deliberately NO cache here. get_ai_config() hits the database on every
call, which is what lets update_ai_config() take effect on the very next message
with nothing to invalidate. The ~60s cache in bot/ai_context.py belongs to the
Calendar slots and has nothing to do with this table — do not add a config cache
without also giving the settings screen a way to clear it.
"""

import logging

from database.db import get_connection
from integrations.store import DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)

# Returned when a tenant has no ai_configs row. The conversation must still work
# (degrade, never crash), so the prompt builder gets safe, obviously-empty text
# instead of a missing key. Seeding (migration 005) normally makes this unused.
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


def update_ai_config(
    academy_name: str,
    assistant_name: str,
    tone: str,
    business_info: str,
    flow_emphasis: str,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> bool:
    """Overwrite the customizable prompt layer for a tenant.

    All five fields are written together: the settings screen submits the whole
    section as one form, so a partial update would silently blank whatever the
    caller left out.

    There is no version history — the owner's last save wins (the screen is the
    only writer, and an audit trail nobody reads is just a second thing to keep
    in sync). Nothing is cached, so the next message the AI answers already uses
    these values; see the module docstring.

    The text is NOT validated for content here. It is untrusted by design and
    bot/ai_context.py is what confines it to fixed points in the prompt; this
    function only refuses the structurally empty case (see the route, which
    rejects blank fields before calling).

    Args:
        academy_name (str): Gym name the attendant uses.
        assistant_name (str): Name the attendant introduces itself with.
        tone (str): Personality/tone description.
        business_info (str): Business facts (modalities, address, hours, prices).
        flow_emphasis (str): What the funnel should push for.
        tenant_id (str): Tenant identifier. Fixed to DEFAULT_TENANT_ID for the pilot.

    Returns:
        bool: True if a row was updated, False if the tenant has no row (which
        would mean the migration seed never ran — the caller should say so
        rather than pretend the save worked).
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ai_configs
                SET academy_name = %s,
                    assistant_name = %s,
                    tone = %s,
                    business_info = %s,
                    flow_emphasis = %s,
                    updated_at = NOW()
                WHERE tenant_id = %s
                """,
                (academy_name, assistant_name, tone, business_info, flow_emphasis, tenant_id),
            )
            updated: bool = cur.rowcount > 0
            conn.commit()

    if updated:
        logger.info("AI config updated for tenant %s.", tenant_id)
    else:
        logger.warning("No ai_configs row to update for tenant '%s'.", tenant_id)

    return updated
