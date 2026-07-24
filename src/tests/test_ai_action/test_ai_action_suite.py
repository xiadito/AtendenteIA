"""Automated end-to-end suite for the Module 3 goal-driven scheduling AI.

Runs the scenarios documented in AI_ACTION_TESTING.md and prints a PASS/FAIL
report boiled down to an exit code, in the same style as
tests/test_scheduling/test_scheduling_suite.py.

Determinism: the LLM is stubbed (handlers.get_ai_response is replaced with a
queue of canned raw responses), so the suite exercises the HANDLER logic —
defensive parser, event_id validation, action execution, pause and timeout —
without depending on what Haiku happens to emit. whatsapp_service.send_message
is captured instead of sending over Twilio.

Two tiers:
- Live booking tests (adults, child, missing_child_name) drive the REAL
  book_slot() against a real available slot and verify trial_bookings. They
  SkipTest when Google Calendar isn't connected or no suitable slot exists.
- Everything else is fully deterministic: get_cached_slots and/or book_slot are
  patched, and only Postgres (the sessions table) is touched.

Teardown removes only what this run wrote — sessions and trial_bookings for the
suite's 5522000... senders — and restores the description of any real event a
live booking patched.

Run from src/:
    python tests/test_ai_action/test_ai_action_suite.py
    python tests/test_ai_action/test_ai_action_suite.py --skip-live   # no Calendar writes
    python tests/test_ai_action/test_ai_action_suite.py --keep        # don't clean up (debug)
    python tests/test_ai_action/test_ai_action_suite.py --json        # tests/outputs/report_<date>.json

Exit code is 0 only when every test passed (skips don't fail the run).

WARNING: live booking tests write to the real Google Calendar and the real
Postgres pointed at by DATABASE_URL. Do not point this at production.
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

# Locate src/ by NAME, like app.py and the scheduling suite.
SRC_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "src")
sys.path.insert(0, str(SRC_DIR))

import bot.ai_context as ai_context  # noqa: E402
import bot.bookings as bookings  # noqa: E402
import bot.handlers as handlers  # noqa: E402
import bot.scheduling as scheduling  # noqa: E402
import integrations.store as store  # noqa: E402
import whatsapp.whatsapp_service as whatsapp_service  # noqa: E402
from database.db import get_connection  # noqa: E402

# All suite leads share this prefix so teardown can scope its DELETEs and never
# touch a real lead or the scheduling suite's 5521000... senders.
SENDER_PREFIX = "5522000"

MIGRATION_STATE = "006_add_conversation_state_to_sessions"
MIGRATION_CONFIGS = "007_create_ai_configs"

DEFAULT_REPORT_DIR = SRC_DIR / "tests" / "outputs"


# ---------------------------------------------------------------------------
# Report / console output (same shape as the scheduling suite)
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
            print(self.console.green("\n IA de agendamento OK — todos os testes passaram.\n"))
        return not failed

    def to_json(self, path: Path) -> None:
        path.write_text(json.dumps({
            "passed": sum(1 for r in self.results if r["status"] == "PASS"),
            "failed": len(self.failed),
            "skipped": sum(1 for r in self.results if r["status"] == "SKIP"),
            "results": self.results,
        }, indent=2, ensure_ascii=False), encoding="utf-8")


class LogCapture(logging.Handler):
    """Captures records from a named logger so a test can assert on them.

    Used by the timeout test to prove the conversation was recorded as
    closed_no_booking, which Module 3 only emits as a log line (no events table).
    """

    def __init__(self, logger_name: str, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self.messages: list[str] = []
        self._logger = logging.getLogger(logger_name)
        self._level = level
        self._previous_level = logging.NOTSET

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())

    def __enter__(self) -> "LogCapture":
        self.messages.clear()
        self._previous_level = self._logger.level
        self._logger.setLevel(self._level)
        self._logger.addHandler(self)
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._logger.removeHandler(self)
        self._logger.setLevel(self._previous_level)


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

class AiStub:
    """Stands in for get_ai_response: returns the single next canned response.

    A single slot, not a queue, on purpose: a paused turn returns from the
    handler before the AI is ever called, so a queued response would go
    unconsumed and shift every later test by one. An unconsumed next_raw is
    simply overwritten by the following drive(), so the pause path can't leak.
    """

    def __init__(self) -> None:
        self.next_raw: str | None = None
        self.calls: int = 0
        self.last_system_prompt: str | None = None

    def __call__(self, history: list[dict[str, str]], system_prompt: str) -> str:
        self.calls += 1
        self.last_system_prompt = system_prompt
        raw = self.next_raw
        self.next_raw = None
        if raw is None:
            return _raw("(sem resposta programada)", stage="greeting", qualification="unknown", action="none")
        return raw


class SendCapture:
    """Stands in for whatsapp_service.send_message: records every reply."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def __call__(self, sender: str, message: str) -> None:
        self.sent.append((sender, message))

    def count_for(self, sender: str) -> int:
        return sum(1 for to, _ in self.sent if to == sender)

    def last_for(self, sender: str) -> str | None:
        for to, message in reversed(self.sent):
            if to == sender:
                return message
        return None


def _raw(message: str, **fields: Any) -> str:
    """Build a raw AI response: a Portuguese message plus one action block."""
    return f"{message}\n<corujai_action>{json.dumps(fields, ensure_ascii=False)}</corujai_action>"


def _synthetic_slot(event_id: str, class_type: str, label: str, remaining: int | None) -> dict:
    """A slot dict shaped like get_available_slots() output (only the keys the
    handler and prompt actually read)."""
    return {
        "event_id": event_id,
        "class_type": class_type,
        "label": label,
        "remaining_slots": remaining,
    }


# ---------------------------------------------------------------------------
# Suite
# ---------------------------------------------------------------------------

class AiActionSuite:
    """Owns fixtures, tests and teardown for the Module 3 handler."""

    def __init__(self, report: Report, keep: bool, skip_live: bool) -> None:
        self.report = report
        self.keep = keep
        self.skip_live = skip_live
        self.ai = AiStub()
        self.send = SendCapture()
        self._n = 0

        # Live-booking fixtures (filled by prepare_fixtures).
        self.integration_ok = False
        self.adult_slot: dict | None = None
        self.child_slot: dict | None = None
        # event_id -> original description, so teardown can undo a live patch.
        self._original_descriptions: dict[str, str] = {}

    # -- infrastructure -----------------------------------------------------

    def install(self) -> None:
        """Route the handler's LLM and WhatsApp calls into the test doubles."""
        handlers.get_ai_response = self.ai
        whatsapp_service.send_message = self.send

    def next_sender(self) -> str:
        self._n += 1
        return f"{SENDER_PREFIX}{self._n:06d}"

    def drive(self, sender: str, text: str, raw: str | None = None) -> str | None:
        """Feed one user message through handle_text_message.

        Args:
            sender (str): The suite lead's number.
            text (str): The user message.
            raw (str | None): The raw AI response to queue for this turn.

        Returns:
            str | None: The last message sent to this sender, if any.
        """
        if raw is not None:
            self.ai.next_raw = raw
        handlers.handle_text_message(sender, text)
        return self.send.last_for(sender)

    def _session_row(self, sender: str) -> dict | None:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT stage, qualification, lead_name, child_name, is_paused, history
                    FROM sessions WHERE sender = %s
                    """,
                    (sender,),
                )
                return cur.fetchone()

    def _bookings_for(self, sender: str) -> list[dict]:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM trial_bookings WHERE sender = %s", (sender,))
                return [dict(row) for row in cur.fetchall()]

    def _age_session(self, sender: str, hours: int) -> None:
        """Push a session's updated_at into the past to simulate inactivity."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE sessions SET updated_at = NOW() - (%s || ' hours')::interval WHERE sender = %s",
                    (str(hours), sender),
                )
            conn.commit()

    # -- prerequisites ------------------------------------------------------

    def check_migrations(self) -> str:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT version FROM schema_migrations WHERE version IN (%s, %s)",
                    (MIGRATION_STATE, MIGRATION_CONFIGS),
                )
                found = {row["version"] for row in cur.fetchall()}
        missing = {MIGRATION_STATE, MIGRATION_CONFIGS} - found
        expect(not missing, f"migrations não aplicadas: {sorted(missing)} (suba a app para rodar init_db)")
        return "migrations 006 e 007 aplicadas"

    # -- fixtures -----------------------------------------------------------

    def prepare_fixtures(self) -> str:
        """Install doubles and discover real slots for the live tests."""
        self.install()

        if self.skip_live:
            return "modo --skip-live: testes de agendamento real serão pulados"

        owner = store.get_owner_credentials()
        self.integration_ok = bool(owner and owner.get("integration_status") == "connected" and owner.get("calendar_id"))
        if not self.integration_ok:
            return "Google Calendar não conectado: testes de agendamento real serão pulados"

        try:
            slots = scheduling.get_available_slots()
        except (scheduling.IntegrationNotConnectedError, scheduling.IntegrationNeedsReconnectError):
            self.integration_ok = False
            return "integração indisponível: testes de agendamento real serão pulados"

        self.adult_slot = next((s for s in slots if s["class_type"] == "ADULTOS"), None)
        self.child_slot = next((s for s in slots if s["class_type"] in {"BABY", "CRIANCAS"}), None)
        found = [name for name, slot in (("adultos", self.adult_slot), ("infantil", self.child_slot)) if slot]
        return f"slots reais encontrados: {', '.join(found) if found else 'nenhum'}"

    def _remember_description(self, event_id: str) -> None:
        """Capture an event's current description once, before a live booking patches it."""
        if event_id in self._original_descriptions or not self.integration_ok:
            return
        service, calendar_id = scheduling._get_service_or_raise()
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
        self._original_descriptions[event_id] = event.get("description") or ""

    # -- tests: live bookings ----------------------------------------------

    def test_book_adults(self) -> str:
        if not self.adult_slot:
            raise SkipTest("sem slot de adultos disponível no calendário")
        event_id = self.adult_slot["event_id"]
        self._remember_description(event_id)
        sender = self.next_sender()

        out = self.drive(
            sender, "Quero marcar a aula experimental de adultos",
            _raw("Perfeito, Carlos! Agendei sua aula 💪", stage="booked",
                 lead_name="Carlos Suite", qualification="qualified",
                 action="book", event_id=event_id),
        )
        row = self._session_row(sender)
        expect_equal(row["stage"], "booked", "stage")
        matches = [b for b in self._bookings_for(sender) if b["calendar_event_id"] == event_id]
        expect(len(matches) == 1, "esperava exatamente 1 reserva criada")
        expect(matches[0]["child_name"] is None, "adulto não deve ter child_name")
        expect(out is not None, "uma mensagem deveria ter sido enviada")
        return f"reserva de adultos criada (child_name NULL) no evento {event_id[:10]}…"

    def test_book_child(self) -> str:
        if not self.child_slot:
            raise SkipTest("sem slot infantil (BABY/CRIANCAS) disponível no calendário")
        event_id = self.child_slot["event_id"]
        self._remember_description(event_id)
        sender = self.next_sender()

        self.drive(sender, "Oi, é pro meu filho", _raw("Que ótimo! Qual o nome dele?",
                   stage="interest", lead_name="Marina Suite", qualification="qualified", action="none"))
        out = self.drive(
            sender, "O nome dele é Pedro",
            _raw("Fechado, Marina! Agendei a aula do Pedro 🧒", stage="booked",
                 lead_name="Marina Suite", child_name="Pedro", qualification="qualified",
                 action="book", event_id=event_id),
        )
        row = self._session_row(sender)
        expect_equal(row["stage"], "booked", "stage")
        matches = [b for b in self._bookings_for(sender) if b["calendar_event_id"] == event_id]
        expect(len(matches) == 1, "esperava exatamente 1 reserva infantil criada")
        expect_equal(matches[0]["child_name"], "Pedro", "child_name gravado")
        expect_equal(matches[0]["lead_name"], "Marina Suite", "lead_name (responsável)")
        expect(out is not None, "uma mensagem deveria ter sido enviada")
        return "reserva infantil criada com os dois nomes (Marina / Pedro)"

    def test_missing_child_name(self) -> str:
        if not self.child_slot:
            raise SkipTest("sem slot infantil disponível no calendário")
        event_id = self.child_slot["event_id"]
        sender = self.next_sender()

        out = self.drive(
            sender, "Quero marcar pro meu filho nesse horário",
            _raw("Agendado!", stage="booked", lead_name="Sem Nome", qualification="qualified",
                 action="book", event_id=event_id),
        )
        row = self._session_row(sender)
        expect(row["stage"] != "booked", "não deveria ter marcado sem o nome da criança")
        expect(len(self._bookings_for(sender)) == 0, "nenhuma reserva deveria existir")
        expect(out is not None and "criança" in out.lower(), "a IA deveria pedir o nome da criança")
        return "missing_child_name: reserva recusada, conversa pediu o nome"

    # -- tests: deterministic (no Calendar) --------------------------------

    def test_lead_states_time_upfront(self) -> str:
        """Lead who names the slot immediately → the AI books in one turn."""
        slot = _synthetic_slot("suite-adult-1", "ADULTOS", "Segunda, 10/08 às 19:00 — Adultos", None)
        recorded: dict = {}

        def fake_book(event_id: str, lead: dict) -> dict:
            recorded["event_id"] = event_id
            recorded["lead"] = lead
            return {"status": "created", "booking_id": "x", "active_count": 1, "calendar_synced": True}

        sender = self.next_sender()
        with patched(handlers, "get_cached_slots", lambda days_ahead=14: [slot]), \
                patched(scheduling, "book_slot", fake_book):
            self.drive(sender, "Quero a de segunda 19h de adultos, sou o João",
                       _raw("Fechado, João!", stage="booked", lead_name="João",
                            qualification="qualified", action="book", event_id="suite-adult-1"))
        expect_equal(recorded.get("event_id"), "suite-adult-1", "event_id repassado ao book_slot")
        expect_equal(self._session_row(sender)["stage"], "booked", "stage")
        return "pulou etapas e marcou em um turno"

    def test_objection_then_flow(self) -> str:
        sender = self.next_sender()
        self.drive(sender, "Tá muito caro isso aí", _raw("Entendo! A primeira aula é gratuita 🙂",
                   stage="objection", qualification="unknown", action="none"))
        expect_equal(self._session_row(sender)["stage"], "objection", "stage após objeção")
        self.drive(sender, "Ah, então quero experimentar", _raw("Que dias você pode?",
                   stage="availability", qualification="qualified", action="none"))
        expect_equal(self._session_row(sender)["stage"], "availability", "stage voltou ao fluxo")
        return "objeção tratada e fluxo retomado"

    def test_unqualified_recorded(self) -> str:
        sender = self.next_sender()
        self.drive(sender, "Só quero vender um plano de internet pra vocês",
                   _raw("Obrigada, mas não temos interesse.", stage="interest",
                        qualification="unqualified", action="none"))
        expect_equal(self._session_row(sender)["qualification"], "unqualified", "qualification")
        return "qualification=unqualified gravada"

    def test_handoff_pauses_and_isolates(self) -> str:
        paused = self.next_sender()
        other = self.next_sender()

        self.drive(paused, "Quero falar com um humano", _raw("Vou te conectar com nossa equipe!",
                   stage="handoff_requested", qualification="qualified", action="handoff"))
        row = self._session_row(paused)
        expect(row["is_paused"] is True, "sessão deveria estar pausada")
        expect_equal(row["stage"], "handoff_requested", "stage")

        before = self.send.count_for(paused)
        calls_before = self.ai.calls
        self.drive(paused, "Alô? Tem alguém aí?", _raw("não deveria responder", stage="interest",
                   qualification="qualified", action="none"))
        expect_equal(self.send.count_for(paused), before, "lead pausado não deveria receber resposta")
        expect_equal(self.ai.calls, calls_before, "o LLM não deveria nem ser chamado para lead pausado")

        out = self.drive(other, "Oi, quero uma aula", _raw("Oi! Claro 😄", stage="greeting",
                         qualification="unknown", action="none"))
        expect(out is not None, "outro lead deveria continuar sendo atendido")
        return "handoff pausa o lead, isola dos demais, e o bot fica mudo para ele"

    def test_timeout_resets_and_records(self) -> str:
        sender = self.next_sender()
        self.drive(sender, "oi", _raw("Oi! Bem-vindo 🥋", stage="interest",
                   lead_name="Fulano", qualification="qualified", action="none"))
        self._age_session(sender, hours=2)

        with LogCapture("bot.handlers") as log:
            self.drive(sender, "voltei", _raw("Olá de novo! Bem-vindo 🥋", stage="greeting",
                       qualification="unknown", action="none"))
        expect(any("closed_no_booking" in m for m in log.messages),
               "o timeout deveria registrar closed_no_booking no log")
        history = self._session_row(sender)["history"]
        expect_equal(len(history), 2, "histórico deveria ter reiniciado (só o novo turno)")
        return "timeout encerra, registra closed_no_booking e reinicia do zero"

    def test_timeout_does_not_unpause(self) -> str:
        sender = self.next_sender()
        self.drive(sender, "quero humano", _raw("Conectando você à equipe!",
                   stage="handoff_requested", qualification="qualified", action="handoff"))
        self._age_session(sender, hours=2)

        before = self.send.count_for(sender)
        self.drive(sender, "ainda aí?", _raw("não deveria responder", stage="interest",
                   qualification="unknown", action="none"))
        row = self._session_row(sender)
        expect(row["is_paused"] is True, "a pausa deveria sobreviver ao timeout")
        expect_equal(row["stage"], "handoff_requested", "stage não deveria ter reiniciado")
        expect_equal(self.send.count_for(sender), before, "lead pausado não deveria receber resposta")
        return "pausa isenta do timeout: continua pausada com updated_at antigo"

    def test_active_booking_injected_after_timeout(self) -> str:
        sender = self.next_sender()
        # A real trial_bookings row (fake calendar id, unlimited class so no lock/capacity).
        result = bookings.create_booking_with_lock(
            calendar_event_id=f"suite-fake-evt-{sender}", sender=sender, lead_name="Retornante",
            class_type="ADULTOS", slot_start=datetime.now(scheduling.TIMEZONE),
            slot_end=datetime.now(scheduling.TIMEZONE), capacity=None,
        )
        expect_equal(result["status"], "created", "pré-condição: reserva criada")
        self.drive(sender, "oi", _raw("Oi!", stage="interest", qualification="qualified", action="none"))
        self._age_session(sender, hours=2)

        with patched(handlers, "get_cached_slots", lambda days_ahead=14: []):
            self.drive(sender, "posso remarcar?", _raw("Claro! Vi que você já tem uma aula marcada.",
                       stage="greeting", qualification="qualified", action="none"))
        prompt = self.ai.last_system_prompt or ""
        expect("ACTIVE BOOKINGS" in prompt and "Retornante" in prompt,
               "os agendamentos ativos do lead deveriam ter sido injetados no prompt após o timeout")
        return "lead com reserva ativa que volta pós-timeout: a IA recebe o agendamento no contexto"

    def test_timeout_notice_in_prompt(self) -> str:
        sender = self.next_sender()
        with patched(handlers, "get_cached_slots", lambda days_ahead=14: []):
            self.drive(sender, "oi", _raw("Olá!", stage="greeting", qualification="unknown", action="none"))
        prompt = (self.ai.last_system_prompt or "").lower()
        expect("1 hour" in prompt or "1h" in prompt, "o aviso de timeout deveria constar na camada protegida")
        return "instrução do aviso de 1h presente no system prompt"

    def test_malformed_json(self) -> str:
        sender = self.next_sender()
        out = self.drive(sender, "oi", 'Olá, tudo bem?\n<corujai_action>{"stage": "interest", oops}</corujai_action>')
        expect(out == "Olá, tudo bem?", "a mensagem (sem o bloco) deveria chegar ao lead")
        expect_equal(self._session_row(sender)["stage"], "greeting", "estado não deveria mudar com JSON quebrado")
        return "JSON malformado: mensagem entregue, nenhuma ação"

    def test_absent_block(self) -> str:
        sender = self.next_sender()
        out = self.drive(sender, "oi", "Bom dia! Como posso ajudar?")
        expect_equal(out, "Bom dia! Como posso ajudar?", "mensagem sem bloco deveria passar intacta")
        expect_equal(self._session_row(sender)["stage"], "greeting", "estado permanece o inicial")
        return "bloco ausente: conversa segue normal, sem warning"

    def test_invalid_event_id_refused(self) -> str:
        slot = _synthetic_slot("suite-real-1", "ADULTOS", "Terça, 11/08 às 20:00 — Adultos", None)
        called = {"n": 0}

        def fake_book(event_id: str, lead: dict) -> dict:
            called["n"] += 1
            return {"status": "created", "booking_id": "x", "active_count": 1, "calendar_synced": True}

        sender = self.next_sender()
        with patched(handlers, "get_cached_slots", lambda days_ahead=14: [slot]), \
                patched(scheduling, "book_slot", fake_book):
            out = self.drive(sender, "marca esse aí", _raw("Agendado!", stage="booked",
                             lead_name="Ana", qualification="qualified", action="book",
                             event_id="id-que-a-ia-inventou"))
        expect_equal(called["n"], 0, "book_slot NÃO deveria ser chamado com event_id inválido")
        expect(self._session_row(sender)["stage"] != "booked", "não deveria marcar com id inválido")
        expect(out is not None, "deveria responder reofertando horários")
        return "event_id fora da lista injetada: ação recusada antes do book_slot"

    def test_two_blocks_last_wins(self) -> str:
        sender = self.next_sender()
        raw = ('Deixa eu ver...\n'
               '<corujai_action>{"stage": "interest", "qualification": "unknown", "action": "none"}</corujai_action>\n'
               'corrigindo\n'
               '<corujai_action>{"stage": "proposal", "qualification": "qualified", "action": "none"}</corujai_action>')
        self.drive(sender, "oi", raw)
        expect_equal(self._session_row(sender)["stage"], "proposal", "o último bloco deveria vencer")
        return "dois blocos: o último vence"

    def test_slot_full_recovers(self) -> str:
        slot = _synthetic_slot("suite-full-1", "CRIANCAS", "Quarta, 12/08 às 17:00 — Crianças", 1)
        sender = self.next_sender()
        with patched(handlers, "get_cached_slots", lambda days_ahead=14: [slot]), \
                patched(scheduling, "book_slot", lambda e, l: {"status": "full", "active_count": 4}):
            out = self.drive(sender, "marca essa", _raw("Agendado!", stage="booked",
                             lead_name="Bia", child_name="Lu", qualification="qualified",
                             action="book", event_id="suite-full-1"))
        expect(self._session_row(sender)["stage"] != "booked", "slot cheio não deveria ficar booked")
        expect(out is not None and "lot" in out.lower(), "deveria avisar que lotou")
        return "full: conversa avisa e se recupera"

    def test_duplicate_booking(self) -> str:
        slot = _synthetic_slot("suite-dup-1", "ADULTOS", "Quinta, 13/08 às 19:00 — Adultos", None)
        sender = self.next_sender()
        with patched(handlers, "get_cached_slots", lambda days_ahead=14: [slot]), \
                patched(scheduling, "book_slot", lambda e, l: {"status": "duplicate"}):
            out = self.drive(sender, "marca de novo", _raw("Agendado!", stage="booked",
                             lead_name="Rui", qualification="qualified", action="book",
                             event_id="suite-dup-1"))
        expect(out is not None and "já tem" in out.lower(), "deveria informar reserva já existente")
        return "duplicate: informa que o lead já tem esse horário"

    def test_calendar_disconnected(self) -> str:
        """Disconnected calendar → get_cached_slots returns [] → AI keeps talking."""
        sender = self.next_sender()
        with patched(handlers, "get_cached_slots", lambda days_ahead=14: []):
            out = self.drive(sender, "quero uma aula", _raw("Oi! Me conta o que procura 🙂",
                             stage="interest", qualification="unknown", action="none"))
        expect(out is not None, "a conversa deveria continuar mesmo sem calendário")
        prompt = self.ai.last_system_prompt or ""
        expect("none available" in prompt, "sem slots, o prompt deveria indicar que não há horários")
        return "calendário desconectado: IA segue conversando, sem oferecer horários"

    # -- teardown -----------------------------------------------------------

    def teardown(self) -> None:
        if self.keep:
            print("  --keep: sessões, reservas e descrições de eventos preservadas.")
            return

        # Restore any real event description a live booking patched.
        for event_id, description in self._original_descriptions.items():
            try:
                service, calendar_id = scheduling._get_service_or_raise()
                service.events().patch(
                    calendarId=calendar_id, eventId=event_id,
                    body={"description": description,
                          "extendedProperties": {"private": {"corujai_booked_count": None}}},
                ).execute()
            except Exception as exc:  # noqa: BLE001 - report and keep cleaning
                print(f"  falha ao restaurar descrição do evento {event_id[:10]}…: {exc}")

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM trial_bookings WHERE sender LIKE %s", (SENDER_PREFIX + "%",))
                removed_bookings = cur.rowcount
                cur.execute("DELETE FROM sessions WHERE sender LIKE %s", (SENDER_PREFIX + "%",))
                removed_sessions = cur.rowcount
            conn.commit()
        print(f"  limpeza: {removed_sessions} sessão(ões) e {removed_bookings} reserva(s) de teste removidas.")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _resolve_report_path(path: Path) -> Path:
    if path.is_dir() or path.suffix.lower() != ".json":
        path = path / f"report_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Suíte automatizada da IA de agendamento (AI_ACTION_TESTING.md).")
    parser.add_argument("--keep", action="store_true", help="Não desfaz nada ao final (para depurar à mão).")
    parser.add_argument("--skip-live", action="store_true",
                        help="Pula os testes que escrevem no Google Calendar real.")
    parser.add_argument("--no-color", action="store_true", help="Saída sem cores ANSI.")
    parser.add_argument("--json", nargs="?", type=Path, const=DEFAULT_REPORT_DIR, default=None,
                        metavar="ARQUIVO",
                        help="Também grava o relatório em JSON (sozinha: tests/outputs/ com nome datado).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.ERROR, format="%(levelname)s - %(message)s")

    console = Console(color=not args.no_color and sys.stdout.isatty())
    report = Report(console)

    print(console.bold("\n═══ Corujai · Módulo 3 — suíte da IA com JSON de ação ═══"))
    print(console.dim(" Roteiro: AI_ACTION_TESTING.md"))

    report.section("Pré-requisitos")
    suite = AiActionSuite(report, keep=args.keep, skip_live=args.skip_live)
    if not report.run("P1", "Migrations 006 e 007 aplicadas", suite.check_migrations):
        print(console.red("\n Pré-requisitos falharam — a suíte não pode continuar."))
        report.summary()
        sys.exit(1)

    report.section("Preparo")
    atexit.register(suite.teardown)
    report.run("F1", "Doubles instalados e slots reais localizados", suite.prepare_fixtures)

    report.section("Roteiro de testes")
    tests: list[tuple[str, str, Callable[[], str | None]]] = [
        ("1", "Agendamento de adultos (E2E) → trial_bookings, child_name NULL", suite.test_book_adults),
        ("2", "Agendamento infantil (E2E) → dois nomes gravados", suite.test_book_child),
        ("3", "Aula infantil sem nome da criança → missing_child_name", suite.test_missing_child_name),
        ("4", "Lead que já diz o horário → marca em um turno", suite.test_lead_states_time_upfront),
        ("5", "Objeção fora do roteiro → responde e volta ao fluxo", suite.test_objection_then_flow),
        ("6", "Lead desqualificado → qualification gravada", suite.test_unqualified_recorded),
        ("7", "Handoff → pausa, isola e silencia o lead", suite.test_handoff_pauses_and_isolates),
        ("8", "Timeout de 1h → reinicia e registra closed_no_booking", suite.test_timeout_resets_and_records),
        ("9", "Timeout NÃO desfaz a pausa", suite.test_timeout_does_not_unpause),
        ("10", "Reserva ativa injetada após o timeout", suite.test_active_booking_injected_after_timeout),
        ("11", "Aviso de timeout presente no system prompt", suite.test_timeout_notice_in_prompt),
        ("12", "JSON malformado → mensagem entregue, sem ação", suite.test_malformed_json),
        ("13", "Bloco ausente → conversa segue normal", suite.test_absent_block),
        ("14", "event_id fora da lista → ação recusada", suite.test_invalid_event_id_refused),
        ("15", "Dois blocos → o último vence", suite.test_two_blocks_last_wins),
        ("16", "Slot lotou entre oferta e confirmação → full", suite.test_slot_full_recovers),
        ("17", "Mesmo lead reserva o mesmo slot → duplicate", suite.test_duplicate_booking),
        ("18", "Google desconectado → IA segue sem oferecer horários", suite.test_calendar_disconnected),
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
