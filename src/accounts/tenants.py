"""Tenant identifiers: turning a gym's name into a readable slug (Module S3a).

``tenant_id`` is a readable slug, not a UUID — it appears in logs, in every
manual SQL query, and in the founder's CLI, and "delariva-itaipuacu" is
answerable at a glance where "a3f9c1e2-..." is not.

NO FLASK, and no writes: this module only generates and checks. The row that
makes a tenant real is written by ``accounts/provision.py``.
"""

import logging
import re
import secrets
import unicodedata

from database.db import get_connection

logger = logging.getLogger(__name__)

# Matches owners.tenant_id's column width.
_TENANT_ID_MAX_LENGTH: int = 64

# Room left at the end of a truncated slug for a collision suffix ("-9" or
# "-ab12"), so appending one can never overflow the column.
_SUFFIX_RESERVE: int = 5

# Slugs nobody may generate. 'default' is the pilot's tenant and migrations
# 003/005/008 seed rows for it by hand; a second gym landing on that id would
# silently inherit the Delariva's configuration. The owners lookup below would
# already refuse it while that row exists — this set makes the intent explicit
# and survives someone deleting the row.
_RESERVED_TENANT_IDS: frozenset[str] = frozenset({"default"})

# Everything that is not a lowercase letter or a digit becomes a separator.
_NON_SLUG_CHARS: re.Pattern[str] = re.compile(r"[^a-z0-9]+")


def _strip_accents(value: str) -> str:
    """Remove accents so "Itaipuaçu" can become "itaipuacu".

    Twin of bot/class_types.py::_strip_accents, deliberately duplicated rather
    than imported: that one is private to the class-type marker rules, and the
    two normalizers answer different questions (a marker is upper case and
    letters only; a slug is lower case and allows digits and hyphens). The
    project already keeps normalize_owner_phone and normalize_marker apart for
    the same reason.

    Args:
        value (str): Any text, e.g. "Itaipuaçu".

    Returns:
        str: The same text with combining marks removed, e.g. "Itaipuacu".
    """
    normalized: str = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def slugify_tenant_id(academy_name: str | None) -> str | None:
    """Turn a gym's name into a candidate tenant id. Pure, no database.

    PURELY MECHANICAL, ON PURPOSE. "Academia Delariva Itaipuaçu" becomes
    "academia-delariva-itaipuacu" — the leading word is NOT dropped. Stripping
    generic words ("academia", "gym", "box") would reproduce a prettier example
    at the cost of a culture-specific word list to maintain forever, and of
    surprising answers for names made entirely of those words. When the founder
    wants a shorter id, the CLI takes an explicit --tenant-id, which is
    validated against these same rules.

    Args:
        academy_name (str | None): The gym's name as typed.

    Returns:
        str | None: A candidate slug, or None if the name contains nothing
        usable (pure punctuation or emoji).
    """
    if not academy_name:
        return None

    candidate: str = _strip_accents(academy_name).strip().lower()
    candidate = _NON_SLUG_CHARS.sub("-", candidate).strip("-")

    if not candidate:
        return None

    # Truncate with room for a collision suffix, then strip again so the result
    # can never end on the hyphen the cut landed in the middle of.
    if len(candidate) > _TENANT_ID_MAX_LENGTH - _SUFFIX_RESERVE:
        candidate = candidate[: _TENANT_ID_MAX_LENGTH - _SUFFIX_RESERVE].strip("-")

    return candidate or None


def is_tenant_id_taken(tenant_id: str) -> bool:
    """Check whether a tenant id already exists.

    `owners` is the tenant registry — every other per-tenant table hangs off a
    tenant that has a row there, and migration 009's foreign key makes that
    formal for `users`.

    Args:
        tenant_id (str): The candidate id.

    Returns:
        bool: True if some tenant already owns it.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM owners WHERE tenant_id = %s", (tenant_id,))
            row = cur.fetchone()

    return row is not None


def generate_tenant_id(academy_name: str | None) -> str:
    """Generate a free tenant id from a gym's name.

    RACE NOTE: two concurrent calls can pick the same slug, because "is it
    taken?" and "take it" are two statements. What actually guarantees
    uniqueness is `owners.tenant_id UNIQUE` (migration 003), and
    provision_tenant() catches the resulting UniqueViolation and regenerates
    once. With a founder-run CLI as the only caller, contention is zero — but if
    provisioning ever becomes a web route, the constraint is what holds, not
    this function.

    Args:
        academy_name (str | None): The gym's name as typed.

    Returns:
        str: A slug no tenant currently holds.

    Raises:
        ValueError: The name contains nothing that can become a slug.
        RuntimeError: Every candidate collided, which means something is wrong
            rather than merely unlucky — never a silent duplicate.
    """
    base: str | None = slugify_tenant_id(academy_name)

    if base is None:
        raise ValueError(
            "O nome da academia não gera um identificador válido. "
            "Use ao menos uma letra ou um número."
        )

    if base not in _RESERVED_TENANT_IDS and not is_tenant_id_taken(base):
        return base

    for suffix in range(2, 10):
        candidate: str = f"{base}-{suffix}"
        if candidate not in _RESERVED_TENANT_IDS and not is_tenant_id_taken(candidate):
            logger.info("Tenant id '%s' was taken; using '%s'.", base, candidate)
            return candidate

    for _ in range(5):
        candidate = f"{base}-{secrets.token_hex(2)}"
        if candidate not in _RESERVED_TENANT_IDS and not is_tenant_id_taken(candidate):
            logger.info("Tenant id '%s' was taken; using '%s'.", base, candidate)
            return candidate

    raise RuntimeError(
        f"Não consegui gerar um identificador livre a partir de '{base}'. "
        "Passe um --tenant-id explícito."
    )


def validate_explicit_tenant_id(tenant_id: str | None) -> str:
    """Check a hand-picked tenant id against the same rules the generator uses.

    An explicit --tenant-id is the escape hatch for a prettier slug, not a way
    around the format: an id with an uppercase letter or a space would be
    storable but would read differently everywhere it appears.

    Args:
        tenant_id (str | None): The id the founder typed.

    Returns:
        str: The same id, confirmed canonical and free.

    Raises:
        ValueError: Not canonical, reserved, or already taken.
    """
    if not tenant_id:
        raise ValueError("O identificador não pode ficar em branco.")

    candidate: str = tenant_id.strip()

    if slugify_tenant_id(candidate) != candidate:
        raise ValueError(
            f"'{tenant_id}' não está na forma canônica. "
            "Use apenas letras minúsculas sem acento, números e hífens."
        )

    if candidate in _RESERVED_TENANT_IDS:
        raise ValueError(f"'{candidate}' é reservado e não pode ser usado.")

    if is_tenant_id_taken(candidate):
        raise ValueError(f"O identificador '{candidate}' já pertence a outra academia.")

    return candidate
