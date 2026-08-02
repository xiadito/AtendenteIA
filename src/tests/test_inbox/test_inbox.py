"""Manual CLI for the operator inbox — one operation per command.

The suite asserts; this shows. Each command touches the real Postgres through
the same functions the dashboard routes use, so what you see here is what the
operator sees on screen.

WhatsApp sending is stubbed by default: `reply` prints to the terminal instead
of calling Twilio. Pass --live to actually send, and only against a number you
own.

Run from src/, e.g.:
    python tests/test_inbox/test_inbox.py list
    python tests/test_inbox/test_inbox.py show   --sender 5524000000001
    python tests/test_inbox/test_inbox.py send   --sender 5524000000001 --author lead --text "quero uma aula"
    python tests/test_inbox/test_inbox.py reply  --sender 5524000000001 --text "Oi! Aqui é o Isac"
    python tests/test_inbox/test_inbox.py unread --sender 5524000000001
    python tests/test_inbox/test_inbox.py pause  --sender 5524000000001
    python tests/test_inbox/test_inbox.py resume --sender 5524000000001
    python tests/test_inbox/test_inbox.py prompt --sender 5524000000001

See INBOX_TESTING.md for the full roteiro.
"""
import argparse
import sys
from pathlib import Path

# Locate src/ by NAME (not by counting .parent hops), like app.py and the other
# test CLIs do, so moving this file never silently breaks the import.
SRC_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "src")
sys.path.insert(0, str(SRC_DIR))

import bot.messages as messages  # noqa: E402
import bot.session as session  # noqa: E402
import whatsapp.whatsapp_service as whatsapp_service  # noqa: E402

AUTHOR_LABELS: dict[str, str] = {"lead": "Lead", "ai": "IA", "operator": "Operador"}


def _install_console_send() -> None:
    """Replace the WhatsApp sender with one that prints to the terminal."""
    def _print_send(to: str, text: str) -> str:
        print(f"\n📤 (dublado) Corujai → {to}:\n{text}\n")
        return "SM_stubbed"

    whatsapp_service.send_message = _print_send


def _cmd_list(args: argparse.Namespace) -> None:
    """Print the inbox list exactly in the order the screen shows it."""
    conversations = messages.list_conversations()

    if not conversations:
        print("(nenhuma conversa ainda)")
        return

    print(f"\n{len(conversations)} conversa(s) — pausadas e não lidas primeiro:\n")
    for conversation in conversations:
        tags = []
        if conversation["is_paused"]:
            tags.append("⏸ ASSUMIDA")
        if conversation["unread_count"]:
            tags.append(f"{conversation['unread_count']} não lida(s)")
        tag_text = f"  [{' | '.join(tags)}]" if tags else ""

        when = conversation["last_created_at"]
        when_text = when.strftime("%d/%m %H:%M") if when else "—"
        name = conversation["lead_name"] or "(sem nome)"

        print(f"  {conversation['sender']}  {name}  · {conversation['stage']} · {when_text}{tag_text}")

        preview = conversation["last_content"]
        if preview:
            label = AUTHOR_LABELS.get(conversation["last_author"], "?")
            single_line = preview.replace("\n", " ")
            print(f"      {label}: {single_line[:88]}")
        else:
            print("      (sem mensagens)")
    print()


def _cmd_show(args: argparse.Namespace) -> None:
    """Print one whole conversation, oldest first."""
    conversation = messages.get_conversation(args.sender)

    if not conversation:
        print(f"(nenhuma mensagem para {args.sender})")
        return

    print(f"\nConversa de {args.sender} — {len(conversation)} mensagem(ns):\n")
    for message in conversation:
        label = AUTHOR_LABELS.get(message["author"], message["author"])
        unread = "" if message["is_read"] else "  ← não lida"
        print(f"  [{message['created_at'].strftime('%d/%m %H:%M')}] {label}:{unread}")
        for line in message["content"].splitlines() or [""]:
            print(f"      {line}")
    print()

    if args.read:
        marked = messages.mark_conversation_read(args.sender)
        print(f"{marked} mensagem(ns) marcada(s) como lida(s).\n")


def _ensure_session(sender: str) -> None:
    """Create the sessions row that messages.sender's FK requires.

    In production the session always exists first — the handler creates it on
    the lead's very first message. This CLI can be pointed at a number that has
    never written, so it has to do the same thing explicitly.
    """
    session.get_session(sender)


def _cmd_send(args: argparse.Namespace) -> None:
    """Append a message as any author, without going through the AI."""
    _ensure_session(args.sender)
    messages.add_message(args.sender, args.author, args.text, is_read=args.author != "lead")
    print(f"Mensagem de '{args.author}' gravada para {args.sender}.")


def _cmd_reply(args: argparse.Namespace) -> None:
    """Answer as the operator would from the dashboard: send first, store on success.

    Mirrors webhook/routes.py::inbox_reply — including the order, which is the
    inverse of the AI route on purpose.
    """
    _ensure_session(args.sender)

    if not args.live:
        _install_console_send()

    try:
        whatsapp_service.send_message(args.sender, args.text)
    except Exception as exc:
        print(f"\n❌ Envio falhou: {exc}")
        print("   Nada foi gravado — o registro só reflete o que o lead recebeu de fato.\n")
        return

    messages.add_message(args.sender, "operator", args.text, is_read=True)
    print(f"Resposta enviada e gravada para {args.sender}.")


def _cmd_unread(args: argparse.Namespace) -> None:
    """Show the unread count for one conversation."""
    print(f"{args.sender}: {messages.count_unread(args.sender)} mensagem(ns) não lida(s) do lead.")


def _cmd_pause(args: argparse.Namespace) -> None:
    """Pause a conversation, as a handoff would."""
    state = session.get_session(args.sender)
    state["is_paused"] = True
    state["stage"] = "handoff_requested"
    session.save_session(args.sender, state)
    print(f"Conversa de {args.sender} pausada. A IA não responde este lead até você devolver.")


def _cmd_resume(args: argparse.Namespace) -> None:
    """Hand a conversation back to the AI, exactly as the inbox button does."""
    state = session.get_session(args.sender)
    state["is_paused"] = False
    state["stage"] = "interest"
    state["needs_resume_note"] = True
    session.save_session(args.sender, state)
    print(f"Conversa de {args.sender} devolvida à IA (stage=interest, nota de retomada armada).")


def _cmd_prompt(args: argparse.Namespace) -> None:
    """Report whether the resume note would ride on the next prompt."""
    state = session.get_session(args.sender)

    if state.get("needs_resume_note"):
        print(f"{args.sender}: a nota de retomada ENTRA no próximo prompt (needs_resume_note=true).")
        print("   Depois desse turno o marcador é limpo e a nota não volta.")
    else:
        print(f"{args.sender}: nenhuma nota de retomada pendente (needs_resume_note=false).")

    print(f"   estado → stage={state['stage']} | is_paused={state['is_paused']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="CLI manual do inbox do operador (Módulo 5).")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def _with_sender(sub: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sub.add_argument("--sender", required=True, help='Número do lead, ex.: "5524000000001"')
        return sub

    listing = subparsers.add_parser("list", help="Lista as conversas na ordem do inbox.")
    listing.set_defaults(func=_cmd_list)

    show = _with_sender(subparsers.add_parser("show", help="Mostra uma conversa inteira."))
    show.add_argument("--read", action="store_true", help="Também marca as mensagens do lead como lidas.")
    show.set_defaults(func=_cmd_show)

    send = _with_sender(subparsers.add_parser("send", help="Grava uma mensagem sem passar pela IA."))
    send.add_argument("--author", required=True, choices=sorted(messages.valid_authors))
    send.add_argument("--text", required=True)
    send.set_defaults(func=_cmd_send)

    reply = _with_sender(subparsers.add_parser("reply", help="Responde como operador (envia, depois grava)."))
    reply.add_argument("--text", required=True)
    reply.add_argument("--live", action="store_true", help="Envia de verdade pelo Twilio (padrão: dublado).")
    reply.set_defaults(func=_cmd_reply)

    unread = _with_sender(subparsers.add_parser("unread", help="Conta as não lidas de uma conversa."))
    unread.set_defaults(func=_cmd_unread)

    pause = _with_sender(subparsers.add_parser("pause", help="Pausa a conversa (simula um handoff)."))
    pause.set_defaults(func=_cmd_pause)

    resume = _with_sender(subparsers.add_parser("resume", help="Devolve a conversa à IA."))
    resume.set_defaults(func=_cmd_resume)

    prompt = _with_sender(subparsers.add_parser("prompt", help="Diz se a nota de retomada entra no próximo prompt."))
    prompt.set_defaults(func=_cmd_prompt)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
