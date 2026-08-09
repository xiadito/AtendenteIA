"""Funnel aggregates for the owner's metrics screen (Module S4).

READ-ONLY. Nothing here writes, and there is no metrics table: every number is
counted from rows the product already produces. That is deliberate — a stored
counter is a second record of a fact `messages` and `trial_bookings` already
hold, and the two drift (same reasoning that kept `accounts/onboarding.py`
stateless).

WHAT IS *NOT* MEASURED HERE, AND MUST NEVER BE (problem P1)
    Whether the lead actually SHOWED UP to the trial class. `trial_bookings`
    has three statuses — pending_confirmation, confirmed, cancelled — and none
    of them captures attendance. `confirmed` means THE OWNER SAID THE CLASS WILL
    HAPPEN; it is a decision taken days before the class, not an observation
    taken after it. Deriving an attendance or "show rate" number from it would
    be presenting a guess as a measurement to the one person who would act on
    it. Measuring attendance needs a new booking state and a moment of capture
    that does not exist in this product yet. Until it does, this module counts
    leads, bookings, confirmations and cancellations — and nothing else.

TENANT SCOPE. Every function takes `tenant_id` and filters on it. There is no
aggregate here without a tenant in its WHERE: this is the screen the owner looks
at every day, so a single unfiltered COUNT would show gym A the leads of gym B —
the most visible leak the schema allows.
"""

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from database.db import get_connection
from integrations.store import DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)

# The gym's wall clock, so a period boundary lands on local midnight rather than
# on 21:00 of the previous day.
#
# DELIBERATELY RE-DECLARED, not imported from bot/scheduling.py, which owns the
# same constant. That module imports integrations.google_calendar at module
# level, which pulls in googleapiclient — importing the constant from there
# would drag the whole Google client stack into a screen that only reads
# Postgres, and into the test suite that covers it. One duplicated line is the
# cheaper trade; if a third module ever needs it, promote it to config.py.
TIMEZONE: ZoneInfo = ZoneInfo("America/Sao_Paulo")

# Periods the screen offers. Anything else falls back to DEFAULT_PERIOD rather
# than being honoured, so a hand-typed ?period=3650 cannot turn the dashboard
# into a full-table scan.
ALLOWED_PERIODS: tuple[int, ...] = (7, 30, 90)

DEFAULT_PERIOD: int = 30


def parse_period(raw: str | None) -> int:
    """Turn an untrusted ?period= query-string value into an allowed window.

    Total function on purpose: the metrics route builds this from user input,
    and a period that raises would 500 the screen over a typo in the URL.
    Anything not in ALLOWED_PERIODS — None, "", "abc", "0", "-5", "365" —
    answers DEFAULT_PERIOD.

    Args:
        raw (str | None): The raw query-string value, or None when absent.

    Returns:
        int: One of ALLOWED_PERIODS.
    """
    try:
        days: int = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_PERIOD

    return days if days in ALLOWED_PERIODS else DEFAULT_PERIOD


def period_window(days: int) -> tuple[datetime, datetime]:
    """Build the [start, end) instants for a window of N calendar days.

    The window is N calendar days INCLUDING TODAY, in the gym's timezone: for
    days=7 it opens at 00:00 of six days ago and closes at this instant. Aligning
    the start to local midnight is what stops the number from creeping every time
    the owner refreshes.

    Both bounds are timezone-aware, and every column compared against them is
    TIMESTAMPTZ, so Postgres compares absolute instants and the offset is exact.
    A naive datetime here would be read as the SERVER's zone — UTC on Railway —
    and silently move the boundary three hours.

    Args:
        days (int): Length of the window in calendar days. Normally from
            parse_period().

    Returns:
        tuple[datetime, datetime]: (start, end), both aware. `start` is
        inclusive, `end` is exclusive.
    """
    end: datetime = datetime.now(TIMEZONE)
    start: datetime = (end - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return start, end


def _rate(numerator: int, denominator: int) -> float | None:
    """Express numerator/denominator as a percentage, or None when undefined.

    None rather than 0.0 for a zero denominator: a new gym with no leads has no
    conversion rate, which is a different statement from "its conversion rate is
    zero", and the screen renders the two differently ("—" vs "0%"). Returning
    0.0 would quietly tell an owner their funnel is failing when it simply has
    not started.

    Args:
        numerator (int): The count being measured.
        denominator (int): The count it is measured against.

    Returns:
        float | None: Percentage rounded to one decimal, or None when the
        denominator is zero. Never NaN or Infinity — the zero case is checked
        before the division, not after it.
    """
    if denominator <= 0:
        return None

    return round(numerator * 100 / denominator, 1)


def _count_new_leads(tenant_id: str, start: datetime, end: datetime) -> int:
    """Count leads whose FIRST contact with this gym falls inside the window.

    WHY THIS READS `messages` AND NOT `sessions.conversation_started_at`, which
    looks like the obvious column: that column is rewritten. The 1-hour
    inactivity timeout stamps it with NOW() (bot/handlers.py::_reset_session), so
    it means "when the CURRENT conversation started", and a lead from March who
    writes again today would be counted as a lead who arrived today. `sessions`
    has no created_at at all, so there is no stable arrival timestamp there.

    A lead's first message is stable — messages are only ever deleted by
    clear_session() — which makes MIN(created_at) a real first-contact instant.
    The GROUP BY is served by idx_messages_tenant_sender_created_at, which
    migration 011 already built as (tenant_id, sender, created_at).

    Only `author = 'lead'` counts. In practice the lead always speaks first, so
    this matches MIN over all authors today; being explicit keeps the number
    honest if an outbound-first flow ever exists.

    Args:
        tenant_id (str): The gym being measured.
        start (datetime): Inclusive start of the window, timezone-aware.
        end (datetime): Exclusive end of the window, timezone-aware.

    Returns:
        int: Number of distinct senders whose first message landed in the window.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS leads
                FROM (
                    SELECT sender
                    FROM messages
                    WHERE tenant_id = %s AND author = 'lead'
                    GROUP BY sender
                    HAVING MIN(created_at) >= %s AND MIN(created_at) < %s
                ) first_contacts
                """,
                (tenant_id, start, end),
            )
            return cur.fetchone()["leads"]


def _count_bookings(tenant_id: str, start: datetime, end: datetime) -> dict[str, int]:
    """Count one gym's bookings CREATED in the window, split by current status.

    ONE COHORT, ONE ANCHOR: `created_at`. All four numbers describe the same set
    of rows — the bookings this gym took in the window — and differ only in how
    that set is sliced by the status each row carries today. The arithmetic the
    owner can check by eye follows from that:

        booked == confirmed + cancelled + pending

    Anchoring the decisions on `updated_at` instead ("what did the owner decide
    this week?") is a legitimate question, but a DIFFERENT one, and mixing the
    two anchors in one table would allow more confirmations than bookings in the
    same period. That would be arithmetic nobody can trust.

    Args:
        tenant_id (str): The gym being measured.
        start (datetime): Inclusive start of the window, timezone-aware.
        end (datetime): Exclusive end of the window, timezone-aware.

    Returns:
        dict[str, int]: Keys "booked", "confirmed", "cancelled", "pending".
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*)                                                AS booked,
                    COUNT(*) FILTER (WHERE status = 'confirmed')            AS confirmed,
                    COUNT(*) FILTER (WHERE status = 'cancelled')            AS cancelled,
                    COUNT(*) FILTER (WHERE status = 'pending_confirmation') AS pending
                FROM trial_bookings
                WHERE tenant_id = %s AND created_at >= %s AND created_at < %s
                """,
                (tenant_id, start, end),
            )
            row = cur.fetchone()

    return {
        "booked": row["booked"],
        "confirmed": row["confirmed"],
        "cancelled": row["cancelled"],
        "pending": row["pending"],
    }


def get_funnel(
    tenant_id: str = DEFAULT_TENANT_ID,
    days: int = DEFAULT_PERIOD,
) -> dict[str, Any]:
    """Build one gym's whole funnel for a period: four counts and three rates.

    Two indexed reads, not five: the four booking numbers come out of a single
    SELECT with COUNT(*) FILTER.

    Note that lead_to_booking_rate CAN exceed 100%, and that is not a bug: a lead
    whose first contact predates the window may book inside it, so the two counts
    are not nested sets. The screen shows the real number and only clamps the
    BAR, never the figure.

    Args:
        tenant_id (str): The gym being measured. The dashboard passes
            current_user.tenant_id — never read from the session here, so this
            module stays usable outside a request (decision 14A).
        days (int): Window length in calendar days. Normally from parse_period().

    Returns:
        dict[str, Any]: period_days, period_start, period_end (aware datetimes),
        the counts leads/booked/confirmed/cancelled/pending, the percentages
        lead_to_booking_rate / booking_to_confirmed_rate /
        booking_to_cancelled_rate (float | None), and has_data.

        There is no attendance key here, and there must never be one — see the
        module docstring.
    """
    start, end = period_window(days)

    leads: int = _count_new_leads(tenant_id, start, end)
    counts: dict[str, int] = _count_bookings(tenant_id, start, end)

    funnel: dict[str, Any] = {
        "period_days": days,
        "period_start": start,
        "period_end": end,
        "leads": leads,
        "booked": counts["booked"],
        "confirmed": counts["confirmed"],
        "cancelled": counts["cancelled"],
        "pending": counts["pending"],
        "lead_to_booking_rate": _rate(counts["booked"], leads),
        "booking_to_confirmed_rate": _rate(counts["confirmed"], counts["booked"]),
        "booking_to_cancelled_rate": _rate(counts["cancelled"], counts["booked"]),
        "has_data": leads > 0 or counts["booked"] > 0,
    }

    # Counts only. The repository is public and this module sits next to whole
    # conversations — no sender, no name, ever.
    logger.info(
        "Funnel for tenant %s over %dd: %d lead(s), %d booking(s).",
        tenant_id, days, leads, counts["booked"],
    )
    return funnel


def get_funnel_summary(
    tenant_id: str = DEFAULT_TENANT_ID,
    days: int = DEFAULT_PERIOD,
) -> dict[str, Any]:
    """Trim the funnel down to the three numbers the menu shows (decision 19C).

    A THIN WRAPPER, NOT A SECOND SET OF QUERIES. "Lighter" here means fewer
    numbers on screen, not fewer reads: get_funnel() is already two indexed
    counts, and the menu route spends more than that in
    onboarding_steps.pending_count(). A hand-tuned summary query would be a
    second definition of the same numbers, free to drift from the screen the
    owner clicks through to — which is exactly the bug that makes a dashboard
    stop being believed.

    Args:
        tenant_id (str): The gym being measured.
        days (int): Window length in calendar days.

    Returns:
        dict[str, Any]: period_days, leads, booked, confirmed, has_data.
    """
    funnel: dict[str, Any] = get_funnel(tenant_id, days=days)

    return {
        "period_days": funnel["period_days"],
        "leads": funnel["leads"],
        "booked": funnel["booked"],
        "confirmed": funnel["confirmed"],
        "has_data": funnel["has_data"],
    }
