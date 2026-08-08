"""Automated end-to-end suite for booking confirmation by the owner (Module 6).

Runs the scenarios documented in CONFIRMATION_TESTING.md and prints a PASS/FAIL
report boiled down to an exit code, in the same style as
tests/test_owner_notifications/test_owner_notifications_suite.py and
tests/test_inbox/test_inbox_suite.py.

Everything here is fully deterministic: whatsapp_service.send_message is
replaced with a capture double (or a raising one, for the failure-path test), so
no real WhatsApp message ever leaves this run, and no LLM is involved at all —
the coordinator never calls the AI. The pilot's single owners row
(tenant_id='default') has its owner_phone temporarily overwritten with a fixture
number and restored in teardown.

The suite creates its bookings against a FAKE calendar_event_id. That is
deliberate: nothing here reads Google Calendar, because decision 1A means
cancelling never touches it — the seat is freed by counting Postgres rows, which
test 3 asserts directly through count_active_bookings().

Teardown removes only what this run wrote — owner_notifications, trial_bookings
and sessions rows for the suite's 5525000... senders (messages cascade off
sessions) — and restores the owner's original owner_phone. A crashed previous
run's backup is restored automatically at startup.

Run from src/:
    python tests/test_confirmation/test_confirmation_suite.py
    python tests/test_confirmation/test_confirmation_suite.py --keep
    python tests/test_confirmation/test_confirmation_suite.py --json

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
import bot.bookings as bookings  # noqa: E402
import bot.confirmations as confirmations  # noqa: E402
import bot.messages as messages  # noqa: E402
import bot.owner_notifications as owner_notifications  # noqa: E402
import bot.scheduling as scheduling  # noqa: E402
import bot.session as session_store  # noqa: E402
import integrations.store as store  # noqa: E402
import webhook.routes as routes  # noqa: E402
import whatsapp.whatsapp_service as whatsapp_service  # noqa: E402
from database.db import get_connection  # noqa: E402

# All suite leads share this prefix so teardown can scope its DELETEs and never
# touch a real lead or the other suites' senders (5521000... through 5524000...).
SENDER_PREFIX = "5525000"

# Fixture number the suite temporarily writes into owners.owner_phone.
OWNER_PHONE_TEST = "5525099999999"

# Where the owner's real owner_phone is backed up before the suite mutates it.
# Kept outside the repo so a crashed run never leaves a fixture number in git.
OWNER_BACKUP_PATH = Path("/tmp/corujai_confirmation_owner_backup.json")

MIGRATION_BOOKINGS = "004_create_trial_bookings"
MIGRATION_NOTIFICATIONS = "006_create_owner_notifications"
MIGRATION_MESSAGES = "007_create_messages"

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
            print(self.console.green("\n Confirmação de agendamento OK — todos os testes passaram.\n"))
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
SUITE_EMAIL: str = "suite-confirmation@suite.corujai.test"
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


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class SendCapture:
    """Stands in for whatsapp_service.send_message: records or fails sends."""

    def __init__(self, raise_error: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.raise_error = raise_error

    def __call__(self, to: str, text: str) -> str:
        if self.raise_error:
            raise RuntimeError("stubbed send failure")
        self.sent.append((to, text))
        return "SM-stub"


# ---------------------------------------------------------------------------
# Suite
# ---------------------------------------------------------------------------

class ConfirmationSuite:
    """Owns fixtures, tests and teardown for the booking-confirmation feature."""

    def __init__(self, report: Report, keep: bool) -> None:
        self.report = report
        self.keep = keep
        self._n = 0
        self.owner_id: int | None = None
        self.app: Any = None
        self.client: Any = None
        self._original_owner_phone: str | None = None
        self._had_original_owner_phone = False

    # -- infrastructure -----------------------------------------------------

    def next_sender(self) -> str:
        self._n += 1
        return f"{SENDER_PREFIX}{self._n:06d}"

    def _write_owner_phone(self, owner_phone: str | None) -> None:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE owners SET owner_phone = %s, updated_at = NOW() WHERE tenant_id = %s",
                    (owner_phone, store.DEFAULT_TENANT_ID),
                )
            conn.commit()

    def _new_booking(self, event_id: str | None = None, child_name: str | None = None) -> tuple[str, str]:
        """Create a lead with a session and a pending booking.

        The session row is not optional: the lead notice writes to messages,
        which has a foreign key to sessions.

        Args:
            event_id (str | None): Calendar event id to book against. Defaults to
                one unique to this booking. Pass an explicit id to make two
                bookings share a slot.
            child_name (str | None): Set for a kids-class booking.

        Returns:
            tuple[str, str]: (sender, booking_id).
        """
        sender = self.next_sender()
        session_store.get_session(sender)  # lazily creates the row
        event_id = event_id or f"suite-confirmation-{sender}"
        start = datetime.now(scheduling.TIMEZONE) + timedelta(days=2)

        result = bookings.create_booking_with_lock(
            calendar_event_id=event_id,
            sender=sender,
            lead_name="Suite Teste",
            class_type="CRIANCAS" if child_name else "ADULTOS",
            slot_start=start,
            slot_end=start + timedelta(hours=1),
            capacity=None,
            child_name=child_name,
        )
        expect_equal(result["status"], "created", "pré-condição: reserva criada")
        return sender, result["booking_id"]

    def _enqueue_sent_notification(self, sender: str, booking_id: str | None, event_type: str) -> int:
        """Enqueue a notification and mark it sent, as the cron would have."""
        created = owner_notifications.enqueue_notification(
            owner_id=self.owner_id, owner_phone=OWNER_PHONE_TEST,
            event_type=event_type, lead_sender=sender, booking_id=booking_id,
        )
        expect(created, f"pré-condição: notificação de {event_type} enfileirada")

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id FROM owner_notifications
                    WHERE lead_sender = %s AND event_type = %s
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (sender, event_type),
                )
                notification_id = cur.fetchone()["id"]

        owner_notifications.mark_sent(notification_id)
        return notification_id

    def _fetch_notification_by_id(self, notification_id: int) -> dict:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM owner_notifications WHERE id = %s", (notification_id,))
                row = cur.fetchone()
        expect(row is not None, f"notificação {notification_id} não encontrada")
        return dict(row)

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
            # CSRF desligado no cliente de teste (Módulo S3c). O Flask-WTF NÃO desliga
            # sozinho por causa de TESTING — ele olha só WTF_CSRF_ENABLED — e sem isto
            # todo POST desta suíte voltaria 400 sem chegar no código sob teste.
            self.app.config["WTF_CSRF_ENABLED"] = False
            self.client = self.app.test_client()
            _login_suite_user(self.client)
        return self.client

    # -- prerequisites --------------------------------------------------

    def check_schema(self) -> str:
        """Check every table this module touches is in place."""
        required = (MIGRATION_BOOKINGS, MIGRATION_NOTIFICATIONS, MIGRATION_MESSAGES)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT version FROM schema_migrations WHERE version = ANY(%s)",
                    (list(required),),
                )
                applied = {row["version"] for row in cur.fetchall()}

        missing = [version for version in required if version not in applied]
        expect(not missing, f"migrações não aplicadas: {', '.join(missing)} (suba a app para rodar init_db)")
        return "trial_bookings, owner_notifications e messages aplicadas"

    # -- fixtures -------------------------------------------------------

    def prepare_fixtures(self) -> str:
        """Snapshot the pilot owner row and point owner_phone at a fixture number."""
        owner = store.get_owner_for_notification()
        expect(owner is not None, "nenhuma linha em owners para tenant_id='default'")
        self.owner_id = owner["id"]
        self._original_owner_phone = owner.get("owner_phone")
        self._had_original_owner_phone = True

        OWNER_BACKUP_PATH.write_text(
            json.dumps({"owner_phone": self._original_owner_phone}), encoding="utf-8"
        )
        self._write_owner_phone(OWNER_PHONE_TEST)
        return f"owner_id={self.owner_id}; owner_phone de teste definido para {OWNER_PHONE_TEST}"

    # -- tests ------------------------------------------------------------

    def test_01_register_owner_response_returns_row(self) -> str:
        """3A: o retorno passou de bool para a linha carimbada (dict | None)."""
        sender, booking_id = self._new_booking()
        notification_id = self._enqueue_sent_notification(sender, booking_id, "booking")

        row = owner_notifications.register_owner_response(OWNER_PHONE_TEST, "confirmed")
        expect(row is not None, "deveria ter devolvido a notificação carimbada")
        expect_equal(row["id"], notification_id, "id devolvido")
        expect_equal(row["event_type"], "booking", "event_type devolvido")
        expect_equal(row["booking_id"], booking_id, "booking_id devolvido")

        # Sem notificação em aberto, a segunda resposta não acha nada — é o que
        # torna uma resposta duplicada do dono inofensiva.
        again = owner_notifications.register_owner_response(OWNER_PHONE_TEST, "confirmed")
        expect(again is None, "sem notificação em aberto o retorno deveria ser None")

        return "register_owner_response devolve a linha carimbada e None quando não há nada em aberto"

    def test_02_coordinator_confirms_and_notifies_lead(self) -> str:
        """2C: confirmar baixa o status e avisa o lead, gravando como 'ai'."""
        sender, booking_id = self._new_booking()

        send = SendCapture()
        with patched(whatsapp_service, "send_message", send):
            result = confirmations.confirm_or_cancel_booking(booking_id, "confirmed")

        expect_equal(result["result"], "applied", "resultado da coordenadora")
        expect_equal(result["decision"], "confirmed", "decisão aplicada")
        expect(result["lead_notified"], "o lead deveria ter sido avisado")
        expect_equal(bookings.get_booking(booking_id)["status"], "confirmed", "status da reserva")

        expect_equal(len(send.sent), 1, "deveria ter saído exatamente um aviso")
        expect_equal(send.sent[0][0], sender, "o aviso vai para o lead")

        conversation = messages.get_conversation(sender)
        expect_equal(len(conversation), 1, "o aviso deveria ter virado uma mensagem")
        expect_equal(conversation[0]["author"], "ai", "autor da mensagem gravada")
        expect_equal(conversation[0]["content"], send.sent[0][1], "o texto gravado é o texto enviado")
        expect(conversation[0]["is_read"], "mensagem da IA nasce lida (não é fila do operador)")

        return "confirmar: status → confirmed, lead avisado, mensagem gravada como 'ai'"

    def test_03_cancelling_frees_the_seat(self) -> str:
        """1A: cancelar devolve a vaga pela contagem, sem tocar no Calendar."""
        event_id = f"suite-confirmation-shared-{self._n}"
        sender, booking_id = self._new_booking(event_id=event_id)
        expect_equal(bookings.count_active_bookings(event_id), 1, "pré-condição: vaga ocupada")

        send = SendCapture()
        with patched(whatsapp_service, "send_message", send):
            result = confirmations.confirm_or_cancel_booking(booking_id, "cancelled")

        expect_equal(result["result"], "applied", "resultado da coordenadora")
        expect_equal(bookings.get_booking(booking_id)["status"], "cancelled", "status da reserva")
        expect_equal(bookings.count_active_bookings(event_id), 0,
                     "a vaga deveria ter voltado (count_active_bookings ignora 'cancelled')")

        expect_equal(len(send.sent), 1, "o lead também é avisado no cancelamento")
        expect_equal(send.sent[0][0], sender, "o aviso vai para o lead")

        return "cancelar: status → cancelled, vaga liberada pela contagem, lead avisado"

    def test_04_guard_skips_a_decided_booking(self) -> str:
        """5A: um agendamento já resolvido é ignorado, sem novo aviso."""
        _, booking_id = self._new_booking()

        send = SendCapture()
        with patched(whatsapp_service, "send_message", send):
            first = confirmations.confirm_or_cancel_booking(booking_id, "confirmed")
            second = confirmations.confirm_or_cancel_booking(booking_id, "cancelled")

        expect_equal(first["result"], "applied", "a primeira decisão vale")
        expect_equal(second["result"], "skipped", "a segunda deveria ser ignorada")
        expect_equal(second["status"], "confirmed", "o guard devolve o status atual")
        expect_equal(bookings.get_booking(booking_id)["status"], "confirmed",
                     "a segunda decisão não pode sobrescrever a primeira")
        expect_equal(len(send.sent), 1, "um agendamento ignorado não gera novo aviso ao lead")

        # E um id que não existe também não explode.
        missing = confirmations.confirm_or_cancel_booking("nao-existe", "confirmed")
        expect_equal(missing["result"], "not_found", "id inexistente")

        return "guard 5A: só age a partir de pending_confirmation; duplicado vira 'skipped'"

    def test_05_handoff_never_touches_trial_bookings(self) -> str:
        """Armadilha #2: handoff tem booking_id NULL e não fecha reserva nenhuma."""
        sender, booking_id = self._new_booking()
        # Uma reserva pendente existe para este lead, mas a notificação aberta é
        # de handoff — responder a ela não pode fechá-la.
        notification_id = self._enqueue_sent_notification(sender, None, "handoff")

        send = SendCapture()
        with patched(routes, "send_message", send), patched(whatsapp_service, "send_message", send):
            routes.receive_twilio_owner(OWNER_PHONE_TEST, "1")

        row = self._fetch_notification_by_id(notification_id)
        expect_equal(row["owner_response"], "confirmed", "a resposta deveria ter sido registrada")
        expect(row["booking_id"] is None, "handoff não carrega booking_id")
        expect_equal(bookings.get_booking(booking_id)["status"], "pending_confirmation",
                     "handoff não pode ter mexido em trial_bookings")
        expect_equal(len(send.sent), 0, "handoff não gera aviso ao lead")

        return "handoff: resposta registrada, trial_bookings intacto, nenhum aviso ao lead"

    def test_06_send_failure_does_not_undo_the_decision(self) -> str:
        """Armadilha #4: falhar o aviso não reverte o status nem estoura."""
        sender, booking_id = self._new_booking()

        failing = SendCapture(raise_error=True)
        with patched(whatsapp_service, "send_message", failing):
            result = confirmations.confirm_or_cancel_booking(booking_id, "confirmed")

        expect_equal(result["result"], "applied", "a decisão continua valendo")
        expect(not result["lead_notified"], "lead_notified deveria ser False")
        expect_equal(bookings.get_booking(booking_id)["status"], "confirmed",
                     "a falha no aviso não pode reverter o status")
        expect_equal(len(messages.get_conversation(sender)), 0,
                     "nada deveria ter sido gravado: o lead não recebeu a mensagem")

        # E o mesmo pelo webhook do dono: a exceção não pode escapar, senão o
        # Twilio reenviaria o "1" do dono.
        sender2, booking_id2 = self._new_booking()
        self._enqueue_sent_notification(sender2, booking_id2, "booking")
        with patched(routes, "send_message", failing), patched(whatsapp_service, "send_message", failing):
            routes.receive_twilio_owner(OWNER_PHONE_TEST, "2")
        expect_equal(bookings.get_booking(booking_id2)["status"], "cancelled",
                     "o webhook do dono deveria ter fechado a reserva mesmo com o aviso falhando")

        return "falha de envio: status preservado, lead_notified=False, nenhuma exceção vaza"

    def test_07_dashboard_routes_go_through_the_coordinator(self) -> str:
        """4B: os botões do painel usam a mesma coordenadora, com o mesmo guard."""
        client = self._authenticated_client()
        sender, booking_id = self._new_booking()

        send = SendCapture()
        with patched(whatsapp_service, "send_message", send):
            response = client.post(f"/dashboard/bookings/{booking_id}/confirm")

        expect_equal(response.status_code, 200, "o painel sempre responde 200 (o HTMX não troca em 4xx/5xx)")
        expect_equal(bookings.get_booking(booking_id)["status"], "confirmed", "status após o clique")
        expect_equal(len(send.sent), 1, "o lead é avisado pelo painel também")
        expect_equal(send.sent[0][0], sender, "o aviso vai para o lead")
        expect("Agendamento confirmado" in response.get_data(as_text=True),
               "o painel deveria confirmar a ação ao dono")

        # Segundo clique: cai no guard, sem novo aviso.
        with patched(whatsapp_service, "send_message", send):
            repeat = client.post(f"/dashboard/bookings/{booking_id}/cancel")
        expect_equal(repeat.status_code, 200, "clique repetido também responde 200")
        expect_equal(bookings.get_booking(booking_id)["status"], "confirmed", "o guard preserva a decisão")
        expect_equal(len(send.sent), 1, "clique repetido não gera novo aviso")
        expect("já estava" in repeat.get_data(as_text=True), "o painel deveria avisar que já estava resolvido")

        # Id inexistente não vira 500.
        unknown = client.post("/dashboard/bookings/nao-existe/cancel")
        expect_equal(unknown.status_code, 200, "id desconhecido não pode virar 500")
        expect("não encontrado" in unknown.get_data(as_text=True), "o painel deveria avisar que não achou")

        # E a tela precisa listar o que existe.
        listing = client.get("/dashboard/bookings")
        expect_equal(listing.status_code, 200, "a tela de agendamentos deveria abrir")
        expect("Suite Teste" in listing.get_data(as_text=True), "a reserva do teste deveria aparecer na lista")

        return "painel: confirm/cancel passam pela coordenadora, herdam o guard e nunca devolvem 5xx"

    def test_08_dashboard_stamps_the_notification(self) -> str:
        """Coerência: agir pelo painel também carimba o owner_response."""
        client = self._authenticated_client()
        sender, booking_id = self._new_booking()
        notification_id = self._enqueue_sent_notification(sender, booking_id, "booking")

        send = SendCapture()
        with patched(whatsapp_service, "send_message", send):
            response = client.post(f"/dashboard/bookings/{booking_id}/cancel")

        expect_equal(response.status_code, 200, "resposta do painel")
        expect_equal(bookings.get_booking(booking_id)["status"], "cancelled", "status da reserva")
        row = self._fetch_notification_by_id(notification_id)
        expect_equal(row["owner_response"], "cancelled",
                     "a notificação daquele booking deveria ter sido carimbada pelo painel")

        return "painel carimba owner_response: as duas fontes contam a mesma história"

    def test_09_routes_require_auth(self) -> str:
        """As rotas novas não podem ficar de fora do @require_auth."""
        import app as flask_app

        if self.app is None:
            self._authenticated_client()
        anonymous = self.app.test_client()

        paths = [
            ("get", "/dashboard/bookings"),
            ("get", "/dashboard/bookings/list"),
            ("post", "/dashboard/bookings/qualquer-id/confirm"),
            ("post", "/dashboard/bookings/qualquer-id/cancel"),
        ]
        for method, path in paths:
            response = getattr(anonymous, method)(path)
            expect_equal(response.status_code, 302, f"{method.upper()} {path} deveria redirecionar")
            expect("/dashboard/login" in response.headers.get("Location", ""),
                   f"{method.upper()} {path} deveria redirecionar para o login")

        return "as quatro rotas de agendamentos exigem sessão do painel"

    # -- teardown -----------------------------------------------------------

    def teardown(self) -> None:
        if self.keep:
            print("  --keep: reservas, notificações, sessões e owner_phone de teste preservados.")
            return

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM owner_notifications WHERE lead_sender LIKE %s", (SENDER_PREFIX + "%",))
                removed_notifications = cur.rowcount
                cur.execute("DELETE FROM trial_bookings WHERE sender LIKE %s", (SENDER_PREFIX + "%",))
                removed_bookings = cur.rowcount
                # As mensagens somem junto, por ON DELETE CASCADE em messages.sender.
                cur.execute("DELETE FROM sessions WHERE sender LIKE %s", (SENDER_PREFIX + "%",))
                removed_sessions = cur.rowcount
            conn.commit()

        accounts_users.delete_user(SUITE_EMAIL)

        if self._had_original_owner_phone:
            self._write_owner_phone(self._original_owner_phone)
        OWNER_BACKUP_PATH.unlink(missing_ok=True)

        print(
            f"  limpeza: {removed_notifications} notificação(ões), {removed_bookings} reserva(s) e "
            f"{removed_sessions} sessão(ões) de teste removidas; owner_phone original restaurado."
        )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _resolve_report_path(path: Path) -> Path:
    if path.is_dir() or path.suffix.lower() != ".json":
        path = path / f"confirmation-{datetime.now():%Y%m%d_%H%M%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _restore_owner_backup() -> None:
    """Put the owner's real owner_phone back if a previous run died mid-suite."""
    if not OWNER_BACKUP_PATH.exists():
        return

    backup = json.loads(OWNER_BACKUP_PATH.read_text(encoding="utf-8"))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE owners SET owner_phone = %s, updated_at = NOW() WHERE tenant_id = %s",
                (backup["owner_phone"], store.DEFAULT_TENANT_ID),
            )
        conn.commit()

    OWNER_BACKUP_PATH.unlink(missing_ok=True)
    print(f"  owner_phone do dono restaurado a partir de {OWNER_BACKUP_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Suíte automatizada de confirmação de agendamento (CONFIRMATION_TESTING.md).")
    parser.add_argument("--keep", action="store_true", help="Não desfaz nada ao final (para depurar à mão).")
    parser.add_argument("--no-color", action="store_true", help="Saída sem cores ANSI.")
    parser.add_argument("--json", nargs="?", type=Path, const=DEFAULT_REPORT_DIR, default=None,
                        metavar="ARQUIVO",
                        help="Também grava o relatório em JSON (sozinha: tests/outputs/ com nome datado).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.ERROR, format="%(levelname)s - %(message)s")

    console = Console(color=not args.no_color and sys.stdout.isatty())
    report = Report(console)

    print(console.bold("\n═══ Corujai · suíte de confirmação de agendamento ═══"))
    print(console.dim(" Roteiro: CONFIRMATION_TESTING.md"))

    # A crashed previous run may have left the fixture owner_phone in place.
    _restore_owner_backup()

    report.section("Pré-requisitos")
    suite = ConfirmationSuite(report, keep=args.keep)
    if not report.run("P1", "Schema de reservas, notificações e mensagens aplicado", suite.check_schema):
        print(console.red("\n Pré-requisitos falharam — a suíte não pode continuar."))
        report.summary()
        sys.exit(1)

    report.section("Preparo")
    atexit.register(suite.teardown)
    report.run("F1", "owner_phone de teste configurado", suite.prepare_fixtures)

    report.section("Roteiro de testes")
    tests: list[tuple[str, str, Callable[[], str | None]]] = [
        ("1", "register_owner_response devolve a linha carimbada",
         suite.test_01_register_owner_response_returns_row),
        ("2", "Coordenadora confirma: status, aviso ao lead e mensagem gravada",
         suite.test_02_coordinator_confirms_and_notifies_lead),
        ("3", "Coordenadora cancela: a vaga volta pela contagem",
         suite.test_03_cancelling_frees_the_seat),
        ("4", "Guard 5A: agendamento já resolvido vira 'skipped'",
         suite.test_04_guard_skips_a_decided_booking),
        ("5", "Handoff (booking_id NULL) nunca toca trial_bookings",
         suite.test_05_handoff_never_touches_trial_bookings),
        ("6", "Falha no aviso ao lead não reverte nem estoura",
         suite.test_06_send_failure_does_not_undo_the_decision),
        ("7", "Rotas do painel passam pela coordenadora",
         suite.test_07_dashboard_routes_go_through_the_coordinator),
        ("8", "Painel carimba o owner_response da notificação",
         suite.test_08_dashboard_stamps_the_notification),
        ("9", "As rotas de agendamentos exigem autenticação",
         suite.test_09_routes_require_auth),
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
