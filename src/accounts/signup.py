"""Abuse guards for the public signup form (Module S3c).

`POST /dashboard/signup` is the first unauthenticated endpoint in the project
that creates rows — five tables at a time, through `provision_tenant()`. This
module holds the two cheap guards that stand in front of it:

- a **honeypot**, which catches the dumb bots that fill every field they find;
- a **per-IP ceiling**, counted in Postgres because gunicorn workers share
  nothing else.

Neither is a serious defence against someone determined; together they are what
keeps a drive-by script from filling the database, at the cost of one table and
no new dependency. A CAPTCHA would be stronger and would break the project's
"no build pipeline, no third-party script" rule.

NO FLASK. The route extracts the client IP and hands it in — this module never
touches `request`, so it stays testable and matches the rest of `accounts/`.
"""

import hashlib
import logging

from config import Config
from database.db import get_connection

logger = logging.getLogger(__name__)

# How many signup attempts one client may make in the window below. Generous on
# purpose: a real person who mistypes their password twice, gets the "e-mail já
# existe" answer, and tries a different address has burned four. The ceiling is
# there to stop a loop, not to police typing.
MAX_ATTEMPTS_PER_WINDOW: int = 5
ATTEMPT_WINDOW_MINUTES: int = 60

# The hidden form field a human never sees and a naive bot always fills. Named
# like something worth filling — "honeypot" or "leave_blank" is a hint to
# anything reading the markup.
HONEYPOT_FIELD: str = "website"


def hash_ip(ip: str | None) -> str:
    """Reduce a client IP to the only form this module ever stores.

    The throttle only asks "have I seen this client before?", which equality
    answers — nothing ever needs to read an address back out. An IP is personal
    data and this project's schema is public, so the digest is what goes in the
    table, salted with the application secret so a database dump alone cannot be
    reversed by rainbow table.

    Args:
        ip (str | None): The client address, or None if it could not be
            determined (then every such request shares one bucket, which is the
            safe direction to fail).

    Returns:
        str: 64 hex characters, matching signup_attempts.ip_hash's width.
    """
    salt: str = Config.SECRET_KEY or "corujai-signup-salt"
    return hashlib.sha256(f"{salt}:{ip or 'unknown'}".encode("utf-8")).hexdigest()


def is_honeypot_filled(value: str | None) -> bool:
    """Say whether the hidden field came back filled.

    A browser leaves it empty because CSS hides it and no human types into what
    they cannot see. A bot that walks the DOM and fills every input trips it.

    Args:
        value (str | None): Whatever arrived in the honeypot field.

    Returns:
        bool: True when the request should be silently discarded.
    """
    return bool(value and value.strip())


def too_many_attempts(ip: str | None) -> bool:
    """Say whether this client has already used up its window.

    Counted in the DATABASE, not in a module global: gunicorn runs several
    worker processes, each with its own memory, so an in-process counter would
    see roughly one Nth of the attempts and let N times the intended rate
    through. The workers share the database and nothing else.

    Args:
        ip (str | None): The client address, hashed here.

    Returns:
        bool: True if the ceiling was reached — the caller must refuse without
        provisioning anything.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS total
                FROM signup_attempts
                WHERE ip_hash = %s
                  AND created_at > NOW() - (%s * INTERVAL '1 minute')
                """,
                (hash_ip(ip), ATTEMPT_WINDOW_MINUTES),
            )
            total: int = int(cur.fetchone()["total"])

    if total >= MAX_ATTEMPTS_PER_WINDOW:
        # The address is never logged, only the fact. Same rule as the rest of
        # the project: this repository is public.
        logger.warning(
            "Signup throttled: a client reached %d attempts in %d minutes.",
            total, ATTEMPT_WINDOW_MINUTES,
        )
        return True

    return False


def record_attempt(ip: str | None) -> None:
    """Record one signup attempt against a client.

    Called for every POST that gets past the honeypot, SUCCESSFUL OR NOT. That
    is deliberate: counting only failures would let a script create accounts at
    full speed, which is the outcome the ceiling exists to prevent.

    Args:
        ip (str | None): The client address, hashed here.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO signup_attempts (ip_hash) VALUES (%s)",
                (hash_ip(ip),),
            )
        conn.commit()
