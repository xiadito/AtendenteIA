"""Manual CLI to exercise the scheduling engine without the AI.

Run from src/, e.g.:
    python scripts/test_scheduling.py list
    python scripts/test_scheduling.py book <event_id> --sender 5521999999999 --name "Ana"
"""
import argparse
import sys
from pathlib import Path

# Lets this script import sibling packages (bot, config, ...) the same way
# app.py does, even though it lives one directory deeper, in src/scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.scheduling import (  # noqa: E402
    IntegrationNeedsReconnectError,
    IntegrationNotConnectedError,
    book_slot,
    get_available_slots,
)


def _cmd_list(args: argparse.Namespace) -> None:
    slots = get_available_slots(days_ahead=args.days)

    if not slots:
        print("Nenhuma vaga disponível.")
        return

    for slot in slots:
        remaining = "ilimitado" if slot["remaining_slots"] is None else slot["remaining_slots"]
        print(f"{slot['event_id']}  |  {slot['label']}  |  vagas restantes: {remaining}")


def _cmd_book(args: argparse.Namespace) -> None:
    lead = {"sender": args.sender, "name": args.name}
    result = book_slot(args.event_id, lead)
    print(result)


def main() -> None:
    parser = argparse.ArgumentParser(description="Testa manualmente o motor de agendamento do Corujai.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="Lista as vagas disponíveis.")
    list_parser.add_argument("--days", type=int, default=14, help="Quantos dias à frente buscar (padrão: 14).")
    list_parser.set_defaults(func=_cmd_list)

    book_parser = subparsers.add_parser("book", help="Agenda uma vaga.")
    book_parser.add_argument("event_id", help="ID do evento no Calendar (primeira coluna do comando 'list').")
    book_parser.add_argument("--sender", required=True, help="Telefone do lead, ex.: 5521999999999")
    book_parser.add_argument("--name", required=True, help="Nome do lead.")
    book_parser.set_defaults(func=_cmd_book)

    args = parser.parse_args()

    try:
        args.func(args)
    except IntegrationNotConnectedError:
        print("Integração com o Google Calendar não está conectada. Acesse /integrations/google para conectar.")
        sys.exit(1)
    except IntegrationNeedsReconnectError:
        print("O Google recusou o token salvo; reconecte em /integrations/google.")
        sys.exit(1)


if __name__ == "__main__":
    main()
