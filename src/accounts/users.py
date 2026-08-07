"""The users table: identity and password verification (Module S3a).

Owns ``users``, created by migration 009. One row per person who can log into
the dashboard, each pointing at exactly one tenant.

NO FLASK. Everything here reads through ``database.db.get_connection()`` with an
explicit ``tenant_id``, never through ``flask.g`` or a request — the
provisioning CLI runs outside any application context. The Flask-aware half of
authentication lives in ``accounts/auth.py``, which wraps these rows in a
``UserMixin``.

NOTHING HERE EVER LOGS AN EMAIL OR A PASSWORD. The repository is public and
these rows identify real people — the same rule ``bot/messages.py`` follows for
conversation text.
"""

import logging

from werkzeug.security import check_password_hash, generate_password_hash

from database.db import get_connection

logger = logging.getLogger(__name__)

# Matches users.email's column width.
_EMAIL_MAX_LENGTH: int = 255

# Password bounds. The floor is the only real security decision here; the
# ceiling exists because scrypt's cost is proportional to nothing the user
# controls EXCEPT input length, and a megabyte pasted into the field would burn
# real CPU on a request nobody authenticated yet.
MIN_PASSWORD_LENGTH: int = 8
MAX_PASSWORD_LENGTH: int = 200

# A real hash of a value nobody knows, compared against when the email does not
# exist so that a failed login costs the same either way. Without it, "unknown
# email" returns in microseconds while "wrong password" pays for scrypt, and the
# difference tells whoever is trying which addresses are registered.
_DUMMY_HASH: str = generate_password_hash("corujai-timing-equalizer")


def normalize_email(raw: str | None) -> str | None:
    """Reduce a typed email to the single canonical form the system stores.

    THIS IS THE ONE PLACE THAT DEFINES "CANONICAL", the same contract
    ``store.normalize_owner_phone()`` and ``class_types.normalize_marker()``
    have. Both the write path (provisioning) and the read path (login) call it,
    so a founder who provisions "Dono@Academia.com " can log in typing
    "dono@academia.com" and the plain UNIQUE on the column is enough — no citext
    extension, no functional index.

    Deliberately NOT a full RFC-5322 validator. A shape check plus the
    ``type="email"`` input on the form is the whole guard; a stricter regex
    rejects valid addresses for no gain, and the address is only ever used as a
    lookup key, never to send anything.

    Pure function, no database access, so a caller can validate before deciding
    whether to write at all.

    Args:
        raw (str | None): Whatever was typed, e.g. " Dono@Academia.com ".

    Returns:
        str | None: The canonical email (trimmed, lower case), or None if it
        could never work as one — empty, missing "@", or too long for the
        column.
    """
    if not raw:
        return None

    candidate: str = raw.strip().lower()

    if not candidate or len(candidate) > _EMAIL_MAX_LENGTH:
        return None

    # Shape only: something before the "@" and something after it.
    local, separator, domain = candidate.partition("@")
    if not separator or not local or not domain:
        return None

    return candidate


def validate_password(raw: str | None) -> str | None:
    """Check a password against the project's rules, without hashing it.

    Pure function returning the ERROR rather than a bool, so the CLI and any
    future screen can show the reason instead of a generic refusal. Portuguese,
    because the only reader is a person.

    Args:
        raw (str | None): The password as typed.

    Returns:
        str | None: A Portuguese error message, or None if the password is
        acceptable.
    """
    if not raw:
        return "A senha não pode ficar em branco."

    if len(raw) < MIN_PASSWORD_LENGTH:
        return f"A senha precisa ter pelo menos {MIN_PASSWORD_LENGTH} caracteres."

    if len(raw) > MAX_PASSWORD_LENGTH:
        return f"A senha não pode passar de {MAX_PASSWORD_LENGTH} caracteres."

    return None


def create_user(email: str, password: str, tenant_id: str, conn=None) -> int | None:
    """Create one dashboard user, storing the password as a hash.

    The optional ``conn`` is what lets ``provision.provision_tenant()`` run this
    inside its single transaction: passed a connection, this function neither
    opens nor commits one, so a failure further down the provisioning sequence
    rolls the user back with everything else. Called without it, it opens and
    commits its own, like every other writer in the project.

    ON CONFLICT DO NOTHING is load-bearing, not decorative: gunicorn calls
    ``create_app()`` once per worker, so several workers can reach
    ``bootstrap_first_user()`` at the same instant and try to insert the same
    email.

    Args:
        email (str): Already normalized by normalize_email().
        password (str): Plain text, hashed here and never stored or logged.
        tenant_id (str): The tenant this user belongs to. Must already have an
            `owners` row — the foreign key added by migration 009 enforces it.
        conn: An open psycopg2 connection to reuse, or None to open one.

    Returns:
        int | None: The new user's id, or None if the email was already taken.
    """
    def _insert(connection) -> int | None:
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (email, password_hash, tenant_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (email) DO NOTHING
                RETURNING id
                """,
                (email, generate_password_hash(password), tenant_id),
            )
            row = cur.fetchone()
        return row["id"] if row else None

    if conn is not None:
        return _insert(conn)

    with get_connection() as own_conn:
        user_id: int | None = _insert(own_conn)
        own_conn.commit()

    # The email is never logged: it identifies a real person and the repository
    # is public (same rule as bot/messages.py).
    if user_id is not None:
        logger.info("User created for tenant %s.", tenant_id)
    else:
        logger.warning("A user with that email already exists; nothing created.")

    return user_id


def get_user_by_id(user_id: int) -> dict | None:
    """Load one user by primary key — the hot path, called on every request.

    Args:
        user_id (int): The user's id.

    Returns:
        dict | None: {id, email, tenant_id, created_at}, or None. The password
        hash is deliberately not selected: the loader has no use for it, and a
        hash that is never fetched cannot be leaked into a template or a log.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, email, tenant_id, created_at FROM users WHERE id = %s",
                (user_id,),
            )
            row = cur.fetchone()

    return dict(row) if row else None


def get_user_by_email(email: str | None) -> dict | None:
    """Load one user by email, hash included, for the login comparison.

    Args:
        email (str | None): Normalized here, so a caller cannot forget to.

    Returns:
        dict | None: {id, email, password_hash, tenant_id}, or None.
    """
    normalized: str | None = normalize_email(email)
    if normalized is None:
        return None

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, email, password_hash, tenant_id FROM users WHERE email = %s",
                (normalized,),
            )
            row = cur.fetchone()

    return dict(row) if row else None


def authenticate(email: str | None, password: str | None) -> dict | None:
    """Verify a login attempt, in constant-ish time.

    ONE OUTCOME FOR BOTH FAILURES, on purpose. An unknown email and a wrong
    password are indistinguishable to the caller, and the dummy-hash comparison
    below makes them roughly indistinguishable in duration too. Two different
    answers — in the response or in the timing — tell whoever is trying which
    addresses are registered, which is the first step of a targeted attempt.

    Args:
        email (str | None): As typed; normalized downstream.
        password (str | None): As typed.

    Returns:
        dict | None: The user row {id, email, password_hash, tenant_id} on
        success, None on any failure.
    """
    row: dict | None = get_user_by_email(email)

    if row is None:
        # Still pay for a hash comparison, so "no such user" does not return
        # measurably faster than "wrong password".
        check_password_hash(_DUMMY_HASH, password or "")
        return None

    if not check_password_hash(row["password_hash"], password or ""):
        return None

    return row


def set_password(email: str | None, password: str) -> bool:
    """Replace a user's password. The founder's escape hatch, via the CLI.

    Args:
        email (str | None): Normalized here.
        password (str): Plain text, hashed here.

    Returns:
        bool: True if a row was updated, False if no such email.
    """
    normalized: str | None = normalize_email(email)
    if normalized is None:
        return False

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET password_hash = %s WHERE email = %s",
                (generate_password_hash(password), normalized),
            )
            updated: bool = cur.rowcount > 0
            conn.commit()

    if updated:
        logger.info("Password updated for one user.")
    else:
        logger.warning("No user to update for the given email.")

    return updated


def count_users() -> int:
    """Count every user, across every tenant.

    Used by bootstrap_first_user() as its idempotency check: once ANY user
    exists the bootstrap never runs again, so restarting the app can never
    re-seed a password the founder has since changed.

    Returns:
        int: Total rows in `users`.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM users")
            row = cur.fetchone()

    return int(row["total"]) if row else 0


def list_users(tenant_id: str | None = None) -> list[dict]:
    """List users for the founder's CLI, without their hashes.

    Args:
        tenant_id (str | None): Restrict to one tenant, or None for all of them.
            This is one of the few reads in the project that is deliberately
            cross-tenant: the founder listing every account is the whole point.

    Returns:
        list[dict]: {id, email, tenant_id, created_at}, oldest first. The
        password hash is never selected.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            if tenant_id is None:
                cur.execute(
                    "SELECT id, email, tenant_id, created_at FROM users ORDER BY id"
                )
            else:
                cur.execute(
                    """
                    SELECT id, email, tenant_id, created_at
                    FROM users WHERE tenant_id = %s ORDER BY id
                    """,
                    (tenant_id,),
                )
            rows: list[dict] = cur.fetchall()

    return [dict(row) for row in rows]


def delete_user(email: str | None) -> bool:
    """Delete one user by email. Used by the CLI and by the test suites' teardown.

    Args:
        email (str | None): Normalized here.

    Returns:
        bool: True if a row was deleted.
    """
    normalized: str | None = normalize_email(email)
    if normalized is None:
        return False

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE email = %s", (normalized,))
            deleted: bool = cur.rowcount > 0
            conn.commit()

    return deleted
