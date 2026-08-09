"""Automated end-to-end suite for the funnel metrics screen (Module S4).

Runs the scenarios documented in METRICS_TESTING.md and prints a PASS/FAIL
report boiled down to an exit code, in the same style as
tests/test_tenant_isolation/test_tenant_isolation_suite.py.

Fully deterministic: no LLM, no WhatsApp, no Google Calendar. Everything here is
Postgres plus the Flask test client.

TWO THINGS THIS SUITE EXISTS TO PIN, above the ordinary arithmetic:

1. THE NUMBERS COME FROM THE LOG, NOT FROM THE SESSION STATE. `sessions.stage`
   is a snapshot that moves; `trial_bookings` is a durable ledger. Test 3 sets a
   lead's stage to 'booked' with no booking row behind it and proves the count
   does not budge — which is the assertion that would have caught the obvious
   wrong implementation.

2. NOTHING HERE MEASURES ATTENDANCE (problem P1). Test 9 fails the run if an
   attendance-shaped key or funnel label ever appears, and also fails if the
   honest disclaimer is ever deleted from the screen. `confirmed` means the
   owner said the class will happen; deriving "showed up" from it would be a
   fabricated number on the one screen the owner trusts.

NO /tmp BACKUP FILE, deliberately — the same reasoning as test_accounts and
test_tenant_isolation. This suite never writes to the pilot: every scenario runs
on three fixture tenants built by provision_tenant() under the prefix below.
_drop_orphan_fixtures(), called at the start of main(), plays the crash-repair
role the backup file plays elsewhere.

Run from src/:
    python tests/test_metrics/test_metrics_suite.py
    python tests/test_metrics/test_metrics_suite.py --keep
    python tests/test_metrics/test_metrics_suite.py --json

Exit code is 0 only when every test passed.
"""

import argparse
import atexit
import json
import logging
import re
import sys
import time
import traceback
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

# Locate src/ by NAME, like app.py and the other suites.
SRC_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "src")
sys.path.insert(0, str(SRC_DIR))

import accounts.provision as provision  # noqa: E402
import bot.bookings as bookings  # noqa: E402
import bot.messages as messages  # noqa: E402
import bot.metrics as metrics  # noqa: E402
import bot.session as session_store  # noqa: E402
from database.db import get_connection  # noqa: E402

# Fixture tenants. Hyphens, because these ids come out of the real slug generator.
TENANT_PREFIX = "suite-s4-"

# A DOMAIN OF THIS SUITE'S OWN, for the same reason test_tenant_isolation has
# one: test_accounts tears down with
# `DELETE FROM users WHERE email LIKE '%@suite.corujai.test'`, and this string
# deliberately does NOT match that pattern.
EMAIL_DOMAIN = "@suite-s4.corujai.test"

# Sender prefix registry: 5521000 scheduling, 5522000 ai action, 5523000 owner
# notifications, 5524000 inbox, 5525000 confirmation, 5526000 settings,
# 5527000 class types, 5528000 accounts, 5529000 signup, 5530000 tenant
# isolation. This one is next.
SENDER_PREFIX = "5531000"

SUITE_PASSWORD = "suite-password-s4"

DEFAULT_REPORT_DIR = SRC_DIR / "tests" / "outputs"

# Vocabulary that must never label a number on this screen (problem P1). Applied
# to the aggregate's KEYS and to the funnel's LABELS — not to the page's prose,
# which says out loud that attendance is not measured and must keep saying it.
ATTENDANCE_WORDS = re.compile(r"comparec|presen[çc]|show.?rate|frequ[êe]nc|attend", re.IGNORECASE)

# The disclaimer the screen must keep carrying, so nobody deletes it quietly.
DISCLAIMER_FRAGMENT = "não registra se a pessoa compareceu"


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
            print(self.console.green("\n Métricas do funil OK — todos os testes passaram.\n"))
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


def _drop_orphan_fixtures() -> None:
    """Delete anything a previous run left behind, before this one starts.

    This is what stands in for the /tmp backup the other suites keep. Scoped to
    the fixture prefix, the fixture email domain and the fixture sender prefix,
    so it can never touch the pilot or the founder's own account.
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


class MetricsSuite:
    """Owns three fixture tenants, the funnel tests and the teardown.

    THE SEEDED SHAPE IS THE FIXTURE, and every expected number below is derived
    from it by hand rather than recomputed with the code under test — a test
    that asks the implementation what the answer should be proves nothing.

    Gym A (the one under test), with days measured back from today:

        leads (first contact)        bookings (created_at -> status today)
        ------------------------     ------------------------------------
        3 leads  ->   2 days ago     2 days ago:  1 confirmed, 1 cancelled, 1 pending
        2 leads  ->  15 days ago    15 days ago:  1 confirmed
        1 lead   ->  45 days ago    45 days ago:  1 cancelled
        1 lead   -> 200 days ago   200 days ago:  1 confirmed
        1 lead   -> 200 days ago, but writing again 1 day ago (the RETURNING lead)

    Which gives, per window:

                    7d      30d     90d
        leads        3        5       6
        booked       3        4       5
        confirmed    1        2       2
        cancelled    1        1       2
        pending      1        1       1

    Gym B is seeded flat and much bigger (7 leads, 6 bookings, all confirmed,
    all 2 days ago) so that ANY leak into A's numbers changes them visibly
    instead of hiding inside a plausible total.

    Gym C is seeded with nothing at all: it is the zero/division-by-zero case.
    """

    def __init__(self, report: Report, keep: bool) -> None:
        self.report = report
        self.keep = keep
        self._n = 0
        self.app: Any = None
        self.tenant_a: str = ""
        self.tenant_b: str = ""
        self.tenant_c: str = ""
        self.email_a: str = ""
        self.email_b: str = ""
        self.email_c: str = ""
        # A lead of A whose stage will be forced to 'booked' with no booking row.
        self.stage_liar: str = ""
        # A lead of A who has a booking but whose stage stays at 'interest'.
        self.stage_quiet: str = ""

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
            # WTF_CSRF_ENABLED. The login POST below would answer 400 without
            # this (Module S3c). This suite is the eighth file to need the line.
            self.app.config["WTF_CSRF_ENABLED"] = False
        return self.app

    def logged_in_client(self, email: str) -> Any:
        """Return a test client authenticated as one gym's owner.

        Through the REAL POST /dashboard/login, like every suite since S3a:
        stuffing Flask-Login's private session keys would prove nothing about
        whether current_user.tenant_id reaches the metrics route.
        """
        client = self.application().test_client()
        response = client.post(
            "/dashboard/login",
            data={"email": email, "password": SUITE_PASSWORD},
            follow_redirects=False,
        )
        expect(response.status_code in (302, 303), f"login de {email} não redirecionou")
        return client

    # -- seeding ------------------------------------------------------------

    def _days_ago(self, days: int) -> datetime:
        """An instant N days back, at midday LOCAL time.

        Midday, not midnight: the window boundary is local midnight, and a
        fixture sitting exactly on it would make an off-by-one in
        period_window() pass half the time. Twelve hours of slack on each side
        makes every seeded row unambiguously inside or outside.
        """
        return (datetime.now(metrics.TIMEZONE) - timedelta(days=days)).replace(
            hour=12, minute=0, second=0, microsecond=0
        )

    def _add_message_at(
        self, tenant_id: str, sender: str, when: datetime, content: str = "oi"
    ) -> None:
        """Record a lead message through the real writer, then move its clock.

        `messages.created_at` defaults to NOW() and add_message() takes no
        override, so the instant is set afterwards on the row just written —
        located by the highest id for the pair, which is what the ordering trap
        note in bot/messages.py relies on too.
        """
        messages.add_message(sender, "lead", content, tenant_id=tenant_id)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE messages SET created_at = %s
                    WHERE id = (
                        SELECT id FROM messages
                        WHERE tenant_id = %s AND sender = %s
                        ORDER BY id DESC LIMIT 1
                    )
                    """,
                    (when, tenant_id, sender),
                )
            conn.commit()

    def seed_lead(self, tenant_id: str, first_contact_days: int) -> str:
        """Create one lead whose first contact sits N days back."""
        sender: str = self.next_sender()
        session_store.get_session(sender, tenant_id=tenant_id)
        self._add_message_at(tenant_id, sender, self._days_ago(first_contact_days))
        return sender

    def seed_booking(
        self, tenant_id: str, sender: str, created_days_ago: int, status: str
    ) -> str:
        """Create one booking through the real writer, backdated and set."""
        slot_start: datetime = datetime.now(metrics.TIMEZONE) + timedelta(days=3)
        result: dict = bookings.create_booking_with_lock(
            calendar_event_id=f"evt-s4-{uuid.uuid4().hex[:12]}",
            sender=sender,
            lead_name="Lead da Suíte",
            class_type="ADULTOS",
            slot_start=slot_start,
            slot_end=slot_start + timedelta(hours=1),
            capacity=None,
            tenant_id=tenant_id,
        )
        expect_equal(result["status"], "created", "criação da reserva de fixture")
        booking_id: str = result["booking_id"]

        if status != "pending_confirmation":
            bookings.update_booking_status(booking_id, status, tenant_id=tenant_id)

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE trial_bookings SET created_at = %s WHERE tenant_id = %s AND id = %s",
                    (self._days_ago(created_days_ago), tenant_id, booking_id),
                )
            conn.commit()

        return booking_id

    def prepare_fixtures(self) -> str:
        """Provision the three gyms and seed the shape documented on the class."""
        self.email_a = f"{TENANT_PREFIX}a{EMAIL_DOMAIN}"
        self.email_b = f"{TENANT_PREFIX}b{EMAIL_DOMAIN}"
        self.email_c = f"{TENANT_PREFIX}c{EMAIL_DOMAIN}"

        self.tenant_a = provision.provision_tenant(
            academy_name="Suite S4 Academia A", email=self.email_a,
            password=SUITE_PASSWORD, owner_phone=f"{SENDER_PREFIX}900001",
            tenant_id=f"{TENANT_PREFIX}alfa",
        )["tenant_id"]
        self.tenant_b = provision.provision_tenant(
            academy_name="Suite S4 Academia B", email=self.email_b,
            password=SUITE_PASSWORD, owner_phone=f"{SENDER_PREFIX}900002",
            tenant_id=f"{TENANT_PREFIX}beta",
        )["tenant_id"]
        self.tenant_c = provision.provision_tenant(
            academy_name="Suite S4 Academia C", email=self.email_c,
            password=SUITE_PASSWORD, owner_phone=f"{SENDER_PREFIX}900003",
            tenant_id=f"{TENANT_PREFIX}gama",
        )["tenant_id"]

        # --- Gym A: leads ---
        recent: list[str] = [self.seed_lead(self.tenant_a, 2) for _ in range(3)]
        mid: list[str] = [self.seed_lead(self.tenant_a, 15) for _ in range(2)]
        old: str = self.seed_lead(self.tenant_a, 45)
        self.seed_lead(self.tenant_a, 200)

        # THE RETURNING LEAD. First contact 200 days ago, active yesterday. He is
        # the reason the leads count reads `messages` instead of
        # `sessions.conversation_started_at`, which the 1h timeout rewrites.
        returning: str = self.seed_lead(self.tenant_a, 200)
        self._add_message_at(self.tenant_a, returning, self._days_ago(1), "voltei")

        # --- Gym A: bookings, anchored on created_at ---
        self.seed_booking(self.tenant_a, recent[0], 2, "confirmed")
        self.seed_booking(self.tenant_a, recent[1], 2, "cancelled")
        self.seed_booking(self.tenant_a, recent[2], 2, "pending_confirmation")
        self.seed_booking(self.tenant_a, mid[0], 15, "confirmed")
        self.seed_booking(self.tenant_a, old, 45, "cancelled")
        self.seed_booking(self.tenant_a, returning, 200, "confirmed")

        # Two senders test 3 will use to prove the numbers ignore `stage`.
        self.stage_liar = mid[1]     # stage 'booked', no booking row at all
        self.stage_quiet = recent[0]  # stage 'interest', but a real booking

        # --- Gym B: flat, bigger, all confirmed, all recent ---
        # 7 leads, 6 of them with a booking, so B's shape (7/6/6/0) shares no
        # number with A's (5/4/2/1) — a leak cannot hide as a plausible total.
        for index in range(7):
            sender_b: str = self.seed_lead(self.tenant_b, 2)
            if index < 6:
                self.seed_booking(self.tenant_b, sender_b, 2, "confirmed")

        # --- Gym C: nothing. That is the fixture. ---

        return f"tenants {self.tenant_a}, {self.tenant_b} e {self.tenant_c} semeados"

    # -- tests --------------------------------------------------------------

    def test_01_leads_come_from_first_contact(self) -> str:
        """Leads contam o PRIMEIRO contato dentro da janela, e só ele.

        3 leads falaram há 2 dias, 2 há 15, 1 há 45 e 2 há 200. A janela de 7
        dias tem que enxergar exatamente os 3 primeiros — os de fora não entram
        por serem antigos, e nenhum entra duas vezes por ter várias mensagens.
        """
        expect_equal(metrics.get_funnel(self.tenant_a, days=7)["leads"], 3, "leads em 7d")
        expect_equal(metrics.get_funnel(self.tenant_a, days=30)["leads"], 5, "leads em 30d")
        expect_equal(metrics.get_funnel(self.tenant_a, days=90)["leads"], 6, "leads em 90d")
        return "3 / 5 / 6 leads em 7 / 30 / 90 dias"

    def test_02_returning_lead_is_not_a_new_lead(self) -> str:
        """Um lead antigo que volta a escrever NÃO vira lead novo.

        É a armadilha que derrubaria a implementação óbvia. A coluna que parece
        servir — `sessions.conversation_started_at` — é reescrita pelo timeout de
        1h (bot/handlers.py::_reset_session), então esse lead de 200 dias atrás,
        ativo ontem, apareceria como chegada de ontem. `MIN(messages.created_at)`
        não se move.
        """
        funnel: dict = metrics.get_funnel(self.tenant_a, days=7)
        expect_equal(funnel["leads"], 3, "leads em 7d com o lead que voltou")

        # E a prova de que ele de fato escreveu dentro da janela: se não tivesse
        # escrito, este teste passaria por acidente.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) AS recentes FROM messages
                    WHERE tenant_id = %s AND created_at >= %s
                    """,
                    (self.tenant_a, self._days_ago(3)),
                )
                recentes: int = cur.fetchone()["recentes"]
        expect(recentes >= 4, f"o lead que voltou não escreveu na janela ({recentes} msgs)")
        return f"{recentes} mensagens recentes, ainda assim 3 leads novos"

    def test_03_numbers_ignore_session_stage(self) -> str:
        """Os números vêm de `trial_bookings`, nunca de `sessions.stage`.

        `stage` é o estado ATUAL de uma conversa, não um histórico: contar
        "agendou" por `stage = 'booked'` subconta e superconta ao mesmo tempo.
        Aqui um lead recebe `stage='booked'` sem nenhuma reserva, e outro fica em
        'interest' tendo uma reserva de verdade. Os dois números têm que ignorar
        o palpite e seguir o log.
        """
        before: dict = metrics.get_funnel(self.tenant_a, days=30)

        liar: dict = session_store.get_session(self.stage_liar, tenant_id=self.tenant_a)
        liar["stage"] = "booked"
        session_store.save_session(self.stage_liar, liar, tenant_id=self.tenant_a)

        quiet: dict = session_store.get_session(self.stage_quiet, tenant_id=self.tenant_a)
        quiet["stage"] = "interest"
        session_store.save_session(self.stage_quiet, quiet, tenant_id=self.tenant_a)

        after: dict = metrics.get_funnel(self.tenant_a, days=30)
        expect_equal(after["booked"], before["booked"], "agendamentos após mexer no stage")
        expect_equal(after["booked"], 4, "agendamentos em 30d")
        expect_equal(after["confirmed"], 2, "confirmados em 30d")
        return "stage='booked' sem reserva não contou; stage='interest' com reserva contou"

    def test_04_statuses_split_one_cohort(self) -> str:
        """Confirmados/cancelados/pendentes fatiam UMA coorte: agendamentos.

        A soma tem que fechar em todas as janelas. É a propriedade que deixa o
        dono conferir a tela de cabeça, e a razão de a coorte estar ancorada em
        `created_at` e não em `updated_at`.
        """
        expected: dict[int, tuple[int, int, int, int]] = {
            # days: (booked, confirmed, cancelled, pending)
            7: (3, 1, 1, 1),
            30: (4, 2, 1, 1),
            90: (5, 2, 2, 1),
        }
        for days, (booked, confirmed, cancelled, pending) in expected.items():
            funnel: dict = metrics.get_funnel(self.tenant_a, days=days)
            expect_equal(funnel["booked"], booked, f"agendamentos em {days}d")
            expect_equal(funnel["confirmed"], confirmed, f"confirmados em {days}d")
            expect_equal(funnel["cancelled"], cancelled, f"cancelados em {days}d")
            expect_equal(funnel["pending"], pending, f"pendentes em {days}d")
            expect_equal(
                funnel["confirmed"] + funnel["cancelled"] + funnel["pending"],
                funnel["booked"],
                f"soma dos status em {days}d",
            )
        return "a soma fecha em 7, 30 e 90 dias"

    def test_05_tenant_isolation(self) -> str:
        """Os números de A não incluem nada de B, e vice-versa (Armadilha #3).

        A academia B foi semeada grande e chapada (7 leads, 6 reservas, todas
        confirmadas) justamente para um vazamento aparecer como um número
        estranho, e não como um total plausível. Um `COUNT(*)` sem
        `WHERE tenant_id` aqui mostraria 12 leads em vez de 5.
        """
        a: dict = metrics.get_funnel(self.tenant_a, days=30)
        b: dict = metrics.get_funnel(self.tenant_b, days=30)

        expect_equal(a["leads"], 5, "leads de A em 30d")
        expect_equal(a["booked"], 4, "agendamentos de A em 30d")
        expect_equal(b["leads"], 7, "leads de B em 30d")
        expect_equal(b["booked"], 6, "agendamentos de B em 30d")
        expect_equal(b["confirmed"], 6, "confirmados de B em 30d")
        expect_equal(b["cancelled"], 0, "cancelados de B em 30d")

        # A soma dos dois não pode aparecer em nenhum dos dois.
        expect(a["leads"] + b["leads"] == 12, "fixture inconsistente")
        expect(a["leads"] != 12 and b["leads"] != 12, "um dos tenants enxergou o outro")
        return "A: 5/4 · B: 7/6 — nenhum dos dois viu 12"

    def test_06_period_windows_cut_correctly(self) -> str:
        """7/30/90 recortam de verdade, e a janela alinha na meia-noite local.

        A reserva de 45 dias atrás é a testemunha: dentro de 90, fora de 30 e de
        7. E `period_window()` tem que devolver instantes AWARE começando à
        meia-noite de São Paulo — um datetime ingênuo seria lido no fuso do
        servidor (UTC na Railway) e moveria a fronteira em 3 horas.
        """
        for days in metrics.ALLOWED_PERIODS:
            start, end = metrics.period_window(days)
            expect(start.tzinfo is not None and end.tzinfo is not None,
                   f"janela de {days}d não é timezone-aware")
            expect_equal(
                (start.hour, start.minute, start.second, start.microsecond),
                (0, 0, 0, 0),
                f"início da janela de {days}d não é meia-noite",
            )
            # O FUSO, não o offset. Asserir -03:00 na mão passaria hoje (o
            # Brasil não tem horário de verão desde 2019) e falharia no dia em
            # que voltar a ter — sem que nada de errado tenha acontecido. A
            # propriedade real é a fronteira ser a meia-noite de São Paulo, e não
            # a do servidor, que na Railway é UTC.
            expect_equal(str(start.tzinfo), "America/Sao_Paulo",
                         f"fuso da janela de {days}d")
            expect_equal((end - start).days + 1, days, f"tamanho da janela de {days}d")

        expect_equal(metrics.get_funnel(self.tenant_a, days=7)["cancelled"], 1, "cancelados em 7d")
        expect_equal(metrics.get_funnel(self.tenant_a, days=90)["cancelled"], 2, "cancelados em 90d")
        return "a reserva de 45 dias entra só no recorte de 90"

    def test_07_invalid_period_falls_back_to_30(self) -> str:
        """Período inválido cai em 30 sem levantar exceção.

        `parse_period()` é total de propósito: a rota monta esse valor a partir
        da query string, e um período que levantasse derrubaria a tela por causa
        de um erro de digitação na URL.
        """
        for raw in (None, "", "  ", "abc", "0", "-5", "365", "7.5", "30x", "١٠"):
            expect_equal(metrics.parse_period(raw), 30, f"parse_period({raw!r})")
        for raw, expected in (("7", 7), (" 90 ", 90), ("30", 30)):
            expect_equal(metrics.parse_period(raw), expected, f"parse_period({raw!r})")
        return "10 valores inválidos caíram em 30; os 3 válidos passaram"

    def test_08_empty_tenant_does_not_break(self) -> str:
        """Academia sem nada: zeros, taxas None, e nada de NaN/Infinity.

        None e não 0.0 nos denominadores zerados, porque "essa academia não tem
        taxa de conversão" é diferente de "a taxa dela é zero" — e a tela mostra
        as duas coisas de jeitos diferentes.
        """
        funnel: dict = metrics.get_funnel(self.tenant_c, days=30)
        for key in ("leads", "booked", "confirmed", "cancelled", "pending"):
            expect_equal(funnel[key], 0, f"{key} numa academia vazia")
        for key in ("lead_to_booking_rate", "booking_to_confirmed_rate",
                    "booking_to_cancelled_rate"):
            expect(funnel[key] is None, f"{key} devia ser None, veio {funnel[key]!r}")
        expect_equal(funnel["has_data"], False, "has_data numa academia vazia")

        # E a distinção que importa: zero sobre um denominador real é 0.0, não None.
        b: dict = metrics.get_funnel(self.tenant_b, days=30)
        expect_equal(b["booking_to_cancelled_rate"], 0.0, "taxa de cancelamento de B")
        return "tudo zero, taxas None, e 0.0 continua distinto de None"

    def test_09_rates_are_correct(self) -> str:
        """As taxas batem com a aritmética à mão, em percentual arredondado."""
        funnel: dict = metrics.get_funnel(self.tenant_a, days=30)
        expect_equal(funnel["lead_to_booking_rate"], 80.0, "agendamentos/leads (4/5)")
        expect_equal(funnel["booking_to_confirmed_rate"], 50.0, "confirmados/agendamentos (2/4)")
        expect_equal(funnel["booking_to_cancelled_rate"], 25.0, "cancelados/agendamentos (1/4)")
        return "80% / 50% / 25% em 30 dias"

    def test_10_no_attendance_metric_anywhere(self) -> str:
        """Nenhum número desta tela mede comparecimento (problema P1).

        `confirmed` é a resposta do DONO de que a aula vai acontecer, dias antes
        dela — não a presença do lead. Este teste falha se aparecer uma chave ou
        um rótulo de comparecimento, E TAMBÉM se a nota que diz isso ao dono for
        removida da tela: sem a nota, o número volta a ser lido como presença.
        """
        funnel: dict = metrics.get_funnel(self.tenant_a, days=30)
        summary: dict = metrics.get_funnel_summary(self.tenant_a, days=30)
        bad_keys: list[str] = [
            key for key in list(funnel) + list(summary) if ATTENDANCE_WORDS.search(key)
        ]
        expect(not bad_keys, f"chave de comparecimento na agregação: {bad_keys}")

        client = self.logged_in_client(self.email_a)
        html: str = client.get("/dashboard/metrics").get_data(as_text=True)

        labels: list[str] = re.findall(r'class="funnel-label">([^<]+)<', html)
        expect_equal(
            [label.strip() for label in labels],
            ["Leads", "Agendamentos", "Confirmados", "Cancelados"],
            "rótulos do funil",
        )
        bad_labels: list[str] = [l for l in labels if ATTENDANCE_WORDS.search(l)]
        expect(not bad_labels, f"rótulo de comparecimento na tela: {bad_labels}")

        expect(DISCLAIMER_FRAGMENT in html,
               "a nota do P1 sumiu de metrics.html — sem ela o dono lê "
               "'Confirmados' como presença")
        return "chaves e rótulos limpos; a nota do P1 continua na tela"

    def test_11_route_requires_login(self) -> str:
        """/dashboard/metrics é rota de painel: sem login, não renderiza."""
        client = self.application().test_client()
        response = client.get("/dashboard/metrics", follow_redirects=False)
        expect(response.status_code in (302, 303),
               f"anônimo recebeu {response.status_code} em vez de um redirect")
        expect("/dashboard/login" in response.headers.get("Location", ""),
               f"redirect foi para {response.headers.get('Location')!r}, não para o login")
        return f"anônimo -> {response.status_code} para o login"

    def test_12_route_renders_its_own_tenant(self) -> str:
        """A tela mostra os números da academia LOGADA e honra ?period=.

        A rota é onde o isolamento pode ser perdido de graça: parametrizar
        get_funnel() e esquecer de passar current_user.tenant_id deixaria tudo
        lendo o piloto sem nada quebrar.
        """
        client = self.logged_in_client(self.email_a)

        def figures(query: str) -> list[int]:
            html: str = client.get(f"/dashboard/metrics{query}").get_data(as_text=True)
            return [int(v) for v in re.findall(r'class="funnel-value">(\d+)<', html)]

        expect_equal(figures(""), [5, 4, 2, 1], "números com o período padrão (30d)")
        expect_equal(figures("?period=7"), [3, 3, 1, 1], "números com ?period=7")
        expect_equal(figures("?period=90"), [6, 5, 2, 2], "números com ?period=90")
        expect_equal(figures("?period=abc"), [5, 4, 2, 1], "?period inválido caiu em 30d")

        # E a academia B, na mesma app, vê os dela.
        client_b = self.logged_in_client(self.email_b)
        html_b: str = client_b.get("/dashboard/metrics").get_data(as_text=True)
        figures_b: list[int] = [int(v) for v in re.findall(r'class="funnel-value">(\d+)<', html_b)]
        expect_equal(figures_b, [7, 6, 6, 0], "números de B na própria tela")
        return "A vê 5/4/2/1, B vê 7/6/6/0, e o período vem da query string"

    def test_13_empty_tenant_renders_the_empty_state(self) -> str:
        """A academia vazia mostra 'sem dados', não um funil de zeros nem um erro."""
        client = self.logged_in_client(self.email_c)
        response = client.get("/dashboard/metrics")
        html: str = response.get_data(as_text=True)

        expect_equal(response.status_code, 200, "status da tela vazia")
        expect("Sem dados no período" in html, "faltou o estado vazio")
        expect("funnel-value" not in html, "a tela vazia desenhou o funil mesmo assim")
        expect("NaN" not in html and "Infinity" not in html, "vazou NaN/Infinity no HTML")
        return "estado vazio renderizado, sem funil e sem NaN"

    def test_14_menu_shows_its_own_summary(self) -> str:
        """O menu abre com os três números da PRÓPRIA academia (decisão 19C)."""
        client = self.logged_in_client(self.email_a)
        html: str = client.get("/dashboard/menu").get_data(as_text=True)

        values: list[int] = [int(v) for v in re.findall(r'class="summary-value">(\d+)<', html)]
        expect_equal(values, [5, 4, 2], "resumo de A no menu (leads/agendamentos/confirmados)")
        expect("/dashboard/metrics" in html, "faltou o link para a tela cheia")

        client_b = self.logged_in_client(self.email_b)
        values_b: list[int] = [
            int(v) for v in
            re.findall(r'class="summary-value">(\d+)<',
                       client_b.get("/dashboard/menu").get_data(as_text=True))
        ]
        expect_equal(values_b, [7, 6, 6], "resumo de B no menu")
        return "A vê 5/4/2 e B vê 7/6/6 no menu"

    def test_15_summary_agrees_with_the_full_screen(self) -> str:
        """O resumo do menu e a tela cheia nunca discordam.

        get_funnel_summary() é um wrapper fino sobre get_funnel() exatamente para
        isso: uma segunda query "otimizada" seria uma segunda definição dos
        mesmos números, livre para divergir da tela que o dono abre em seguida —
        que é o bug que faz um painel deixar de ser acreditado.
        """
        for tenant in (self.tenant_a, self.tenant_b, self.tenant_c):
            for days in metrics.ALLOWED_PERIODS:
                full: dict = metrics.get_funnel(tenant, days=days)
                short: dict = metrics.get_funnel_summary(tenant, days=days)
                for key in ("period_days", "leads", "booked", "confirmed", "has_data"):
                    expect_equal(short[key], full[key], f"{key} ({tenant}, {days}d)")
        return "resumo e tela cheia batem nos 3 tenants × 3 períodos"

    def test_16_teardown_leaves_nothing(self) -> str:
        """A limpeza não deixa conta, tenant, sessão nem reserva pendurada."""
        if self.keep:
            raise SkipTest("--keep: as fixtures foram preservadas de propósito")

        self.teardown()

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM users WHERE email LIKE %s",
                            ("%" + EMAIL_DOMAIN,))
                expect_equal(cur.fetchone()["n"], 0, "usuários restantes")
                for table in ("owners", "sessions", "messages", "trial_bookings"):
                    cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE tenant_id LIKE %s",
                                (TENANT_PREFIX + "%",))
                    expect_equal(cur.fetchone()["n"], 0, f"linhas restantes em {table}")
                cur.execute("SELECT COUNT(*) AS n FROM sessions WHERE sender LIKE %s",
                            (SENDER_PREFIX + "%",))
                expect_equal(cur.fetchone()["n"], 0, "sessões restantes pelo prefixo")
        return "nada sobrou"

    # -- cleanup ------------------------------------------------------------

    def teardown(self) -> None:
        """Delete the three fixture tenants and everything hanging off them."""
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
    return DEFAULT_REPORT_DIR / f"metrics-{stamp}.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Suíte E2E das métricas do funil (Módulo S4)")
    parser.add_argument("--keep", action="store_true",
                        help="não remove os tenants de teste ao final")
    parser.add_argument("--json", nargs="?", const="", default=None,
                        help="grava o relatório em JSON (caminho opcional)")
    parser.add_argument("--no-color", action="store_true", help="desliga as cores ANSI")
    args = parser.parse_args()

    logging.basicConfig(level=logging.ERROR, format="%(levelname)s - %(message)s")

    console = Console(color=not args.no_color and sys.stdout.isatty())
    report = Report(console)

    print(console.bold("\n Suíte de métricas do funil — Módulo S4"))
    print(console.dim(" Três academias de teste: uma com um funil de forma conhecida,"))
    print(console.dim(" uma grande para o vazamento aparecer, e uma vazia.\n"))

    report.section("Preparo")
    _drop_orphan_fixtures()

    suite = MetricsSuite(report, keep=args.keep)
    atexit.register(suite.teardown)
    if not report.run("F1", "Três academias provisionadas e semeadas",
                      suite.prepare_fixtures):
        print(console.red("\n Sem as fixtures não há o que contar."))
        report.summary()
        sys.exit(1)

    report.section("Roteiro de testes")
    tests: list[tuple[str, str, Callable[[], str | None]]] = [
        ("1", "Leads contam o primeiro contato dentro da janela",
         suite.test_01_leads_come_from_first_contact),
        ("2", "Lead antigo que volta não vira lead novo (Armadilha #2)",
         suite.test_02_returning_lead_is_not_a_new_lead),
        ("3", "Os números vêm do log de reservas, não do `stage`",
         suite.test_03_numbers_ignore_session_stage),
        ("4", "Confirmados + cancelados + pendentes fecham em agendamentos",
         suite.test_04_statuses_split_one_cohort),
        ("5", "Uma academia não enxerga a outra (Armadilha #3)",
         suite.test_05_tenant_isolation),
        ("6", "7/30/90 recortam certo, com a meia-noite de São Paulo (Armadilha #4)",
         suite.test_06_period_windows_cut_correctly),
        ("7", "Período inválido cai em 30 sem levantar",
         suite.test_07_invalid_period_falls_back_to_30),
        ("8", "Academia vazia não quebra e não devolve NaN (Armadilha #5)",
         suite.test_08_empty_tenant_does_not_break),
        ("9", "As taxas batem com a conta à mão",
         suite.test_09_rates_are_correct),
        ("10", "Nada mede comparecimento, e a nota do P1 segue na tela (Armadilha #1)",
         suite.test_10_no_attendance_metric_anywhere),
        ("11", "A rota /metrics exige login",
         suite.test_11_route_requires_login),
        ("12", "A tela mostra a academia logada e honra ?period=",
         suite.test_12_route_renders_its_own_tenant),
        ("13", "A academia vazia renderiza o estado vazio",
         suite.test_13_empty_tenant_renders_the_empty_state),
        ("14", "O menu abre com o resumo da própria academia",
         suite.test_14_menu_shows_its_own_summary),
        ("15", "Resumo do menu e tela cheia nunca discordam",
         suite.test_15_summary_agrees_with_the_full_screen),
        ("16", "A limpeza não deixa nada pendurado",
         suite.test_16_teardown_leaves_nothing),
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
