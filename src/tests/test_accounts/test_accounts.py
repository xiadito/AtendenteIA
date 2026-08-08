"""Manual CLI for accounts, login and tenant provisioning (Module S3a).

Where the suite asserts, this one lets you SEE. It pairs with DBeaver: provision
a tenant here, then read the five tables back and confirm they hold what the
command claimed.

    0. Ver o que já existe
       python tests/test_accounts/test_accounts.py tenants

    1. Ver que slug um nome geraria, sem escrever nada
       python tests/test_accounts/test_accounts.py slug --name "Academia Delariva Itaipuaçu"

    2. Provisionar uma academia de teste
       python tests/test_accounts/test_accounts.py provision \\
           --name "Suite Manual Box" --email manual@suite.corujai.test --password senha-de-teste

    3. Conferir as cinco tabelas do tenant novo
       python tests/test_accounts/test_accounts.py show --tenant suite-manual-box

    4. Testar o login pela rota de verdade
       python tests/test_accounts/test_accounts.py login \\
           --email manual@suite.corujai.test --password senha-de-teste

    5. Ver como um "To" resolve o tenant
       python tests/test_accounts/test_accounts.py resolve --to "whatsapp:+14155238886"

    6. Limpar
       python tests/test_accounts/test_accounts.py drop-tenant --tenant suite-manual-box

⛔ Isto cria tenants de verdade. Rode contra um banco de DESENVOLVIMENTO: até o
Módulo S3b, as leituras não filtram por tenant e uma segunda academia enxerga as
conversas da primeira.

Run from src/.
"""

import argparse
import sys
from pathlib import Path
from typing import Any

# Locate src/ by NAME, like app.py and the suites.
SRC_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "src")
sys.path.insert(0, str(SRC_DIR))

import accounts.provision as provision  # noqa: E402
import accounts.tenants as tenants  # noqa: E402
import accounts.users as accounts_users  # noqa: E402
import bot.ai_configs as ai_configs  # noqa: E402
import bot.class_types as class_types  # noqa: E402
import integrations.store as store  # noqa: E402
from database.db import get_connection  # noqa: E402

RULE: str = "─" * 68


def _cmd_slug(args: argparse.Namespace) -> None:
    """Show the slug a name generates, and whether it is free. Writes nothing."""
    candidate: str | None = tenants.slugify_tenant_id(args.name)

    if candidate is None:
        print("Esse nome não gera identificador nenhum.")
        print("Use ao menos uma letra ou um número (um nome só de emoji não serve).")
        return

    taken: bool = tenants.is_tenant_id_taken(candidate)
    print(f"  nome  : {args.name}")
    print(f"  slug  : {candidate}")
    print(f"  livre : {'NÃO — receberia um sufixo (-2, -3, ...)' if taken else 'sim'}")
    print()
    print("  A regra é mecânica: sem acento, minúsculo, tudo que não é letra ou")
    print("  número vira hífen. Palavras como 'Academia' NÃO são removidas — se")
    print("  quiser um slug mais curto, passe --tenant-id no provisionamento.")


def _cmd_provision(args: argparse.Namespace) -> None:
    """Provision a whole tenant and report what was created."""
    try:
        result: dict[str, Any] = provision.provision_tenant(
            academy_name=args.name,
            email=args.email,
            password=args.password,
            owner_phone=args.owner_phone,
            whatsapp_number=args.whatsapp_number,
            tenant_id=args.tenant_id,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"Recusado: {exc}")
        return

    if not result["created"]:
        print(f"Esse e-mail já tem conta, no tenant '{result['tenant_id']}'.")
        print("Nada foi criado — o provisionamento é idempotente pelo e-mail.")
        return

    print(f"Tenant '{result['tenant_id']}' criado (usuário #{result['user_id']}).")
    print(f"Confira com: python tests/test_accounts/test_accounts.py show "
          f"--tenant {result['tenant_id']}")


def _cmd_show(args: argparse.Namespace) -> None:
    """Print the five tables a provisioned tenant owns."""
    owner: dict | None = None
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM owners WHERE tenant_id = %s", (args.tenant,))
            row = cur.fetchone()
            owner = dict(row) if row else None

    if owner is None:
        print(f"Nenhum tenant '{args.tenant}'.")
        return

    print(RULE)
    print(f" TENANT  {args.tenant}")
    print(RULE)

    print("\n owners")
    print(f"   telefone do dono : {owner['owner_phone'] or '—'}")
    print(f"   número da conta  : {owner['whatsapp_number'] or '— (mensagens caem no default)'}")
    print(f"   Google Calendar  : {owner['integration_status']}")

    config = ai_configs.get_ai_config(args.tenant)
    print("\n ai_configs")
    print(f"   academia   : {config['academy_name']}")
    print(f"   atendente  : {config['assistant_name']}")
    print(f"   tom        : {config['tone'][:60]}")

    print("\n class_types  (lido DIRETO da tabela — load_class_types() inventaria uma)")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT marker, label, capacity, requires_child_name, is_fallback
                FROM class_types WHERE tenant_id = %s ORDER BY marker
                """,
                (args.tenant,),
            )
            rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        print("   NENHUMA LINHA — o tenant está rodando no fallback sintético.")
        print("   Isso funciona, mas ignora capacidade. Provisionamento incompleto.")
    for row in rows:
        capacity: str = "ilimitada" if row["capacity"] is None else str(row["capacity"])
        flags: str = "".join([
            " [PADRÃO]" if row["is_fallback"] else "",
            " [exige nome da criança]" if row["requires_child_name"] else "",
        ])
        print(f"   [{row['marker']}] {row['label']} · {capacity}{flags}")

    bundle = class_types.load_class_types(args.tenant)
    print(f"\n   invariante do S2: fallback '{bundle['fallback']}' "
          f"{'ESTÁ' if bundle['fallback'] in bundle['capacities'] else 'NÃO ESTÁ'} em capacities")

    print("\n scheduling_configs")
    print(f"   janela de busca : {class_types.get_scheduling_config(args.tenant)['days_ahead']} dias")

    print("\n users")
    for user in accounts_users.list_users(args.tenant):
        print(f"   #{user['id']}  {user['email']}  (criado em {user['created_at']:%d/%m/%Y %H:%M})")
    print()


def _cmd_login(args: argparse.Namespace) -> None:
    """Try a login through the real route, with the Flask test client."""
    import app as flask_app

    app = flask_app.create_app()
    app.config["TESTING"] = True
    # CSRF desligado no cliente de teste (Módulo S3c). O Flask-WTF NÃO desliga
    # sozinho por causa de TESTING — ele olha só WTF_CSRF_ENABLED — e sem isto
    # todo POST desta suíte voltaria 400 sem chegar no código sob teste.
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()

    response = client.post(
        "/dashboard/login",
        data={"email": args.email, "password": args.password},
    )

    if response.status_code == 302 and "/dashboard/menu" in response.headers.get("Location", ""):
        print("Login OK — redirecionou para o menu.")
        menu = client.get("/dashboard/menu")
        print(f"  /dashboard/menu com a sessão: {menu.status_code}")
        anonymous = app.test_client().get("/dashboard/menu")
        print(f"  /dashboard/menu sem sessão  : {anonymous.status_code} "
              f"→ {anonymous.headers.get('Location', '')}")
        return

    print(f"Login recusado (status {response.status_code}).")
    print("A mensagem é a mesma para e-mail inexistente e senha errada, de propósito:")
    print("duas mensagens diferentes contariam a quem tenta quais e-mails existem.")


def _cmd_resolve(args: argparse.Namespace) -> None:
    """Show how an inbound 'To' resolves to a tenant."""
    found: str | None = store.find_tenant_by_whatsapp_number(args.to)
    digits: str | None = store.normalize_owner_phone(args.to)

    print(f"  'To' recebido : {args.to}")
    print(f"  normalizado   : {digits or '— (não parece um telefone)'}")

    if found is not None:
        print(f"  tenant        : {found}")
        print("\n  Caminho ROTEADO: dono-vs-lead é decidido DENTRO desse tenant.")
        return

    print(f"  tenant        : nenhum → cai em '{store.DEFAULT_TENANT_ID}' com WARNING no log")
    print("\n  Caminho SANDBOX: é o que acontece hoje, porque o Sandbox do Twilio dá")
    print("  o mesmo número para todo mundo e ninguém pode reivindicá-lo. O")
    print("  comportamento é idêntico ao de antes do S3a.")


def _cmd_tenants(args: argparse.Namespace) -> None:
    """List every tenant and its users."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tenant_id, whatsapp_number FROM owners ORDER BY tenant_id")
            rows = [dict(r) for r in cur.fetchall()]

    print(f"{len(rows)} tenant(s):\n")
    for row in rows:
        users = accounts_users.list_users(row["tenant_id"])
        logins: str = ", ".join(u["email"] for u in users) or "(sem login)"
        number: str = row["whatsapp_number"] or "—"
        print(f"  {row['tenant_id']:<28} número: {number:<16} {logins}")

    if len(rows) > 1:
        print("\n  ⛔ Há mais de um tenant. Isso é seguro em desenvolvimento e NÃO é")
        print("     seguro em produção até o Módulo S3b: as leituras ainda não")
        print("     filtram por tenant, então uma academia enxerga a outra.")
    print()


def _cmd_drop_tenant(args: argparse.Namespace) -> None:
    """Delete a tenant and everything hanging off it."""
    if args.tenant == store.DEFAULT_TENANT_ID:
        print("Recusado: 'default' é o tenant do piloto e não deve ser apagado.")
        return

    with get_connection() as conn:
        with conn.cursor() as cur:
            # users primeiro por clareza (a FK tem ON DELETE CASCADE de qualquer
            # forma); as outras três não têm FK nenhuma e precisam ser explícitas.
            for table in ("users", "class_types", "ai_configs",
                          "scheduling_configs", "owners"):
                cur.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (args.tenant,))
        conn.commit()

    print(f"Tenant '{args.tenant}' removido das cinco tabelas.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CLI manual de contas e provisionamento (ver ACCOUNTS_TESTING.md).")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("tenants", help="Lista os tenants e seus logins.").set_defaults(
        func=_cmd_tenants)

    slug = sub.add_parser("slug", help="Mostra o slug que um nome geraria. Não escreve nada.")
    slug.add_argument("--name", required=True)
    slug.set_defaults(func=_cmd_slug)

    prov = sub.add_parser("provision", help="Provisiona uma academia de teste.")
    prov.add_argument("--name", required=True)
    prov.add_argument("--email", required=True)
    prov.add_argument("--password", required=True)
    prov.add_argument("--tenant-id", dest="tenant_id", default=None)
    prov.add_argument("--owner-phone", dest="owner_phone", default=None)
    prov.add_argument("--whatsapp-number", dest="whatsapp_number", default=None)
    prov.set_defaults(func=_cmd_provision)

    show = sub.add_parser("show", help="Mostra as cinco tabelas de um tenant.")
    show.add_argument("--tenant", required=True)
    show.set_defaults(func=_cmd_show)

    login = sub.add_parser("login", help="Testa o login pela rota de verdade.")
    login.add_argument("--email", required=True)
    login.add_argument("--password", required=True)
    login.set_defaults(func=_cmd_login)

    resolve = sub.add_parser("resolve", help="Mostra como um 'To' resolve o tenant.")
    resolve.add_argument("--to", required=True, help='Ex.: "whatsapp:+14155238886"')
    resolve.set_defaults(func=_cmd_resolve)

    drop = sub.add_parser("drop-tenant", help="Apaga um tenant de teste inteiro.")
    drop.add_argument("--tenant", required=True)
    drop.set_defaults(func=_cmd_drop_tenant)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
