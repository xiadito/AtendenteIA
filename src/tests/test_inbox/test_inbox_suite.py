"""Automated end-to-end suite for the operator inbox (Module 5).

Runs the scenarios documented in INBOX_TESTING.md and prints a PASS/FAIL report
boiled down to an exit code, in the same style as
tests/test_owner_notifications/test_owner_notifications_suite.py and
tests/test_ai_action/test_ai_action_suite.py.

Everything here is deterministic: the LLM is replaced with a scripted double and
whatsapp_service.send_message with a capture double (or a raising one, for the
failure-path test), so no real WhatsApp message ever leaves this run and no
tokens are spent. The dashboard routes are exercised through Flask's test
client with the session pre-authenticated — the reply and resume paths are HTTP
behaviour (status codes, HX-Trigger, the error banner), not just data access, so
calling the functions directly would test the wrong thing.

Teardown removes only what this run wrote: sessions for the suite's 5524000...
senders. Their messages go with them, since messages.sender carries ON DELETE
CASCADE.

Run from src/:
    python tests/test_inbox/test_inbox_suite.py
    python tests/test_inbox/test_inbox_suite.py --keep
    python tests/test_inbox/test_inbox_suite.py --json

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

# Locate src/ by NAME, like app.py and the other suites.
SRC_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "src")
sys.path.insert(0, str(SRC_DIR))

import bot.ai_context as ai_context  # noqa: E402
import bot.handlers as handlers  # noqa: E402
import bot.messages as messages  # noqa: E402
import bot.session as session  # noqa: E402
import integrations.store as store  # noqa: E402
import webhook.routes as routes  # noqa: E402
import whatsapp.whatsapp_service as whatsapp_service  # noqa: E402
from database.db import get_connection  # noqa: E402

# All suite leads share this prefix so teardown can scope its DELETEs and never
# touch a real lead or the other suites' senders (5521000... / 5522000... /
# 5523000...).
SENDER_PREFIX = "5524000"

MIGRATION_MESSAGES = "007_create_messages"

# Columns migration 001 must have after Module 5 edited it in place.
NEW_STATE_COLUMNS = {"needs_resume_note", "conversation_started_at"}

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
            print(self.console.green("\n Inbox do operador OK — todos os testes passaram.\n"))
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


class AIStub:
    """Stands in for get_ai_response: scripted reply, records what it received.

    Captures the payload and the system prompt of every call, which is how the
    ordering tests and the resume-note test observe what the model would have
    seen without spending a token.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.payloads: list[list[dict[str, str]]] = []
        self.prompts: list[str] = []
        self.next_raw = _raw("Certo!")

    def __call__(self, payload: list[dict[str, str]], system_prompt: str) -> str:
        self.calls += 1
        self.payloads.append(payload)
        self.prompts.append(system_prompt)
        return self.next_raw


def _raw(message: str, stage: str = "interest", qualification: str = "unknown",
         action: str = "none") -> str:
    """Build a raw AI response with a well-formed action block."""
    block = json.dumps({"stage": stage, "qualification": qualification, "action": action})
    return f"{message}\n<{ai_context.ACTION_TAG}>{block}</{ai_context.ACTION_TAG}>"


# ---------------------------------------------------------------------------
# Suite
# ---------------------------------------------------------------------------

class InboxSuite:
    """Owns fixtures, tests and teardown for the operator inbox."""

    def __init__(self, report: Report, keep: bool) -> None:
        self.report = report
        self.keep = keep
        self._n = 0
        self.app: Any = None
        self.client: Any = None

    # -- infrastructure -----------------------------------------------------

    def next_sender(self) -> str:
        """Return a fresh suite-scoped lead number."""
        self._n += 1
        return f"{SENDER_PREFIX}{self._n:06d}"

    def ensure_session(self, sender: str) -> dict:
        """Create the sessions row a message's FK requires."""
        return session.get_session(sender)

    def drive(self, sender: str, text: str, ai: AIStub, raw: str | None = None) -> None:
        """Run one full turn through the real handler with the AI stubbed."""
        if raw is not None:
            ai.next_raw = raw
        with patched(handlers, "get_ai_response", ai):
            handlers.handle_text_message(sender, text)

    def _session_row(self, sender: str) -> dict | None:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT stage, is_paused, needs_resume_note, conversation_started_at
                    FROM sessions WHERE sender = %s
                    """,
                    (sender,),
                )
                return cur.fetchone()

    def _age_session(self, sender: str, hours: int) -> None:
        """Push a session's updated_at into the past to simulate inactivity."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE sessions SET updated_at = NOW() - (%s || ' hours')::interval WHERE sender = %s",
                    (str(hours), sender),
                )
            conn.commit()

    def _application(self) -> Any:
        """Build the Flask app once and cache it (create_app re-runs init_db)."""
        if self.app is None:
            import app as flask_app

            self.app = flask_app.create_app()
            self.app.config["TESTING"] = True
        return self.app

    def _authenticated_client(self) -> Any:
        """Return a test client with the dashboard session already logged in.

        The inbox routes sit behind @_require_auth, so without this every request
        would 302 to the login page and the tests would pass on a redirect.
        """
        if self.client is None:
            self.client = self._application().test_client()
            with self.client.session_transaction() as flask_session:
                flask_session["dashboard_authenticated"] = True
        return self.client

    def _anonymous_client(self) -> Any:
        """Return a test client with no dashboard session."""
        return self._application().test_client()

    # -- prerequisites ------------------------------------------------------

    def check_schema(self) -> str:
        """Check the Module 5 schema is in place.

        Two separate checks, because they can fail independently: migration 007
        is a NEW file and shows up in schema_migrations, but the columns added to
        001 do NOT — 001 was edited in place, and init_db only ever compares
        version strings. A database created before this module has 001 recorded
        as applied and the old sessions table forever, which is exactly the
        failure this catches.
        """
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version FROM schema_migrations WHERE version = %s", (MIGRATION_MESSAGES,))
                messages_applied = cur.fetchone() is not None
                cur.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'sessions'
                    """
                )
                columns = {row["column_name"] for row in cur.fetchall()}

        expect(messages_applied,
               f"migration {MIGRATION_MESSAGES} não aplicada (suba a app para rodar init_db)")
        missing = NEW_STATE_COLUMNS - columns
        expect(not missing,
               f"colunas ausentes em sessions: {sorted(missing)} — a 001 foi editada, "
               "então o banco precisa ser RECRIADO, não migrado (ver INBOX_TESTING.md)")
        expect("history" not in columns,
               "a coluna history ainda existe em sessions — o banco é anterior a este módulo, recrie-o")
        return "007 aplicada, sessions sem history e com os marcadores novos"

    # -- tests --------------------------------------------------------------

    def test_01_add_message_authors(self) -> str:
        """1. add_message grava os três autores; autor inválido levanta erro."""
        sender = self.next_sender()
        self.ensure_session(sender)

        messages.add_message(sender, "lead", "quero uma aula")
        messages.add_message(sender, "ai", "Claro! Qual modalidade?", is_read=True)
        messages.add_message(sender, "operator", "Aqui é o Isac", is_read=True)

        conversation = messages.get_conversation(sender)
        expect_equal([m["author"] for m in conversation], ["lead", "ai", "operator"],
                     "os três autores deveriam estar gravados, em ordem")
        expect_equal([m["is_read"] for m in conversation], [False, True, True],
                     "só a mensagem do lead deveria nascer não lida")

        raised = False
        try:
            messages.add_message(sender, "manager", "não deveria gravar")
        except ValueError:
            raised = True
        expect(raised, "um author fora de valid_authors deveria levantar ValueError")
        expect_equal(len(messages.get_conversation(sender)), 3,
                     "o author inválido não deveria ter gravado nada")

        return "lead/ai/operator gravam; author inválido levanta ValueError e não grava"

    def test_02_paused_stores_without_calling_ai(self) -> str:
        """2. Pausa (3B): mensagem do lead é gravada e a IA NÃO é chamada."""
        sender = self.next_sender()
        ai = AIStub()
        send = SendCapture()

        with patched(whatsapp_service, "send_message", send):
            self.drive(sender, "oi, quero treinar", ai, _raw("Oi! Bem-vindo 🥋"))
            calls_before = ai.calls
            sent_before = len(send.sent)

            # Um handoff pausa a conversa; daqui em diante o operador é quem fala.
            state = session.get_session(sender)
            state["is_paused"] = True
            state["stage"] = "handoff_requested"
            session.save_session(sender, state)

            self.drive(sender, "alô? tem alguém aí?", ai, _raw("não deveria ser gerada"))

        expect_equal(ai.calls, calls_before, "o LLM não deveria ser chamado para lead pausado")
        expect_equal(len(send.sent), sent_before, "o lead pausado não deveria receber resposta")

        conversation = messages.get_conversation(sender)
        expect_equal(conversation[-1]["author"], "lead",
                     "a última mensagem deveria ser a do lead — gravada durante a pausa")
        expect_equal(conversation[-1]["content"], "alô? tem alguém aí?",
                     "o texto enviado durante a pausa deveria estar gravado")
        expect_equal(conversation[-1]["is_read"], False,
                     "a mensagem recebida durante a pausa deveria nascer não lida")

        return "pausa grava a mensagem do lead, não chama o LLM e não responde"

    def test_03_payload_order_and_roles(self) -> str:
        """3. Payload: últimos N em ordem cronológica, com autor→role correto."""
        sender = self.next_sender()
        self.ensure_session(sender)

        messages.add_message(sender, "lead", "m1")
        messages.add_message(sender, "ai", "m2", is_read=True)
        messages.add_message(sender, "operator", "m3", is_read=True)
        messages.add_message(sender, "lead", "m4")

        recent = messages.get_recent_messages(sender, 10)
        expect_equal([m["content"] for m in recent], ["m1", "m2", "m3", "m4"],
                     "get_recent_messages deveria devolver em ordem cronológica")

        # O LIMIT tem de ficar com as MAIS RECENTES, não com o começo da conversa.
        tail = messages.get_recent_messages(sender, 2)
        expect_equal([m["content"] for m in tail], ["m3", "m4"],
                     "o limite deveria manter as mensagens mais recentes")

        payload = handlers._to_llm_payload(recent)
        expect_equal([item["role"] for item in payload],
                     ["user", "assistant", "assistant", "user"],
                     "lead→user, ai e operator→assistant")

        # Uma janela que abre no meio de uma rajada do operador não pode começar
        # em "assistant": o endpoint compatível da Anthropic recusa.
        burst = handlers._to_llm_payload(messages.get_recent_messages(sender, 3))
        expect_equal(burst[0]["role"], "user",
                     "o payload nunca deveria começar em assistant")
        expect_equal([item["content"] for item in burst], ["m4"],
                     "as mensagens assistant do início da janela deveriam ser descartadas")

        return "janela cronológica, mais recentes preservadas, roles corretos e sem assistant à frente"

    def test_04_unread_count_and_mark_read(self) -> str:
        """4. Não lidas: contagem correta; mark_conversation_read zera as do lead."""
        sender = self.next_sender()
        self.ensure_session(sender)

        messages.add_message(sender, "lead", "oi")
        messages.add_message(sender, "lead", "tem aula hoje?")
        messages.add_message(sender, "ai", "Tem sim!", is_read=True)
        messages.add_message(sender, "operator", "Pode vir às 19h", is_read=True)

        expect_equal(messages.count_unread(sender), 2, "só as duas do lead deveriam estar não lidas")

        marked = messages.mark_conversation_read(sender)
        expect_equal(marked, 2, "mark_conversation_read deveria marcar exatamente as duas do lead")
        expect_equal(messages.count_unread(sender), 0, "não deveria sobrar mensagem não lida")

        expect_equal(messages.mark_conversation_read(sender), 0,
                     "marcar de novo não deveria escrever nada")

        conversation = messages.get_conversation(sender)
        expect(all(m["is_read"] for m in conversation),
               "todas as mensagens deveriam estar lidas depois da marcação")

        return "contagem correta, marcação zera só as do lead e é idempotente"

    def test_05_operator_reply_sends_then_stores(self) -> str:
        """5. Resposta do operador: envia→grava; falha de envio NÃO grava."""
        sender = self.next_sender()
        self.ensure_session(sender)
        client = self._authenticated_client()

        # O patch é em routes.send_message, não em whatsapp_service.send_message:
        # routes faz `from whatsapp.whatsapp_service import send_message`, que
        # liga o nome no módulo de destino. Trocar o atributo na origem não
        # alcançaria a rota — e o teste enviaria de verdade pelo Twilio.

        # -- caminho feliz: envia, depois grava
        send = SendCapture()
        with patched(routes, "send_message", send):
            response = client.post(f"/dashboard/inbox/{sender}/reply", data={"text": "Oi! Aqui é o Isac"})

        expect_equal(response.status_code, 200, "a resposta do operador deveria voltar 200")
        expect_equal(len(send.sent), 1, "deveria ter saído exatamente um envio")
        expect_equal(send.sent[0], (sender, "Oi! Aqui é o Isac"), "o envio deveria levar o texto do operador")

        conversation = messages.get_conversation(sender)
        expect_equal(len(conversation), 1, "deveria haver exatamente uma mensagem gravada")
        expect_equal(conversation[0]["author"], "operator", "a mensagem deveria ser do operador")
        expect_equal(conversation[0]["is_read"], True, "a mensagem do operador deveria nascer lida")
        expect_equal(response.headers.get("HX-Trigger"), "reply-sent",
                     "o sucesso deveria disparar reply-sent para limpar o campo")

        # -- caminho de falha: NÃO grava, e não devolve 500
        failing = SendCapture(raise_error=True)
        with patched(routes, "send_message", failing):
            response = client.post(f"/dashboard/inbox/{sender}/reply", data={"text": "essa não sai"})

        expect_equal(response.status_code, 200,
                     "a falha deveria voltar 200 com aviso, não um 500 (o HTMX não troca em 5xx)")
        expect_equal(len(messages.get_conversation(sender)), 1,
                     "uma falha de envio não pode deixar mensagem fantasma gravada")
        body = response.get_data(as_text=True)
        expect("Não foi possível enviar" in body, "o operador deveria ver um aviso de falha em português")
        expect("essa não sai" not in body, "o texto que falhou não deveria aparecer como mensagem enviada")
        expect(response.headers.get("HX-Trigger") is None,
               "a falha não deveria disparar reply-sent — o texto tem de continuar no campo")

        return "envia→grava no sucesso; falha devolve 200 com aviso e não grava nada"

    def test_06_resume_unpauses_and_injects_note_once(self) -> str:
        """6. Despausar (4B): is_paused cai, stage reseta, nota entra uma única vez."""
        sender = self.next_sender()
        ai = AIStub()
        send = SendCapture()
        client = self._authenticated_client()

        # O handoff enfileiraria uma notificação ao dono (Módulo 4). Isso é
        # testado lá, não aqui, e deixaria uma linha em owner_notifications fora
        # do alcance do teardown — o stub corta o efeito colateral na raiz.
        with patched(whatsapp_service, "send_message", send), \
                patched(store, "get_owner_for_notification", lambda: None):
            self.drive(sender, "quero falar com uma pessoa", ai,
                       _raw("Vou te conectar com a equipe!", stage="handoff_requested", action="handoff"))

            row = self._session_row(sender)
            expect_equal(row["is_paused"], True, "o handoff deveria ter pausado a conversa")

            response = client.post(f"/dashboard/inbox/{sender}/resume")
            expect_equal(response.status_code, 302, "o resume deveria redirecionar de volta para a conversa")

            row = self._session_row(sender)
            expect_equal(row["is_paused"], False, "o resume deveria despausar a conversa")
            expect_equal(row["stage"], "interest", "o resume deveria resetar o stage para um valor neutro")
            expect(row["stage"] in session.valid_stages, "o stage de retomada precisa ser válido")
            expect_equal(row["needs_resume_note"], True, "o resume deveria armar a nota de retomada")

            # Próximo turno: a nota entra no prompt e o marcador é limpo.
            self.drive(sender, "voltei", ai, _raw("Claro, continuando..."))
            expect(ai_context.RESUME_NOTE in ai.prompts[-1],
                   "a nota de retomada deveria aparecer no próximo system prompt")
            expect_equal(self._session_row(sender)["needs_resume_note"], False,
                         "o marcador deveria ter sido limpo na mesma passagem")

            # E o turno seguinte já não a recebe.
            self.drive(sender, "e aí?", ai, _raw("Bora marcar?"))
            expect(ai_context.RESUME_NOTE not in ai.prompts[-1],
                   "a nota não deveria voltar no turno seguinte")

        return "resume despausa, reseta o stage e injeta a nota exatamente uma vez"

    def test_07_list_conversations_ordering(self) -> str:
        """7. list_conversations: pausadas/não lidas no topo."""
        quiet = self.next_sender()
        unread = self.next_sender()
        paused = self.next_sender()

        for sender in (quiet, unread, paused):
            self.ensure_session(sender)

        # A "quieta" é a mais RECENTE das três, para provar que a prioridade
        # vence a recência em vez de a ordenação ser só por data.
        messages.add_message(unread, "lead", "tem vaga?")
        messages.add_message(paused, "lead", "quero um humano")
        messages.add_message(quiet, "ai", "Bem-vindo!", is_read=True)

        state = session.get_session(paused)
        state["is_paused"] = True
        session.save_session(paused, state)
        messages.mark_conversation_read(paused)

        listing = messages.list_conversations()
        positions = {row["sender"]: index for index, row in enumerate(listing) if row["sender"] in
                     {quiet, unread, paused}}

        expect(positions[paused] < positions[quiet],
               "a conversa pausada deveria vir antes da conversa quieta")
        expect(positions[unread] < positions[quiet],
               "a conversa com não lidas deveria vir antes da conversa quieta")

        by_sender = {row["sender"]: row for row in listing}
        expect_equal(by_sender[unread]["unread_count"], 1, "a contagem de não lidas deveria ser 1")
        expect_equal(by_sender[paused]["unread_count"], 0,
                     "a pausada foi marcada como lida — deveria subir pela pausa, não pela contagem")
        expect_equal(by_sender[paused]["is_paused"], True, "is_paused deveria vir do join com sessions")
        expect_equal(by_sender[quiet]["last_author"], "ai", "a prévia deveria trazer o autor da última mensagem")

        # Uma sessão sem mensagem nenhuma tem de aparecer, não sumir da vista.
        empty = self.next_sender()
        self.ensure_session(empty)
        row = next((r for r in messages.list_conversations() if r["sender"] == empty), None)
        expect(row is not None, "uma conversa sem mensagens ainda deveria aparecer na lista")
        expect(row["last_content"] is None, "a conversa sem mensagens não deveria ter prévia")

        return "pausadas e não lidas no topo; conversa sem mensagem continua listada"

    def test_08_timeout_moves_boundary_without_deleting(self) -> str:
        """Extra: o timeout de 1h reinicia a janela da IA sem apagar o inbox."""
        sender = self.next_sender()
        ai = AIStub()
        send = SendCapture()

        with patched(whatsapp_service, "send_message", send):
            self.drive(sender, "oi", ai, _raw("Olá! 🥋"))
            self._age_session(sender, hours=2)
            self.drive(sender, "voltei", ai, _raw("Olá de novo!", stage="greeting"))

        boundary = self._session_row(sender)["conversation_started_at"]
        expect(datetime.now(timezone.utc) - boundary < timedelta(minutes=5),
               "o timeout deveria ter movido conversation_started_at para agora")

        window = messages.get_recent_messages(sender, 20, since=boundary)
        expect_equal(len(window), 2, "a janela da IA deveria conter só o turno novo")
        expect_equal(len(messages.get_conversation(sender)), 4,
                     "o inbox deveria manter as mensagens anteriores ao timeout")

        return "timeout move a fronteira; a IA recomeça e o operador não perde nada"

    def test_09_routes_require_auth(self) -> str:
        """Extra: as rotas do inbox ficam atrás do login."""
        sender = self.next_sender()
        self.ensure_session(sender)

        anonymous = self._anonymous_client()

        for method, path in (
            ("get", "/dashboard/inbox"),
            ("get", "/dashboard/inbox/conversations"),
            ("get", f"/dashboard/inbox/{sender}"),
            ("get", f"/dashboard/inbox/{sender}/messages"),
            ("post", f"/dashboard/inbox/{sender}/reply"),
            ("post", f"/dashboard/inbox/{sender}/resume"),
        ):
            response = getattr(anonymous, method)(path)
            expect_equal(response.status_code, 302, f"{method.upper()} {path} deveria redirecionar para o login")
            expect("/dashboard/login" in response.headers.get("Location", ""),
                   f"{method.upper()} {path} deveria apontar para a tela de login")

        expect_equal(len(messages.get_conversation(sender)), 0,
                     "uma requisição não autenticada não pode ter gravado nada")

        return "as seis rotas do inbox exigem sessão autenticada"

    def test_10_unknown_sender_does_not_create_a_session(self) -> str:
        """Extra: um número desconhecido na URL dá 404, não vira conversa fantasma."""
        unknown = f"{SENDER_PREFIX}999999"
        expect(not session.session_exists(unknown), "o número de teste não deveria existir ainda")

        client = self._authenticated_client()
        send = SendCapture()

        with patched(routes, "send_message", send):
            for method, path in (
                ("get", f"/dashboard/inbox/{unknown}"),
                ("get", f"/dashboard/inbox/{unknown}/messages"),
                ("post", f"/dashboard/inbox/{unknown}/reply"),
                ("post", f"/dashboard/inbox/{unknown}/resume"),
            ):
                response = getattr(client, method)(path, data={"text": "oi"})
                expect_equal(response.status_code, 404,
                             f"{method.upper()} {path} deveria dar 404 para número sem sessão")

        expect(not session.session_exists(unknown),
               "abrir a URL de um número desconhecido não pode criar sessão — "
               "ela apareceria na lista do operador como conversa fantasma")
        expect_equal(len(send.sent), 0, "nada deveria ter sido enviado para um número sem conversa")

        return "número desconhecido dá 404 e não cria sessão nem envia mensagem"

    # -- teardown -----------------------------------------------------------

    def teardown(self) -> None:
        if self.keep:
            print("  --keep: sessões e mensagens de teste preservadas.")
            return

        with get_connection() as conn:
            with conn.cursor() as cur:
                # As mensagens vão junto: messages.sender tem ON DELETE CASCADE.
                cur.execute("SELECT COUNT(*) AS total FROM messages WHERE sender LIKE %s",
                            (SENDER_PREFIX + "%",))
                removed_messages = cur.fetchone()["total"]
                # Rede de segurança: o teste de handoff dubla o lookup do dono
                # para não enfileirar nada, mas uma run interrompida antes do
                # stub poderia ter deixado linha para trás.
                cur.execute("DELETE FROM owner_notifications WHERE lead_sender LIKE %s",
                            (SENDER_PREFIX + "%",))
                removed_notifications = cur.rowcount
                cur.execute("DELETE FROM sessions WHERE sender LIKE %s", (SENDER_PREFIX + "%",))
                removed_sessions = cur.rowcount
            conn.commit()

        print(f"  limpeza: {removed_sessions} sessão(ões), {removed_messages} mensagem(ns) e "
              f"{removed_notifications} notificação(ões) de teste removidas.")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _resolve_report_path(target: Path) -> Path:
    """Turn --json's value into a concrete file path, creating the directory."""
    if target.suffix:
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return target / f"inbox-{stamp}.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Suíte E2E do inbox do operador (Módulo 5).")
    parser.add_argument("--keep", action="store_true",
                        help="Não apaga as sessões/mensagens de teste no final.")
    parser.add_argument("--no-color", action="store_true", help="Desliga as cores ANSI.")
    parser.add_argument("--json", nargs="?", type=Path, const=DEFAULT_REPORT_DIR, default=None,
                        metavar="ARQUIVO",
                        help="Também grava o relatório em JSON (sozinha: tests/outputs/ com nome datado).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.ERROR, format="%(levelname)s - %(message)s")

    console = Console(color=not args.no_color and sys.stdout.isatty())
    report = Report(console)

    print(console.bold("\n═══ Corujai · suíte do inbox do operador ═══"))
    print(console.dim(" Roteiro: INBOX_TESTING.md"))

    report.section("Pré-requisitos")
    suite = InboxSuite(report, keep=args.keep)
    if not report.run("P1", "Schema do Módulo 5 aplicado", suite.check_schema):
        print(console.red("\n Pré-requisitos falharam — a suíte não pode continuar."))
        report.summary()
        sys.exit(1)

    report.section("Roteiro de testes")
    atexit.register(suite.teardown)
    tests: list[tuple[str, str, Callable[[], str | None]]] = [
        ("1", "add_message grava os três autores; inválido levanta erro",
         suite.test_01_add_message_authors),
        ("2", "Pausa grava a mensagem do lead e não chama a IA",
         suite.test_02_paused_stores_without_calling_ai),
        ("3", "Payload: ordem cronológica e autor→role", suite.test_03_payload_order_and_roles),
        ("4", "Não lidas: contagem e marcação", suite.test_04_unread_count_and_mark_read),
        ("5", "Resposta do operador: envia→grava, falha não grava",
         suite.test_05_operator_reply_sends_then_stores),
        ("6", "Despausar reseta o stage e injeta a nota uma vez",
         suite.test_06_resume_unpauses_and_injects_note_once),
        ("7", "list_conversations: pausadas/não lidas no topo",
         suite.test_07_list_conversations_ordering),
        ("8", "Timeout move a fronteira sem apagar o inbox",
         suite.test_08_timeout_moves_boundary_without_deleting),
        ("9", "As rotas do inbox exigem autenticação", suite.test_09_routes_require_auth),
        ("10", "Número desconhecido dá 404 e não vira conversa fantasma",
         suite.test_10_unknown_sender_does_not_create_a_session),
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
