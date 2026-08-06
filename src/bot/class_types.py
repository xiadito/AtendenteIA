"""Per-tenant scheduling configuration: which classes exist, and how far ahead to look.

Owns the two tables created by migration 008:

- ``class_types`` — one row per class a tenant offers (marker, label, capacity,
  whether it needs the attending child's name, and which one catches events with
  no recognized marker). Until Module S2 this was two dicts hardcoded at the top
  of ``bot/scheduling.py``, which meant no second gym could exist.
- ``scheduling_configs`` — ``days_ahead``, the Calendar search horizon that used
  to be a hardcoded default of 14.

NO FLASK. Everything here reads through ``database.db.get_connection()`` with an
explicit ``tenant_id``, never through ``flask.g``, a session, or a request:
``jobs/drain_notifications.py`` runs this code from the Railway cron, outside any
request context.

NO CACHE, on purpose. ``load_class_types()`` is one indexed read of a handful of
rows, and every caller is already doing something far more expensive (an HTTP
round-trip to Google, an LLM call, a page render). A TTL cache would buy nothing
measurable and would make the settings screen lie for its duration — unlike
``ai_context.get_cached_slots()``, whose cache exists to avoid the Calendar call
itself.
"""

import logging
import re
import unicodedata
from typing import Any

from database.db import get_connection
from integrations.store import DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)

# A canonical marker after normalization: letters only, upper case. The title
# parser's regex (scheduling._TITLE_MARKER_PATTERN) only ever matches
# [a-zA-ZÀ-ÿ]+ inside the brackets, so a marker containing a digit, a space or a
# hyphen could be stored but could never be read back from an event title. It
# would be invisible forever, and every slot of that class would silently fall
# back. Rejecting it at the write path is what keeps that impossible.
_CANONICAL_MARKER_PATTERN: re.Pattern[str] = re.compile(r"^[A-Z]+$")

# Matches class_types.marker's column width.
_MARKER_MAX_LENGTH: int = 32

# The last-resort fallback class type, synthesized in memory (never written to
# the database) when a tenant has no row flagged is_fallback and no row that
# could stand in for one. Unlimited and requiring no child name, because the
# whole point of a fallback is that a mis-typed event DEGRADES instead of
# blocking a booking — see load_class_types().
_SYNTHETIC_FALLBACK_MARKER: str = "ADULTOS"
_SYNTHETIC_FALLBACK_LABEL: str = "Adultos"

# Default horizon when a tenant has no scheduling_configs row. Same value
# get_available_slots() hardcoded before migration 008.
DEFAULT_DAYS_AHEAD: int = 14

# Bounds enforced by scheduling_configs' CHECK constraint. Kept here too so the
# settings screen can refuse a bad value with a Portuguese notice instead of
# letting psycopg2 raise.
MIN_DAYS_AHEAD: int = 1
MAX_DAYS_AHEAD: int = 90


def _strip_accents(value: str) -> str:
    """Remove accents so marker comparison is accent-insensitive.

    Args:
        value (str): Any text, e.g. "Crianças".

    Returns:
        str: The same text with combining marks removed, e.g. "Criancas".
    """
    normalized: str = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def normalize_marker(raw: str | None) -> str | None:
    """Reduce a marker to the single canonical form both sides of the system use.

    THIS IS THE ONE PLACE THAT DEFINES "CANONICAL". The write path (the settings
    screen) and the read path (scheduling._parse_class_type, on every Calendar
    event title) both call it, so the stored marker and the parsed marker cannot
    drift into two different notions of the same word: "[ Crianças ]" typed in a
    calendar title and "crianças" typed in the form both become "CRIANCAS".

    Pure function, no database access, so a route can validate before deciding
    whether to write at all.

    Args:
        raw (str | None): Whatever was typed or parsed, e.g. " crianças ".

    Returns:
        str | None: The canonical marker (accent-free, upper case, letters
        only), or None if the input could never work as one — empty, too long
        for the column, or containing anything the title regex cannot match.
    """
    if not raw:
        return None

    candidate: str = _strip_accents(raw).strip().upper()

    if not candidate or len(candidate) > _MARKER_MAX_LENGTH:
        return None

    if not _CANONICAL_MARKER_PATTERN.match(candidate):
        return None

    return candidate


def load_class_types(tenant_id: str = DEFAULT_TENANT_ID) -> dict[str, Any]:
    """Load everything one scheduling operation needs about a tenant's classes.

    ONE READ PER OPERATION. Callers load this once and use the returned dicts
    for every slot they then process — get_available_slots() loops over every
    event in the Calendar window, and going back to the database per event would
    turn one query into one query per slot.

    THE FALLBACK INVARIANT — the reason this returns a bundle instead of a dict.
    scheduling.get_available_slots() and scheduling.book_slot() look capacity up
    directly (``capacities[class_type]``), with no ``.get`` and no default. That
    was safe while the types lived in code, because _parse_class_type() could
    only ever return a key of the CLASS_CAPACITY literal. Read from a table it
    is not: a tenant may have no "ADULTOS" row at all, and a mis-typed event
    title would raise KeyError in the middle of a lead's booking.

    So this function guarantees a post-condition its callers can rely on:

        result["fallback"] in result["capacities"]     — always true

    resolved in three steps:

      1. the row flagged ``is_fallback`` for this tenant (the seeded case: the
         pilot's ADULTOS, so behaviour is byte-for-byte what it was before);
      2. failing that, an existing row named ADULTOS, if the tenant happens to
         have one — never overwritten, only pointed at;
      3. failing that, a synthetic unlimited ADULTOS merged into the returned
         dicts and NOT written to the database.

    Step 3 synthesizes rather than borrowing the tenant's first row on purpose:
    borrowing (say) a BABY row would make an unmarked event capacity-2 AND
    require a child's name, so a typo in a title would BLOCK bookings — the
    exact opposite of what the fallback exists to do. A synthetic type is always
    unlimited and never asks for a child's name, so degrading stays degrading.

    Args:
        tenant_id (str): Tenant identifier. Fixed to DEFAULT_TENANT_ID until
            Module S3 isolates reads per tenant.

    Returns:
        dict[str, Any]: Four keys —
            "capacities" (dict[str, int | None]): marker → seats, None =
                unlimited. Same shape as the retired CLASS_CAPACITY dict.
            "labels" (dict[str, str]): marker → human-readable label. Same shape
                as the retired CLASS_TYPE_LABELS dict.
            "child_name_required" (set[str]): markers whose bookings need the
                attending child's name.
            "fallback" (str): marker for titles with no recognized marker;
                always present in "capacities".
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT marker, label, capacity, requires_child_name, is_fallback
                FROM class_types
                WHERE tenant_id = %s
                ORDER BY marker
                """,
                (tenant_id,),
            )
            rows: list[dict] = cur.fetchall()

    capacities: dict[str, int | None] = {}
    labels: dict[str, str] = {}
    child_name_required: set[str] = set()
    fallback: str | None = None

    for row in rows:
        marker: str = row["marker"]
        capacities[marker] = row["capacity"]
        labels[marker] = row["label"]
        if row["requires_child_name"]:
            child_name_required.add(marker)
        if row["is_fallback"]:
            fallback = marker

    if fallback is None:
        if _SYNTHETIC_FALLBACK_MARKER in capacities:
            # The tenant has the type but never flagged it. Point at it as-is.
            fallback = _SYNTHETIC_FALLBACK_MARKER
            logger.warning(
                "Tenant '%s' has no class type flagged is_fallback; using existing '%s'.",
                tenant_id, fallback,
            )
        else:
            fallback = _SYNTHETIC_FALLBACK_MARKER
            capacities[fallback] = None
            labels[fallback] = _SYNTHETIC_FALLBACK_LABEL
            logger.warning(
                "Tenant '%s' has no fallback class type; synthesizing an unlimited '%s' "
                "so an unrecognized event title degrades instead of failing.",
                tenant_id, fallback,
            )

    return {
        "capacities": capacities,
        "labels": labels,
        "child_name_required": child_name_required,
        "fallback": fallback,
    }


def list_class_types(tenant_id: str = DEFAULT_TENANT_ID) -> list[dict]:
    """List a tenant's class types as raw rows, for the settings screen.

    Separate from load_class_types() because the screen needs one row per type
    with every column on it, while the scheduling engine needs lookup dicts and
    a guaranteed fallback. Feeding the screen from the bundle would show the
    synthetic fallback as if it were a saved row the owner could edit.

    Args:
        tenant_id (str): Tenant identifier.

    Returns:
        list[dict]: Rows ordered by marker, so the screen's order is stable.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT marker, label, capacity, requires_child_name, is_fallback, updated_at
                FROM class_types
                WHERE tenant_id = %s
                ORDER BY marker
                """,
                (tenant_id,),
            )
            rows: list[dict] = cur.fetchall()

    return [dict(row) for row in rows]


def create_class_type(
    marker: str,
    label: str,
    capacity: int | None,
    requires_child_name: bool,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> str:
    """Add a class type to a tenant.

    Never creates a fallback: the first type a tenant adds is not automatically
    the one that catches unmarked events, because that is a decision with
    consequences (its capacity applies to every mis-typed title). The owner
    picks it explicitly via set_fallback_class_type(), and until they do,
    load_class_types() covers the gap with a synthetic one.

    Args:
        marker (str): Canonical marker, already through normalize_marker().
        label (str): Human-readable label, with accents.
        capacity (int | None): Seats, or None for unlimited.
        requires_child_name (bool): Whether booking needs the child's name.
        tenant_id (str): Tenant identifier.

    Returns:
        str: "created", or "duplicate" if the tenant already has this marker.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO class_types
                    (tenant_id, marker, label, capacity, requires_child_name)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, marker) DO NOTHING
                """,
                (tenant_id, marker, label, capacity, requires_child_name),
            )
            created: bool = cur.rowcount > 0
            conn.commit()

    if created:
        logger.info("Class type '%s' created for tenant %s.", marker, tenant_id)
        return "created"

    logger.info("Class type '%s' already exists for tenant %s.", marker, tenant_id)
    return "duplicate"


def update_class_type(
    marker: str,
    label: str,
    capacity: int | None,
    requires_child_name: bool,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> bool:
    """Overwrite a class type's editable fields.

    The marker itself is not editable — it is the primary key, and it is also
    written by hand into every Calendar event title. Renaming it here would
    orphan every existing event of that class, which would then fall back
    silently. Changing a marker is delete + create, so the owner has to see the
    types disappear and knows the titles need editing too.

    Args:
        marker (str): Canonical marker identifying the row.
        label (str): New human-readable label.
        capacity (int | None): New seat count, or None for unlimited.
        requires_child_name (bool): Whether booking needs the child's name.
        tenant_id (str): Tenant identifier.

    Returns:
        bool: True if a row was updated, False if the tenant has no such marker.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE class_types
                SET label = %s,
                    capacity = %s,
                    requires_child_name = %s,
                    updated_at = NOW()
                WHERE tenant_id = %s AND marker = %s
                """,
                (label, capacity, requires_child_name, tenant_id, marker),
            )
            updated: bool = cur.rowcount > 0
            conn.commit()

    if updated:
        logger.info("Class type '%s' updated for tenant %s.", marker, tenant_id)
    else:
        logger.warning("No class type '%s' to update for tenant '%s'.", marker, tenant_id)

    return updated


def delete_class_type(marker: str, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    """Delete a class type, refusing to delete the tenant's fallback.

    The guard lives here rather than in the route because it protects the data,
    not the screen: deleting the flagged row would leave the tenant relying on a
    synthetic type nobody chose, and the owner would have no way to tell from
    the list why unmarked events started behaving differently. To remove it,
    flag another type as the fallback first.

    Deleting never touches trial_bookings: past bookings keep their class_type
    string, which is exactly the historical record wanted. Rendering falls back
    to the raw marker wherever a label is looked up (every call site uses
    ``labels.get(class_type, class_type)``).

    Args:
        marker (str): Canonical marker identifying the row.
        tenant_id (str): Tenant identifier.

    Returns:
        str: "deleted", "not_found", or "is_fallback" (nothing was deleted).
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT is_fallback FROM class_types WHERE tenant_id = %s AND marker = %s",
                (tenant_id, marker),
            )
            row: dict | None = cur.fetchone()

            if row is None:
                return "not_found"

            if row["is_fallback"]:
                logger.info(
                    "Refused to delete fallback class type '%s' for tenant %s.",
                    marker, tenant_id,
                )
                return "is_fallback"

            cur.execute(
                "DELETE FROM class_types WHERE tenant_id = %s AND marker = %s",
                (tenant_id, marker),
            )
            conn.commit()

    logger.info("Class type '%s' deleted for tenant %s.", marker, tenant_id)
    return "deleted"


def set_fallback_class_type(marker: str, tenant_id: str = DEFAULT_TENANT_ID) -> bool:
    """Make one class type the tenant's fallback, clearing whichever held it.

    Both writes happen in ONE transaction, and in this order, because the
    partial unique index allows at most one flagged row per tenant: setting
    before clearing would collide with the incumbent. If the marker does not
    exist the transaction is rolled back — otherwise the clear would have
    succeeded on its own and left the tenant with no fallback at all, which is
    strictly worse than the state it started in.

    Args:
        marker (str): Canonical marker of the type to flag.
        tenant_id (str): Tenant identifier.

    Returns:
        bool: True if the flag moved, False if the tenant has no such marker.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE class_types
                SET is_fallback = FALSE, updated_at = NOW()
                WHERE tenant_id = %s AND is_fallback
                """,
                (tenant_id,),
            )
            cur.execute(
                """
                UPDATE class_types
                SET is_fallback = TRUE, updated_at = NOW()
                WHERE tenant_id = %s AND marker = %s
                """,
                (tenant_id, marker),
            )
            flagged: bool = cur.rowcount > 0

            if not flagged:
                conn.rollback()
                logger.warning(
                    "No class type '%s' for tenant '%s'; fallback unchanged.",
                    marker, tenant_id,
                )
                return False

            conn.commit()

    logger.info("Class type '%s' is now the fallback for tenant %s.", marker, tenant_id)
    return True


def get_scheduling_config(tenant_id: str = DEFAULT_TENANT_ID) -> dict[str, Any]:
    """Load a tenant's scheduling knobs.

    Args:
        tenant_id (str): Tenant identifier.

    Returns:
        dict[str, Any]: {"tenant_id": str, "days_ahead": int}. A tenant with no
        row gets DEFAULT_DAYS_AHEAD rather than an error — the search horizon is
        not worth failing a conversation over.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tenant_id, days_ahead FROM scheduling_configs WHERE tenant_id = %s",
                (tenant_id,),
            )
            row: dict | None = cur.fetchone()

    if row is None:
        logger.warning(
            "No scheduling_configs row for tenant '%s'; using days_ahead=%d.",
            tenant_id, DEFAULT_DAYS_AHEAD,
        )
        return {"tenant_id": tenant_id, "days_ahead": DEFAULT_DAYS_AHEAD}

    return dict(row)


def update_days_ahead(days_ahead: int, tenant_id: str = DEFAULT_TENANT_ID) -> bool:
    """Set how many days ahead the engine looks for slots.

    Args:
        days_ahead (int): New horizon, already range-checked by the caller
            against MIN_DAYS_AHEAD/MAX_DAYS_AHEAD (the column's CHECK is the
            backstop, not the validation).
        tenant_id (str): Tenant identifier.

    Returns:
        bool: True if a row was updated, False if the tenant has no row (which
        would mean the migration seed never ran).
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE scheduling_configs
                SET days_ahead = %s, updated_at = NOW()
                WHERE tenant_id = %s
                """,
                (days_ahead, tenant_id),
            )
            updated: bool = cur.rowcount > 0
            conn.commit()

    if updated:
        logger.info("days_ahead set to %d for tenant %s.", days_ahead, tenant_id)
    else:
        logger.warning("No scheduling_configs row to update for tenant '%s'.", tenant_id)

    return updated
