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


def get_owner_by_phone_in_tenant(owner_phone: str, tenant_id: str) -> dict | None:
    """Ask the same question as get_owner_by_phone(), but INSIDE one tenant.

    THE DIFFERENCE IS THE WHOLE POINT OF MODULE S3a. get_owner_by_phone() scans
    every tenant, which was correct while exactly one existed. Once each gym has
    its own inbound number, the right question is no longer "is this number some
    owner's?" but "is this number the owner of the gym this message was written
    TO?" — because with a global scan, gym B's owner writing to gym A's number
    would be routed to the owner handler and their "1" would confirm one of gym
    A's bookings.

    Args:
        owner_phone (str): Plain-digit number, as clean_number in
            webhook/routes.py.
        tenant_id (str): The tenant already resolved from the message's "To".

    Returns:
        dict | None: {id, tenant_id, owner_phone} if this number is THAT
        tenant's owner, else None (anyone else is a lead of that tenant).
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, tenant_id, owner_phone
                FROM owners
                WHERE owner_phone = %s AND tenant_id = %s
                """,
                (owner_phone, tenant_id),
            )
            row = cur.fetchone()

    return dict(row) if row else None


def find_tenant_by_whatsapp_number(to: str | None) -> str | None:
    """Identify which tenant an inbound message was written TO.

    `to` is Twilio's "To" field: the gym's own WhatsApp number. Since each gym
    gets its own number, that field is the tenant routing key.

    Returns None rather than a default on purpose — the caller has to be able to
    tell "this number belongs to gym X" from "no gym has claimed this number",
    because the two lead to genuinely different routing. resolve_tenant_by_
    whatsapp_number() is the wrapper for callers that just want an answer.

    Args:
        to (str | None): The raw "To" field, e.g. "whatsapp:+14155238886".

    Returns:
        str | None: The tenant that owns this number, or None if it is missing,
        unparseable, or registered to nobody.
    """
    number: str | None = normalize_owner_phone(to)
    if number is None:
        return None

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tenant_id FROM owners WHERE whatsapp_number = %s",
                (number,),
            )
            row = cur.fetchone()

    return row["tenant_id"] if row else None


def get_whatsapp_number(tenant_id: str = DEFAULT_TENANT_ID) -> str | None:
    """Read a tenant's own WhatsApp number — the line its leads write to.

    THE MIRROR OF find_tenant_by_whatsapp_number(). That one asks "which gym owns
    this number?" on the way IN; this one asks "which number does this gym own?"
    on the way OUT, so whatsapp/whatsapp_service.py can send the reply from the
    gym's own line instead of one number shared by everybody (Module S3d).

    A separate function rather than a column added to get_owner_credentials():
    that one is the OAuth screen's reader and widening it would touch a screen
    with no interest in phone numbers at all.

    Args:
        tenant_id (str): Tenant identifier.

    Returns:
        str | None: Plain digits (e.g. "5521999999999"), or None if the tenant
        has no number registered or no owners row at all. None is not an error:
        it is the sandbox state, where every gym shares one inbound number.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT whatsapp_number FROM owners WHERE tenant_id = %s",
                (tenant_id,),
            )
            row = cur.fetchone()

    return row["whatsapp_number"] if row else None


def resolve_tenant_by_whatsapp_number(to: str | None) -> str:
    """Resolve a tenant from an inbound "To", degrading to the pilot tenant.

    THE FALLBACK IS THE SANDBOX, and it is why the pilot keeps working. The
    Twilio Sandbox gives every gym the SAME inbound number, so no tenant can
    claim it and `owners.whatsapp_number` stays NULL everywhere — meaning this
    function currently always returns DEFAULT_TENANT_ID, which is exactly what
    every call in the project defaulted to before S3a. Nothing observable
    changes until real numbers arrive, and the correct shape is already in
    place for when they do.

    It never blocks a message. An unrecognized number is a configuration gap,
    not a reason to drop a lead.

    Args:
        to (str | None): The raw "To" field.

    Returns:
        str: The resolved tenant, or DEFAULT_TENANT_ID with a WARNING logged.
    """
    tenant_id: str | None = find_tenant_by_whatsapp_number(to)

    if tenant_id is not None:
        return tenant_id

    # Only the last four digits are logged. A gym's business line is less
    # sensitive than a lead's personal number, but the rule that this public
    # repository never logs a whole number should not acquire an exception.
    digits: str | None = normalize_owner_phone(to)
    logger.warning(
        "No tenant registered for the destination number ending in %s; using '%s'.",
        digits[-4:] if digits else "?",
        DEFAULT_TENANT_ID,
    )
    return DEFAULT_TENANT_ID


def update_whatsapp_number(
    whatsapp_number: str | None,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> bool:
    """Store (or clear) the gym's own inbound WhatsApp number.

    Thin writer, like its neighbours: it assumes the value is ALREADY normalized
    and already checked for the two-roles collision. Those guards live in
    accounts/provision.py::set_whatsapp_number(), because checking `sessions`
    needs bot/ and nothing under integrations/ imports bot/.

    Args:
        whatsapp_number (str | None): Plain digits, or None to clear it.
        tenant_id (str): Tenant identifier.

    Returns:
        bool: True if a row was updated, False if the tenant has no owners row.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE owners
                SET whatsapp_number = %s,
                    updated_at = NOW()
                WHERE tenant_id = %s
                """,
                (whatsapp_number, tenant_id),
            )
            updated: bool = cur.rowcount > 0
            conn.commit()

    if updated:
        logger.info("WhatsApp number updated for tenant %s.", tenant_id)
    else:
        logger.warning("No owners row to update for tenant '%s'.", tenant_id)

    return updated


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
