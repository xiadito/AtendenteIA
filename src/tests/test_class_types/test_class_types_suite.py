"""Automated end-to-end suite for per-tenant class types (Module S2).

Runs the scenarios documented in CLASS_TYPES_TESTING.md and prints a PASS/FAIL
report boiled down to an exit code, in the same style as
tests/test_settings/test_settings_suite.py.

Fully deterministic: no LLM, no WhatsApp, no real Google Calendar. The Calendar
service is replaced by a fake that returns fixture events, which is what makes
the scheduling assertions (capacity, fallback, child-name flag) testable without
writing to anyone's agenda.

DESTRUCTIVE, AND PARTLY ON THE PILOT'S OWN ROWS. The settings screen always
writes to tenant 'default' (multi-tenant routing is Module S3), so the screen
tests overwrite the pilot's class_types and scheduling_configs rows. Both tables
are snapshotted to BACKUP_PATH before the first write and restored in teardown;
a run that dies mid-suite is repaired at the start of the next one. Never remove
that backup.

Everything that does NOT need the screen runs against fixture tenants
(SUITE_TENANT*), which teardown deletes outright.

Run from src/:
    python tests/test_class_types/test_class_types_suite.py
    python tests/test_class_types/test_class_types_suite.py --keep
    python tests/test_class_types/test_class_types_suite.py --json

Exit code is 0 only when every test passed.
"""
import argparse
import atexit
import contextlib
import json
import logging
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

# Locate src/ by NAME, like app.py and the other suites.
SRC_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "src")
sys.path.insert(0, str(SRC_DIR))

import accounts.users as accounts_users  # noqa: E402
import bot.class_types as class_types  # noqa: E402
import bot.scheduling as scheduling  # noqa: E402
import integrations.store as store  # noqa: E402
import jobs.drain_notifications as drain  # noqa: E402
from database.db import get_connection  # noqa: E402

# Fixture tenants. Own prefix so teardown can scope its DELETEs and never touch
# the pilot's rows or another suite's data.
TENANT_PREFIX = "suite_ct_"
SUITE_TENANT = f"{TENANT_PREFIX}box"          # CrossFit-shaped: WOD + OPEN
SUITE_TENANT_NO_FALLBACK = f"{TENANT_PREFIX}nofb"   # no fallback flag, no ADULTOS
SUITE_TENANT_IMPLICIT = f"{TENANT_PREFIX}implicit"  # has ADULTOS, never flagged
SUITE_TENANT_EMPTY = f"{TENANT_PREFIX}empty"        # no rows at all

# Leads this suite would create if it needed any. Reserved so a later test can
# use it without colliding with the other suites (5521000...-5526000...).
SENDER_PREFIX = "5527000"

# Where the pilot's real class_types and scheduling_configs rows are snapshotted
# before the screen tests overwrite them. Outside the repo so a crashed run
# never leaves fixture rows in git.
BACKUP_PATH = Path("/tmp/corujai_class_types_backup.json")

MIGRATION_CLASS_TYPES = "008_create_class_types"

# What migration 008 must have seeded for the pilot — the exact contents of the
# two dicts that used to live in bot/scheduling.py. Module S2's whole promise is
# that tenant 'default' behaves identically after the move, so this table is the
# reference the first tests compare against.
EXPECTED_DEFAULT_SEED: dict[str, dict[str, Any]] = {
    "BABY":     {"label": "Baby Class", "capacity": 2,    "requires_child_name": True,  "is_fallback": False},
    "CRIANCAS": {"label": "Crianças",   "capacity": 4,    "requires_child_name": True,  "is_fallback": False},
    "ADULTOS":  {"label": "Adultos",    "capacity": None, "requires_child_name": False, "is_fallback": True},
}

DEFAULT_REPORT_DIR = SRC_DIR / "tests" / "outputs"


# ---------------------------------------------------------------------------
# Report / console output (same shape as the other suites)
# ---------------------------------------------------------------------------

class SkipTest(Exception):
    """Raised by a test that cannot run in the current environment."""


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
            print(self.console.green("\n Tipos de aula por tenant OK — todos os testes passaram.\n"))
        return not failed

    def to_json(self, path: Path) -> None:
        path.write_text(json.dumps({
            "passed": sum(1 for r in self.results if r["status"] == "PASS"),
            "failed": len(self.failed),
            "skipped": sum(1 for r in self.results if r["status"] == "SKIP"),
            "results": self.results,
        }, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------

def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def expect_equal(actual: Any, expected: Any, what: str) -> None:
    if actual != expected:
        raise AssertionError(f"{what}: esperado {expected!r}, veio {actual!r}")


# Throwaway dashboard account this suite logs in with (Module S3a). Its own
# email so two suites' teardowns can never delete each other's row; the pilot
# tenant because users.tenant_id has a foreign key to owners and 'default' is
# the row every suite already works against.
SUITE_EMAIL: str = "suite-class-types@suite.corujai.test"
SUITE_PASSWORD: str = "suite-password-s3a"


def _login_suite_user(client: Any) -> None:
    """Create the suite's user and log the client in through the real route.

    Deliberately NOT forging Flask-Login's private session keys (_user_id,
    _fresh): they are undocumented, and they would still need a real `users` row
    for the user_loader to resolve. Going through POST /dashboard/login is
    honest, version-proof, and exercises the code under test.

    Args:
        client (Any): A Flask test client.
    """
    accounts_users.create_user(SUITE_EMAIL, SUITE_PASSWORD, store.DEFAULT_TENANT_ID)
    response = client.post(
        "/dashboard/login",
        data={"email": SUITE_EMAIL, "password": SUITE_PASSWORD},
    )
    expect_equal(response.status_code, 302, "o login da suíte deveria autenticar")


@contextlib.contextmanager
def patched(obj: Any, attr: str, value: Any) -> Iterator[None]:
    """Temporarily set obj.attr = value, restoring the original afterward."""
    original = getattr(obj, attr)
    setattr(obj, attr, value)
    try:
        yield
    finally:
        setattr(obj, attr, original)


class WarningCapture:
    """Collect WARNING records emitted while the block runs.

    Several guarantees in this module are "degrade, and say so in the log", so
    the log line is part of the contract, not decoration.
    """

    def __init__(self) -> None:
        self.messages: list[str] = []
        self._handler: logging.Handler | None = None

    def __enter__(self) -> "WarningCapture":
        capture = self

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                if record.levelno >= logging.WARNING:
                    capture.messages.append(record.getMessage())

        self._handler = _Handler()
        logging.getLogger().addHandler(self._handler)
        self._previous_level = logging.getLogger().level
        logging.getLogger().setLevel(logging.WARNING)
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self._handler is not None:
            logging.getLogger().removeHandler(self._handler)
        logging.getLogger().setLevel(self._previous_level)


# ---------------------------------------------------------------------------
# Fake Google Calendar
# ---------------------------------------------------------------------------

class FakeCalendar:
    """Minimal stand-in for the Calendar API client used by bot/scheduling.py.

    Implements only the chains scheduling.py actually calls:
    events().list(...), .get(...), .patch(...) and .insert(...), each ending in
    .execute(). Records the arguments it was given so a test can assert on the
    search window (which is how days_ahead is verified) and on the body of a
    created event (which is how the built title is verified).
    """

    def __init__(self, events: list[dict]) -> None:
        self._events = events
        self.last_list_kwargs: dict[str, Any] = {}
        self.patched_bodies: list[dict] = []
        self.inserted_bodies: list[dict] = []

    def events(self) -> "FakeCalendar":
        return self

    def list(self, **kwargs: Any) -> "FakeCall":
        self.last_list_kwargs = kwargs
        return FakeCall({"items": self._events})

    def get(self, calendarId: str, eventId: str) -> "FakeCall":  # noqa: N803 - Google's spelling
        for event in self._events:
            if event["id"] == eventId:
                return FakeCall(event)
        raise AssertionError(f"evento {eventId} não existe no calendário falso")

    def patch(self, calendarId: str, eventId: str, body: dict) -> "FakeCall":  # noqa: N803
        self.patched_bodies.append(body)
        return FakeCall({})

    def insert(self, calendarId: str, body: dict) -> "FakeCall":  # noqa: N803
        self.inserted_bodies.append(body)
        return FakeCall({"id": f"ev-inserido-{len(self.inserted_bodies)}", **body})


class FakeCall:
    """The `.execute()` end of a fake Calendar chain."""

    def __init__(self, result: Any) -> None:
        self._result = result

    def execute(self) -> Any:
        return self._result


def _event(event_id: str, summary: str, hours_from_now: int = 24) -> dict:
    """Build a fixture Calendar event dict shaped like the real API's."""
    start = datetime.now(scheduling.TIMEZONE) + timedelta(hours=hours_from_now)
    end = start + timedelta(hours=1)
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }


# ---------------------------------------------------------------------------
# Direct database access (fixtures and verification)
# ---------------------------------------------------------------------------

def read_class_type_rows(tenant_id: str) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT marker, label, capacity, requires_child_name, is_fallback
                FROM class_types WHERE tenant_id = %s ORDER BY marker
                """,
                (tenant_id,),
            )
            return [dict(row) for row in cur.fetchall()]


def seed_tenant(tenant_id: str, rows: list[tuple[str, str, int | None, bool, bool]],
                days_ahead: int | None = None) -> None:
    """Create a fixture tenant's class types (and optionally its horizon).

    Args:
        tenant_id (str): Fixture tenant.
        rows (list[tuple]): (marker, label, capacity, requires_child_name, is_fallback).
        days_ahead (int | None): Horizon to seed, or None to leave the tenant
            with no scheduling_configs row at all.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM class_types WHERE tenant_id = %s", (tenant_id,))
            cur.execute("DELETE FROM scheduling_configs WHERE tenant_id = %s", (tenant_id,))
            for marker, label, capacity, requires_child, is_fallback in rows:
                cur.execute(
                    """
                    INSERT INTO class_types
                        (tenant_id, marker, label, capacity, requires_child_name, is_fallback)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (tenant_id, marker, label, capacity, requires_child, is_fallback),
                )
            if days_ahead is not None:
                cur.execute(
                    "INSERT INTO scheduling_configs (tenant_id, days_ahead) VALUES (%s, %s)",
                    (tenant_id, days_ahead),
                )
        conn.commit()


# ---------------------------------------------------------------------------
# Suite
# ---------------------------------------------------------------------------

class ClassTypesSuite:
    """Owns fixtures, tests and teardown for per-tenant class types."""

    def __init__(self, report: Report, keep: bool) -> None:
        self.report = report
        self.keep = keep
        self.app: Any = None
        self.client: Any = None
        self._backup: dict[str, Any] | None = None

    # -- infrastructure -----------------------------------------------------

    def _authenticated_client(self) -> Any:
        """Return a test client already logged into the dashboard.

        Since Module S3a the dashboard uses Flask-Login against a real `users`
        row, so stuffing a boolean into the session authenticates nothing. This
        creates a throwaway user for the pilot tenant and logs in through the
        real POST /dashboard/login — more honest than forging Flask-Login's
        private session keys, and immune to them changing.
        """
        if self.client is None:
            import app as flask_app

            self.app = flask_app.create_app()
            self.app.config["TESTING"] = True
            self.client = self.app.test_client()
            _login_suite_user(self.client)
        return self.client

    @staticmethod
    def _notice(response: Any) -> tuple[str, str]:
        """Extract (kind, text) from the notice the settings page rendered."""
        import re

        html: str = response.get_data(as_text=True)
        match = re.search(r'notice notice-(\w+)">([^<]+)<', html)
        expect(match is not None, "a página não trouxe nenhum aviso (notice)")
        return match.group(1), match.group(2).strip()

    def check_schema(self) -> str:
        """Pre-requisite: migration 008 applied and both tables readable."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT version FROM schema_migrations WHERE version = %s",
                    (MIGRATION_CLASS_TYPES,),
                )
                expect(cur.fetchone() is not None,
                       f"migration {MIGRATION_CLASS_TYPES} não aplicada — rode `python app.py` uma vez")
                cur.execute("SELECT COUNT(*) AS n FROM class_types")
                total = cur.fetchone()["n"]
                cur.execute("SELECT COUNT(*) AS n FROM scheduling_configs")
                configs = cur.fetchone()["n"]

        return f"migration 008 aplicada · {total} tipo(s) de aula, {configs} config(s) de agendamento"

    def prepare_fixtures(self) -> str:
        """Snapshot the pilot's rows, then create the fixture tenants."""
        self._backup = {
            "class_types": read_class_type_rows(store.DEFAULT_TENANT_ID),
            "days_ahead": class_types.get_scheduling_config()["days_ahead"],
        }
        BACKUP_PATH.write_text(json.dumps(self._backup, indent=2, ensure_ascii=False), encoding="utf-8")

        # A CrossFit box: nothing in common with the pilot's three types.
        seed_tenant(SUITE_TENANT, [
            ("WOD", "WOD", 12, False, True),
            ("OPEN", "Open Gym", None, False, False),
            ("KIDS", "Kids", 6, True, False),
        ], days_ahead=21)

        # No fallback flagged AND no ADULTOS row: the synthetic-fallback path.
        seed_tenant(SUITE_TENANT_NO_FALLBACK, [("WOD", "WOD", 12, False, False)])

        # Has ADULTOS but never flagged it: the "point at the existing row" path.
        seed_tenant(SUITE_TENANT_IMPLICIT, [
            ("ADULTOS", "Adultos", 30, False, False),
            ("BABY", "Baby", 2, True, False),
        ])

        # No rows at all.
        seed_tenant(SUITE_TENANT_EMPTY, [])

        return (f"backup do '{store.DEFAULT_TENANT_ID}' em {BACKUP_PATH.name} · "
                f"4 tenants de teste criados")

    # -- tests: the seed and the read layer ---------------------------------

    def test_01_default_seed_matches_the_old_dicts(self) -> str:
        """Step 1: migration 008 seeded exactly the three types that were in code."""
        rows = {row["marker"]: row for row in read_class_type_rows(store.DEFAULT_TENANT_ID)}

        expect_equal(sorted(rows), sorted(EXPECTED_DEFAULT_SEED),
                     "marcadores do tenant 'default'")

        for marker, expected in EXPECTED_DEFAULT_SEED.items():
            for field, value in expected.items():
                expect_equal(rows[marker][field], value, f"{marker}.{field}")

        return "BABY/2 · CRIANCAS/4 · ADULTOS/ilimitado · ADULTOS é a turma padrão"

    def test_02_bundle_has_the_old_dict_shape(self) -> str:
        """Step 2: the loader reproduces CLASS_CAPACITY and CLASS_TYPE_LABELS."""
        bundle = class_types.load_class_types()

        expect_equal(bundle["capacities"], {"BABY": 2, "CRIANCAS": 4, "ADULTOS": None},
                     "capacities (formato do antigo CLASS_CAPACITY)")
        expect_equal(bundle["labels"],
                     {"BABY": "Baby Class", "CRIANCAS": "Crianças", "ADULTOS": "Adultos"},
                     "labels (formato do antigo CLASS_TYPE_LABELS)")
        expect_equal(bundle["child_name_required"], {"BABY", "CRIANCAS"},
                     "child_name_required (formato do antigo set em scheduling.py)")
        expect_equal(bundle["fallback"], "ADULTOS", "fallback")

        # The semantics the whole engine depends on: None is unlimited, and it
        # must survive the trip through Postgres as None — not 0, not -1.
        expect(bundle["capacities"]["ADULTOS"] is None,
               "capacidade de ADULTOS deveria ser None (ilimitado), não outro falsy")

        return "capacities, labels e child_name_required idênticos aos dicts aposentados"

    def test_03_fixture_tenant_is_isolated(self) -> str:
        """Step 3: a second tenant reads its own types and never the pilot's."""
        box = class_types.load_class_types(SUITE_TENANT)

        expect_equal(box["capacities"], {"WOD": 12, "OPEN": None, "KIDS": 6},
                     f"capacities de '{SUITE_TENANT}'")
        expect_equal(box["labels"]["OPEN"], "Open Gym", "label de OPEN")
        expect_equal(box["child_name_required"], {"KIDS"}, "child_name_required do box")
        expect_equal(box["fallback"], "WOD", "fallback do box")

        for pilot_marker in ("BABY", "CRIANCAS", "ADULTOS"):
            expect(pilot_marker not in box["capacities"],
                   f"tipo '{pilot_marker}' do piloto vazou para '{SUITE_TENANT}'")

        # And the pilot is untouched by the fixture tenant existing.
        expect("WOD" not in class_types.load_class_types()["capacities"],
               "tipo 'WOD' do tenant de teste vazou para o 'default'")

        return "WOD/12 · OPEN/ilimitado · KIDS/6 — nenhum vazamento nos dois sentidos"

    def test_04_marker_normalization(self) -> str:
        """Step 4: normalize_marker is the single definition of 'canonical'."""
        accepted: list[tuple[str, str]] = [
            ("CRIANCAS", "CRIANCAS"),
            ("crianças", "CRIANCAS"),
            (" Crianças ", "CRIANCAS"),
            ("BEBÊ", "BEBE"),
            ("wod", "WOD"),
        ]
        for raw, expected in accepted:
            expect_equal(class_types.normalize_marker(raw), expected, f"normalize_marker({raw!r})")

        # Anything the title regex could never match must be refused at the write
        # path, or it becomes a class type no event can ever be tagged with.
        refused: list[str | None] = ["WOD-1", "OPEN GYM", "TURMA2", "", "  ", None, "A" * 33]
        for raw in refused:
            expect(class_types.normalize_marker(raw) is None,
                   f"normalize_marker({raw!r}) deveria recusar, veio {class_types.normalize_marker(raw)!r}")

        return f"{len(accepted)} formas aceitas e normalizadas · {len(refused)} recusadas"

    # -- tests: the fallback invariant --------------------------------------

    def test_05_fallback_invariant_holds_everywhere(self) -> str:
        """Step 5: fallback is always a key of capacities, for every tenant shape."""
        tenants = [
            store.DEFAULT_TENANT_ID,
            SUITE_TENANT,
            SUITE_TENANT_NO_FALLBACK,
            SUITE_TENANT_IMPLICIT,
            SUITE_TENANT_EMPTY,
            "tenant_que_nao_existe",
        ]
        for tenant in tenants:
            bundle = class_types.load_class_types(tenant)
            expect(bundle["fallback"] in bundle["capacities"],
                   f"invariante quebrada em '{tenant}': fallback {bundle['fallback']!r} "
                   f"não está em capacities {sorted(bundle['capacities'])}")
            # The direct lookup scheduling.py performs, on every tenant shape.
            bundle["capacities"][bundle["fallback"]]

        return f"fallback ∈ capacities em {len(tenants)} formatos de tenant, incluindo vazio e inexistente"

    def test_06_synthetic_fallback_is_unlimited_and_childless(self) -> str:
        """Step 6: the synthesized fallback degrades, it never blocks."""
        bundle = class_types.load_class_types(SUITE_TENANT_NO_FALLBACK)

        expect_equal(bundle["fallback"], "ADULTOS", "fallback sintético")
        expect(bundle["capacities"]["ADULTOS"] is None,
               "o fallback sintético tem de ser ilimitado — senão um título errado esgota vagas")
        expect("ADULTOS" not in bundle["child_name_required"],
               "o fallback sintético não pode exigir nome de criança")
        expect_equal(bundle["labels"]["ADULTOS"], "Adultos", "label do fallback sintético")

        # Synthesized in memory only: nothing was written to the database.
        rows = read_class_type_rows(SUITE_TENANT_NO_FALLBACK)
        expect_equal([row["marker"] for row in rows], ["WOD"],
                     f"linhas de '{SUITE_TENANT_NO_FALLBACK}' no banco (o sintético não pode ser gravado)")

        return "ADULTOS sintético, ilimitado, sem nome de criança, e ausente do banco"

    def test_07_existing_row_is_never_overwritten(self) -> str:
        """Step 7: an unflagged ADULTOS row is pointed at, not replaced."""
        bundle = class_types.load_class_types(SUITE_TENANT_IMPLICIT)

        expect_equal(bundle["fallback"], "ADULTOS", "fallback implícito")
        expect_equal(bundle["capacities"]["ADULTOS"], 30,
                     "capacidade da linha real (o sintético não pode sobrescrevê-la)")

        return "linha ADULTOS/30 do tenant preservada e usada como fallback"

    # -- tests: the scheduling engine reading from the table ----------------

    def test_08_slots_use_the_tenant_table(self) -> str:
        """Step 8: get_available_slots sizes and labels slots from class_types."""
        calendar = FakeCalendar([
            _event("ev-wod", "[WOD] Treino"),
            _event("ev-kids", "[KIDS] Turma infantil"),
            _event("ev-open", "[OPEN] Livre"),
        ])

        with patched(scheduling, "_get_service_or_raise", lambda: (calendar, "cal")):
            slots = scheduling.get_available_slots(days_ahead=14, tenant_id=SUITE_TENANT)

        by_id = {slot["event_id"]: slot for slot in slots}
        expect_equal(sorted(by_id), ["ev-kids", "ev-open", "ev-wod"], "slots devolvidos")

        expect_equal(by_id["ev-wod"]["class_type"], "WOD", "class_type do [WOD]")
        expect_equal(by_id["ev-wod"]["remaining_slots"], 12, "vagas do [WOD] (capacidade do banco)")
        expect_equal(by_id["ev-wod"]["requires_child_name"], False, "[WOD] exige nome de criança?")
        expect("WOD" in by_id["ev-wod"]["label"], f"label do [WOD]: {by_id['ev-wod']['label']!r}")

        expect_equal(by_id["ev-kids"]["requires_child_name"], True,
                     "[KIDS] deveria exigir o nome da criança (coluna requires_child_name)")
        expect_equal(by_id["ev-kids"]["remaining_slots"], 6, "vagas do [KIDS]")

        expect(by_id["ev-open"]["remaining_slots"] is None,
               "[OPEN] tem capacity NULL, logo vagas ilimitadas (None)")
        expect("Open Gym" in by_id["ev-open"]["label"],
               f"label do [OPEN] deveria usar o label do banco: {by_id['ev-open']['label']!r}")

        return "capacidade, label e exigência de nome vindos da tabela, não de dict"

    def test_09_unmarked_title_falls_back_without_keyerror(self) -> str:
        """Step 9: the Armadilha #1 case — no ADULTOS row, no fallback flag."""
        calendar = FakeCalendar([_event("ev-sem-marcador", "Aula livre sem marcador")])

        with WarningCapture() as capture:
            with patched(scheduling, "_get_service_or_raise", lambda: (calendar, "cal")):
                # Must not raise. Before Module S2 this was CLASS_CAPACITY[...]
                # against a literal that always had the key; from a table it is
                # only safe because load_class_types() guarantees the fallback.
                slots = scheduling.get_available_slots(
                    days_ahead=14, tenant_id=SUITE_TENANT_NO_FALLBACK)

        expect_equal(len(slots), 1, "slots do título sem marcador")
        expect_equal(slots[0]["class_type"], "ADULTOS", "class_type do título sem marcador")
        expect(slots[0]["remaining_slots"] is None, "vagas do fallback sintético")
        expect_equal(slots[0]["requires_child_name"], False,
                     "o fallback não pode exigir nome de criança")

        matching = [msg for msg in capture.messages if "Unrecognized class marker" in msg]
        expect(bool(matching),
               f"nenhum WARNING de marcador desconhecido; capturados: {capture.messages or 'nenhum'}")

        return f"caiu no fallback sem KeyError · log: {matching[0]}"

    def test_10_accented_and_lowercase_markers_match(self) -> str:
        """Step 10: the title parser and the stored marker meet at the same form."""
        calendar = FakeCalendar([
            _event("ev-acento", "[Crianças] Aula Experimental"),
            _event("ev-minusculo", "[baby] Aula Experimental"),
            _event("ev-espacos", "[ CRIANCAS ] Aula Experimental"),
        ])

        with patched(scheduling, "_get_service_or_raise", lambda: (calendar, "cal")):
            slots = scheduling.get_available_slots(days_ahead=14)

        by_id = {slot["event_id"]: slot for slot in slots}
        expect_equal(by_id["ev-acento"]["class_type"], "CRIANCAS", "[Crianças] normalizado")
        expect_equal(by_id["ev-minusculo"]["class_type"], "BABY", "[baby] normalizado")
        expect_equal(by_id["ev-espacos"]["class_type"], "CRIANCAS", "[ CRIANCAS ] normalizado")

        expect_equal(by_id["ev-acento"]["remaining_slots"], 4, "vagas de CRIANCAS")
        expect_equal(by_id["ev-minusculo"]["remaining_slots"], 2, "vagas de BABY")

        return "[Crianças], [baby] e [ CRIANCAS ] casaram com os marcadores canônicos"

    def test_11_full_slot_is_omitted(self) -> str:
        """Step 11: capacity from the table still removes a full slot from the list."""
        seed_tenant(f"{TENANT_PREFIX}cheio", [("UNICA", "Única", 1, False, True)])
        calendar = FakeCalendar([_event("ev-cheio", "[UNICA] Turma")])

        def one_booking(event_id: str) -> int:
            return 1

        with patched(scheduling.bookings, "count_active_bookings", one_booking):
            with patched(scheduling, "_get_service_or_raise", lambda: (calendar, "cal")):
                slots = scheduling.get_available_slots(
                    days_ahead=14, tenant_id=f"{TENANT_PREFIX}cheio")

        expect_equal(slots, [], "slot com capacidade 1 e 1 reserva deveria sumir da lista")

        return "capacidade 1 com 1 reserva ativa: slot omitido, como antes"

    # -- tests: days_ahead ---------------------------------------------------

    def test_12_days_ahead_per_tenant(self) -> str:
        """Step 12: the horizon is read per tenant, with 14 as the fallback."""
        expect_equal(class_types.get_scheduling_config()["days_ahead"], 14,
                     "days_ahead semeado do 'default'")
        expect_equal(class_types.get_scheduling_config(SUITE_TENANT)["days_ahead"], 21,
                     f"days_ahead de '{SUITE_TENANT}'")
        expect_equal(class_types.get_scheduling_config("tenant_sem_linha")["days_ahead"],
                     class_types.DEFAULT_DAYS_AHEAD,
                     "days_ahead de um tenant sem linha (padrão)")

        return "default=14 · box=21 · tenant sem linha=14"

    def test_13_days_ahead_drives_the_calendar_window(self) -> str:
        """Step 13: days_ahead=None actually reaches the Calendar query."""
        calendar = FakeCalendar([])

        with patched(scheduling, "_get_service_or_raise", lambda: (calendar, "cal")):
            scheduling.get_available_slots(tenant_id=SUITE_TENANT)

        time_min = datetime.fromisoformat(calendar.last_list_kwargs["timeMin"])
        time_max = datetime.fromisoformat(calendar.last_list_kwargs["timeMax"])
        window_days = round((time_max - time_min).total_seconds() / 86400)

        expect_equal(window_days, 21, f"janela consultada no Calendar para '{SUITE_TENANT}'")

        # An explicit argument still wins, without touching the database — which
        # is what keeps the Module 2 suite's days_ahead=0/14 calls meaningful.
        with patched(scheduling, "_get_service_or_raise", lambda: (calendar, "cal")):
            scheduling.get_available_slots(days_ahead=3, tenant_id=SUITE_TENANT)

        time_min = datetime.fromisoformat(calendar.last_list_kwargs["timeMin"])
        time_max = datetime.fromisoformat(calendar.last_list_kwargs["timeMax"])
        expect_equal(round((time_max - time_min).total_seconds() / 86400), 3,
                     "janela com days_ahead explícito")

        return "days_ahead=None usou os 21 dias do tenant; explícito=3 prevaleceu"

    # -- tests: the other consumers -----------------------------------------

    def test_14_cron_composes_labels_without_flask(self) -> str:
        """Step 14: the drain job's message uses labels read from the database.

        jobs/drain_notifications.py runs on Railway's cron, outside any request
        context, so this exercises the same call it makes — no app, no session.
        """
        labels: dict[str, str] = class_types.load_class_types()["labels"]

        booking = {
            "id": "booking-fake",
            "class_type": "CRIANCAS",
            "lead_name": "Marina Souza",
            "child_name": "Bento",
            "slot_start": datetime.now(scheduling.TIMEZONE) + timedelta(days=1),
        }
        notification = {"event_type": "booking", "booking_id": "booking-fake", "lead_sender": "5527000000001"}

        with patched(drain.bookings, "get_booking", lambda booking_id: booking):
            text = drain._compose_message(notification, labels)
            # An unknown marker (a class type deleted after the booking) degrades
            # to the raw marker instead of raising.
            text_unknown = drain._compose_message(notification, {})

        expect("Crianças" in text, f"o aviso deveria trazer o label 'Crianças': {text!r}")
        expect("CRIANCAS" not in text, f"o aviso não pode mostrar o marcador cru: {text!r}")
        expect("Bento" in text, "o aviso deveria nomear a criança")

        expect("CRIANCAS" in text_unknown,
               f"sem label cadastrado, o aviso deveria cair no marcador cru: {text_unknown!r}")

        return "label vindo do banco, fora do Flask · marcador sem label degrada"

    def test_15_prompt_tags_child_name_slots(self) -> str:
        """Step 15: the AI prompt marks which slots need the child's name.

        PROTECTED_LAYER no longer names [BABY]/[CRIANCAS] — it points at this
        tag, so a tenant with different markers still gets the rule right.
        """
        import bot.ai_context as ai_context

        slots = [
            {"event_id": "e1", "class_type": "KIDS", "label": "Sábado 10:00 — Kids",
             "remaining_slots": 6, "requires_child_name": True},
            {"event_id": "e2", "class_type": "WOD", "label": "Sábado 18:00 — WOD",
             "remaining_slots": 12, "requires_child_name": False},
        ]
        rendered = ai_context._render_slots(slots)

        kids_line = next(line for line in rendered.splitlines() if "KIDS" in line)
        wod_line = next(line for line in rendered.splitlines() if "WOD" in line)

        expect("exige o nome da criança" in kids_line, f"linha do KIDS sem a marca: {kids_line!r}")
        expect("exige o nome da criança" not in wod_line, f"linha do WOD marcada à toa: {wod_line!r}")

        # And the protected layer must not name the pilot's markers any more.
        for hardcoded in ("[BABY]", "[CRIANCAS]"):
            expect(hardcoded not in ai_context.PROTECTED_LAYER,
                   f"PROTECTED_LAYER ainda cita {hardcoded} — um tenant com outro marcador quebra")

        return "slot infantil marcado, slot adulto não · prompt sem marcador hardcoded"

    # -- tests: the write path and the screen -------------------------------

    def test_16_write_guards(self) -> str:
        """Step 16: the store refuses what would corrupt the configuration."""
        tenant = f"{TENANT_PREFIX}escrita"
        seed_tenant(tenant, [("WOD", "WOD", 12, False, True)])

        expect_equal(class_types.create_class_type("WOD", "Outro", 5, False, tenant), "duplicate",
                     "cadastrar marcador repetido")
        expect_equal(class_types.create_class_type("KIDS", "Kids", 6, True, tenant), "created",
                     "cadastrar marcador novo")

        expect_equal(class_types.delete_class_type("WOD", tenant), "is_fallback",
                     "excluir a turma padrão")
        expect_equal(class_types.delete_class_type("NAOEXISTE", tenant), "not_found",
                     "excluir marcador inexistente")

        expect(class_types.set_fallback_class_type("KIDS", tenant), "trocar a turma padrão")
        markers = {row["marker"]: row["is_fallback"] for row in read_class_type_rows(tenant)}
        expect_equal(markers, {"KIDS": True, "WOD": False}, "quem é a turma padrão depois da troca")

        expect_equal(class_types.delete_class_type("WOD", tenant), "deleted",
                     "excluir a turma que deixou de ser padrão")

        # A failed set_fallback must not leave the tenant with no fallback at all.
        expect(not class_types.set_fallback_class_type("NAOEXISTE", tenant),
               "marcar padrão inexistente deveria falhar")
        expect_equal(class_types.load_class_types(tenant)["fallback"], "KIDS",
                     "turma padrão depois de uma troca recusada (rollback)")

        return "duplicado, exclusão da padrão e troca inexistente recusados · rollback preservou o fallback"

    def test_17_screen_lists_and_saves(self) -> str:
        """Step 17: the Aulas section shows the types and saves an edit."""
        client = self._authenticated_client()

        response = client.get("/dashboard/settings")
        expect_equal(response.status_code, 200, "GET /dashboard/settings")
        html: str = response.get_data(as_text=True)
        for needle in ("Aulas", "[BABY]", "[CRIANCAS]", "[ADULTOS]", "Turma padrão", "Nova turma"):
            expect(needle in html, f"a tela não mostrou {needle!r}")

        response = client.post("/dashboard/settings/class-types",
                               data={"marker": "wod", "label": "WOD", "capacity": "12"})
        kind, text = self._notice(response)
        expect_equal(kind, "success", f"cadastro válido devolveu {text!r}")
        expect_equal(class_types.load_class_types()["capacities"].get("WOD"), 12,
                     "capacidade cadastrada pela tela (marcador normalizado de 'wod')")

        response = client.post("/dashboard/settings/class-types/WOD",
                               data={"label": "WOD do dia", "capacity": "", "requires_child_name": "on"})
        expect_equal(self._notice(response)[0], "success", "edição válida")
        bundle = class_types.load_class_types()
        expect(bundle["capacities"]["WOD"] is None, "capacidade vazia deveria virar ilimitado (None)")
        expect("WOD" in bundle["child_name_required"], "checkbox de nome da criança não gravou")

        expect_equal(class_types.delete_class_type("WOD"), "deleted", "limpeza do tipo de teste")

        return "lista renderizada · cadastro normalizou 'wod' → WOD · capacidade vazia virou ilimitado"

    def test_18_screen_refuses_bad_input(self) -> str:
        """Step 18: every refusal answers 200 with a Portuguese notice."""
        client = self._authenticated_client()

        cases: list[tuple[str, dict[str, str], str]] = [
            ("/dashboard/settings/class-types",
             {"marker": "WOD-1", "label": "Wod", "capacity": "12"}, "marcador com símbolo"),
            ("/dashboard/settings/class-types",
             {"marker": "OPEN GYM", "label": "Open", "capacity": "12"}, "marcador com espaço"),
            ("/dashboard/settings/class-types",
             {"marker": "TESTE", "label": "Teste", "capacity": "0"}, "capacidade zero"),
            ("/dashboard/settings/class-types",
             {"marker": "TESTE", "label": "", "capacity": "5"}, "nome vazio"),
            ("/dashboard/settings/class-types",
             {"marker": "BABY", "label": "Outro", "capacity": "5"}, "marcador duplicado"),
            ("/dashboard/settings/class-types/ADULTOS/delete", {}, "excluir a turma padrão"),
            ("/dashboard/settings/scheduling", {"days_ahead": "200"}, "janela fora do limite"),
            ("/dashboard/settings/scheduling", {"days_ahead": "abc"}, "janela não numérica"),
        ]

        for path, data, what in cases:
            response = client.post(path, data=data)
            expect_equal(response.status_code, 200, f"{what}: status")
            kind, text = self._notice(response)
            expect_equal(kind, "error", f"{what}: veio o aviso {text!r}")

        # Nothing was written by any of them.
        bundle = class_types.load_class_types()
        expect_equal(sorted(bundle["capacities"]), ["ADULTOS", "BABY", "CRIANCAS"],
                     "tipos do 'default' depois das recusas")
        expect_equal(bundle["labels"]["BABY"], "Baby Class", "label do BABY depois da recusa de duplicado")
        expect_equal(class_types.get_scheduling_config()["days_ahead"], 14,
                     "days_ahead depois das recusas")

        return f"{len(cases)} entradas inválidas recusadas com 200 + aviso, sem gravar nada"

    def test_19_screen_saves_days_ahead(self) -> str:
        """Step 19: the horizon field writes scheduling_configs."""
        client = self._authenticated_client()

        response = client.post("/dashboard/settings/scheduling", data={"days_ahead": "30"})
        expect_equal(self._notice(response)[0], "success", "salvar janela válida")
        expect_equal(class_types.get_scheduling_config()["days_ahead"], 30, "days_ahead gravado")

        response = client.post("/dashboard/settings/scheduling", data={"days_ahead": "14"})
        expect_equal(self._notice(response)[0], "success", "voltar a janela para 14")

        return "30 gravado e revertido para 14"

    def test_21_create_class_event_builds_the_title(self) -> str:
        """Step 21: creating a class writes [MARKER] Label, with the right window.

        The title is the whole point of building it instead of letting the owner
        type it: _parse_class_type() has to read the event back as the class the
        owner picked, and a typo would silently drop it into the fallback.
        """
        calendar = FakeCalendar([])
        start = datetime.now(scheduling.TIMEZONE) + timedelta(days=2)
        end = start + timedelta(hours=1)

        with patched(scheduling, "_get_service_or_raise", lambda: (calendar, "cal")):
            result = scheduling.create_class_event("KIDS", start, end, SUITE_TENANT)

        expect_equal(result["status"], "created", "status da criação")
        expect_equal(result["summary"], "[KIDS] Kids", "título montado (marcador + label)")

        body = calendar.inserted_bodies[0]
        expect_equal(body["summary"], "[KIDS] Kids", "summary enviado ao Google")
        expect_equal(body["start"]["dateTime"], start.isoformat(), "início enviado ao Google")
        expect_equal(body["end"]["dateTime"], end.isoformat(), "fim enviado ao Google")
        expect_equal(body["start"]["timeZone"], "America/Sao_Paulo", "fuso enviado ao Google")

        # The event must round-trip: what was written is what the engine reads.
        written = _event("ev-novo", body["summary"])
        reader = FakeCalendar([written])
        with patched(scheduling, "_get_service_or_raise", lambda: (reader, "cal")):
            slots = scheduling.get_available_slots(days_ahead=14, tenant_id=SUITE_TENANT)

        expect_equal(slots[0]["class_type"], "KIDS", "turma lida de volta do título criado")
        expect_equal(slots[0]["requires_child_name"], True, "exigência lida de volta")
        expect_equal(slots[0]["remaining_slots"], 6, "vagas lidas de volta")

        # An unregistered marker never reaches the Calendar.
        with patched(scheduling, "_get_service_or_raise", lambda: (calendar, "cal")):
            refused = scheduling.create_class_event("NAOEXISTE", start, end, SUITE_TENANT)
        expect_equal(refused["status"], "unknown_class_type", "turma não cadastrada")
        expect_equal(len(calendar.inserted_bodies), 1, "nada foi criado para turma inexistente")

        return "[KIDS] Kids criado com data/hora e fuso · lido de volta como KIDS/6/exige criança"

    def test_22_class_event_form_validates(self) -> str:
        """Step 22: the form refuses a bad window before touching the Calendar."""
        client = self._authenticated_client()
        calendar = FakeCalendar([])

        future = (datetime.now(scheduling.TIMEZONE) + timedelta(days=3)).date().isoformat()
        past = (datetime.now(scheduling.TIMEZONE) - timedelta(days=1)).date().isoformat()

        cases: list[tuple[dict[str, str], str]] = [
            ({"marker": "CRIANCAS", "date": future, "start_time": "19:00", "end_time": "18:00"},
             "fim antes do início"),
            ({"marker": "CRIANCAS", "date": future, "start_time": "18:00", "end_time": "18:00"},
             "fim igual ao início"),
            ({"marker": "CRIANCAS", "date": past, "start_time": "18:00", "end_time": "19:00"},
             "data no passado"),
            ({"marker": "CRIANCAS", "date": "", "start_time": "18:00", "end_time": "19:00"},
             "data vazia"),
            ({"marker": "CRIANCAS", "date": future, "start_time": "", "end_time": "19:00"},
             "hora vazia"),
            ({"marker": "", "date": future, "start_time": "18:00", "end_time": "19:00"},
             "turma vazia"),
            ({"marker": "NAOEXISTE", "date": future, "start_time": "18:00", "end_time": "19:00"},
             "turma não cadastrada"),
        ]

        with patched(scheduling, "_get_service_or_raise", lambda: (calendar, "cal")):
            for data, what in cases:
                response = client.post("/dashboard/settings/class-events", data=data)
                expect_equal(response.status_code, 200, f"{what}: status")
                kind, text = self._notice(response)
                expect_equal(kind, "error", f"{what}: veio o aviso {text!r}")

            expect_equal(calendar.inserted_bodies, [],
                         "nenhuma entrada inválida podia ter chegado ao Google")

            # And the valid one does go through.
            response = client.post("/dashboard/settings/class-events", data={
                "marker": "criancas", "date": future, "start_time": "18:00", "end_time": "19:00",
            })
            kind, text = self._notice(response)

        expect_equal(kind, "success", f"criação válida devolveu {text!r}")
        expect_equal(len(calendar.inserted_bodies), 1, "a aula válida deveria ter sido criada")
        expect_equal(calendar.inserted_bodies[0]["summary"], "[CRIANCAS] Crianças",
                     "título da aula criada pela tela (marcador normalizado de 'criancas')")

        return f"{len(cases)} janelas inválidas recusadas sem tocar no Google · a válida criada"

    def test_20_routes_require_auth(self) -> str:
        """Step 20: none of the new routes is reachable without a session."""
        import app as flask_app

        app = flask_app.create_app()
        app.config["TESTING"] = True
        anonymous = app.test_client()

        paths: list[str] = [
            "/dashboard/settings/class-types",
            "/dashboard/settings/class-types/BABY",
            "/dashboard/settings/class-types/BABY/fallback",
            "/dashboard/settings/class-types/BABY/delete",
            "/dashboard/settings/class-events",
            "/dashboard/settings/scheduling",
        ]
        for path in paths:
            response = anonymous.post(path, data={})
            expect_equal(response.status_code, 302, f"POST {path} deveria redirecionar")
            expect("/dashboard/login" in response.headers.get("Location", ""),
                   f"POST {path} deveria redirecionar para o login")

        return f"as {len(paths)} rotas novas exigem sessão do painel"

    # -- teardown -----------------------------------------------------------

    def teardown(self) -> None:
        if self.keep:
            print("  --keep: tenants de teste preservados.")
            print(f"  ATENÇÃO: os tipos de aula do piloto podem estar alterados. Backup em {BACKUP_PATH}.")
            return

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM class_types WHERE tenant_id LIKE %s", (TENANT_PREFIX + "%",))
                removed_types = cur.rowcount
                cur.execute("DELETE FROM scheduling_configs WHERE tenant_id LIKE %s",
                            (TENANT_PREFIX + "%",))
                cur.execute("DELETE FROM sessions WHERE sender LIKE %s", (SENDER_PREFIX + "%",))
                cur.execute("DELETE FROM users WHERE email = %s", (SUITE_EMAIL,))
            conn.commit()

        restored = _restore_backup(self._backup)
        BACKUP_PATH.unlink(missing_ok=True)

        print(f"  limpeza: {removed_types} tipo(s) de teste removido(s); "
              f"{'tipos e janela do piloto restaurados' if restored else 'nada a restaurar'}.")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _resolve_report_path(path: Path) -> Path:
    if path.is_dir() or path.suffix.lower() != ".json":
        path = path / f"class-types-{datetime.now():%Y%m%d_%H%M%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _restore_backup(backup: dict[str, Any] | None) -> bool:
    """Put the pilot's class types and horizon back exactly as they were.

    The whole tenant is rewritten rather than diffed: the screen tests can
    create, edit, delete and re-flag rows, so replacing the set is the only
    restore that is correct for every path through the suite.

    Args:
        backup (dict[str, Any] | None): Snapshot from prepare_fixtures, or None
            if the suite never got that far.

    Returns:
        bool: True if anything was written back.
    """
    if not backup:
        return False

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM class_types WHERE tenant_id = %s", (store.DEFAULT_TENANT_ID,))
            for row in backup["class_types"]:
                cur.execute(
                    """
                    INSERT INTO class_types
                        (tenant_id, marker, label, capacity, requires_child_name, is_fallback)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (store.DEFAULT_TENANT_ID, row["marker"], row["label"], row["capacity"],
                     row["requires_child_name"], row["is_fallback"]),
                )
            cur.execute(
                "UPDATE scheduling_configs SET days_ahead = %s WHERE tenant_id = %s",
                (backup["days_ahead"], store.DEFAULT_TENANT_ID),
            )
        conn.commit()

    return True


def _restore_orphan_backup() -> None:
    """Repair the pilot's rows if a previous run died before its teardown."""
    if not BACKUP_PATH.exists():
        return

    backup = json.loads(BACKUP_PATH.read_text(encoding="utf-8"))
    _restore_backup(backup)
    BACKUP_PATH.unlink(missing_ok=True)
    print(f"  tipos de aula do piloto restaurados a partir de {BACKUP_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Suíte automatizada dos tipos de aula por tenant (CLASS_TYPES_TESTING.md).")
    parser.add_argument("--keep", action="store_true", help="Não desfaz nada ao final (para depurar à mão).")
    parser.add_argument("--no-color", action="store_true", help="Saída sem cores ANSI.")
    parser.add_argument("--json", nargs="?", type=Path, const=DEFAULT_REPORT_DIR, default=None,
                        metavar="ARQUIVO",
                        help="Também grava o relatório em JSON (sozinha: tests/outputs/ com nome datado).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.ERROR, format="%(levelname)s - %(message)s")

    console = Console(color=not args.no_color and sys.stdout.isatty())
    report = Report(console)

    print(console.bold("\n═══ Corujai · suíte dos tipos de aula por tenant ═══"))
    print(console.dim(" Roteiro: CLASS_TYPES_TESTING.md"))

    # A crashed previous run may have left fixture rows in the pilot's tenant.
    _restore_orphan_backup()

    report.section("Pré-requisitos")
    suite = ClassTypesSuite(report, keep=args.keep)
    if not report.run("P1", "Migration 008 aplicada (class_types + scheduling_configs)",
                      suite.check_schema):
        print(console.red("\n Pré-requisitos falharam — a suíte não pode continuar."))
        report.summary()
        sys.exit(1)

    report.section("Preparo")
    atexit.register(suite.teardown)
    if not report.run("F1", "Tipos do piloto salvos e tenants de teste criados",
                      suite.prepare_fixtures):
        print(console.red("\n Sem backup a suíte não pode rodar: ela sobrescreve os dados reais."))
        atexit.unregister(suite.teardown)
        report.summary()
        sys.exit(1)

    report.section("Roteiro de testes")
    tests: list[tuple[str, str, Callable[[], str | None]]] = [
        ("1", "Seed do 'default' reproduz os dois dicts aposentados",
         suite.test_01_default_seed_matches_the_old_dicts),
        ("2", "load_class_types devolve capacities/labels no formato antigo",
         suite.test_02_bundle_has_the_old_dict_shape),
        ("3", "Tenant fictício é lido corretamente e isolado do piloto",
         suite.test_03_fixture_tenant_is_isolated),
        ("4", "normalize_marker aceita e recusa o que deve",
         suite.test_04_marker_normalization),
        ("5", "Invariante do fallback vale para todo formato de tenant",
         suite.test_05_fallback_invariant_holds_everywhere),
        ("6", "Fallback sintético é ilimitado, sem criança e fora do banco",
         suite.test_06_synthetic_fallback_is_unlimited_and_childless),
        ("7", "Linha ADULTOS existente é usada, nunca sobrescrita",
         suite.test_07_existing_row_is_never_overwritten),
        ("8", "get_available_slots dimensiona e rotula pela tabela",
         suite.test_08_slots_use_the_tenant_table),
        ("9", "Título sem marcador cai no fallback sem KeyError",
         suite.test_09_unmarked_title_falls_back_without_keyerror),
        ("10", "Marcador acentuado/minúsculo casa após normalização",
         suite.test_10_accented_and_lowercase_markers_match),
        ("11", "Slot cheio segue sumindo da lista",
         suite.test_11_full_slot_is_omitted),
        ("12", "days_ahead é lido por tenant, com padrão 14",
         suite.test_12_days_ahead_per_tenant),
        ("13", "days_ahead do tenant chega à consulta do Calendar",
         suite.test_13_days_ahead_drives_the_calendar_window),
        ("14", "Cron monta o aviso com label do banco, fora do Flask",
         suite.test_14_cron_composes_labels_without_flask),
        ("15", "Prompt marca os slots que exigem nome da criança",
         suite.test_15_prompt_tags_child_name_slots),
        ("16", "Guardas de escrita no store",
         suite.test_16_write_guards),
        ("17", "Tela lista as turmas e salva uma edição",
         suite.test_17_screen_lists_and_saves),
        ("18", "Tela recusa entrada inválida com 200 + aviso",
         suite.test_18_screen_refuses_bad_input),
        ("19", "Tela salva a janela de horários",
         suite.test_19_screen_saves_days_ahead),
        ("20", "As rotas novas exigem autenticação",
         suite.test_20_routes_require_auth),
        ("21", "Marcar aula monta o título [MARCADOR] Nome e volta legível",
         suite.test_21_create_class_event_builds_the_title),
        ("22", "Formulário da aula recusa janela inválida sem tocar no Google",
         suite.test_22_class_event_form_validates),
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
