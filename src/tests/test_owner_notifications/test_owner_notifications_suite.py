"""Automated end-to-end suite for the owner-notifications feature.

Runs the scenarios documented in OWNER_NOTIFICATIONS_TESTING.md and prints a
PASS/FAIL report boiled down to an exit code, in the same style as
tests/test_ai_action/test_ai_action_suite.py and
tests/test_scheduling/test_scheduling_suite.py.

Everything here is fully deterministic: whatsapp_service.send_message is
replaced with a capture double (or a raising one, for the failure-path test),
so no real WhatsApp message ever leaves this run. The pilot's single owners
row (tenant_id='default') has its owner_phone temporarily overwritten with a
fixture number for the duration of the run and restored in teardown — the
suite never inserts a second owners row, since tenant_id is UNIQUE.

Teardown removes only what this run wrote — owner_notifications and
trial_bookings rows for the suite's 5523000... senders — and restores the
owner's original owner_phone. A crashed previous run's owner_phone backup (if
any) is restored automatically at startup.

Run from src/:
    python tests/test_owner_notifications/test_owner_notifications_suite.py
    python tests/test_owner_notifications/test_owner_notifications_suite.py --keep
    python tests/test_owner_notifications/test_owner_notifications_suite.py --json

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
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

# Locate src/ by NAME, like app.py and the other suites.
SRC_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "src")
sys.path.insert(0, str(SRC_DIR))

import bot.bookings as bookings  # noqa: E402
import bot.owner_notifications as owner_notifications  # noqa: E402
import bot.scheduling as scheduling  # noqa: E402
import integrations.store as store  # noqa: E402
import jobs.drain_notifications as drain_notifications  # noqa: E402
import webhook.routes as routes  # noqa: E402
import whatsapp.whatsapp_service as whatsapp_service  # noqa: E402
from database.db import get_connection  # noqa: E402

# All suite leads share this prefix so teardown can scope its DELETEs and never
# touch a real lead or the other suites' senders (5521000... / 5522000...).
SENDER_PREFIX = "5523000"

# Fixture number the suite temporarily writes into owners.owner_phone.
OWNER_PHONE_TEST = "5523099999999"

# Where the owner's real owner_phone is backed up before the suite mutates it.
# Kept outside the repo so a crashed run never leaves a fixture number in git.
OWNER_BACKUP_PATH = Path("/tmp/corujai_owner_notifications_owner_backup.json")

MIGRATION_NOTIFICATIONS = "006_create_owner_notifications"

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
            print(self.console.green("\n Notificações ao dono OK — todos os testes passaram.\n"))
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

class OwnerNotificationsSuite:
    """Owns fixtures, tests and teardown for the owner-notifications feature."""

    def __init__(self, report: Report, keep: bool) -> None:
        self.report = report
        self.keep = keep
        self._n = 0
        self.owner_id: int | None = None
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

    def _create_booking(self, sender: str) -> str:
        """Create a real trial_bookings row so a booking notification has a valid FK target."""
        result = bookings.create_booking_with_lock(
            calendar_event_id=f"suite-owner-notif-{sender}",
            sender=sender,
            lead_name="Suite Teste",
            class_type="ADULTOS",
            slot_start=datetime.now(scheduling.TIMEZONE),
            slot_end=datetime.now(scheduling.TIMEZONE),
            capacity=None,
        )
        expect_equal(result["status"], "created", "pré-condição: reserva de apoio criada")
        return result["booking_id"]

    def _fetch_notification(self, lead_sender: str, event_type: str) -> dict:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT * FROM owner_notifications
                    WHERE lead_sender = %s AND event_type = %s
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (lead_sender, event_type),
                )
                row = cur.fetchone()
        expect(row is not None, f"notificação não encontrada (lead_sender={lead_sender}, event_type={event_type})")
        return dict(row)

    def _fetch_notification_by_id(self, notification_id: int) -> dict:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM owner_notifications WHERE id = %s", (notification_id,))
                row = cur.fetchone()
        expect(row is not None, f"notificação {notification_id} não encontrada")
        return dict(row)

    # -- prerequisites --------------------------------------------------

    def check_schema(self) -> str:
        """Check the owner_notifications schema is in place.

        owner_phone was added by editing migration 003 in place, so a
        database that applied the OLD 003 would still show it as applied in
        schema_migrations — checked in information_schema instead, the same
        trick the AI-action suite uses for the conversation-state columns.
        """
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT version FROM schema_migrations WHERE version = %s",
                    (MIGRATION_NOTIFICATIONS,),
                )
                notifications_applied = cur.fetchone() is not None
                cur.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'owners' AND column_name = 'owner_phone'
                    """
                )
                has_owner_phone = cur.fetchone() is not None

        expect(notifications_applied,
               f"migração {MIGRATION_NOTIFICATIONS} não aplicada (suba a app para rodar init_db)")
        expect(has_owner_phone,
               "coluna owner_phone ausente em owners (recrie o banco após editar a migração 003)")
        return "owner_notifications aplicada e owner_phone presente em owners"

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

    def test_01_enqueue_creates_pending(self) -> str:
        sender = self.next_sender()
        created = owner_notifications.enqueue_notification(
            owner_id=self.owner_id, owner_phone=OWNER_PHONE_TEST,
            event_type="handoff", lead_sender=sender,
        )
        expect(created, "enqueue_notification deveria ter criado a linha")

        row = self._fetch_notification(sender, "handoff")
        expect_equal(row["status"], "pending", "status inicial")
        expect_equal(row["attempts"], 0, "attempts inicial")
        return "enqueue_notification cria linha pending com attempts=0"

    def test_02_booking_partial_index_blocks_duplicate(self) -> str:
        sender = self.next_sender()
        booking_id = self._create_booking(sender)

        first = owner_notifications.enqueue_notification(
            owner_id=self.owner_id, owner_phone=OWNER_PHONE_TEST,
            event_type="booking", lead_sender=sender, booking_id=booking_id,
        )
        second = owner_notifications.enqueue_notification(
            owner_id=self.owner_id, owner_phone=OWNER_PHONE_TEST,
            event_type="booking", lead_sender=sender, booking_id=booking_id,
        )
        expect(first, "primeiro enqueue de booking deveria ter criado a linha")
        expect(not second, "segundo enqueue do MESMO booking_id deveria ser bloqueado pelo índice parcial")

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM owner_notifications WHERE booking_id = %s",
                    (booking_id,),
                )
                count = cur.fetchone()["n"]
        expect_equal(count, 1, "deveria existir só uma notificação para esse booking_id")
        return "índice parcial de booking impede segunda notificação para o mesmo booking_id"

    def test_03_handoff_partial_index_open_then_allows_after_response(self) -> str:
        sender = self.next_sender()
        first = owner_notifications.enqueue_notification(
            owner_id=self.owner_id, owner_phone=OWNER_PHONE_TEST,
            event_type="handoff", lead_sender=sender,
        )
        blocked = owner_notifications.enqueue_notification(
            owner_id=self.owner_id, owner_phone=OWNER_PHONE_TEST,
            event_type="handoff", lead_sender=sender,
        )
        expect(first, "primeiro handoff deveria ter sido enfileirado")
        expect(not blocked, "um segundo handoff pendente para o mesmo lead deveria ser bloqueado")

        notification_id = self._fetch_notification(sender, "handoff")["id"]
        owner_notifications.mark_sent(notification_id)
        updated = owner_notifications.register_owner_response(OWNER_PHONE_TEST, "confirmed")
        expect(updated, "register_owner_response deveria ter encontrado a notificação enviada")

        allowed = owner_notifications.enqueue_notification(
            owner_id=self.owner_id, owner_phone=OWNER_PHONE_TEST,
            event_type="handoff", lead_sender=sender,
        )
        expect(allowed, "um novo handoff deveria ser permitido após o dono responder o anterior")
        return "índice parcial de handoff: bloqueia enquanto pendente, libera após owner_response"

    def test_04_cron_happy_path_marks_sent(self) -> str:
        sender = self.next_sender()
        booking_id = self._create_booking(sender)
        owner_notifications.enqueue_notification(
            owner_id=self.owner_id, owner_phone=OWNER_PHONE_TEST,
            event_type="booking", lead_sender=sender, booking_id=booking_id,
        )
        notification = self._fetch_notification(sender, "booking")

        send = SendCapture()
        with patched(whatsapp_service, "send_message", send), \
                patched(owner_notifications, "list_pending_notifications", lambda max_attempts: [notification]):
            exit_code = drain_notifications.main()

        expect_equal(exit_code, 0, "main() deveria retornar 0")
        expect_equal(len(send.sent), 1, "deveria ter enviado exatamente uma mensagem")
        expect_equal(send.sent[0][0], OWNER_PHONE_TEST, "mensagem deveria ter ido para o número de teste do dono")

        row = self._fetch_notification_by_id(notification["id"])
        expect_equal(row["status"], "sent", "status após o drain")
        expect(row["sent_at"] is not None, "sent_at deveria estar preenchido")
        return "cron: pending → sent no caminho feliz (envio stubbado)"

    def test_05_cron_failure_increments_attempts_then_fails(self) -> str:
        sender = self.next_sender()
        owner_notifications.enqueue_notification(
            owner_id=self.owner_id, owner_phone=OWNER_PHONE_TEST,
            event_type="handoff", lead_sender=sender,
        )
        notification_id = self._fetch_notification(sender, "handoff")["id"]

        def _only_this_one(max_attempts: int) -> list[dict]:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT * FROM owner_notifications WHERE id = %s AND status = 'pending' AND attempts < %s",
                        (notification_id, max_attempts),
                    )
                    rows = cur.fetchall()
            return [dict(row) for row in rows]

        send = SendCapture(raise_error=True)
        with patched(whatsapp_service, "send_message", send), \
                patched(owner_notifications, "list_pending_notifications", _only_this_one):
            for _ in range(drain_notifications.MAX_ATTEMPTS):
                drain_notifications.main()

        row = self._fetch_notification_by_id(notification_id)
        expect_equal(row["attempts"], drain_notifications.MAX_ATTEMPTS, "attempts deveria ter chegado ao teto")
        expect_equal(row["status"], "failed", "status deveria ser failed após esgotar as tentativas")
        return f"cron: {drain_notifications.MAX_ATTEMPTS} falhas consecutivas → status=failed"

    def test_06_register_owner_response_via_webhook(self) -> str:
        # "1" → confirmed, on a booking notification: trial_bookings must stay untouched.
        sender = self.next_sender()
        booking_id = self._create_booking(sender)
        owner_notifications.enqueue_notification(
            owner_id=self.owner_id, owner_phone=OWNER_PHONE_TEST,
            event_type="booking", lead_sender=sender, booking_id=booking_id,
        )
        notification_id = self._fetch_notification(sender, "booking")["id"]
        owner_notifications.mark_sent(notification_id)
        status_before = bookings.get_booking(booking_id)["status"]

        send = SendCapture()
        with patched(routes, "send_message", send):
            routes.receive_twilio_owner(OWNER_PHONE_TEST, "1")

        row = self._fetch_notification_by_id(notification_id)
        expect_equal(row["owner_response"], "confirmed", "'1' deveria gravar 'confirmed'")
        expect_equal(len(send.sent), 0, "uma resposta reconhecida não deveria gerar mensagem de volta")
        expect_equal(bookings.get_booking(booking_id)["status"], status_before,
                     "trial_bookings.status não deveria ter sido tocado")

        # "2" → cancelled, on an independent handoff notification.
        sender2 = self.next_sender()
        owner_notifications.enqueue_notification(
            owner_id=self.owner_id, owner_phone=OWNER_PHONE_TEST,
            event_type="handoff", lead_sender=sender2,
        )
        notification_id2 = self._fetch_notification(sender2, "handoff")["id"]
        owner_notifications.mark_sent(notification_id2)
        with patched(routes, "send_message", send):
            routes.receive_twilio_owner(OWNER_PHONE_TEST, "2")
        row2 = self._fetch_notification_by_id(notification_id2)
        expect_equal(row2["owner_response"], "cancelled", "'2' deveria gravar 'cancelled'")

        return "receive_twilio_owner: '1'→confirmed, '2'→cancelled, nunca toca trial_bookings"

    def test_07_get_owner_by_phone(self) -> str:
        found = store.get_owner_by_phone(OWNER_PHONE_TEST)
        expect(found is not None, "deveria reconhecer o número de teste do dono")
        expect_equal(found["id"], self.owner_id, "id do owner encontrado")

        unknown = store.get_owner_by_phone("0000000000000")
        expect(unknown is None, "número desconhecido deveria retornar None (segue como lead)")
        return "get_owner_by_phone reconhece o dono e retorna None para número desconhecido"

    def test_08_webhook_routing(self) -> str:
        from flask import Flask

        app = Flask(__name__)
        app.register_blueprint(routes.webhook_bp)
        client = app.test_client()

        calls: dict[str, list] = {"owner": [], "lead": []}

        def fake_owner(owner_phone: str, body: str) -> None:
            calls["owner"].append((owner_phone, body))

        def fake_lead(sender: str, body: str) -> None:
            calls["lead"].append((sender, body))

        lead_sender = self.next_sender()
        with patched(routes, "receive_twilio_owner", fake_owner), \
                patched(routes, "handle_text_message", fake_lead):
            client.post("/webhook", data={"From": f"whatsapp:+{OWNER_PHONE_TEST}", "Body": "1"})
            client.post("/webhook", data={"From": f"whatsapp:+{lead_sender}", "Body": "oi"})

        expect_equal(len(calls["owner"]), 1, "número do dono deveria cair em receive_twilio_owner")
        expect_equal(len(calls["lead"]), 1, "número desconhecido deveria cair em handle_text_message")
        expect_equal(calls["owner"][0][0], OWNER_PHONE_TEST, "clean_number repassado ao handler do dono")
        return "roteamento: número do dono → receive_twilio_owner; lead → handle_text_message"

    # -- teardown -----------------------------------------------------------

    def teardown(self) -> None:
        if self.keep:
            print("  --keep: notificações, reservas de apoio e owner_phone de teste preservados.")
            return

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM owner_notifications WHERE lead_sender LIKE %s", (SENDER_PREFIX + "%",))
                removed_notifications = cur.rowcount
                cur.execute("DELETE FROM trial_bookings WHERE sender LIKE %s", (SENDER_PREFIX + "%",))
                removed_bookings = cur.rowcount
            conn.commit()

        if self._had_original_owner_phone:
            self._write_owner_phone(self._original_owner_phone)
        OWNER_BACKUP_PATH.unlink(missing_ok=True)

        print(
            f"  limpeza: {removed_notifications} notificação(ões) e {removed_bookings} reserva(s) de "
            "teste removidas; owner_phone original restaurado."
        )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _resolve_report_path(path: Path) -> Path:
    if path.is_dir() or path.suffix.lower() != ".json":
        path = path / f"report_{datetime.now():%Y%m%d_%H%M%S}.json"
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
        description="Suíte automatizada de notificações ao dono (OWNER_NOTIFICATIONS_TESTING.md).")
    parser.add_argument("--keep", action="store_true", help="Não desfaz nada ao final (para depurar à mão).")
    parser.add_argument("--no-color", action="store_true", help="Saída sem cores ANSI.")
    parser.add_argument("--json", nargs="?", type=Path, const=DEFAULT_REPORT_DIR, default=None,
                        metavar="ARQUIVO",
                        help="Também grava o relatório em JSON (sozinha: tests/outputs/ com nome datado).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.ERROR, format="%(levelname)s - %(message)s")

    console = Console(color=not args.no_color and sys.stdout.isatty())
    report = Report(console)

    print(console.bold("\n═══ Corujai · suíte de notificações ao dono ═══"))
    print(console.dim(" Roteiro: OWNER_NOTIFICATIONS_TESTING.md"))

    # A crashed previous run may have left the fixture owner_phone in place.
    _restore_owner_backup()

    report.section("Pré-requisitos")
    suite = OwnerNotificationsSuite(report, keep=args.keep)
    if not report.run("P1", "Schema de owner_notifications aplicado", suite.check_schema):
        print(console.red("\n Pré-requisitos falharam — a suíte não pode continuar."))
        report.summary()
        sys.exit(1)

    report.section("Preparo")
    atexit.register(suite.teardown)
    report.run("F1", "owner_phone de teste configurado", suite.prepare_fixtures)

    report.section("Roteiro de testes")
    tests: list[tuple[str, str, Callable[[], str | None]]] = [
        ("1", "enqueue_notification cria linha pending", suite.test_01_enqueue_creates_pending),
        ("2", "Índice parcial de booking bloqueia duplicata", suite.test_02_booking_partial_index_blocks_duplicate),
        ("3", "Índice parcial de handoff: bloqueia e libera após resposta",
         suite.test_03_handoff_partial_index_open_then_allows_after_response),
        ("4", "Cron: pending → sent no caminho feliz", suite.test_04_cron_happy_path_marks_sent),
        ("5", "Cron: falhas consecutivas → attempts e depois failed",
         suite.test_05_cron_failure_increments_attempts_then_fails),
        ("6", "receive_twilio_owner: 1/2 gravam a resposta, sem tocar trial_bookings",
         suite.test_06_register_owner_response_via_webhook),
        ("7", "get_owner_by_phone reconhece o dono e ignora número desconhecido",
         suite.test_07_get_owner_by_phone),
        ("8", "Webhook roteia dono → receive_twilio_owner, lead → handle_text_message",
         suite.test_08_webhook_routing),
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
