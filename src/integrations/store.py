import logging

from database.db import get_connection

logger = logging.getLogger(__name__)

DEFAULT_TENANT_ID: str = "default"

valid_integration_statuses: set[str] = {
    "connected",        # refresh_token valid and calendar linked
    "disconnected",     # no credentials stored (initial state, or after user disconnect)
    "needs_reconnect",  # Google rejected the refresh_token (invalid_grant)
}


def get_owner_credentials(tenant_id: str = DEFAULT_TENANT_ID) -> dict | None:
    """Return the stored Google Calendar credentials for a tenant.

    Args:
        tenant_id (str): Tenant identifier. Fixed to DEFAULT_TENANT_ID for the pilot.

    Returns:
        dict | None: Row with tenant_id, google_email, refresh_token, calendar_id
        and integration_status, or None if no row exists for tenant_id.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tenant_id, google_email, refresh_token, calendar_id, integration_status
                FROM owners
                WHERE tenant_id = %s
                """,
                (tenant_id,),
            )
            row = cur.fetchone()

    return dict(row) if row else None


def save_owner_credentials(
    google_email: str,
    refresh_token: str,
    calendar_id: str,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> None:
    """Persist credentials after a successful OAuth callback and mark as connected.

    Args:
        google_email (str): Google account email the tokens belong to.
        refresh_token (str): Long-lived refresh token returned by Google.
        calendar_id (str): ID of the "Aulas Experimentais" calendar.
        tenant_id (str): Tenant identifier. Fixed to DEFAULT_TENANT_ID for the pilot.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE owners
                SET google_email = %s,
                    refresh_token = %s,
                    calendar_id = %s,
                    integration_status = 'connected',
                    updated_at = NOW()
                WHERE tenant_id = %s
                """,
                (google_email, refresh_token, calendar_id, tenant_id),
            )
            conn.commit()

    logger.info("Owner credentials saved for tenant %s (email=%s).", tenant_id, google_email)


def mark_needs_reconnect(tenant_id: str = DEFAULT_TENANT_ID) -> None:
    """Flag the stored credentials as invalid, prompting the user to reconnect.

    Args:
        tenant_id (str): Tenant identifier. Fixed to DEFAULT_TENANT_ID for the pilot.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE owners
                SET integration_status = 'needs_reconnect',
                    updated_at = NOW()
                WHERE tenant_id = %s
                """,
                (tenant_id,),
            )
            conn.commit()

    logger.warning("Owner credentials for tenant %s marked as needs_reconnect.", tenant_id)


def clear_owner_credentials(tenant_id: str = DEFAULT_TENANT_ID) -> None:
    """Clear stored credentials and mark the integration as disconnected.

    Args:
        tenant_id (str): Tenant identifier. Fixed to DEFAULT_TENANT_ID for the pilot.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE owners
                SET google_email = NULL,
                    refresh_token = NULL,
                    calendar_id = NULL,
                    integration_status = 'disconnected',
                    updated_at = NOW()
                WHERE tenant_id = %s
                """,
                (tenant_id,),
            )
            conn.commit()

    logger.info("Owner credentials cleared for tenant %s.", tenant_id)
