"""Manual CLI to drive owner notifications without WhatsApp or Twilio.

Runs the real bot/owner_notifications.py and jobs/drain_notifications.py code
against the real Postgres pointed at by DATABASE_URL. whatsapp_service's
send_message is monkeypatched so any message that would go to the owner
prints to the console instead of going out over Twilio.

Run from src/, e.g.:
    python tests/test_owner_notifications/test_owner_notifications.py set-phone --phone 5521999999999
    python tests/test_owner_notifications/test_owner_notifications.py enqueue --lead-sender 5523000000001 --event-type handoff
    python tests/test_owner_notifications/test_owner_notifications.py list
    python tests/test_owner_notifications/test_owner_notifications.py drain
    python tests/test_owner_notifications/test_owner_notifications.py reply --phone 5521999999999 --body 1
    python tests/test_owner_notifications/test_owner_notifications.py reset --lead-sender 5523000000001
"""
import argparse
import sys
from pathlib import Path

# Locate src/ by NAME (not by counting .parent hops), like app.py and the
# other test suites do, so moving this file never silently breaks the import.
SRC_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "src")
sys.path.insert(0, str(SRC_DIR))

import bot.owner_notifications as owner_notifications  # noqa: E402
import integrations.store as store  # noqa: E402
import jobs.drain_notifications as drain_notifications  # noqa: E402
import whatsapp.whatsapp_service as whatsapp_service  # noqa: E402
from database.db import get_connection  # noqa: E402

# High enough that --list/--drain never hide a notification just because it
# already retried a few times.
MAX_ATTEMPTS_FOR_CLI = 1000


def _install_console_send() -> None:
    """Replace the WhatsApp sender with one that prints to the terminal."""
    def _print_send(to: str, text: str) -> str:
        print(f"\n📣 Corujai → dono {to}:\n{text}\n")
        return "SM-console"

    whatsapp_service.send_message = _print_send


def _cmd_set_phone(args: argparse.Namespace) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE owners SET owner_phone = %s, updated_at = NOW() WHERE tenant_id = %s",
                (args.phone, store.DEFAULT_TENANT_ID),
            )
            changed = cur.rowcount
        conn.commit()
    print(f"{'owner_phone atualizado' if changed else 'Nenhuma linha em owners para tenant_id=default'}: {args.phone}")


def _cmd_enqueue(args: argparse.Namespace) -> None:
    owner = store.get_owner_for_notification()
    if owner is None:
        print("Nenhuma linha em owners para tenant_id='default'.")
        return
    if not owner.get("owner_phone"):
        print("owner_phone está NULL — rode 'set-phone' primeiro.")
        return

    created = owner_notifications.enqueue_notification(
        owner_id=owner["id"],
        owner_phone=owner["owner_phone"],
        event_type=args.event_type,
        lead_sender=args.lead_sender,
        booking_id=args.booking_id,
    )
    print(
        f"{'Enfileirada' if created else 'Bloqueada pelo índice parcial (já existia uma pendente/enviada)'}: "
        f"lead_sender={args.lead_sender} event_type={args.event_type} booking_id={args.booking_id}"
    )


def _cmd_list(args: argparse.Namespace) -> None:
    rows = owner_notifications.list_pending_notifications(MAX_ATTEMPTS_FOR_CLI)
    if not rows:
        print("(nenhuma notificação pendente)")
        return
    for row in rows:
        print(
            f"  #{row['id']:<4} {row['event_type']:<8} lead={row['lead_sender']:<15} "
            f"booking_id={row['booking_id']} attempts={row['attempts']} status={row['status']}"
        )


def _cmd_drain(args: argparse.Namespace) -> None:
    _install_console_send()
    exit_code = drain_notifications.main()
    print(f"drain_notifications.main() saiu com código {exit_code}")


def _cmd_reply(args: argparse.Namespace) -> None:
    _install_console_send()
    # Imported lazily: webhook/routes.py touches Flask's `session`, which is
    # only meaningful inside a request, but receive_twilio_owner() itself
    # doesn't need one.
    import webhook.routes as routes

    routes.receive_twilio_owner(args.phone, args.body)
    print(f"Resposta '{args.body}' processada para o dono {args.phone}.")


def _cmd_reset(args: argparse.Namespace) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM owner_notifications WHERE lead_sender = %s", (args.lead_sender,))
            removed = cur.rowcount
        conn.commit()
    print(f"{removed} notificação(ões) removida(s) para lead_sender={args.lead_sender}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="CLI manual de notificações ao dono do Corujai.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sub = subparsers.add_parser("set-phone", help="Define owners.owner_phone para o tenant 'default'.")
    sub.add_argument("--phone", required=True, help="Número em dígitos puros, ex.: 5521999999999")
    sub.set_defaults(func=_cmd_set_phone)

    sub = subparsers.add_parser("enqueue", help="Enfileira uma notificação para o dono do tenant 'default'.")
    sub.add_argument("--lead-sender", required=True, help="Número do lead, ex.: 5523000000001")
    sub.add_argument("--event-type", required=True, choices=sorted(owner_notifications.valid_event_types))
    sub.add_argument("--booking-id", default=None, help="Obrigatório na prática para event-type=booking.")
    sub.set_defaults(func=_cmd_enqueue)

    sub = subparsers.add_parser("list", help="Lista notificações pendentes.")
    sub.set_defaults(func=_cmd_list)

    sub = subparsers.add_parser("drain", help="Roda o cron uma vez (drain_notifications.main()).")
    sub.set_defaults(func=_cmd_drain)

    sub = subparsers.add_parser("reply", help="Simula a resposta do dono (1/2) sem passar pelo Twilio.")
    sub.add_argument("--phone", required=True, help="Número do dono, dígitos puros.")
    sub.add_argument("--body", required=True, help='Texto da resposta, ex.: "1" ou "2".')
    sub.set_defaults(func=_cmd_reply)

    sub = subparsers.add_parser("reset", help="Apaga as notificações de um lead_sender.")
    sub.add_argument("--lead-sender", required=True)
    sub.set_defaults(func=_cmd_reset)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
