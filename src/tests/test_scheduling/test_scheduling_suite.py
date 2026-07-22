"""Automated end-to-end suite for the Module 2 scheduling engine.

Runs every scenario documented in SCHEDULING_ENGINE_TESTING.md and prints a
PASS/FAIL report. Unlike tests/test_scheduling/test_scheduling.py (a manual CLI that pokes
the engine one call at a time), this script drives the whole roteiro end to
end and boils it down to an exit code.

Fixtures are REUSED, not recreated: the suite reads the "Aulas Experimentais"
calendar and classifies whatever is already there (created by hand, or left
over from an earlier run) into the seven roles the roteiro needs. Only
genuinely missing roles are created, and teardown deletes only what this run
created — an event the owner made is never touched.

Bookings are different: every trial_bookings row the suite writes is deleted
in teardown, including rows on reused events.

Run from src/:
    python tests/test_scheduling/test_scheduling_suite.py
    python tests/test_scheduling/test_scheduling_suite.py --reset-bookings   # wipe old test bookings first
    python tests/test_scheduling/test_scheduling_suite.py --keep             # don't clean up (debugging)
    python tests/test_scheduling/test_scheduling_suite.py --skip-token-test
    python tests/test_scheduling/test_scheduling_suite.py --json               # tests/outputs/report_<data>.json
    python tests/test_scheduling/test_scheduling_suite.py --json /tmp/r.json   # caminho explícito

Exit code is 0 only when every test passed (skips don't fail the run).

WARNING: this writes to the real Google Calendar and the real Postgres
pointed at by DATABASE_URL. Do not point it at production.
"""
import argparse
import atexit
import json
import logging
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

# Lets this script import the app's packages (bot, config, integrations, ...)
# the same way app.py does. src/ is located by NAME instead of by counting
# parent directories: this file has already moved once (src/scripts/ →
# src/tests/test_scheduling/), and a hardcoded number of .parent hops breaks
# silently on the next move — the path stays valid, it just points elsewhere.
SRC_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "src")
sys.path.insert(0, str(SRC_DIR))

import integrations.store as store  # noqa: E402
from bot import bookings, scheduling  # noqa: E402
from bot.scheduling import (  # noqa: E402
    CLASS_CAPACITY,
    TIMEZONE,
    IntegrationNeedsReconnectError,
    IntegrationNotConnectedError,
    book_slot,
    get_available_slots,
)
from database.db import get_connection  # noqa: E402

TZ_NAME = "America/Sao_Paulo"

# Every fixture title carries this suffix so a human who spots one in the
# Calendar UI knows it is disposable. The class-type marker lives at the
# START of the title, so a suffix never affects _parse_class_type().
FIXTURE_SUFFIX = " ~ SUITE AUTOMATIZADA"

FIXTURE_DESCRIPTION = (
    "Evento temporário criado por tests/test_scheduling/test_scheduling_suite.py.\n"
    "Deve ser apagado automaticamente ao fim da suíte. Se sobrou, "
    "rode a suíte de novo ou apague à mão."
)

# Where the owners row is backed up before the revoked-token test mutates it.
# Kept outside the repo so a crashed run never leaves a token in git.
OWNERS_BACKUP_PATH = Path("/tmp/corujai_owners_backup.json")

MIGRATION_VERSION = "004_create_trial_bookings"

# Onde --json grava quando nenhum caminho é passado. Ancorado em SRC_DIR pelo
# mesmo motivo do sys.path acima: um caminho relativo aqui seria resolvido
# contra o CWD, então rodar a suíte de src/ escreveria em src/src/tests/.
DEFAULT_REPORT_DIR = SRC_DIR / "tests" / "outputs"

# Bogus refresh_token used by test 12. Google answers invalid_grant for it,
# which is the same error a genuinely revoked token produces.
BOGUS_REFRESH_TOKEN = "1//0-corujai-suite-invalid-refresh-token"


class SkipTest(Exception):
    """Raised by a test that cannot run in the current environment."""


# ---------------------------------------------------------------------------
# Report / console output
# ---------------------------------------------------------------------------

class Console:
    """Tiny ANSI helper that degrades to plain text when piped to a file."""

    def __init__(self, color: bool) -> None:
        self.color = color

    def paint(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def green(self, text: str) -> str:
        return self.paint(text, "32")

    def red(self, text: str) -> str:
        return self.paint(text, "31")

    def yellow(self, text: str) -> str:
        return self.paint(text, "33")

    def dim(self, text: str) -> str:
        return self.paint(text, "2")

    def bold(self, text: str) -> str:
        return self.paint(text, "1")


class Report:
    """Collects test outcomes and renders the final console report."""

    _SYMBOLS = {"PASS": "✔", "FAIL": "✖", "ERROR": "✖", "SKIP": "○"}

    def __init__(self, console: Console) -> None:
        self.console = console
        self.results: list[dict[str, Any]] = []
        self.started_at = time.monotonic()

    def section(self, title: str) -> None:
        print(f"\n{self.console.bold('▸ ' + title)}")

    def run(self, step: str, title: str, test: Callable[[], str | None]) -> bool:
        """Execute one test, record and print its outcome.

        Args:
            step (str): Step label from SCHEDULING_ENGINE_TESTING.md, e.g. "6".
            title (str): Short human description of what is being asserted.
            test (Callable): Zero-arg callable. Returns an optional detail
                string on success, raises AssertionError to fail, or raises
                SkipTest when the environment can't support it.

        Returns:
            bool: True when the test passed or was skipped.
        """
        started = time.monotonic()
        detail, trace, status = "", None, "PASS"

        try:
            detail = test() or ""
        except SkipTest as exc:
            status, detail = "SKIP", str(exc)
        except AssertionError as exc:
            status, detail = "FAIL", str(exc)
        except Exception as exc:  # noqa: BLE001 - any crash is a test failure
            status = "ERROR"
            detail = f"{type(exc).__name__}: {exc}"
            trace = traceback.format_exc()

        self.results.append({
            "step": step,
            "title": title,
            "status": status,
            "detail": detail,
            "traceback": trace,
            "seconds": round(time.monotonic() - started, 2),
        })
        self._print_line(step, title, status, detail)
        return status in ("PASS", "SKIP")

    def _print_line(self, step: str, title: str, status: str, detail: str) -> None:
        painter = {
            "PASS": self.console.green,
            "FAIL": self.console.red,
            "ERROR": self.console.red,
            "SKIP": self.console.yellow,
        }[status]
        symbol = painter(self._SYMBOLS[status])
        print(f"  {symbol} {step:>3}  {title}")
        if detail:
            print(f"         {self.console.dim(detail)}")

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [r for r in self.results if r["status"] in ("FAIL", "ERROR")]

    def summary(self) -> bool:
        """Print the closing summary. Returns True when nothing failed."""
        passed = sum(1 for r in self.results if r["status"] == "PASS")
        skipped = sum(1 for r in self.results if r["status"] == "SKIP")
        failed = len(self.failed)
        elapsed = time.monotonic() - self.started_at

        print("\n" + "─" * 72)
        parts = [
            f"{len(self.results)} testes",
            self.console.green(f"{passed} passaram"),
            self.console.red(f"{failed} falharam") if failed else "0 falharam",
            self.console.yellow(f"{skipped} pulados") if skipped else "0 pulados",
        ]
        print(f" {' · '.join(parts)}{self.console.dim(f'   ({elapsed:.1f}s)')}")
        print("─" * 72)

        for result in self.failed:
            print(f"\n{self.console.red('FALHOU')} passo {result['step']} — {result['title']}")
            print(f"  {result['detail']}")
            if result["traceback"]:
                print(self.console.dim("".join(f"  {line}" for line in
                                              result["traceback"].splitlines(keepends=True))))

        if not failed:
            print(self.console.green("\n Motor de agendamento OK — todos os testes passaram.\n"))
        return not failed

    def to_json(self, path: Path) -> None:
        path.write_text(json.dumps({
            "passed": sum(1 for r in self.results if r["status"] == "PASS"),
            "failed": len(self.failed),
            "skipped": sum(1 for r in self.results if r["status"] == "SKIP"),
            "results": self.results,
        }, indent=2, ensure_ascii=False), encoding="utf-8")


class WarningCapture(logging.Handler):
    """Captures bot.scheduling WARNING records so test 9 can assert on them.

    Lowering the LOGGER's level is the part that matters: main() configures
    the root at ERROR, and logger.warning() drops the call before building a
    LogRecord whenever the effective level is above WARNING. A handler alone
    would never see anything. propagate is switched off for the duration so
    the captured warning doesn't scribble over the report on stderr.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []
        self._logger = logging.getLogger("bot.scheduling")
        self._previous_level = logging.NOTSET
        self._previous_propagate = True

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())

    def __enter__(self) -> "WarningCapture":
        self.messages.clear()
        self._previous_level = self._logger.level
        self._previous_propagate = self._logger.propagate
        self._logger.setLevel(logging.WARNING)
        self._logger.propagate = False
        self._logger.addHandler(self)
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._logger.removeHandler(self)
        self._logger.setLevel(self._previous_level)
        self._logger.propagate = self._previous_propagate


# ---------------------------------------------------------------------------
# Small assertion helpers
# ---------------------------------------------------------------------------

def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def expect_equal(actual: Any, expected: Any, what: str) -> None:
    if actual != expected:
        raise AssertionError(f"{what}: esperado {expected!r}, veio {actual!r}")


def _sender(index: int) -> str:
    """Deterministic fake WhatsApp number for the suite's leads."""
    return f"5521000{index:06d}"


def _at(days: int, hour: int, minute: int) -> datetime:
    """Timezone-aware datetime N days from now, at the given local time."""
    base = datetime.now(TIMEZONE) + timedelta(days=days)
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _find_slot(slots: list[dict], event_id: str) -> dict | None:
    return next((slot for slot in slots if slot["event_id"] == event_id), None)


# ---------------------------------------------------------------------------
# Suite
# ---------------------------------------------------------------------------

class SchedulingSuite:
    """Owns fixtures, tests and teardown for the Module 2 engine."""

    def __init__(self, report: Report, keep: bool, token_test: bool,
                 reset_bookings: bool) -> None:
        self.report = report
        self.keep = keep
        self.token_test = token_test
        self.reset_bookings = reset_bookings
        self.service: Any = None
        self.calendar_id: str = ""
        self.events: dict[str, dict] = {}      # fixture key -> Calendar event resource
        self.instances: list[dict] = []        # expanded instances of the recurring series
        self.touched_event_ids: set[str] = set()  # every event_id that may own bookings
        self.created_event_ids: set[str] = set()  # subset this run created (safe to delete)
        self.reused_keys: list[str] = []
        self.original_descriptions: dict[str, str] = {}  # key -> description before any booking

    # -- prerequisites ------------------------------------------------------

    def check_integration(self) -> str:
        owner = store.get_owner_credentials()
        expect(owner is not None, "Nenhuma linha em owners para tenant_id='default'.")
        expect_equal(owner["integration_status"], "connected", "owners.integration_status")
        expect(bool(owner["calendar_id"]), "owners.calendar_id está vazio.")
        return f"conta {owner['google_email']} · calendar_id {owner['calendar_id'][:28]}…"

    def check_migration(self) -> str:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT applied_at FROM schema_migrations WHERE version = %s",
                    (MIGRATION_VERSION,),
                )
                row = cur.fetchone()

        expect(row is not None, f"Migration {MIGRATION_VERSION} não aplicada — suba o app uma vez.")
        return f"aplicada em {row['applied_at']:%d/%m/%Y %H:%M}"

    # -- fixtures -----------------------------------------------------------

    def connect(self) -> None:
        """Build the same authenticated client the engine uses."""
        self.service, self.calendar_id = scheduling._get_service_or_raise()

    def _classify(self, event: dict, now: datetime) -> str | None:
        """Map an existing Calendar event onto one of the roteiro's fixture roles.

        Matching is by SHAPE (recurring? all-day? past? which title marker?)
        rather than by exact title, so events created by hand, by the Apps
        Script, or by a previous run of this suite all classify the same way
        regardless of wording, accents or suffixes.

        Args:
            event (dict): Event resource from events().list().
            now (datetime): Timezone-aware reference for "past".

        Returns:
            str | None: Fixture key, or None when the event plays no role.
        """
        # singleEvents=False does NOT guarantee only series masters: Google
        # also returns instances that carry an exception (a moved time, an
        # extendedProperty written by the Apps Script...). Those must never
        # claim a role — the master already covers "recorrente", and letting
        # an instance take, say, "adultos" would make two tests fight over
        # the same event_id. recurringEventId is what tells them apart.
        if event.get("recurringEventId"):
            return None

        if event.get("recurrence"):
            return "recorrente"

        start = event.get("start", {})
        if not start.get("dateTime"):
            return "dia_inteiro"

        starts_at = datetime.fromisoformat(start["dateTime"]).astimezone(TIMEZONE)
        summary = event.get("summary") or ""
        match = scheduling._TITLE_MARKER_PATTERN.match(summary)
        marker = scheduling._strip_accents(match.group(1)).upper() if match else None

        if starts_at < now:
            # Only a past event with a *recognized* marker is the "aula passada"
            # fixture; anything else in the past is the owner's own history.
            return "passada" if marker in CLASS_CAPACITY else None

        if marker not in CLASS_CAPACITY:
            return "sem_marcador"  # unrecognized or absent → engine falls back to ADULTOS

        return {"BABY": "baby", "CRIANCAS": "criancas", "ADULTOS": "adultos"}[marker]

    def _insert(self, key: str, summary: str, start: datetime, end: datetime,
                **extra: Any) -> dict:
        body = {
            "summary": summary + FIXTURE_SUFFIX,
            "description": FIXTURE_DESCRIPTION,
            "start": {"dateTime": start.isoformat(), "timeZone": TZ_NAME},
            "end": {"dateTime": end.isoformat(), "timeZone": TZ_NAME},
        }
        body.update(extra)
        event = self.service.events().insert(calendarId=self.calendar_id, body=body).execute()
        self._register(key, event, created=True)
        return event

    def _register(self, key: str, event: dict, created: bool) -> None:
        self.events[key] = event
        self.touched_event_ids.add(event["id"])
        self.original_descriptions[key] = event.get("description") or ""
        if created:
            self.created_event_ids.add(event["id"])
        else:
            self.reused_keys.append(key)

    def _create_missing(self, key: str) -> None:
        """Create the one fixture the calendar doesn't already provide."""
        if key == "baby":
            # Capacity 2 — drives the full/race tests (steps 3-6).
            self._insert(key, "[BABY] Aula Experimental", _at(2, 9, 0), _at(2, 9, 30))
        elif key == "criancas":
            # Capacity 4 — drives the duplicate-sender test (step 7).
            self._insert(key, "[CRIANCAS] Aula Experimental", _at(3, 16, 0), _at(3, 16, 45))
        elif key == "adultos":
            # Capacity None (unlimited) — step 8.
            self._insert(key, "[ADULTOS] Aula Experimental", _at(4, 19, 0), _at(4, 20, 0))
        elif key == "sem_marcador":
            # Must NOT start with "[...]" or the fallback never fires (step 9).
            self._insert(key, "Horário sem marcador", _at(5, 18, 0), _at(5, 18, 30))
        elif key == "dia_inteiro":
            # Valid marker on purpose: proves the engine skips it for lacking
            # start.dateTime, not because of the title (step 10).
            day = _at(6, 0, 0).date()
            event = self.service.events().insert(calendarId=self.calendar_id, body={
                "summary": "[ADULTOS] Aula Dia Inteiro" + FIXTURE_SUFFIX,
                "description": FIXTURE_DESCRIPTION,
                "start": {"date": day.isoformat()},
                "end": {"date": (day + timedelta(days=1)).isoformat()},
            }).execute()
            self._register(key, event, created=True)
        elif key == "recorrente":
            # singleEvents=True expands this into one bookable instance per
            # week, each with its own id (step 11).
            self._insert(key, "[ADULTOS] Aula Recorrente", _at(1, 7, 0), _at(1, 8, 0),
                         recurrence=["RRULE:FREQ=WEEKLY;COUNT=4"])
        elif key == "passada":
            # Before timeMin — must never be listed (step 10).
            self._insert(key, "[BABY] Aula Passada", _at(-2, 9, 0), _at(-2, 9, 30))

    def prepare_fixtures(self) -> str:
        """Reuse the events already in the calendar; create only what's missing."""
        now = datetime.now(TIMEZONE)

        # singleEvents=False keeps a recurring series collapsed into its master,
        # which is what "recorrente" needs — the instances are expanded later,
        # in test 11, exactly the way the engine does it.
        response = self.service.events().list(
            calendarId=self.calendar_id,
            timeMin=(now - timedelta(days=7)).isoformat(),
            timeMax=(now + timedelta(days=30)).isoformat(),
            singleEvents=False,
            maxResults=250,
        ).execute()

        candidates = [event for event in response.get("items", [])
                      if event.get("status") != "cancelled"]
        # Earliest first, so a role claimed twice keeps the soonest event.
        candidates.sort(key=lambda e: (e.get("start", {}).get("dateTime")
                                       or e.get("start", {}).get("date") or ""))

        for event in candidates:
            key = self._classify(event, now)
            if key and key not in self.events:
                self._register(key, event, created=False)

        required = ["baby", "criancas", "adultos", "sem_marcador",
                    "dia_inteiro", "recorrente", "passada"]
        for key in required:
            if key not in self.events:
                self._create_missing(key)

        # Register the series' instances up front: they own bookings of their
        # own, so both the clean-ledger pre-check and teardown must see them
        # even though test 11 is what expands them for real.
        try:
            series = self.service.events().instances(
                calendarId=self.calendar_id, eventId=self.events["recorrente"]["id"],
                maxResults=20,
            ).execute()
            for item in series.get("items", []):
                if item.get("status") != "cancelled":
                    self.touched_event_ids.add(item["id"])
        except Exception as exc:  # noqa: BLE001 - test 11 reports this properly
            print(f"    aviso: não consegui expandir a série recorrente agora: {exc}")

        reused = len(self.reused_keys)
        created = len(self.created_event_ids)
        detail = f"{reused} evento(s) reaproveitados"
        if created:
            missing = [k for k in required if self.events[k]["id"] in self.created_event_ids]
            detail += f" · {created} criado(s) por estarem faltando: {', '.join(missing)}"
        return detail

    def check_existing_bookings(self) -> str:
        """Reused events must start with a clean ledger or the counts won't match."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT calendar_event_id, COUNT(*) AS total
                    FROM trial_bookings
                    WHERE calendar_event_id = ANY(%s) AND status != 'cancelled'
                    GROUP BY calendar_event_id
                    """,
                    (list(self.touched_event_ids),),
                )
                rows = cur.fetchall()

        if not rows:
            return "nenhuma reserva prévia nos eventos reaproveitados"

        if not self.reset_bookings:
            detalhes = ", ".join(f"{row['calendar_event_id'][:20]}…={row['total']}" for row in rows)
            raise AssertionError(
                f"{len(rows)} evento(s) já têm reservas ativas ({detalhes}). As contagens "
                "esperadas pelo roteiro não batem com um ledger sujo. Rode de novo com "
                "--reset-bookings para apagá-las, ou limpe à mão em trial_bookings.")

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM trial_bookings WHERE calendar_event_id = ANY(%s)",
                    (list(self.touched_event_ids),),
                )
                deleted = cur.rowcount
            conn.commit()

        return f"--reset-bookings: {deleted} reserva(s) antiga(s) apagadas"

    # -- tests --------------------------------------------------------------

    def test_01_empty_window(self) -> str:
        """Step 1: an empty result set yields no slots, without raising.

        The doc's step 1 asks for this against a genuinely empty calendar —
        impossible to reproduce here since prepare_fixtures() has already
        populated the calendar by the time any test runs (the fixtures'
        event_ids are needed by every other test). A days_ahead=1 window was
        tried first, but the "recorrente" fixture starts tomorrow 07:00,
        which lands inside a 24h window depending on what time this suite
        runs — a flaky false SKIP, blaming "eventos manuais do dono" for
        what was actually this suite's own fixture.

        A zero-width window sidesteps the whole problem: get_available_slots()
        computes timeMin and timeMax from the SAME `now` (scheduling.py:151),
        so days_ahead=0 makes timeMax == timeMin with no extra plumbing.
        Google always returns items: [] for that, regardless of anything on
        the calendar, real or fixture — the exact code path the doc's step 1
        describes, exercised deterministically.
        """
        slots = get_available_slots(days_ahead=0)
        expect_equal(slots, [], "vagas retornadas com timeMin == timeMax")
        return "janela de largura zero (timeMin == timeMax) → [] sem erro, determinístico"

    def test_02_three_types(self) -> str:
        """Step 2: each class type reports the right remaining seats."""
        slots = get_available_slots(days_ahead=14)
        expectations = {
            "baby": ("BABY", 2),
            "criancas": ("CRIANCAS", 4),
            "adultos": ("ADULTOS", None),
            "sem_marcador": ("ADULTOS", None),  # fallback
        }

        for key, (class_type, remaining) in expectations.items():
            event_id = self.events[key]["id"]
            slot = _find_slot(slots, event_id)
            expect(slot is not None, f"fixture '{key}' não apareceu em get_available_slots()")
            expect_equal(slot["class_type"], class_type, f"class_type de '{key}'")
            expect_equal(slot["remaining_slots"], remaining, f"vagas restantes de '{key}'")

        return "baby=2 · crianças=4 · adultos=ilimitado · sem marcador→adultos ilimitado"

    def test_03_first_booking_keeps_slot(self) -> str:
        """Step 3: one booking in the baby class leaves 1 seat, slot still listed."""
        event_id = self.events["baby"]["id"]
        result = book_slot(event_id, {"sender": _sender(1), "name": "Ana"})

        expect_equal(result["status"], "created", "status do 1º book na baby class")
        expect_equal(result.get("calendar_synced"), True, "calendar_synced")

        slot = _find_slot(get_available_slots(days_ahead=14), event_id)
        expect(slot is not None, "slot [BABY] sumiu da lista com apenas 1/2 ocupado")
        expect_equal(slot["remaining_slots"], 1, "vagas restantes após 1 reserva")

        row = bookings.get_booking(result["booking_id"])
        expect(row is not None, "reserva não encontrada no Postgres")
        expect_equal(row["status"], "pending_confirmation", "status da reserva no banco")
        expect_equal(row["class_type"], "BABY", "class_type gravado")

        return "reserva criada, sincronizada no Calendar, slot segue com 1 vaga"

    def test_04_second_booking_hides_slot(self) -> str:
        """Step 4: the second booking fills the baby class, slot disappears."""
        event_id = self.events["baby"]["id"]
        result = book_slot(event_id, {"sender": _sender(2), "name": "Beto"})

        expect_equal(result["status"], "created", "status do 2º book na baby class")
        expect_equal(bookings.count_active_bookings(event_id), 2, "reservas ativas")

        slot = _find_slot(get_available_slots(days_ahead=14), event_id)
        expect(slot is None, "slot [BABY] continua listado mesmo com 2/2 ocupado")

        return "2/2 ocupado → slot removido da lista"

    def test_05_third_booking_rejected(self) -> str:
        """Step 5: a third lead is refused, and nothing is written."""
        event_id = self.events["baby"]["id"]
        result = book_slot(event_id, {"sender": _sender(3), "name": "Carla"})

        expect_equal(result["status"], "full", "status do 3º book")
        expect_equal(result.get("active_count"), 2, "active_count na recusa")
        expect_equal(bookings.count_active_bookings(event_id), 2, "reservas ativas após recusa")

        return "recusado com status='full', contagem intacta em 2"

    def test_06_race_on_last_seat(self) -> str:
        """Step 6: two concurrent bookings can never both take the last seat."""
        event_id = self.events["baby"]["id"]

        # Reopen exactly one seat, the way the doc's SQL does.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE trial_bookings SET status = 'cancelled', updated_at = NOW()
                    WHERE calendar_event_id = %s AND sender = %s
                    """,
                    (event_id, _sender(2)),
                )
            conn.commit()

        expect_equal(bookings.count_active_bookings(event_id), 1, "vagas ativas após reabrir 1")

        # A barrier lines the threads up as closely as the runtime allows; the
        # advisory lock is what actually guarantees correctness, so this only
        # needs to make the collision *likely*, not certain.
        barrier = threading.Barrier(2)
        outcomes: list[dict] = []
        lock = threading.Lock()

        def racer(index: int) -> None:
            barrier.wait()
            result = book_slot(event_id, {"sender": _sender(10 + index), "name": f"Racer{index}"})
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=racer, args=(i,)) for i in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        statuses = sorted(result["status"] for result in outcomes)
        expect_equal(statuses, ["created", "full"], "resultados das duas chamadas concorrentes")
        expect_equal(bookings.count_active_bookings(event_id), 2, "reservas ativas após a corrida")

        return "1 created + 1 full · contagem final 2, nunca 3"

    def test_07_duplicate_sender(self) -> str:
        """Step 7: the same sender cannot book the same slot twice."""
        event_id = self.events["criancas"]["id"]
        lead = {"sender": _sender(1), "name": "Ana"}

        first = book_slot(event_id, lead)
        expect_equal(first["status"], "created", "1ª reserva na aula de crianças")

        second = book_slot(event_id, lead)
        expect_equal(second["status"], "duplicate", "2ª reserva do mesmo sender")
        expect_equal(bookings.count_active_bookings(event_id), 1, "reservas ativas")

        return "2ª tentativa recusada com status='duplicate' pela UNIQUE(event_id, sender)"

    def test_08_adults_never_fill(self) -> str:
        """Step 8: an unlimited class stays open no matter how many book it."""
        event_id = self.events["adultos"]["id"]

        for index in range(20, 25):
            result = book_slot(event_id, {"sender": _sender(index), "name": f"Lead{index}"})
            expect_equal(result["status"], "created", f"reserva #{index - 19} em [ADULTOS]")

        expect_equal(bookings.count_active_bookings(event_id), 5, "reservas ativas em [ADULTOS]")

        slot = _find_slot(get_available_slots(days_ahead=14), event_id)
        expect(slot is not None, "slot [ADULTOS] sumiu da lista — capacidade deveria ser ilimitada")
        expect_equal(slot["remaining_slots"], None, "vagas restantes (None = ilimitado)")
        expect_equal(CLASS_CAPACITY["ADULTOS"], None, "CLASS_CAPACITY['ADULTOS']")

        return "5 reservas · slot segue listado como ilimitado"

    def test_09_unknown_marker_warns(self) -> str:
        """Step 9: an unrecognized title falls back to ADULTOS and logs a warning."""
        event = self.events["sem_marcador"]
        event_id = event["id"]
        # The title comes from whatever event plays this role in the calendar,
        # so the warning is matched against the real summary, never a literal.
        summary = event.get("summary") or ""

        with WarningCapture() as capture:
            slots = get_available_slots(days_ahead=14)

        slot = _find_slot(slots, event_id)
        expect(slot is not None, f"slot sem marcador ('{summary}') não apareceu na lista")
        expect_equal(slot["class_type"], "ADULTOS", "class_type do título sem marcador")
        expect_equal(slot["remaining_slots"], None, "vagas restantes do título sem marcador")
        expect("Adultos" in slot["label"], f"label não diz 'Adultos': {slot['label']!r}")

        matching = [msg for msg in capture.messages
                    if "Unrecognized class marker" in msg and summary in msg]
        expect(bool(matching),
               f"nenhum WARNING 'Unrecognized class marker' para '{summary}'; "
               f"warnings capturados: {capture.messages or 'nenhum'}")

        return f"{slot['label']} | vagas: ilimitado\n         log: {matching[0]}"

    def test_10_all_day_and_past_ignored(self) -> str:
        """Step 10: all-day events and past events never appear as slots."""
        slots = get_available_slots(days_ahead=14)

        all_day_id = self.events["dia_inteiro"]["id"]
        past_id = self.events["passada"]["id"]

        expect(_find_slot(slots, all_day_id) is None,
               "evento de dia inteiro apareceu na lista (deveria ser ignorado)")
        expect(_find_slot(slots, past_id) is None,
               "evento no passado apareceu na lista")

        return "dia inteiro e evento passado ausentes da lista"

    def test_11_recurrence_expands(self) -> str:
        """Step 11: a weekly series expands into independent instances."""
        series_id = self.events["recorrente"]["id"]
        now = datetime.now(TIMEZONE)

        response = self.service.events().instances(
            calendarId=self.calendar_id, eventId=series_id, maxResults=20,
        ).execute()

        # instances() does not promise chronological order — this calendar
        # returns 06/08, 13/08, 30/07, 23/07 — so "1ª" and "2ª" instância only
        # mean anything after an explicit sort.
        self.instances = sorted(
            (item for item in response.get("items", [])
             if item.get("status") != "cancelled"
             and item.get("start", {}).get("dateTime")
             and now <= datetime.fromisoformat(item["start"]["dateTime"]).astimezone(TIMEZONE)
             <= now + timedelta(days=14)),
            key=lambda item: item["start"]["dateTime"],
        )
        for instance in self.instances:
            self.touched_event_ids.add(instance["id"])

        expect(len(self.instances) >= 2,
               f"a série gerou {len(self.instances)} instância(s) na janela de 14 dias; "
               "esperado ao menos 2")

        ids = [instance["id"] for instance in self.instances]
        expect_equal(len(set(ids)), len(ids), "instâncias com event_id distintos")

        slots = get_available_slots(days_ahead=14)
        listed = [i for i in ids if _find_slot(slots, i) is not None]
        expect_equal(len(listed), len(ids), "instâncias visíveis em get_available_slots()")

        # Booking one instance must not touch the others: capacity is per
        # instance, tracked by that instance's own event_id.
        first, second = ids[0], ids[1]
        result = book_slot(first, {"sender": _sender(30), "name": "Recorrente1"})
        expect_equal(result["status"], "created", "reserva na 1ª instância")
        expect_equal(bookings.count_active_bookings(first), 1, "reservas na 1ª instância")
        expect_equal(bookings.count_active_bookings(second), 0, "reservas na 2ª instância")

        return f"{len(ids)} instâncias distintas · reserva isolada por instância"

    def test_12_revoked_token(self) -> str:
        """Step 12: a rejected refresh_token surfaces cleanly and flags reconnect."""
        if not self.token_test:
            raise SkipTest("desativado por --skip-token-test")

        owner = store.get_owner_credentials()
        OWNERS_BACKUP_PATH.write_text(json.dumps(owner), encoding="utf-8")

        try:
            self._write_owner(refresh_token=BOGUS_REFRESH_TOKEN, integration_status="connected")

            try:
                get_available_slots(days_ahead=14)
            except IntegrationNeedsReconnectError:
                pass
            else:
                raise AssertionError(
                    "get_available_slots() não levantou IntegrationNeedsReconnectError "
                    "com um refresh_token inválido")

            status = store.get_owner_credentials()["integration_status"]
            expect_equal(status, "needs_reconnect", "owners.integration_status após invalid_grant")
        finally:
            self._write_owner(
                refresh_token=owner["refresh_token"],
                integration_status=owner["integration_status"],
            )
            OWNERS_BACKUP_PATH.unlink(missing_ok=True)

        restored = store.get_owner_credentials()["integration_status"]
        expect_equal(restored, "connected", "integration_status restaurado após o teste")

        return "invalid_grant → IntegrationNeedsReconnectError + needs_reconnect (token restaurado)"

    def test_13_disconnected(self) -> str:
        """Step 13: a disconnected integration fails without calling Google."""
        owner = store.get_owner_credentials()
        google_was_called = threading.Event()

        def spy(*_args: Any, **_kwargs: Any) -> Any:
            google_was_called.set()
            raise AssertionError("get_calendar_service() foi chamado com a integração desconectada")

        # scheduling imports get_calendar_service by name, so the patch has to
        # land on bot.scheduling's own reference, not on the source module.
        original = scheduling.get_calendar_service
        scheduling.get_calendar_service = spy

        try:
            self._write_owner(refresh_token=owner["refresh_token"],
                              integration_status="disconnected")

            try:
                get_available_slots(days_ahead=14)
            except IntegrationNotConnectedError:
                pass
            else:
                raise AssertionError(
                    "get_available_slots() não levantou IntegrationNotConnectedError")

            expect(not google_was_called.is_set(),
                   "a API do Google foi chamada mesmo com integration_status='disconnected'")

            result = book_slot(self.events["adultos"]["id"], {"sender": _sender(40), "name": "X"})
            expect_equal(result["status"], "integration_not_connected", "status de book_slot()")
        finally:
            scheduling.get_calendar_service = original
            self._write_owner(refresh_token=owner["refresh_token"],
                              integration_status=owner["integration_status"])

        return "erro limpo em list e book · nenhuma chamada à API do Google"

    def test_14_calendar_description_patched(self) -> str:
        """Extra: the Calendar event carries the booking ledger after a booking."""
        event_id = self.events["criancas"]["id"]
        event = self.service.events().get(
            calendarId=self.calendar_id, eventId=event_id).execute()

        description = event.get("description") or ""
        original = self.original_descriptions["criancas"]

        expect(scheduling.BOOKING_SECTION_MARKER in description,
               f"seção '{scheduling.BOOKING_SECTION_MARKER}' ausente da descrição do evento")
        expect(description.startswith(original),
               "a descrição original do dono foi sobrescrita em vez de receber append")
        expect(_sender(1) in description, "o telefone do lead não aparece na seção de reservas")
        expect("Ana" in description, "o nome do lead não aparece na seção de reservas")

        booked_count = (event.get("extendedProperties", {})
                        .get("private", {})
                        .get("corujai_booked_count"))
        expect_equal(booked_count, "1", "extendedProperties.private.corujai_booked_count")

        return "descrição preservada + append sob o marcador · corujai_booked_count=1"

    # -- helpers ------------------------------------------------------------

    def _write_owner(self, refresh_token: str, integration_status: str) -> None:
        """Directly set the owners row. Only used to stage failure scenarios."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE owners
                    SET refresh_token = %s, integration_status = %s, updated_at = NOW()
                    WHERE tenant_id = %s
                    """,
                    (refresh_token, integration_status, store.DEFAULT_TENANT_ID),
                )
            conn.commit()

    # -- teardown -----------------------------------------------------------

    def teardown(self) -> None:
        """Undo everything this run did, without touching the owner's own data.

        Three different reversals, because the suite leaves three kinds of
        trace: events it created (deleted), events it reused (description and
        booked-count restored to what they were), and trial_bookings rows
        (always deleted, on created and reused events alike).
        """
        if self.keep:
            print(f"\n{self.report.console.yellow('--keep: nada foi desfeito.')}")
            for key, event in self.events.items():
                origem = "criado" if event["id"] in self.created_event_ids else "reaproveitado"
                print(f"    {key:14} {origem:14} {event['id']}")
            return

        deleted_events, restored = 0, 0
        for key, event in self.events.items():
            try:
                if event["id"] in self.created_event_ids:
                    self.service.events().delete(
                        calendarId=self.calendar_id, eventId=event["id"]).execute()
                    deleted_events += 1
                elif key in ("baby", "criancas", "adultos", "recorrente"):
                    # Only bookable roles ever got patched by book_slot().
                    self.service.events().patch(
                        calendarId=self.calendar_id, eventId=event["id"],
                        body={
                            "description": self.original_descriptions.get(key, ""),
                            "extendedProperties": {"private": {"corujai_booked_count": None}},
                        },
                    ).execute()
                    restored += 1
            except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
                print(f"    aviso: falha ao limpar o fixture '{key}': {exc}")

        deleted_rows = 0
        if self.touched_event_ids:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM trial_bookings WHERE calendar_event_id = ANY(%s)",
                        (list(self.touched_event_ids),),
                    )
                    deleted_rows = cur.rowcount
                conn.commit()

        print(f"\n  limpeza: {deleted_events} evento(s) criados apagados · "
              f"{restored} evento(s) reaproveitados restaurados · "
              f"{deleted_rows} reserva(s) removidas de trial_bookings")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _resolve_report_path(path: Path) -> Path:
    """Turn whatever --json received into a path write_text() can actually use.

    Accepts either a file ("relatorio.json") or a directory ("outputs/", or the
    default), filling in a timestamped filename for the latter so runs never
    overwrite each other. Missing parents are created here because write_text()
    doesn't create them — it just raises after the whole suite has run.

    Args:
        path (Path): Raw value from --json, absolute or relative to the CWD.

    Returns:
        Path: An existing directory plus a .json filename.
    """
    if path.is_dir() or path.suffix.lower() != ".json":
        path = path / f"report_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _restore_owner_backup() -> None:
    """Put the owners row back if a previous run died mid-token-test."""
    if not OWNERS_BACKUP_PATH.exists():
        return

    owner = json.loads(OWNERS_BACKUP_PATH.read_text(encoding="utf-8"))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE owners SET refresh_token = %s, integration_status = %s, updated_at = NOW()
                WHERE tenant_id = %s
                """,
                (owner["refresh_token"], owner["integration_status"], owner["tenant_id"]),
            )
        conn.commit()

    OWNERS_BACKUP_PATH.unlink(missing_ok=True)
    print(f"  credenciais do owner restauradas a partir de {OWNERS_BACKUP_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Suíte automatizada do motor de agendamento (SCHEDULING_ENGINE_TESTING.md).")
    parser.add_argument("--keep", action="store_true",
                        help="Não desfaz nada ao final (para depurar à mão).")
    parser.add_argument("--reset-bookings", action="store_true",
                        help="Apaga reservas pré-existentes nos eventos reaproveitados.")
    parser.add_argument("--skip-token-test", action="store_true",
                        help="Pula o passo 12, que troca o refresh_token temporariamente.")
    parser.add_argument("--no-color", action="store_true", help="Saída sem cores ANSI.")
    parser.add_argument("--json", nargs="?", type=Path, const=DEFAULT_REPORT_DIR,
                        default=None, metavar="ARQUIVO",
                        help="Também grava o relatório em JSON. Sozinha, grava em "
                             "tests/outputs/ com nome datado; com um valor, usa o "
                             "caminho dado (arquivo .json ou diretório).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.ERROR, format="%(levelname)s - %(message)s")

    console = Console(color=not args.no_color and sys.stdout.isatty())
    report = Report(console)

    print(console.bold("\n═══ Corujai · Módulo 2 — suíte do motor de agendamento ═══"))
    print(console.dim(" Roteiro: SCHEDULING_ENGINE_TESTING.md"))

    # A crashed previous run may have left a bogus token in owners.
    _restore_owner_backup()

    report.section("Pré-requisitos")
    suite = SchedulingSuite(report, keep=args.keep, token_test=not args.skip_token_test,
                            reset_bookings=args.reset_bookings)

    ok = report.run("P1", "Integração Google Calendar conectada", suite.check_integration)
    ok &= report.run("P2", f"Migration {MIGRATION_VERSION} aplicada", suite.check_migration)

    if not ok:
        print(console.red("\n Pré-requisitos falharam — a suíte não pode continuar."))
        report.summary()
        sys.exit(1)

    report.section("Preparo do calendário")
    suite.connect()
    atexit.register(suite.teardown)

    ok = report.run("F1", 'Fixtures localizados em "Aulas Experimentais"', suite.prepare_fixtures)
    ok &= report.run("F2", "Ledger de reservas limpo para os eventos usados",
                     suite.check_existing_bookings)

    if not ok:
        report.section("Limpeza")
        atexit.unregister(suite.teardown)
        suite.teardown()
        report.summary()
        sys.exit(1)

    report.section("Roteiro de testes")
    tests: list[tuple[str, str, Callable[[], str | None]]] = [
        ("1", "Janela vazia → nenhuma vaga, sem erro", suite.test_01_empty_window),
        ("2", "Três tipos de aula + fallback, com vagas corretas", suite.test_02_three_types),
        ("3", "1ª reserva na baby class → slot continua com 1 vaga", suite.test_03_first_booking_keeps_slot),
        ("4", "2ª reserva lota a baby class → slot some da lista", suite.test_04_second_booking_hides_slot),
        ("5", "3ª reserva é recusada por lotação", suite.test_05_third_booking_rejected),
        ("6", "Corrida na última vaga → só uma reserva vence", suite.test_06_race_on_last_seat),
        ("7", "Mesmo sender no mesmo slot → duplicate", suite.test_07_duplicate_sender),
        ("8", "Aula de adultos nunca lota", suite.test_08_adults_never_fill),
        ("9", "Título sem marcador → adultos + WARNING", suite.test_09_unknown_marker_warns),
        ("10", "Dia inteiro e evento passado são ignorados", suite.test_10_all_day_and_past_ignored),
        ("11", "Recorrência expande em instâncias independentes", suite.test_11_recurrence_expands),
        ("12", "Token recusado → needs_reconnect, sem traceback", suite.test_12_revoked_token),
        ("13", "Integração desconectada → falha limpa", suite.test_13_disconnected),
        ("14", "Descrição do evento recebe a seção de reservas", suite.test_14_calendar_description_patched),
    ]

    for step, title, test in tests:
        report.run(step, title, test)

    atexit.unregister(suite.teardown)
    report.section("Limpeza")
    suite.teardown()

    if args.json is not None:
        report_path = _resolve_report_path(args.json)
        report.to_json(report_path)
        print(f"  relatório JSON: {report_path}")

    sys.exit(0 if report.summary() else 1)


if __name__ == "__main__":
    main()
