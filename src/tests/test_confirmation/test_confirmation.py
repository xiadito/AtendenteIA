"""Manual CLI for the booking-confirmation feature (Module 6).

One operation per invocation, so each step of CONFIRMATION_TESTING.md can be
run by hand and inspected in DBeaver between commands. WhatsApp is replaced by a
console double everywhere: nothing here ever sends a real message.

Run from src/:
    # 1. Point the pilot owner at a number you control (digits only)
    python tests/test_confirmation/test_confirmation.py set-phone --phone 5521999999999

    # 2. Create a lead + a pending booking to play with
    python tests/test_confirmation/test_confirmation.py seed-booking --sender 5525000000001
    python tests/test_confirmation/test_confirmation.py seed-booking --sender 5525000000002 \
        --child-name "Miguel" --notify

    # 3. See what is on the owner's screen
    python tests/test_confirmation/test_confirmation.py list

    # 4. Decide it — the two channels, same coordinator
    python tests/test_confirmation/test_confirmation.py confirm --booking-id <id>
    python tests/test_confirmation/test_confirmation.py cancel --booking-id <id>
    python tests/test_confirmation/test_confirmation.py reply --phone 5521999999999 --body 1

    # 5. Read the conversation the lead would see
    python tests/test_confirmation/test_confirmation.py conversation --sender 5525000000001

    # 6. Clean up one lead (bookings, notifications, session and messages)
    python tests/test_confirmation/test_confirmation.py reset --sender 5525000000001
"""
import argparse
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

# Locate src/ by NAME, like app.py and the suites.
SRC_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "src")
sys.path.insert(0, str(SRC_DIR))

import bot.bookings as bookings  # noqa: E402
import bot.confirmations as confirmations  # noqa: E402
import bot.messages as messages  # noqa: E402
import bot.owner_notifications as owner_notifications  # noqa: E402
import bot.scheduling as scheduling  # noqa: E402
import bot.session as session_store  # noqa: E402
import integrations.store as store  # noqa: E402
import whatsapp.whatsapp_service as whatsapp_service  # noqa: E402
from database.db import get_connection  # noqa: E402

_STATUS_LABELS = {
    "pending_confirmation": "aguardando",
    "confirmed": "confirmado",
    "cancelled": "cancelado",
}


def _install_console_send() -> None:
    """Replace the WhatsApp sender with one that prints to the terminal.

    Patched on whatsapp_service itself because bot/confirmations.py calls it
    through the module. Anything that reaches send_message from here is printed,
    never sent.
    """
    def _print_send(to: str, text: str, tenant_id: str = "default") -> str:
        print(f"\n📲 Corujai → {to}:\n{text}\n")
        return "SM-console"

    whatsapp_service.send_message = _print_send


def _cmd_set_phone(args: argparse.Namespace) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE owners SET owner_phone = %s, updated_at = NOW() WHERE tenant_id = %s",
                (args.phone, store.DEFAULT_TENANT_ID),
            )
            updated = cur.rowcount
        conn.commit()

    if updated:
        print(f"owner_phone do tenant '{store.DEFAULT_TENANT_ID}' definido para {args.phone}.")
    else:
        print(f"Nenhuma linha em owners para tenant_id='{store.DEFAULT_TENANT_ID}'.")


def _cmd_seed_booking(args: argparse.Namespace) -> None:
    """Create a session + a pending booking, optionally with a sent notification."""
    session_store.get_session(args.sender)  # a FK de messages exige a sessão
    start = datetime.now(scheduling.TIMEZONE) + timedelta(days=args.days_ahead)

    result = bookings.create_booking_with_lock(
        calendar_event_id=f"manual-confirmation-{uuid.uuid4().hex[:12]}",
        sender=args.sender,
        lead_name=args.lead_name,
        class_type="CRIANCAS" if args.child_name else "ADULTOS",
        slot_start=start,
        slot_end=start + timedelta(hours=1),
        capacity=None,
        child_name=args.child_name,
    )

    if result["status"] != "created":
        print(f"Reserva não criada: {result}")
        return

    booking_id = result["booking_id"]
    print(f"Reserva criada: {booking_id} ({start:%d/%m %H:%M}, status pending_confirmation).")

    if not args.notify:
        return

    owner = store.get_owner_for_notification()
    if owner is None or not owner.get("owner_phone"):
        print("Sem owner_phone configurado — rode set-phone antes de usar --notify.")
        return

    owner_notifications.enqueue_notification(
        owner_id=owner["id"], owner_phone=owner["owner_phone"],
        event_type="booking", lead_sender=args.sender, booking_id=booking_id,
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM owner_notifications WHERE booking_id = %s AND event_type = 'booking'",
                (booking_id,),
            )
            row = cur.fetchone()

    if row is not None:
        # Marca como enviada sem passar pelo cron: register_owner_response só
        # enxerga notificações 'sent'.
        owner_notifications.mark_sent(row["id"])
        print(f"Notificação {row['id']} enfileirada e marcada como 'sent' — pronta para o comando reply.")


def _cmd_list(args: argparse.Namespace) -> None:
    """Print the bookings in the same order the dashboard shows them."""
    rows = bookings.list_bookings_for_review()
    if not rows:
        print("Nenhum agendamento.")
        return

    print(f"{'status':<12} {'quando':<12} {'lead':<20} {'aluno':<16} id")
    print("─" * 92)
    for row in rows:
        label = _STATUS_LABELS.get(row["status"], row["status"])
        when = row["slot_start"].astimezone(scheduling.TIMEZONE).strftime("%d/%m %H:%M")
        print(f"{label:<12} {when:<12} {row['lead_name'][:19]:<20} "
              f"{(row['child_name'] or '-')[:15]:<16} {row['id']}")


def _decide(booking_id: str, decision: str) -> None:
    _install_console_send()
    result = confirmations.confirm_or_cancel_booking(booking_id, decision)

    if result["result"] == "not_found":
        print(f"Agendamento {booking_id} não encontrado.")
    elif result["result"] == "skipped":
        print(f"Ignorado pelo guard: o agendamento já estava '{result['status']}'.")
    else:
        aviso = "lead avisado" if result["lead_notified"] else "AVISO AO LEAD FALHOU (status mantido)"
        print(f"Agendamento {booking_id} → {result['decision']}; {aviso}.")


def _cmd_confirm(args: argparse.Namespace) -> None:
    _decide(args.booking_id, "confirmed")


def _cmd_cancel(args: argparse.Namespace) -> None:
    _decide(args.booking_id, "cancelled")


def _cmd_reply(args: argparse.Namespace) -> None:
    """Simulate the owner's WhatsApp reply without going through Twilio."""
    _install_console_send()
    # Imported lazily: webhook/routes.py touches Flask's `session`, which is
    # only meaningful inside a request, but receive_twilio_owner() itself
    # doesn't need one.
    import webhook.routes as routes

    # routes.py binds send_message by name, so the console double has to be
    # installed there too (see the suite's note on the double patch).
    routes.send_message = whatsapp_service.send_message
    routes.receive_twilio_owner(args.phone, args.body)


def _cmd_conversation(args: argparse.Namespace) -> None:
    """Print the whole conversation, so the lead's notice can be read in place."""
    conversation = messages.get_conversation(args.sender)
    if not conversation:
        print(f"Nenhuma mensagem para {args.sender}.")
        return

    for message in conversation:
        stamp = message["created_at"].strftime("%d/%m %H:%M")
        print(f"[{stamp}] {message['author']:>8}: {message['content']}")


def _cmd_reset(args: argparse.Namespace) -> None:
    """Remove one lead's bookings, notifications and session (messages cascade)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM owner_notifications WHERE lead_sender = %s", (args.sender,))
            notifications = cur.rowcount
            cur.execute("DELETE FROM trial_bookings WHERE sender = %s", (args.sender,))
            reservas = cur.rowcount
            cur.execute("DELETE FROM sessions WHERE sender = %s", (args.sender,))
            sessoes = cur.rowcount
        conn.commit()

    print(f"{args.sender}: {notifications} notificação(ões), {reservas} reserva(s) e "
          f"{sessoes} sessão(ões) removidas (mensagens em cascata).")


def main() -> None:
    parser = argparse.ArgumentParser(description="CLI manual de confirmação de agendamento do Corujai.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sub = subparsers.add_parser("set-phone", help="Define owners.owner_phone para o tenant 'default'.")
    sub.add_argument("--phone", required=True, help="Número em dígitos puros, ex.: 5521999999999")
    sub.set_defaults(func=_cmd_set_phone)

    sub = subparsers.add_parser("seed-booking", help="Cria um lead com uma reserva pendente.")
    sub.add_argument("--sender", required=True, help="Número do lead, ex.: 5525000000001")
    sub.add_argument("--lead-name", default="Teste Manual", help="Nome do responsável.")
    sub.add_argument("--child-name", default=None, help="Nome da criança (torna a aula [CRIANCAS]).")
    sub.add_argument("--days-ahead", type=int, default=2, help="Daqui a quantos dias é a aula.")
    sub.add_argument("--notify", action="store_true",
                     help="Também enfileira a notificação ao dono e a marca como 'sent'.")
    sub.set_defaults(func=_cmd_seed_booking)

    sub = subparsers.add_parser("list", help="Lista os agendamentos na ordem da tela do dono.")
    sub.set_defaults(func=_cmd_list)

    sub = subparsers.add_parser("confirm", help="Confirma um agendamento pela coordenadora (como o painel).")
    sub.add_argument("--booking-id", required=True)
    sub.set_defaults(func=_cmd_confirm)

    sub = subparsers.add_parser("cancel", help="Cancela um agendamento pela coordenadora (como o painel).")
    sub.add_argument("--booking-id", required=True)
    sub.set_defaults(func=_cmd_cancel)

    sub = subparsers.add_parser("reply", help="Simula a resposta do dono (1/2) sem passar pelo Twilio.")
    sub.add_argument("--phone", required=True, help="Número do dono, dígitos puros.")
    sub.add_argument("--body", required=True, help='Texto da resposta, ex.: "1" ou "2".')
    sub.set_defaults(func=_cmd_reply)

    sub = subparsers.add_parser("conversation", help="Imprime a conversa inteira de um lead.")
    sub.add_argument("--sender", required=True)
    sub.set_defaults(func=_cmd_conversation)

    sub = subparsers.add_parser("reset", help="Apaga reservas, notificações e sessão de um lead.")
    sub.add_argument("--sender", required=True)
    sub.set_defaults(func=_cmd_reset)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
