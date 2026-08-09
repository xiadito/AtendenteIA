"""Manual CLI for the funnel metrics (Module S4).

Where the suite asserts, this one lets you SEE. It pairs with DBeaver: seed a
funnel of a known shape here, read the numbers back through the same functions
the screen uses, then run the raw SQL in `rows` and confirm the two agree.

    1. Criar uma academia de teste
       python tests/test_metrics/test_metrics.py setup

    2. Semear um funil de forma conhecida (leads e reservas datados)
       python tests/test_metrics/test_metrics.py seed

    3. Ler os números como a tela lê
       python tests/test_metrics/test_metrics.py show
       python tests/test_metrics/test_metrics.py show --period 7
       python tests/test_metrics/test_metrics.py show --tenant default

    4. Ver a mesma pergunta feita direto no banco, sem passar pelo código
       python tests/test_metrics/test_metrics.py rows

    5. Limpar tudo
       python tests/test_metrics/test_metrics.py teardown

`show --tenant default` é o comando útil no piloto: ele NÃO escreve nada, só lê.
Todo o resto vive sob o prefixo `suite-s4-` e nunca toca nas linhas do piloto.

Run from src/.
"""

import argparse
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

# Locate src/ by NAME, like app.py and the suites.
SRC_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "src")
sys.path.insert(0, str(SRC_DIR))

import accounts.provision as provision  # noqa: E402
import bot.bookings as bookings  # noqa: E402
import bot.messages as messages  # noqa: E402
import bot.metrics as metrics  # noqa: E402
import bot.session as session_store  # noqa: E402
from database.db import get_connection  # noqa: E402

TENANT_PREFIX = "suite-s4-"
TENANT_ID = f"{TENANT_PREFIX}cli"
EMAIL = f"{TENANT_PREFIX}cli@suite-s4.corujai.test"
PASSWORD = "suite-password-s4"
SENDER_PREFIX = "5531000"

# (days back, how many leads, booking status or None)
SEED_PLAN: tuple[tuple[int, int, str | None], ...] = (
    (2, 3, "confirmed"),
    (2, 1, "cancelled"),
    (2, 1, "pending_confirmation"),
    (15, 2, "confirmed"),
    (45, 1, "cancelled"),
    (200, 2, "confirmed"),
)


def _days_ago(days: int) -> datetime:
    """N days back at midday local time, safely away from the midnight boundary."""
    return (datetime.now(metrics.TIMEZONE) - timedelta(days=days)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )


def cmd_setup(args: argparse.Namespace) -> None:
    """Provision the CLI's own gym."""
    result: dict = provision.provision_tenant(
        academy_name="Suite S4 CLI",
        email=EMAIL,
        password=PASSWORD,
        owner_phone=f"{SENDER_PREFIX}900009",
        tenant_id=TENANT_ID,
    )
    print(f"tenant:  {result['tenant_id']}")
    print(f"login:   {EMAIL} / {PASSWORD}")
    print("\nAgora rode `seed` e depois `show`.")


def cmd_seed(args: argparse.Namespace) -> None:
    """Seed leads and bookings at controlled instants."""
    counter: int = 0
    created: int = 0

    for days_back, how_many, status in SEED_PLAN:
        for _ in range(how_many):
            counter += 1
            sender: str = f"{SENDER_PREFIX}{counter:06d}"

            session_store.get_session(sender, tenant_id=TENANT_ID)
            messages.add_message(sender, "lead", "oi", tenant_id=TENANT_ID)
            _backdate_last_message(sender, _days_ago(days_back))

            if status is None:
                continue

            slot_start: datetime = datetime.now(metrics.TIMEZONE) + timedelta(days=3)
            result: dict = bookings.create_booking_with_lock(
                calendar_event_id=f"evt-s4-cli-{uuid.uuid4().hex[:12]}",
                sender=sender,
                lead_name="Lead da CLI",
                class_type="ADULTOS",
                slot_start=slot_start,
                slot_end=slot_start + timedelta(hours=1),
                capacity=None,
                tenant_id=TENANT_ID,
            )
            if result["status"] != "created":
                print(f"  aviso: reserva de {sender} não criada ({result['status']})")
                continue

            if status != "pending_confirmation":
                bookings.update_booking_status(result["booking_id"], status,
                                               tenant_id=TENANT_ID)
            _backdate_booking(result["booking_id"], _days_ago(days_back))
            created += 1

    print(f"{counter} lead(s) e {created} reserva(s) semeados em {TENANT_ID}.")
    print("A forma esperada:  7d -> 5 leads / 5 reservas   30d -> 7 / 7   90d -> 8 / 8")
    print("Os 2 leads de 200 dias atrás ficam de fora de todas as janelas.")


def _backdate_last_message(sender: str, when: datetime) -> None:
    """Move the most recent message of a lead to a chosen instant."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE messages SET created_at = %s
                WHERE id = (
                    SELECT id FROM messages
                    WHERE tenant_id = %s AND sender = %s
                    ORDER BY id DESC LIMIT 1
                )
                """,
                (when, TENANT_ID, sender),
            )
        conn.commit()


def _backdate_booking(booking_id: str, when: datetime) -> None:
    """Move a booking's created_at to a chosen instant."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE trial_bookings SET created_at = %s WHERE tenant_id = %s AND id = %s",
                (when, TENANT_ID, booking_id),
            )
        conn.commit()


def cmd_show(args: argparse.Namespace) -> None:
    """Print the funnel exactly as the screen computes it."""
    tenant_id: str = args.tenant or TENANT_ID
    days: int = metrics.parse_period(str(args.period))
    funnel: dict = metrics.get_funnel(tenant_id, days=days)

    print(f"\n  {tenant_id} — últimos {funnel['period_days']} dias")
    print(f"  de {funnel['period_start'].strftime('%d/%m/%Y %H:%M %Z')}")
    print(f"  até {funnel['period_end'].strftime('%d/%m/%Y %H:%M %Z')}\n")

    if not funnel["has_data"]:
        print("  sem dados no período\n")
        return

    rows: tuple[tuple[str, int, float | None, str], ...] = (
        ("Leads", funnel["leads"], None, ""),
        ("Agendamentos", funnel["booked"], funnel["lead_to_booking_rate"], "dos leads"),
        ("Confirmados", funnel["confirmed"], funnel["booking_to_confirmed_rate"],
         "dos agendamentos"),
        ("Cancelados", funnel["cancelled"], funnel["booking_to_cancelled_rate"],
         "dos agendamentos"),
    )
    biggest: int = max(row[1] for row in rows) or 1

    for label, value, rate, of in rows:
        bar: str = "█" * round(value * 24 / biggest)
        share: str = "" if rate is None else f"{rate:>6.1f}% {of}"
        print(f"  {label:<14} {value:>5}  {bar:<24} {share}")

    print(f"\n  pendentes: {funnel['pending']}"
          f"   (confirmados + cancelados + pendentes = {funnel['booked']})")
    print("\n  Nenhum destes números mede comparecimento — ver METRICS_TESTING.md.\n")


def cmd_rows(args: argparse.Namespace) -> None:
    """Ask the database the same two questions, without going through the code."""
    tenant_id: str = args.tenant or TENANT_ID
    days: int = metrics.parse_period(str(args.period))
    start, end = metrics.period_window(days)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sender, MIN(created_at) AS first_contact
                FROM messages
                WHERE tenant_id = %s AND author = 'lead'
                GROUP BY sender
                ORDER BY 2
                """,
                (tenant_id,),
            )
            contacts = cur.fetchall()

            cur.execute(
                """
                SELECT status, created_at
                FROM trial_bookings
                WHERE tenant_id = %s
                ORDER BY created_at
                """,
                (tenant_id,),
            )
            booking_rows = cur.fetchall()

    print(f"\n  janela: {start.isoformat()}  ->  {end.isoformat()}\n")
    print("  PRIMEIRO CONTATO POR LEAD")
    for row in contacts:
        inside: str = "dentro" if start <= row["first_contact"] < end else "  fora"
        print(f"    {inside}  {row['sender']}  {row['first_contact'].isoformat()}")

    print("\n  RESERVAS (por created_at, não por slot_start)")
    for row in booking_rows:
        inside = "dentro" if start <= row["created_at"] < end else "  fora"
        print(f"    {inside}  {row['status']:<22} {row['created_at'].isoformat()}")
    print()


def cmd_teardown(args: argparse.Namespace) -> None:
    """Delete the CLI's gym and everything hanging off it."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            for table in ("owner_notifications", "trial_bookings", "messages", "sessions",
                          "users", "class_types", "ai_configs", "scheduling_configs", "owners"):
                cur.execute(f"DELETE FROM {table} WHERE tenant_id LIKE %s",
                            (TENANT_PREFIX + "%",))
            cur.execute("DELETE FROM users WHERE email = %s", (EMAIL,))
            cur.execute("DELETE FROM sessions WHERE sender LIKE %s", (SENDER_PREFIX + "%",))
        conn.commit()
    print(f"Removido tudo sob o prefixo {TENANT_PREFIX}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="CLI manual das métricas do funil (Módulo S4)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="cria a academia de teste").set_defaults(func=cmd_setup)
    sub.add_parser("seed", help="semeia leads e reservas datados").set_defaults(func=cmd_seed)

    show = sub.add_parser("show", help="imprime o funil como a tela calcula")
    show.add_argument("--tenant", default=None, help="tenant_id (padrão: o da CLI)")
    show.add_argument("--period", default=30, help="7, 30 ou 90 (padrão 30)")
    show.set_defaults(func=cmd_show)

    rows = sub.add_parser("rows", help="mostra as linhas cruas que sustentam os números")
    rows.add_argument("--tenant", default=None, help="tenant_id (padrão: o da CLI)")
    rows.add_argument("--period", default=30, help="7, 30 ou 90 (padrão 30)")
    rows.set_defaults(func=cmd_rows)

    sub.add_parser("teardown", help="remove tudo").set_defaults(func=cmd_teardown)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
