"""Manual CLI to hold a conversation with the scheduling AI, without WhatsApp.

Drives the real bot/handlers.py pipeline (real LLM, real Postgres, real Google
Calendar) from the terminal. whatsapp_service.send_message is monkeypatched so
replies print to the console instead of going out over Twilio.

Run from src/, e.g.:
    python tests/test_ai_action/test_ai_action.py chat --sender 5522000000001
    python tests/test_ai_action/test_ai_action.py state --sender 5522000000001
    python tests/test_ai_action/test_ai_action.py reset --sender 5522000000001
    python tests/test_ai_action/test_ai_action.py unpause --sender 5522000000001
    python tests/test_ai_action/test_ai_action.py timeout --sender 5522000000001

`chat` is an interactive REPL: type a message, see the bot reply, and after each
turn the current session state (stage / qualification / names / paused) is shown.
"""
import argparse
import sys
from pathlib import Path

# Locate src/ by NAME (not by counting .parent hops), like app.py and the
# scheduling tests do, so moving this file never silently breaks the import.
SRC_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "src")
sys.path.insert(0, str(SRC_DIR))

import bot.handlers as handlers  # noqa: E402
import bot.session as session  # noqa: E402
import whatsapp.whatsapp_service as whatsapp_service  # noqa: E402
from database.db import get_connection  # noqa: E402


def _install_console_send() -> None:
    """Replace the WhatsApp sender with one that prints to the terminal."""
    def _print_send(sender: str, message: str) -> None:
        print(f"\n🤖 Corujai → {sender}:\n{message}\n")

    whatsapp_service.send_message = _print_send


def _print_state(sender: str) -> None:
    """Print the current session-state columns for a sender."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT stage, qualification, lead_name, child_name, is_paused, updated_at
                FROM sessions WHERE sender = %s
                """,
                (sender,),
            )
            row = cur.fetchone()

    if row is None:
        print(f"(sem sessão para {sender})")
        return

    print(
        f"   estado → stage={row['stage']} | qualification={row['qualification']} | "
        f"lead_name={row['lead_name']!r} | child_name={row['child_name']!r} | "
        f"is_paused={row['is_paused']}"
    )


def _cmd_chat(args: argparse.Namespace) -> None:
    _install_console_send()
    print(f"Conversa com o Corujai como lead {args.sender}. Ctrl-D ou 'sair' para encerrar.\n")

    while True:
        try:
            text = input("👤 você: ").strip()
        except EOFError:
            print()
            break

        if text.lower() in {"sair", "exit", "quit"}:
            break
        if not text:
            continue

        handlers.handle_text_message(args.sender, text)
        _print_state(args.sender)


def _cmd_state(args: argparse.Namespace) -> None:
    _print_state(args.sender)


def _cmd_reset(args: argparse.Namespace) -> None:
    session.clear_session(args.sender)
    print(f"Sessão de {args.sender} apagada (histórico e estado zerados).")


def _cmd_unpause(args: argparse.Namespace) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE sessions SET is_paused = FALSE WHERE sender = %s", (args.sender,))
            changed = cur.rowcount
        conn.commit()
    print(f"{'Despausado' if changed else 'Nada a fazer (sem sessão)'}: {args.sender}")


def _cmd_timeout(args: argparse.Namespace) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET updated_at = NOW() - INTERVAL '2 hours' WHERE sender = %s",
                (args.sender,),
            )
            changed = cur.rowcount
        conn.commit()
    print(
        f"{'updated_at recuado 2h' if changed else 'Nada a fazer (sem sessão)'}: {args.sender}. "
        "A próxima mensagem deve reiniciar a conversa."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Conversa manual com a IA de agendamento do Corujai.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, func, help_text in (
        ("chat", _cmd_chat, "Abre uma conversa interativa com o bot."),
        ("state", _cmd_state, "Mostra o estado atual da sessão."),
        ("reset", _cmd_reset, "Apaga a sessão do lead (zera histórico e estado)."),
        ("unpause", _cmd_unpause, "Remove a pausa de handoff da sessão."),
        ("timeout", _cmd_timeout, "Recua updated_at em 2h para simular inatividade."),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--sender", required=True, help="Telefone do lead, ex.: 5522000000001")
        sub.set_defaults(func=func)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
