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


def get_owner_by_phone(owner_phone: str) -> dict | None:
    """Find the owner whose owner_phone matches an incoming WhatsApp number.

    Used by the webhook to decide whether an incoming message is the gym
    owner replying to a notification, rather than a lead.

    Args:
        owner_phone (str): Plain-digit number, e.g. "5521999999999" (same
            format as clean_number in webhook/routes.py).

    Returns:
        dict | None: {id, tenant_id, owner_phone} if this number belongs to
        a registered owner, else None (an unknown number is a lead).
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, tenant_id, owner_phone FROM owners WHERE owner_phone = %s",
                (owner_phone,),
            )
            row = cur.fetchone()

    return dict(row) if row else None


def normalize_owner_phone(raw: str | None) -> str | None:
    """Reduce a typed-in phone number to the exact format the webhook compares against.

    THIS IS A ROUTING KEY, NOT A DISPLAY FIELD. webhook/routes.py builds
    clean_number as `sender.replace("whatsapp:+", "")` and hands it to
    get_owner_by_phone() on EVERY incoming message to decide whether the writer
    is the owner or a lead. Stored in any other shape — with "whatsapp:+", a
    space, a dash — the comparison simply never matches, and the owner silently
    stops being recognized: their "1"/"2" replies stop closing bookings, with no
    error anywhere. Hence: keep the digits, drop everything else.

    Pure function, no database access, so the route can validate before deciding
    whether to write at all.

    Args:
        raw (str | None): Whatever the owner typed, e.g. "whatsapp:+55 21 99999-9999".

    Returns:
        str | None: Digits only (e.g. "5521999999999"), or None if the input has
        no plausible phone number in it. The bounds are 10-15 digits: 15 is the
        E.164 maximum, and the lower bound rejects a truncated paste. A Brazilian
        number with country code is 12 or 13, but pinning it there would lock the
        product to one country for no gain.
    """
    if not raw:
        return None

    digits: str = "".join(char for char in raw if char.isdigit())

    if not 10 <= len(digits) <= 15:
        return None

    return digits


def update_owner_phone(owner_phone: str, tenant_id: str = DEFAULT_TENANT_ID) -> bool:
    """Store the owner's WhatsApp number for a tenant.

    Thin writer, like every other function here: it assumes owner_phone is
    ALREADY normalized by normalize_owner_phone() and already checked against
    the leads in `sessions`. Both guards live in the caller — see the settings
    route in webhook/routes.py — because the collision check needs bot/session.py
    and nothing under integrations/ imports bot/.

    Args:
        owner_phone (str): Plain-digit number, as normalize_owner_phone() returns.
        tenant_id (str): Tenant identifier. Fixed to DEFAULT_TENANT_ID for the pilot.

    Returns:
        bool: True if a row was updated, False if the tenant has no owners row.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE owners
                SET owner_phone = %s,
                    updated_at = NOW()
                WHERE tenant_id = %s
                """,
                (owner_phone, tenant_id),
            )
            updated: bool = cur.rowcount > 0
            conn.commit()

    # The number itself is not logged: it identifies a real person and the
    # repository is public (same rule as bot/messages.py).
    if updated:
        logger.info("Owner phone updated for tenant %s.", tenant_id)
    else:
        logger.warning("No owners row to update for tenant '%s'.", tenant_id)

    return updated


def get_owner_for_notification(tenant_id: str = DEFAULT_TENANT_ID) -> dict | None:
    """Return the owner row a notification enqueue needs.

    Args:
        tenant_id (str): Tenant identifier. Fixed to DEFAULT_TENANT_ID for the pilot.

    Returns:
        dict | None: {id, tenant_id, owner_phone}, or None if no row exists.
        owner_phone may be NULL — the caller must skip enqueueing, not crash.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, tenant_id, owner_phone FROM owners WHERE tenant_id = %s",
                (tenant_id,),
            )
            row = cur.fetchone()

    return dict(row) if row else None
