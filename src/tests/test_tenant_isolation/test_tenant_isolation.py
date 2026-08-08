"""Manual CLI for per-tenant read isolation (Module S3b).

Where the suite asserts, this one lets you SEE. It pairs with DBeaver: build the
same lead in two gyms here, then read the rows back and confirm the isolation is
in the data and not only in the assertions.

    0. Conferir o estado do schema (migration 011: PK, FK e UNIQUE)
       python tests/test_tenant_isolation/test_tenant_isolation.py schema

    1. Criar duas academias de teste
       python tests/test_tenant_isolation/test_tenant_isolation.py setup

    2. Escrever o MESMO lead nas duas, com textos diferentes
       python tests/test_tenant_isolation/test_tenant_isolation.py seed --sender 5530000111111

    3. Ler como cada uma delas — é aqui que o vazamento apareceria
       python tests/test_tenant_isolation/test_tenant_isolation.py read --sender 5530000111111

    4. Ver a mesma pergunta feita direto no banco, sem passar pelo código
       python tests/test_tenant_isolation/test_tenant_isolation.py rows --sender 5530000111111

    5. Provar o CASCADE composto: apaga na A, a B continua inteira
       python tests/test_tenant_isolation/test_tenant_isolation.py cascade --sender 5530000111111

    6. Limpar tudo
       python tests/test_tenant_isolation/test_tenant_isolation.py teardown

Cria academias de verdade — rode contra um banco de DESENVOLVIMENTO. Nada aqui
escreve nas linhas do piloto: tudo vive sob o prefixo `suite-s3b-`.

Run from src/.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Locate src/ by NAME, like app.py and the suites.
SRC_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "src")
sys.path.insert(0, str(SRC_DIR))

import accounts.provision as provision  # noqa: E402
import bot.bookings as bookings  # noqa: E402
import bot.class_types as class_types  # noqa: E402
import bot.messages as messages  # noqa: E402
import bot.session as session_store  # noqa: E402
from database.db import get_connection  # noqa: E402

RULE: str = "─" * 68

TENANT_PREFIX: str = "suite-s3b-"
TENANT_A: str = f"{TENANT_PREFIX}alfa"
TENANT_B: str = f"{TENANT_PREFIX}beta"
EMAIL_DOMAIN: str = "@suite-s3b.corujai.test"
SUITE_PASSWORD: str = "suite-password-s3b"


def _cmd_schema(args: argparse.Namespace) -> None:
    """Print the three keys migration 011 changed, straight from the catalog."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_migrations ORDER BY version")
            versions: list[str] = [row["version"] for row in cur.fetchall()]

            cur.execute(
                """
                SELECT conrelid::regclass::text AS table_name,
                       conname,
                       pg_get_constraintdef(oid) AS definition
                FROM pg_constraint
                WHERE conrelid IN ('sessions'::regclass, 'messages'::regclass,
                                   'trial_bookings'::regclass)
                  AND contype IN ('p', 'f', 'u')
                ORDER BY table_name, conname
                """
            )
            constraints = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT tablename, indexname, indexdef FROM pg_indexes
                WHERE tablename IN ('sessions', 'messages', 'trial_bookings')
                  AND indexname LIKE 'idx_%'
                ORDER BY tablename, indexname
                """
            )
            indexes = [dict(row) for row in cur.fetchall()]

    print(RULE)
    print("MIGRATIONS APLICADAS")
    print(RULE)
    for version in versions:
        mark = " ← este módulo" if version == "011_tenant_isolation" else ""
        print(f"  {version}{mark}")

    if "011_tenant_isolation" not in versions:
        print("\n  ⚠ a 011 NÃO está aplicada. Rode `python app.py` uma vez.")

    print(f"\n{RULE}")
    print("CHAVES (o que a 011 mudou)")
    print(RULE)
    for row in constraints:
        print(f"  {row['table_name']:<16} {row['definition']}")

    print(f"\n{RULE}")
    print("ÍNDICES (o tenant à frente)")
    print(RULE)
    for row in indexes:
        print(f"  {row['indexdef']}")


def _cmd_setup(args: argparse.Namespace) -> None:
    """Provision the two fixture gyms, with deliberately different class labels."""
    for tenant_id, label in ((TENANT_A, "alfa"), (TENANT_B, "beta")):
        email: str = f"{TENANT_PREFIX}{label}{EMAIL_DOMAIN}"
        result = provision.provision_tenant(
            academy_name=f"Suite S3b Academia {label.title()}",
            email=email,
            password=SUITE_PASSWORD,
            tenant_id=tenant_id,
        )
        # Rótulos diferentes de propósito: uma mensagem composta com os rótulos
        # da academia errada fica visível no TEXTO, não só errada no banco.
        class_types.update_class_type(
            "ADULTOS", f"Adultos {label.title()}", None, False, tenant_id=tenant_id
        )
        status: str = "criada" if result["created"] else "já existia"
        print(f"  ✔ {result['tenant_id']:<20} login {email}  ({status})")

    print(f"\n  senha das duas: {SUITE_PASSWORD}")
    print("  entre em /dashboard/login com cada e-mail e compare as telas.")


def _cmd_seed(args: argparse.Namespace) -> None:
    """Write the SAME lead into both gyms, with text that says which is which."""
    sender: str = args.sender

    for tenant_id, label in ((TENANT_A, "Alfa"), (TENANT_B, "Beta")):
        state = session_store.get_session(sender, tenant_id=tenant_id)
        state["lead_name"] = f"Lead da {label}"
        state["stage"] = "proposal" if label == "Alfa" else "greeting"
        session_store.save_session(sender, state, tenant_id=tenant_id)

        messages.add_message(sender, "lead", f"oi, escrevi para a {label}", tenant_id=tenant_id)
        messages.add_message(sender, "ai", f"bem-vindo à {label}!", is_read=True,
                             tenant_id=tenant_id)

        start = datetime.now(timezone.utc) + timedelta(days=2)
        result = bookings.create_booking_with_lock(
            calendar_event_id=f"evt-manual-{sender}", sender=sender,
            lead_name=f"Lead da {label}", class_type="ADULTOS", slot_start=start,
            slot_end=start + timedelta(hours=1), capacity=None, tenant_id=tenant_id,
        )
        print(f"  ✔ {tenant_id:<20} sessão, 2 mensagens, reserva {result['status']}")

    print("\n  Note que o MESMO calendar_event_id e o MESMO sender foram aceitos nas duas:")
    print("  é o UNIQUE (tenant_id, calendar_event_id, sender) da migration 011.")


def _cmd_read(args: argparse.Namespace) -> None:
    """Read the same lead through the code, as each gym. The leak would show here."""
    sender: str = args.sender

    for tenant_id in (TENANT_A, TENANT_B):
        print(RULE)
        print(f"LENDO COMO {tenant_id}")
        print(RULE)

        if not session_store.session_exists(sender, tenant_id=tenant_id):
            print("  (sem sessão nesta academia — rode `seed` primeiro)\n")
            continue

        state = session_store.get_session(sender, tenant_id=tenant_id)
        print(f"  lead_name        : {state['lead_name']}")
        print(f"  stage            : {state['stage']}")

        thread = messages.get_conversation(sender, tenant_id=tenant_id)
        print(f"  mensagens        : {len(thread)}")
        for row in thread:
            print(f"      [{row['author']:<8}] {row['content']}")

        print(f"  não lidas        : {messages.count_unread(sender, tenant_id=tenant_id)}")

        active = bookings.list_active_bookings_by_sender(sender, tenant_id=tenant_id)
        print(f"  reservas ativas  : {len(active)}")
        for row in active:
            print(f"      {row['id']}  {row['lead_name']}  ({row['status']})")

        listing = messages.list_conversations(tenant_id)
        print(f"  conversas no inbox: {len(listing)}")
        for row in listing:
            print(f"      {row['sender']}  {row['lead_name']}  «{row['last_content']}»")
        print()


def _cmd_rows(args: argparse.Namespace) -> None:
    """Ask the same question in raw SQL, with no tenant filter at all.

    This is the control: the table really does hold both gyms' rows. What the
    application never does again is read them together.
    """
    sender: str = args.sender

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tenant_id, sender, lead_name, stage FROM sessions "
                "WHERE sender = %s ORDER BY tenant_id",
                (sender,),
            )
            sessions = [dict(row) for row in cur.fetchall()]

            cur.execute(
                "SELECT tenant_id, author, content FROM messages "
                "WHERE sender = %s ORDER BY tenant_id, created_at, id",
                (sender,),
            )
            msgs = [dict(row) for row in cur.fetchall()]

            cur.execute(
                "SELECT tenant_id, id, lead_name, calendar_event_id, status "
                "FROM trial_bookings WHERE sender = %s ORDER BY tenant_id",
                (sender,),
            )
            books = [dict(row) for row in cur.fetchall()]

    print(RULE)
    print(f"SEM FILTRO DE TENANT — o que a tabela guarda para {sender}")
    print(RULE)
    print(f"\nsessions ({len(sessions)}):")
    for row in sessions:
        print(f"  {row['tenant_id']:<20} {row['lead_name']:<16} {row['stage']}")
    print(f"\nmessages ({len(msgs)}):")
    for row in msgs:
        print(f"  {row['tenant_id']:<20} [{row['author']:<8}] {row['content']}")
    print(f"\ntrial_bookings ({len(books)}):")
    for row in books:
        print(f"  {row['tenant_id']:<20} {row['calendar_event_id']:<26} {row['status']}")
    print("\n  Duas linhas por tabela, mesmo sender. Compare com o `read`:")
    print("  o código só devolve uma delas de cada vez.")


def _cmd_cascade(args: argparse.Namespace) -> None:
    """Delete the lead at gym A and show gym B is untouched."""
    sender: str = args.sender

    before_a = len(messages.get_conversation(sender, tenant_id=TENANT_A))
    before_b = len(messages.get_conversation(sender, tenant_id=TENANT_B))
    print(f"  antes : {TENANT_A} tem {before_a} mensagem(ns), {TENANT_B} tem {before_b}")

    session_store.clear_session(sender, tenant_id=TENANT_A)

    after_a = len(messages.get_conversation(sender, tenant_id=TENANT_A))
    after_b = len(messages.get_conversation(sender, tenant_id=TENANT_B))
    print(f"  depois: {TENANT_A} tem {after_a} mensagem(ns), {TENANT_B} tem {after_b}")
    print(f"  sessão da B ainda existe: {session_store.session_exists(sender, tenant_id=TENANT_B)}")

    if after_a == 0 and after_b == before_b:
        print("\n  ✔ o CASCADE viajou pelo par (tenant_id, sender), como a 011 definiu.")
    else:
        print("\n  ✖ inesperado — confira a FK composta com `schema`.")


def _cmd_teardown(args: argparse.Namespace) -> None:
    """Delete both fixture gyms and everything hanging off them."""
    removed: dict[str, int] = {}
    with get_connection() as conn:
        with conn.cursor() as cur:
            for table in ("owner_notifications", "trial_bookings", "messages", "sessions",
                          "users", "class_types", "ai_configs", "scheduling_configs", "owners"):
                cur.execute(
                    f"DELETE FROM {table} WHERE tenant_id LIKE %s", (TENANT_PREFIX + "%",)
                )
                if cur.rowcount:
                    removed[table] = cur.rowcount
            cur.execute("DELETE FROM users WHERE email LIKE %s", ("%" + EMAIL_DOMAIN,))
            if cur.rowcount:
                removed["users (por e-mail)"] = cur.rowcount
        conn.commit()

    if removed:
        for table, count in removed.items():
            print(f"  removido: {count:>3} de {table}")
    else:
        print("  nada a remover — o banco já estava limpo.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CLI manual do isolamento por tenant (Módulo S3b)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("schema", help="mostra as chaves e índices que a 011 mudou").set_defaults(
        func=_cmd_schema
    )
    sub.add_parser("setup", help="cria as duas academias de teste").set_defaults(func=_cmd_setup)

    for name, help_text, func in (
        ("seed", "escreve o mesmo lead nas duas academias", _cmd_seed),
        ("read", "lê o mesmo lead como cada academia", _cmd_read),
        ("rows", "mostra as linhas cruas, sem filtro de tenant", _cmd_rows),
        ("cascade", "apaga na A e prova que a B fica inteira", _cmd_cascade),
    ):
        node = sub.add_parser(name, help=help_text)
        node.add_argument("--sender", default="5530000111111",
                          help="número do lead (prefixo 5530000 é o desta suíte)")
        node.set_defaults(func=func)

    sub.add_parser("teardown", help="remove as duas academias").set_defaults(func=_cmd_teardown)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
