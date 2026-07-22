"""Manual CLI to exercise the scheduling engine without the AI.

Run from src/, e.g.:
    python tests/test_scheduling/test_scheduling.py list
    python tests/test_scheduling/test_scheduling.py book <event_id> --sender 5521999999999 --name "Ana"
    python tests/test_scheduling/test_scheduling.py cleanup
"""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Lets this script import the app's packages (bot, config, database, ...) the
# same way app.py does. src/ is located by NAME instead of by counting parent
# directories: this file has already moved once (src/scripts/ →
# src/tests/test_scheduling/), and a hardcoded number of .parent hops breaks
# silently on the next move — the path stays valid, it just points elsewhere.
SRC_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "src")
sys.path.insert(0, str(SRC_DIR))

from bot.scheduling import (  # noqa: E402
    TIMEZONE,
    IntegrationNeedsReconnectError,
    IntegrationNotConnectedError,
    _get_service_or_raise,
    book_slot,
    get_available_slots,
)
from database.db import get_connection  # noqa: E402


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


def _collect_events(service, calendar_id: str, days_back: int, days_ahead: int) -> list[dict]:
    """Fetch every non-cancelled event in the window, paging until exhausted.

    singleEvents=False keeps a recurring series collapsed into its master, so
    a weekly class is deleted once as a series instead of once per instance.

    Args:
        service: Authenticated Calendar API client.
        calendar_id (str): ID of the "Aulas Experimentais" calendar.
        days_back (int): How far into the past to sweep.
        days_ahead (int): How far into the future to sweep.

    Returns:
        list[dict]: Event resources, series masters included.
    """
    now = datetime.now(TIMEZONE)
    events: list[dict] = []
    page_token = None

    while True:
        response = service.events().list(
            calendarId=calendar_id,
            timeMin=(now - timedelta(days=days_back)).isoformat(),
            timeMax=(now + timedelta(days=days_ahead)).isoformat(),
            singleEvents=False,
            maxResults=250,
            pageToken=page_token,
        ).execute()

        events.extend(e for e in response.get("items", []) if e.get("status") != "cancelled")
        page_token = response.get("nextPageToken")
        if not page_token:
            return events


def _describe(event: dict) -> str:
    """One-line human summary of an event, for the confirmation listing."""
    start = event.get("start", {})
    when = start.get("dateTime") or start.get("date") or "?"
    kind = " [série recorrente]" if event.get("recurrence") else ""
    return f"  {when:<28} {event.get('summary') or '(sem título)'}{kind}"


def _cmd_cleanup(args: argparse.Namespace) -> None:
    """Wipe the test calendar: every event plus the bookings pointing at them.

    Everything in "Aulas Experimentais" is disposable today — Module 3 hasn't
    wired the AI to the engine yet, so no real lead has ever booked through
    it. This resets the environment to the "calendário vazio" state the
    roteiro's step 1 assumes, whether the events came from the Apps Script,
    from hand-editing, or from a crashed suite run that skipped its teardown.
    """
    service, calendar_id = _get_service_or_raise()
    events = _collect_events(service, calendar_id, args.days_back, args.days_ahead)

    # Deleting a master removes its instances too, so instances are skipped —
    # unless their master fell outside the window, which would leave them
    # orphaned. Those get deleted individually.
    masters = [e for e in events if not e.get("recurringEventId")]
    master_ids = {e["id"] for e in masters}
    orphans = [e for e in events
               if e.get("recurringEventId") and e["recurringEventId"] not in master_ids]
    targets = masters + orphans

    if not targets:
        print(f'Calendário "Aulas Experimentais" já está vazio na janela '
              f"(-{args.days_back}d a +{args.days_ahead}d).")
        return

    print(f"{len(targets)} evento(s) serão APAGADOS de {calendar_id}:\n")
    for event in sorted(targets, key=lambda e: (e.get("start", {}).get("dateTime")
                                                or e.get("start", {}).get("date") or "")):
        print(_describe(event))

    if args.dry_run:
        print("\n--dry-run: nada foi apagado.")
        return

    if not args.yes:
        if not sys.stdin.isatty():
            print("\nSem terminal interativo para confirmar. Use --yes para apagar mesmo assim.")
            sys.exit(1)
        resposta = input("\nApagar todos esses eventos? Isso é IRREVERSÍVEL. [digite 'sim'] ")
        if resposta.strip().lower() != "sim":
            print("Cancelado, nada foi apagado.")
            return

    deleted, failed = 0, 0
    for event in targets:
        try:
            service.events().delete(calendarId=calendar_id, eventId=event["id"]).execute()
            deleted += 1
        except Exception as exc:  # noqa: BLE001 - keep going, report at the end
            failed += 1
            print(f"  falha ao apagar {event.get('summary')!r}: {exc}")

    # Two clauses because a booking can point at either an event we deleted
    # directly (exact match) or at an INSTANCE of a series we deleted. Instance
    # ids are "<master_id>_<timestamp>", so split_part() on "_" maps one back to
    # its series. Google event ids are otherwise alphanumeric, so the
    # underscore is unambiguous. Built from `targets`, not `masters`, so orphan
    # instances get their bookings cleaned too.
    deleted_ids = [event["id"] for event in targets]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM trial_bookings
                WHERE calendar_event_id = ANY(%s)
                   OR split_part(calendar_event_id, '_', 1) = ANY(%s)
                """,
                (deleted_ids, deleted_ids),
            )
            removed_bookings = cur.rowcount
        conn.commit()

    print(f"\n{deleted} evento(s) apagados"
          f"{f', {failed} falha(s)' if failed else ''}"
          f" · {removed_bookings} reserva(s) removidas de trial_bookings.")


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

    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help='Apaga TODOS os eventos do calendário "Aulas Experimentais" e suas reservas.')
    cleanup_parser.add_argument("--days-back", type=int, default=365,
                                help="Quantos dias para trás varrer (padrão: 365).")
    cleanup_parser.add_argument("--days-ahead", type=int, default=365,
                                help="Quantos dias para frente varrer (padrão: 365).")
    cleanup_parser.add_argument("--dry-run", action="store_true",
                                help="Só lista o que seria apagado, sem apagar nada.")
    cleanup_parser.add_argument("--yes", action="store_true",
                                help="Não pede confirmação (para uso em script).")
    cleanup_parser.set_defaults(func=_cmd_cleanup)

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
