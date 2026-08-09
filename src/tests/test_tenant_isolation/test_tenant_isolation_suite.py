"""Automated end-to-end suite for per-tenant read isolation (Module S3b).

Runs the scenarios documented in TENANT_ISOLATION_TESTING.md and prints a
PASS/FAIL report boiled down to an exit code, in the same style as
tests/test_accounts/test_accounts_suite.py.

Fully deterministic: no LLM, no WhatsApp, no Google Calendar. Everything here is
Postgres plus the Flask test client.

THE SHAPE OF EVERY TEST IS THE SAME, and it is the point: build the SAME fact in
two tenants, then read it back as one of them and prove the other's copy is not
there. A test that only checked "tenant A sees its own row" would pass on the
pre-S3b code too, because unfiltered reads return your row as well as everyone
else's. The assertion that matters is always the negative one.

NO /tmp BACKUP FILE, deliberately — the same reasoning as test_accounts. This
suite never writes to the pilot: every scenario runs on two fixture tenants built
by provision_tenant() under the prefix below. _drop_orphan_fixtures(), called at
the start of main(), plays the crash-repair role the backup file plays elsewhere.

Run from src/:
    python tests/test_tenant_isolation/test_tenant_isolation_suite.py
    python tests/test_tenant_isolation/test_tenant_isolation_suite.py --keep
    python tests/test_tenant_isolation/test_tenant_isolation_suite.py --json

Exit code is 0 only when every test passed.
"""

import argparse
import atexit
import contextlib
import json
import logging
import re
import sys
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

# Locate src/ by NAME, like app.py and the other suites.
SRC_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "src")
sys.path.insert(0, str(SRC_DIR))

import accounts.provision as provision  # noqa: E402
import bot.ai_context as ai_context  # noqa: E402
import bot.bookings as bookings  # noqa: E402
import bot.class_types as class_types  # noqa: E402
import bot.confirmations as confirmations  # noqa: E402
import bot.messages as messages  # noqa: E402
import bot.owner_notifications as owner_notifications  # noqa: E402
import bot.session as session_store  # noqa: E402
import jobs.drain_notifications as drain  # noqa: E402
from database.db import get_connection  # noqa: E402

# Fixture tenants. Hyphens, because these ids come out of the real slug generator.
TENANT_PREFIX = "suite-s3b-"

# A DOMAIN OF THIS SUITE'S OWN. test_accounts tears down with
# `DELETE FROM users WHERE email LIKE '%@suite.corujai.test'`, and this string
# deliberately does NOT match that pattern — otherwise one suite's teardown could
# delete the other's fixtures mid-run.
EMAIL_DOMAIN = "@suite-s3b.corujai.test"

# Sender prefix registry: 5521000 scheduling, 5522000 ai action, 5523000 owner
# notifications, 5524000 inbox, 5525000 confirmation, 5526000 settings,
# 5527000 class types, 5528000 accounts, 5529000 signup. This one is next.
SENDER_PREFIX = "5530000"

SUITE_PASSWORD = "suite-password-s3b"

MIGRATION_TENANT_ISOLATION = "011_tenant_isolation"

DEFAULT_REPORT_DIR = SRC_DIR / "tests" / "outputs"


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
            "step": step, "title": title, "status": status,
            "detail": detail, "traceback": trace,
            "seconds": round(time.monotonic() - started, 2),
        })
        self._print_line(step, title, status, detail)
        return status in ("PASS", "SKIP")

    def _print_line(self, step: str, title: str, status: str, detail: str) -> None:
        painter = {
            "PASS": self.console.green, "FAIL": self.console.red,
            "ERROR": self.console.red, "SKIP": self.console.yellow,
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
            print(self.console.green("\n Isolamento por tenant OK — todos os testes passaram.\n"))
        return not failed

    def to_json(self, path: Path) -> None:
        path.write_text(json.dumps({
            "passed": sum(1 for r in self.results if r["status"] == "PASS"),
            "failed": len(self.failed),
            "skipped": sum(1 for r in self.results if r["status"] == "SKIP"),
            "results": self.results,
        }, indent=2, ensure_ascii=False), encoding="utf-8")


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def expect_equal(actual: Any, expected: Any, what: str) -> None:
    if actual != expected:
        raise AssertionError(f"{what}: esperado {expected!r}, veio {actual!r}")


@contextlib.contextmanager
def patched(obj: Any, attr: str, value: Any) -> Iterator[None]:
    """Temporarily set obj.attr = value, restoring the original afterward."""
    original = getattr(obj, attr)
    setattr(obj, attr, value)
    try:
        yield
    finally:
        setattr(obj, attr, original)


def _drop_orphan_fixtures() -> None:
    """Delete anything a previous run left behind, before this one starts.

    This is what stands in for the /tmp backup the other suites keep. Scoped to
    the fixture prefix, the fixture email domain and the fixture sender prefix,
    so it can never touch the pilot or the founder's own account.

    Order matters: trial_bookings and owner_notifications have no foreign key to
    anything, so nothing deletes them for us; `messages` DOES cascade off
    `sessions` since migration 011, but only through the composite key.
    """
    removed: int = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE email LIKE %s", ("%" + EMAIL_DOMAIN,))
            removed = cur.rowcount
            like = TENANT_PREFIX + "%"
            for table in ("owner_notifications", "trial_bookings", "messages", "sessions",
                          "users", "class_types", "ai_configs", "scheduling_configs", "owners"):
                cur.execute(f"DELETE FROM {table} WHERE tenant_id LIKE %s", (like,))
            cur.execute("DELETE FROM sessions WHERE sender LIKE %s", (SENDER_PREFIX + "%",))
        conn.commit()

    if removed:
        print(f"  reparo: {removed} usuário(s) órfão(s) de uma run anterior removido(s)")


class TenantIsolationSuite:
    """Owns two fixture tenants, the isolation tests and the teardown."""

    def __init__(self, report: Report, keep: bool) -> None:
        self.report = report
        self.keep = keep
        self._n = 0
        self.app: Any = None
        self.tenants: list[str] = []
        # Filled by prepare_fixtures, used by every test.
        self.tenant_a: str = ""
        self.tenant_b: str = ""
        self.email_a: str = ""
        self.email_b: str = ""
        self.owner_phone_a: str = ""
        self.owner_phone_b: str = ""
        # The lead who exists at BOTH gyms — the whole reason for the composite key.
        self.shared_sender: str = ""

    # -- infrastructure -----------------------------------------------------

    def next_sender(self) -> str:
        self._n += 1
        return f"{SENDER_PREFIX}{self._n:06d}"

    def application(self) -> Any:
        """Build the Flask app once, reusing it across tests."""
        if self.app is None:
            import app as flask_app

            self.app = flask_app.create_app()
            self.app.config["TESTING"] = True
            # Flask-WTF does NOT disable CSRF for TESTING — it reads only
            # WTF_CSRF_ENABLED. Without this every POST here would answer 400
            # before reaching the code under test (Module S3c).
            self.app.config["WTF_CSRF_ENABLED"] = False
        return self.app

    def logged_in_client(self, email: str) -> Any:
        """Return a test client authenticated as one gym's owner.

        Through the REAL POST /dashboard/login, like every suite since S3a:
        stuffing Flask-Login's private session keys would prove nothing about
        whether current_user.tenant_id reaches the routes.
        """
        client = self.application().test_client()
        response = client.post(
            "/dashboard/login",
            data={"email": email, "password": SUITE_PASSWORD},
            follow_redirects=False,
        )
        expect(response.status_code in (302, 303), f"login de {email} não redirecionou")
        return client

    # -- prerequisites ------------------------------------------------------

    def check_schema(self) -> str:
        """Check migration 011 is applied and the three keys are in their new shape."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT version FROM schema_migrations WHERE version = %s",
                    (MIGRATION_TENANT_ISOLATION,),
                )
                if cur.fetchone() is None:
                    raise AssertionError(
                        f"a migration {MIGRATION_TENANT_ISOLATION} não foi aplicada — "
                        "rode `python app.py` uma vez"
                    )

                cur.execute(
                    """
                    SELECT pg_get_constraintdef(oid) AS def
                    FROM pg_constraint
                    WHERE contype = 'p' AND conrelid = 'sessions'::regclass
                    """
                )
                row = cur.fetchone()
                expect_equal(
                    row["def"] if row else None,
                    "PRIMARY KEY (tenant_id, sender)",
                    "chave primária de sessions",
                )

                cur.execute(
                    """
                    SELECT pg_get_constraintdef(oid) AS def
                    FROM pg_constraint
                    WHERE contype = 'f'
                      AND conrelid = 'messages'::regclass
                      AND confrelid = 'sessions'::regclass
                    """
                )
                row = cur.fetchone()
                expect(row is not None, "messages não tem chave estrangeira para sessions")
                expect(
                    "(tenant_id, sender)" in row["def"] and "ON DELETE CASCADE" in row["def"],
                    f"a FK de messages não é composta com CASCADE: {row['def']}",
                )

                cur.execute(
                    """
                    SELECT indexdef FROM pg_indexes
                    WHERE tablename = 'trial_bookings' AND indexdef LIKE '%UNIQUE%'
                    """
                )
                defs = [r["indexdef"] for r in cur.fetchall()]
                expect(
                    any("(tenant_id, calendar_event_id, sender)" in d for d in defs),
                    f"trial_bookings sem UNIQUE por tenant: {defs}",
                )

        return "011 aplicada; PK composta, FK composta e UNIQUE por tenant no lugar"

    def prepare_fixtures(self) -> str:
        """Provision the two gyms this whole suite compares against each other."""
        self.email_a = f"{TENANT_PREFIX}a{EMAIL_DOMAIN}"
        self.email_b = f"{TENANT_PREFIX}b{EMAIL_DOMAIN}"
        self.owner_phone_a = f"{SENDER_PREFIX}900001"
        self.owner_phone_b = f"{SENDER_PREFIX}900002"

        result_a = provision.provision_tenant(
            academy_name="Suite S3b Academia A",
            email=self.email_a,
            password=SUITE_PASSWORD,
            owner_phone=self.owner_phone_a,
            tenant_id=f"{TENANT_PREFIX}alfa",
        )
        result_b = provision.provision_tenant(
            academy_name="Suite S3b Academia B",
            email=self.email_b,
            password=SUITE_PASSWORD,
            owner_phone=self.owner_phone_b,
            tenant_id=f"{TENANT_PREFIX}beta",
        )
        self.tenant_a = result_a["tenant_id"]
        self.tenant_b = result_b["tenant_id"]
        self.tenants = [self.tenant_a, self.tenant_b]
        self.shared_sender = self.next_sender()

        # Distinct class labels, so a message composed with the wrong tenant's
        # labels is visible in the text rather than merely wrong in the database.
        class_types.update_class_type(
            "ADULTOS", "Adultos Alfa", None, False, tenant_id=self.tenant_a
        )
        class_types.update_class_type(
            "ADULTOS", "Adultos Beta", None, False, tenant_id=self.tenant_b
        )

        return f"tenants {self.tenant_a} e {self.tenant_b}; lead comum {self.shared_sender}"

    # -- tests --------------------------------------------------------------

    def test_01_same_sender_in_two_tenants(self) -> str:
        """O MESMO número existe nas duas academias, sem violar a chave primária.

        É a razão de ser da migration 011. Antes dela `sessions` era chaveada só
        por `sender`: a segunda academia simplesmente não conseguia registrar um
        lead que já treinava na primeira.
        """
        sender = self.shared_sender

        state_a = session_store.get_session(sender, tenant_id=self.tenant_a)
        state_a["lead_name"] = "Lead da Alfa"
        state_a["stage"] = "proposal"
        session_store.save_session(sender, state_a, tenant_id=self.tenant_a)

        state_b = session_store.get_session(sender, tenant_id=self.tenant_b)
        state_b["lead_name"] = "Lead da Beta"
        state_b["stage"] = "greeting"
        session_store.save_session(sender, state_b, tenant_id=self.tenant_b)

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tenant_id, lead_name FROM sessions WHERE sender = %s ORDER BY tenant_id",
                    (sender,),
                )
                rows = [dict(r) for r in cur.fetchall()]

        expect_equal(len(rows), 2, "linhas em sessions para o mesmo sender")
        return f"duas linhas convivem: {rows[0]['tenant_id']} e {rows[1]['tenant_id']}"

    def test_02_get_session_does_not_leak(self) -> str:
        """get_session devolve o estado DA academia perguntada, nunca o da outra."""
        sender = self.shared_sender

        state_a = session_store.get_session(sender, tenant_id=self.tenant_a)
        state_b = session_store.get_session(sender, tenant_id=self.tenant_b)

        expect_equal(state_a["lead_name"], "Lead da Alfa", "lead_name lido pela Alfa")
        expect_equal(state_b["lead_name"], "Lead da Beta", "lead_name lido pela Beta")
        expect_equal(state_a["stage"], "proposal", "stage da Alfa")
        expect_equal(state_b["stage"], "greeting", "stage da Beta")

        # session_exists é a porta que faz as rotas do inbox devolverem 404.
        only_b = self.next_sender()
        session_store.get_session(only_b, tenant_id=self.tenant_b)
        expect(
            not session_store.session_exists(only_b, tenant_id=self.tenant_a),
            "a Alfa não deveria enxergar um lead que só existe na Beta",
        )
        expect(
            session_store.session_exists(only_b, tenant_id=self.tenant_b),
            "a Beta deveria enxergar o próprio lead",
        )
        return "estado, e existência, resolvidos pelo par (tenant, sender)"

    def test_03_conversation_does_not_leak(self) -> str:
        """get_conversation, count_unread e list_conversations ficam na academia."""
        sender = self.shared_sender

        messages.add_message(sender, "lead", "oi, aqui é pra Alfa", tenant_id=self.tenant_a)
        messages.add_message(sender, "ai", "oi! bem-vindo à Alfa", is_read=True,
                             tenant_id=self.tenant_a)
        messages.add_message(sender, "lead", "oi, aqui é pra Beta", tenant_id=self.tenant_b)

        thread_a = messages.get_conversation(sender, tenant_id=self.tenant_a)
        thread_b = messages.get_conversation(sender, tenant_id=self.tenant_b)

        expect_equal(len(thread_a), 2, "mensagens vistas pela Alfa")
        expect_equal(len(thread_b), 1, "mensagens vistas pela Beta")
        expect(
            all("Beta" not in m["content"] for m in thread_a),
            "a conversa da Alfa trouxe texto da Beta",
        )

        expect_equal(messages.count_unread(sender, tenant_id=self.tenant_a), 1,
                     "não lidas na Alfa")
        expect_equal(messages.count_unread(sender, tenant_id=self.tenant_b), 1,
                     "não lidas na Beta")

        # Marcar como lida numa academia não mexe no contador da outra.
        messages.mark_conversation_read(sender, tenant_id=self.tenant_a)
        expect_equal(messages.count_unread(sender, tenant_id=self.tenant_a), 0,
                     "não lidas na Alfa depois de ler")
        expect_equal(messages.count_unread(sender, tenant_id=self.tenant_b), 1,
                     "não lidas na Beta depois da Alfa ler")

        senders_a = {row["sender"] for row in messages.list_conversations(self.tenant_a)}
        senders_b = {row["sender"] for row in messages.list_conversations(self.tenant_b)}
        expect(sender in senders_a and sender in senders_b, "o lead comum some de uma das listas")
        expect(
            not (senders_a - senders_b) & {s for s in senders_b if s not in senders_a},
            "conjuntos inconsistentes",
        )
        return "conversa, contador e lista do inbox isolados"

    def test_04_inbox_preview_comes_from_the_same_tenant(self) -> str:
        """A prévia e o badge do inbox vêm do MESMO tenant das linhas.

        A armadilha específica dos dois LEFT JOIN LATERAL: filtrar só o
        `sessions` de fora deixaria a lista certa e a prévia errada — a última
        mensagem que o mesmo número mandou para OUTRA academia.
        """
        row_a = next(r for r in messages.list_conversations(self.tenant_a)
                     if r["sender"] == self.shared_sender)
        row_b = next(r for r in messages.list_conversations(self.tenant_b)
                     if r["sender"] == self.shared_sender)

        expect(
            "Alfa" in (row_a["last_content"] or ""),
            f"prévia da Alfa veio errada: {row_a['last_content']!r}",
        )
        expect(
            "Beta" in (row_b["last_content"] or ""),
            f"prévia da Beta veio errada: {row_b['last_content']!r}",
        )
        expect_equal(row_a["unread_count"], 0, "badge da Alfa")
        expect_equal(row_b["unread_count"], 1, "badge da Beta")
        expect_equal(row_a["lead_name"], "Lead da Alfa", "nome mostrado pela Alfa")
        return "prévia e badge acompanham o tenant da linha"

    def test_05_bookings_do_not_leak(self) -> str:
        """A lista de agendamentos e o get_booking por id ficam na academia."""
        event_id = f"evt-shared-{uuid.uuid4().hex[:8]}"
        start = datetime.now(timezone.utc) + timedelta(days=2)

        created_a = bookings.create_booking_with_lock(
            calendar_event_id=event_id, sender=self.shared_sender, lead_name="Lead da Alfa",
            class_type="ADULTOS", slot_start=start, slot_end=start + timedelta(hours=1),
            capacity=None, tenant_id=self.tenant_a,
        )
        created_b = bookings.create_booking_with_lock(
            calendar_event_id=event_id, sender=self.shared_sender, lead_name="Lead da Beta",
            class_type="ADULTOS", slot_start=start, slot_end=start + timedelta(hours=1),
            capacity=None, tenant_id=self.tenant_b,
        )
        expect_equal(created_a["status"], "created", "reserva da Alfa")
        expect_equal(created_b["status"], "created", "reserva da Beta")
        self.booking_a: str = created_a["booking_id"]
        self.booking_b: str = created_b["booking_id"]

        ids_a = {b["id"] for b in bookings.list_bookings_for_review(self.tenant_a)}
        ids_b = {b["id"] for b in bookings.list_bookings_for_review(self.tenant_b)}
        expect(self.booking_a in ids_a, "a Alfa não vê a própria reserva")
        expect(self.booking_b not in ids_a, "a Alfa VÊ a reserva da Beta — vazamento")
        expect(self.booking_a not in ids_b, "a Beta VÊ a reserva da Alfa — vazamento")

        # A GUARDA: o id é uuid4 e acharia a linha sozinho. Com o tenant errado,
        # responde None — a mesma resposta de um id inexistente.
        expect(
            bookings.get_booking(self.booking_a, tenant_id=self.tenant_b) is None,
            "get_booking devolveu a reserva da Alfa para a Beta",
        )
        expect(
            bookings.get_booking(self.booking_a, tenant_id=self.tenant_a) is not None,
            "get_booking não achou a reserva na própria academia",
        )

        # E a mesma guarda no caminho de escrita.
        expect(
            not bookings.update_booking_status(self.booking_a, "cancelled",
                                               tenant_id=self.tenant_b),
            "a Beta conseguiu cancelar a reserva da Alfa",
        )
        expect_equal(
            bookings.get_booking(self.booking_a, tenant_id=self.tenant_a)["status"],
            "pending_confirmation",
            "status da reserva da Alfa depois da tentativa da Beta",
        )
        return "lista, leitura por id e escrita por id, todas guardadas pelo tenant"

    def test_06_trial_bookings_unique_is_tenant_scoped(self) -> str:
        """O mesmo (evento, lead) passa em tenants diferentes e é barrado no mesmo.

        As duas reservas do teste anterior já provaram a metade permissiva —
        mesmo `calendar_event_id`, mesmo `sender`, tenants diferentes. Falta a
        metade restritiva, que é a que impede o lead de ocupar duas vagas.
        """
        row = bookings.get_booking(self.booking_a, tenant_id=self.tenant_a)
        duplicate = bookings.create_booking_with_lock(
            calendar_event_id=row["calendar_event_id"], sender=row["sender"],
            lead_name=row["lead_name"], class_type=row["class_type"],
            slot_start=row["slot_start"], slot_end=row["slot_end"],
            capacity=None, tenant_id=self.tenant_a,
        )
        expect_equal(duplicate["status"], "duplicate", "segunda reserva no MESMO tenant")

        # E a contagem de vagas de uma academia não conta a reserva da outra.
        expect_equal(
            bookings.count_active_bookings(row["calendar_event_id"], tenant_id=self.tenant_a),
            1, "reservas ativas contadas pela Alfa",
        )
        return "UNIQUE por tenant permite o cruzado e barra o repetido"

    def test_07_active_bookings_by_sender(self) -> str:
        """A IA de uma academia não lê para o lead a aula que ele marcou na outra."""
        active_a = bookings.list_active_bookings_by_sender(
            self.shared_sender, tenant_id=self.tenant_a)
        active_b = bookings.list_active_bookings_by_sender(
            self.shared_sender, tenant_id=self.tenant_b)

        expect_equal(len(active_a), 1, "reservas ativas do lead na Alfa")
        expect_equal(len(active_b), 1, "reservas ativas do lead na Beta")
        expect_equal(active_a[0]["lead_name"], "Lead da Alfa", "de quem é a reserva vista pela Alfa")
        expect_equal(active_b[0]["lead_name"], "Lead da Beta", "de quem é a reserva vista pela Beta")
        return "cada academia injeta no prompt só a própria reserva"

    def test_08_slots_cache_is_per_tenant(self) -> str:
        """O cache de ~60s de horários não contamina uma academia com a outra.

        Armadilha #3. A chave é (tenant_id, days_ahead): sem o tenant, a primeira
        academia a pedir horários preencheria o cache e a segunda receberia a
        agenda dela por até um minuto — funcionando, e errado.
        """
        ai_context._slots_cache.clear()

        slot_a = {"event_id": "evt-alfa", "class_type": "ADULTOS", "label": "Horário da Alfa"}
        slot_b = {"event_id": "evt-beta", "class_type": "ADULTOS", "label": "Horário da Beta"}

        def fake_slots(days_ahead: int | None = None, tenant_id: str = "") -> list[dict]:
            return [slot_a] if tenant_id == self.tenant_a else [slot_b]

        with patched(ai_context.scheduling, "get_available_slots", fake_slots):
            first_a = ai_context.get_cached_slots(tenant_id=self.tenant_a)
            first_b = ai_context.get_cached_slots(tenant_id=self.tenant_b)

            expect_equal(first_a[0]["event_id"], "evt-alfa", "horários da Alfa")
            expect_equal(first_b[0]["event_id"], "evt-beta", "horários da Beta")

            # DENTRO do TTL, com a busca subjacente sabotada: o que voltar agora
            # veio do cache, e tem de continuar sendo o de cada academia.
            def explode(days_ahead: int | None = None, tenant_id: str = "") -> list[dict]:
                raise AssertionError("get_available_slots foi chamada de novo dentro do TTL")

            with patched(ai_context.scheduling, "get_available_slots", explode):
                cached_a = ai_context.get_cached_slots(tenant_id=self.tenant_a)
                cached_b = ai_context.get_cached_slots(tenant_id=self.tenant_b)

        expect_equal(cached_a[0]["event_id"], "evt-alfa", "cache da Alfa")
        expect_equal(cached_b[0]["event_id"], "evt-beta", "cache da Beta")

        keys = {key[0] for key in ai_context._slots_cache}
        expect(
            self.tenant_a in keys and self.tenant_b in keys,
            f"a chave do cache não carrega o tenant: {list(ai_context._slots_cache)}",
        )
        ai_context._slots_cache.clear()
        return "duas entradas distintas no cache, servidas do cache dentro do TTL"

    def test_09_composite_cascade(self) -> str:
        """Apagar a sessão (A, X) leva as mensagens de A e não toca nas de B.

        O CASCADE já existia; o que mudou é a chave por onde ele viaja. Com a FK
        antiga, apagar o lead numa academia levaria junto as mensagens que ele
        trocou com a outra.
        """
        sender = self.next_sender()
        session_store.get_session(sender, tenant_id=self.tenant_a)
        session_store.get_session(sender, tenant_id=self.tenant_b)
        messages.add_message(sender, "lead", "mensagem da Alfa", tenant_id=self.tenant_a)
        messages.add_message(sender, "lead", "mensagem da Beta", tenant_id=self.tenant_b)

        session_store.clear_session(sender, tenant_id=self.tenant_a)

        expect_equal(len(messages.get_conversation(sender, tenant_id=self.tenant_a)), 0,
                     "mensagens da Alfa depois do clear_session")
        expect_equal(len(messages.get_conversation(sender, tenant_id=self.tenant_b)), 1,
                     "mensagens da Beta depois do clear_session da Alfa")
        expect(
            session_store.session_exists(sender, tenant_id=self.tenant_b),
            "a sessão da Beta foi apagada junto",
        )
        return "cascade viaja pelo par (tenant, sender)"

    def test_10_owner_reply_stays_in_its_tenant(self) -> str:
        """O "1" do dono da Beta não fecha um agendamento da Alfa.

        register_owner_response() procura a notificação em aberto pelo telefone;
        escopar por tenant é o que garante que a resposta que chegou no número da
        Beta só possa resolver a fila da Beta.
        """
        owner_a = self._owner_row(self.tenant_a)
        owner_b = self._owner_row(self.tenant_b)

        owner_notifications.enqueue_notification(
            owner_id=owner_a["id"], owner_phone=self.owner_phone_a, event_type="booking",
            lead_sender=self.shared_sender, booking_id=self.booking_a, tenant_id=self.tenant_a,
        )
        owner_notifications.enqueue_notification(
            owner_id=owner_b["id"], owner_phone=self.owner_phone_b, event_type="booking",
            lead_sender=self.shared_sender, booking_id=self.booking_b, tenant_id=self.tenant_b,
        )
        self._mark_all_sent()

        # O dono da Beta responde, mas o webhook resolveu o tenant da Alfa (o
        # cenário do sequestro): não deve haver nada para carimbar.
        wrong = owner_notifications.register_owner_response(
            self.owner_phone_b, "confirmed", tenant_id=self.tenant_a)
        expect(wrong is None, "a resposta do dono da Beta carimbou uma notificação da Alfa")

        right = owner_notifications.register_owner_response(
            self.owner_phone_b, "confirmed", tenant_id=self.tenant_b)
        expect(right is not None, "a resposta do dono da Beta não achou a própria notificação")
        expect_equal(right["booking_id"], self.booking_b, "notificação carimbada")
        return "a fila do dono é resolvida dentro do tenant que recebeu o 1"

    def test_11_confirm_or_cancel_is_guarded(self) -> str:
        """A coordenadora recusa fechar a reserva de outra academia."""
        with patched(confirmations.whatsapp_service, "send_message", lambda *a, **k: None):
            refused = confirmations.confirm_or_cancel_booking(
                self.booking_a, "cancelled", tenant_id=self.tenant_b)
            expect_equal(refused["result"], "not_found",
                         "decisão da Beta sobre a reserva da Alfa")
            expect_equal(
                bookings.get_booking(self.booking_a, tenant_id=self.tenant_a)["status"],
                "pending_confirmation", "status da reserva da Alfa",
            )

            applied = confirmations.confirm_or_cancel_booking(
                self.booking_a, "confirmed", tenant_id=self.tenant_a)
            expect_equal(applied["result"], "applied", "decisão da Alfa sobre a própria reserva")

        thread = messages.get_conversation(self.shared_sender, tenant_id=self.tenant_a)
        notice = thread[-1]["content"]
        expect("Adultos Alfa" in notice,
               f"o aviso ao lead saiu com o rótulo da academia errada: {notice!r}")
        return "só a dona da reserva decide, e o aviso sai com o rótulo dela"

    def test_12_cron_resolves_the_tenant_per_row(self) -> str:
        """O cron compõe cada notificação com os dados do tenant DA LINHA.

        Armadilha #4: não há Flask, não há current_user, e assumir o piloto faria
        a mensagem do dono da Beta citar a turma da Alfa.

        A fila é global de propósito (é do sistema), então `main()` varreria
        também as linhas do piloto e as marcaria como enviadas. Por isso
        list_pending_notifications é substituída aqui pelas linhas desta suíte:
        o que está sob teste é a resolução POR LINHA dentro do laço, e nada do
        piloto pode ser alterado por uma suíte.
        """
        booking_a2 = self._new_booking(self.tenant_a, "Lead da Alfa")
        booking_b2 = self._new_booking(self.tenant_b, "Lead da Beta")

        owner_notifications.enqueue_notification(
            owner_id=self._owner_row(self.tenant_a)["id"], owner_phone=self.owner_phone_a,
            event_type="booking", lead_sender=self.shared_sender,
            booking_id=booking_a2, tenant_id=self.tenant_a,
        )
        owner_notifications.enqueue_notification(
            owner_id=self._owner_row(self.tenant_b)["id"], owner_phone=self.owner_phone_b,
            event_type="booking", lead_sender=self.shared_sender,
            booking_id=booking_b2, tenant_id=self.tenant_b,
        )

        pending = [
            row for row in owner_notifications.list_pending_notifications(drain.MAX_ATTEMPTS)
            if row["tenant_id"] in (self.tenant_a, self.tenant_b)
        ]
        expect_equal(len(pending), 2, "notificações pendentes das academias de teste")

        # O dublê recebe o tenant também (Módulo S3d): o cron passa o tenant da
        # LINHA para o send_message, e é ele que decide por qual número a
        # notificação sai. Capturado aqui para que o teste cubra as duas metades
        # da resolução por linha — o texto E o remetente.
        sent: list[tuple[str, str, str]] = []
        with patched(drain.owner_notifications, "list_pending_notifications",
                     lambda max_attempts: pending), \
             patched(drain.whatsapp_service, "send_message",
                     lambda phone, text, tenant_id="default": sent.append(
                         (phone, text, tenant_id))):
            expect_equal(drain.main(), 0, "código de saída do cron")

        by_phone = {phone: text for phone, text, _ in sent}
        tenant_by_phone = {phone: tenant for phone, _, tenant in sent}
        expect(self.owner_phone_b in by_phone,
               f"o dono da Beta não recebeu nada: {list(by_phone)}")
        expect(
            "Adultos Beta" in by_phone[self.owner_phone_b],
            f"a mensagem do dono da Beta saiu com a turma errada: {by_phone[self.owner_phone_b]!r}",
        )
        expect(
            "Adultos Alfa" not in by_phone[self.owner_phone_b],
            "a mensagem do dono da Beta cita a turma da Alfa",
        )
        expect(
            "Adultos Alfa" in by_phone[self.owner_phone_a],
            f"a mensagem do dono da Alfa saiu errada: {by_phone[self.owner_phone_a]!r}",
        )
        expect_equal(tenant_by_phone[self.owner_phone_a], self.tenant_a,
                     "o envio ao dono da Alfa saiu pelo tenant errado")
        expect_equal(tenant_by_phone[self.owner_phone_b], self.tenant_b,
                     "o envio ao dono da Beta saiu pelo tenant errado")
        return "cada envio resolve rótulos, reserva e remetente no tenant da própria linha"

    def test_13_dashboard_shows_only_its_own_tenant(self) -> str:
        """Logado como a Beta, o painel não mostra nada da Alfa.

        Armadilha #5, ponta a ponta: é fácil parametrizar a função e esquecer de
        passar o valor na rota — e aí tudo continua lendo 'default' e o
        isolamento não acontece de fato. Só um login de verdade prova.
        """
        client_b = self.logged_in_client(self.email_b)

        inbox = client_b.get("/dashboard/inbox")
        expect_equal(inbox.status_code, 200, "status do inbox da Beta")
        body = inbox.get_data(as_text=True)
        expect("Lead da Beta" in body, "o inbox da Beta não mostra o próprio lead")
        expect("Lead da Alfa" not in body, "o inbox da Beta mostra o lead da Alfa")

        listing = client_b.get("/dashboard/bookings")
        expect_equal(listing.status_code, 200, "status da lista de agendamentos da Beta")
        bookings_body = listing.get_data(as_text=True)
        expect(self.booking_b in bookings_body, "a Beta não vê a própria reserva na tela")
        expect(self.booking_a not in bookings_body, "a Beta vê a reserva da Alfa na tela")

        return "inbox e agendamentos servidos por current_user.tenant_id"

    def test_14_dashboard_cannot_reach_another_tenant_by_url(self) -> str:
        """Digitar o número ou o id da outra academia na URL não abre nada."""
        client_b = self.logged_in_client(self.email_b)

        only_a = self.next_sender()
        session_store.get_session(only_a, tenant_id=self.tenant_a)
        messages.add_message(only_a, "lead", "segredo da Alfa", tenant_id=self.tenant_a)

        expect_equal(client_b.get(f"/dashboard/inbox/{only_a}").status_code, 404,
                     "conversa da Alfa aberta pela Beta")
        expect_equal(client_b.get(f"/dashboard/inbox/{only_a}/messages").status_code, 404,
                     "parcial de mensagens da Alfa aberta pela Beta")
        expect_equal(client_b.post(f"/dashboard/inbox/{only_a}/reply",
                                   data={"text": "invadindo"}).status_code, 404,
                     "resposta da Beta numa conversa da Alfa")
        expect_equal(client_b.post(f"/dashboard/inbox/{only_a}/resume").status_code, 404,
                     "devolução à IA feita pela Beta numa conversa da Alfa")

        expect_equal(len(messages.get_conversation(only_a, tenant_id=self.tenant_a)), 1,
                     "a conversa da Alfa ganhou uma mensagem da Beta")

        # E a decisão por id: responde 200 com aviso, sem mexer na reserva.
        decision = client_b.post(f"/dashboard/bookings/{self.booking_a}/cancel")
        expect_equal(decision.status_code, 200, "status da decisão indevida")
        expect(
            "não encontrado" in decision.get_data(as_text=True).lower(),
            "a tela não avisou que o agendamento não foi encontrado",
        )
        expect_equal(
            bookings.get_booking(self.booking_a, tenant_id=self.tenant_a)["status"],
            "confirmed", "a reserva da Alfa mudou por um POST da Beta",
        )
        return "404 no inbox, 'não encontrado' nos agendamentos, nada escrito"

    def test_15_settings_screen_is_per_tenant(self) -> str:
        """A tela de configurações lê e grava na academia de quem está logado.

        Antes do S3b ela lia e gravava sempre no piloto — o dono da B abriria a
        personalidade da IA do piloto e salvá-la sobrescreveria a dele.
        """
        client_b = self.logged_in_client(self.email_b)

        page = client_b.get("/dashboard/settings")
        expect_equal(page.status_code, 200, "status da tela de configurações")
        body = page.get_data(as_text=True)
        expect("Adultos Beta" in body, "as turmas mostradas não são as da Beta")
        expect("Adultos Alfa" not in body, "a tela da Beta mostra a turma da Alfa")

        saved = client_b.post("/dashboard/settings/ai", data={
            "academy_name": "Academia Beta Renomeada",
            "assistant_name": "Bia",
            "tone": "Direto",
            "business_info": "Só teste",
            "flow_emphasis": "Agendar",
        })
        expect_equal(saved.status_code, 200, "status do POST da IA")

        import bot.ai_configs as ai_configs

        expect_equal(
            ai_configs.get_ai_config(self.tenant_b)["academy_name"],
            "Academia Beta Renomeada", "config da IA da Beta",
        )
        expect(
            ai_configs.get_ai_config(self.tenant_a)["academy_name"] != "Academia Beta Renomeada",
            "salvar na Beta sobrescreveu a configuração da Alfa",
        )
        return "leitura e escrita da tela presas ao tenant logado"

    def test_16_no_s3b_marker_is_left(self) -> str:
        """Nenhum marcador `# S3b:` sobrou no código — critério de pronto do módulo.

        ESTE ARQUIVO É PULADO, e não por conveniência: ele precisa escrever o
        marcador literalmente para poder procurá-lo, então casaria consigo mesmo
        e o teste nunca poderia passar. O que está sendo verificado é o código de
        produção, que é onde a costura ficou aberta.
        """
        this_file = Path(__file__).resolve()
        found: list[str] = []

        for path in SRC_DIR.rglob("*.py"):
            if "__pycache__" in path.parts or path.resolve() == this_file:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"#\s*S3b:", line):
                    found.append(f"{path.relative_to(SRC_DIR)}:{number}")

        expect(not found, "ainda há costura aberta: " + ", ".join(found))
        return "grep de `# S3b:` limpo em todo o código de produção"

    def test_17_teardown_leaves_nothing(self) -> str:
        """A limpeza remove os dois tenants e tudo o que pende deles."""
        if self.keep:
            raise SkipTest("--keep pedido: as fixtures ficam no banco de propósito")

        self.teardown()
        leftovers: dict[str, int] = {}
        with get_connection() as conn:
            with conn.cursor() as cur:
                for table in ("owner_notifications", "trial_bookings", "messages",
                              "sessions", "users", "class_types", "ai_configs",
                              "scheduling_configs", "owners"):
                    cur.execute(
                        f"SELECT COUNT(*) AS total FROM {table} WHERE tenant_id LIKE %s",
                        (TENANT_PREFIX + "%",),
                    )
                    total = int(cur.fetchone()["total"])
                    if total:
                        leftovers[table] = total

        expect(not leftovers, f"sobrou fixture no banco: {leftovers}")
        return "nenhuma conta nem tenant pendurado"

    # -- helpers ------------------------------------------------------------

    def _new_booking(self, tenant_id: str, lead_name: str) -> str:
        """Create one fresh booking for a tenant and return its id."""
        start = datetime.now(timezone.utc) + timedelta(days=3)
        result = bookings.create_booking_with_lock(
            calendar_event_id=f"evt-{uuid.uuid4().hex[:10]}", sender=self.shared_sender,
            lead_name=lead_name, class_type="ADULTOS", slot_start=start,
            slot_end=start + timedelta(hours=1), capacity=None, tenant_id=tenant_id,
        )
        expect_equal(result["status"], "created", f"reserva auxiliar em {tenant_id}")
        return result["booking_id"]

    def _owner_row(self, tenant_id: str) -> dict:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM owners WHERE tenant_id = %s", (tenant_id,))
                return dict(cur.fetchone())

    def _mark_all_sent(self) -> None:
        """Move this suite's notifications to 'sent' so a reply can be recorded.

        register_owner_response() only ever stamps a notification the cron has
        already delivered. Doing it in SQL keeps the test off the WhatsApp path.
        """
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE owner_notifications
                    SET status = 'sent', sent_at = NOW()
                    WHERE tenant_id = ANY(%s)
                    """,
                    (self.tenants,),
                )
            conn.commit()

    def teardown(self) -> None:
        """Delete both fixture tenants and everything hanging off them."""
        if self.keep:
            print("  --keep: fixtures preservadas.")
            return

        with get_connection() as conn:
            with conn.cursor() as cur:
                for table in ("owner_notifications", "trial_bookings", "messages", "sessions",
                              "users", "class_types", "ai_configs", "scheduling_configs",
                              "owners"):
                    cur.execute(
                        f"DELETE FROM {table} WHERE tenant_id LIKE %s", (TENANT_PREFIX + "%",)
                    )
                cur.execute("DELETE FROM users WHERE email LIKE %s", ("%" + EMAIL_DOMAIN,))
                cur.execute("DELETE FROM sessions WHERE sender LIKE %s", (SENDER_PREFIX + "%",))
            conn.commit()


def _resolve_report_path(raw: str | None) -> Path:
    if raw:
        return Path(raw).expanduser().resolve()
    DEFAULT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return DEFAULT_REPORT_DIR / f"tenant-isolation-{stamp}.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Suíte E2E do isolamento por tenant (Módulo S3b)")
    parser.add_argument("--keep", action="store_true",
                        help="não remove os tenants de teste ao final")
    parser.add_argument("--json", nargs="?", const="", default=None,
                        help="grava o relatório em JSON (caminho opcional)")
    parser.add_argument("--no-color", action="store_true", help="desliga as cores ANSI")
    args = parser.parse_args()

    logging.basicConfig(level=logging.ERROR, format="%(levelname)s - %(message)s")

    console = Console(color=not args.no_color and sys.stdout.isatty())
    report = Report(console)

    print(console.bold("\n Suíte de isolamento por tenant — Módulo S3b"))
    print(console.dim(" Duas academias de teste, o mesmo lead nas duas, e a pergunta"))
    print(console.dim(" que importa em cada passo: uma enxerga a outra?\n"))

    report.section("Pré-requisitos")
    _drop_orphan_fixtures()

    suite = TenantIsolationSuite(report, keep=args.keep)
    if not report.run("P1", "Migration 011 aplicada: PK, FK e UNIQUE no formato novo",
                      suite.check_schema):
        print(console.red("\n Pré-requisitos falharam — a suíte não pode continuar."))
        report.summary()
        sys.exit(1)

    report.section("Preparo")
    atexit.register(suite.teardown)
    if not report.run("F1", "Duas academias provisionadas, com turmas de nomes distintos",
                      suite.prepare_fixtures):
        print(console.red("\n Sem as fixtures não há o que comparar."))
        report.summary()
        sys.exit(1)

    report.section("Roteiro de testes")
    tests: list[tuple[str, str, Callable[[], str | None]]] = [
        ("1", "O mesmo lead existe nas duas academias (chave composta)",
         suite.test_01_same_sender_in_two_tenants),
        ("2", "get_session e session_exists não vazam",
         suite.test_02_get_session_does_not_leak),
        ("3", "Conversa, não lidas e lista do inbox não vazam",
         suite.test_03_conversation_does_not_leak),
        ("4", "A prévia e o badge do inbox vêm do mesmo tenant das linhas",
         suite.test_04_inbox_preview_comes_from_the_same_tenant),
        ("5", "Agendamentos não vazam, nem pela lista nem por id (guarda)",
         suite.test_05_bookings_do_not_leak),
        ("6", "O UNIQUE de trial_bookings é por tenant",
         suite.test_06_trial_bookings_unique_is_tenant_scoped),
        ("7", "A IA injeta só a reserva feita na própria academia",
         suite.test_07_active_bookings_by_sender),
        ("8", "O cache de horários é por tenant (Armadilha #3)",
         suite.test_08_slots_cache_is_per_tenant),
        ("9", "O CASCADE viaja pela chave composta (Armadilha #1)",
         suite.test_09_composite_cascade),
        ("10", "O 1/2 do dono resolve só a fila da própria academia",
         suite.test_10_owner_reply_stays_in_its_tenant),
        ("11", "confirm_or_cancel_booking é guardado pelo tenant",
         suite.test_11_confirm_or_cancel_is_guarded),
        ("12", "O cron resolve o tenant por linha (Armadilha #4)",
         suite.test_12_cron_resolves_the_tenant_per_row),
        ("13", "O painel mostra só a academia logada (Armadilha #5)",
         suite.test_13_dashboard_shows_only_its_own_tenant),
        ("14", "Nem digitando a URL da outra academia",
         suite.test_14_dashboard_cannot_reach_another_tenant_by_url),
        ("15", "A tela de configurações é por tenant",
         suite.test_15_settings_screen_is_per_tenant),
        ("16", "Nenhum marcador `# S3b:` sobrou",
         suite.test_16_no_s3b_marker_is_left),
        ("17", "A limpeza não deixa conta nem tenant pendurado",
         suite.test_17_teardown_leaves_nothing),
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
