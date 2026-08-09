"""Provision a whole tenant: the gym, its configuration and its login (Module S3a).

This is the module that makes a second gym exist. It is also both a library and
a command-line entry point, exactly like ``jobs/drain_notifications.py``:

    python -m accounts.provision create --name "…" --email … --password …

THERE IS NO WEB ROUTE FOR THIS, and that is a decision, not an omission. Every
new client depends on a WhatsApp Sender approved on Twilio, which is a manual
step the founder performs — an open signup would create orphan accounts with no
number, and a founder-only screen would need a role column that nothing else in
the project needs yet. A command has no attack surface at all.

A SECOND TENANT IN PRODUCTION IS SAFE SINCE MODULE S3b. While S3a shipped, it
was not: the tenant was resolved but no read filtered by it, so two gyms in one
database saw each other's conversations and bookings. S3b made `sessions` keyed
by (tenant_id, sender) and threaded the tenant through every read, which is what
lifted that restriction.

NO FLASK: the CLI runs with no application context.
"""

import argparse
import getpass
import logging
import sys
from typing import Any

import psycopg2

import bot.class_types as class_types
import integrations.store as store
from accounts import tenants, users
from database.db import get_connection

logger = logging.getLogger(__name__)

# The four guide texts a new tenant's ai_configs row starts with. COPIED
# VERBATIM FROM MIGRATION 005, which only ever seeds 'default' — a migration
# cannot seed a tenant that does not exist yet, so the strings necessarily live
# in two places. tests/test_accounts asserts they still match the pilot's row,
# so the duplication fails loudly instead of drifting.
_AI_CONFIG_SEED: dict[str, str] = {
    "assistant_name": "[NOME DA ATENDENTE]",
    "tone": "[TOM/PERSONALIDADE — ex.: simpática, direta, acolhedora, trata o lead pelo nome]",
    "business_info": (
        "[INFORMAÇÕES DO NEGÓCIO — ex.: modalidades oferecidas (Jiu-Jitsu, CrossFit, "
        "musculação), endereço, horários de funcionamento, valores da mensalidade, "
        "política da aula experimental gratuita]"
    ),
    "flow_emphasis": (
        "[ÊNFASE DO FLUXO — ex.: priorizar agendar a aula experimental o quanto antes; "
        "reforçar que a primeira aula é gratuita e sem compromisso]"
    ),
}


def provision_tenant(
    academy_name: str,
    email: str,
    password: str,
    owner_phone: str | None = None,
    whatsapp_number: str | None = None,
    tenant_id: str | None = None,
    days_ahead: int = class_types.DEFAULT_DAYS_AHEAD,
    fallback_marker: str = class_types._SYNTHETIC_FALLBACK_MARKER,
    fallback_label: str = class_types._SYNTHETIC_FALLBACK_LABEL,
) -> dict[str, Any]:
    """Create a gym, everything it needs to work, and the login that owns it.

    ONE CONNECTION, ONE TRANSACTION, ONE COMMIT — deliberately breaking the
    house pattern where each writer opens and commits its own. **A half
    provisioned tenant is worse than no tenant**, because it works QUIETLY
    WRONG:

    - ``ai_configs.update_ai_config()`` is an UPDATE, not an upsert. A tenant
      with an `owners` row but no `ai_configs` row lets its owner save the AI
      section forever while the function returns False, with nothing anywhere
      explaining why.
    - A tenant with no `class_types` rows runs on the unlimited fallback
      ``load_class_types()`` synthesizes in memory. Bookings work, capacity is
      ignored, and the only symptom is a WARNING in a log nobody reads.

    So the five inserts either all land or none do. This is also why the
    existing per-table writers are not reused here: five commits cannot be
    rolled back as one.

    IDEMPOTENT BY EMAIL. The email is the identity of the request: called twice
    with the same one, the second call writes nothing and reports the tenant the
    first call created.

    Args:
        academy_name (str): The gym's name, used for the slug and as the AI's
            `academy_name`.
        email (str): The owner's login. Normalized here.
        password (str): Plain text, hashed by users.create_user().
        owner_phone (str | None): The owner's personal WhatsApp number — the
            "From" that identifies them as the owner rather than a lead.
        whatsapp_number (str | None): The gym's OWN Twilio number — the "To"
            that identifies the tenant. Left None on the Twilio Sandbox, where
            every gym shares one number.
        tenant_id (str | None): An explicit slug, or None to generate one.
        days_ahead (int): The Calendar search horizon for this tenant.
        fallback_marker (str): The class type that catches events whose title
            has no recognized marker.
        fallback_label (str): Its human-readable name.

    Returns:
        dict[str, Any]: {"tenant_id": str, "user_id": int, "created": bool}.
        `created` is False when the email already existed.

    Raises:
        ValueError: Any input is unusable. Raised BEFORE anything is written.
    """
    # --- Validate everything before touching the database -------------------
    # Nothing partial is possible if nothing has been written yet.
    normalized_email: str | None = users.normalize_email(email)
    if normalized_email is None:
        raise ValueError("E-mail inválido. Informe um endereço no formato nome@dominio.")

    password_error: str | None = users.validate_password(password)
    if password_error is not None:
        raise ValueError(password_error)

    if not academy_name or not academy_name.strip():
        raise ValueError("O nome da academia não pode ficar em branco.")

    marker: str | None = class_types.normalize_marker(fallback_marker)
    if marker is None:
        raise ValueError(
            f"'{fallback_marker}' não serve como marcador de turma. "
            "Use apenas letras, sem acento e sem espaço."
        )

    if not fallback_label or not fallback_label.strip():
        raise ValueError("A turma padrão precisa de um nome visível.")

    if not class_types.MIN_DAYS_AHEAD <= days_ahead <= class_types.MAX_DAYS_AHEAD:
        raise ValueError(
            f"A janela de busca precisa ficar entre {class_types.MIN_DAYS_AHEAD} "
            f"e {class_types.MAX_DAYS_AHEAD} dias."
        )

    clean_owner_phone: str | None = None
    if owner_phone:
        clean_owner_phone = store.normalize_owner_phone(owner_phone)
        if clean_owner_phone is None:
            raise ValueError(
                "O telefone do dono precisa ter entre 10 e 15 dígitos "
                "(DDI + DDD + número)."
            )

    clean_whatsapp_number: str | None = None
    if whatsapp_number:
        clean_whatsapp_number = store.normalize_owner_phone(whatsapp_number)
        if clean_whatsapp_number is None:
            raise ValueError(
                "O número de WhatsApp da academia precisa ter entre 10 e 15 dígitos."
            )

    # --- Idempotency: the email is the identity of the request --------------
    existing: dict | None = users.get_user_by_email(normalized_email)
    if existing is not None:
        logger.warning(
            "A user with that email already exists (tenant %s); nothing provisioned.",
            existing["tenant_id"],
        )
        return {
            "tenant_id": existing["tenant_id"],
            "user_id": existing["id"],
            "created": False,
        }

    # --- Resolve the slug ----------------------------------------------------
    if tenant_id is not None:
        resolved_tenant: str = tenants.validate_explicit_tenant_id(tenant_id)
        may_regenerate: bool = False
    else:
        resolved_tenant = tenants.generate_tenant_id(academy_name)
        may_regenerate = True

    # --- Write, once ---------------------------------------------------------
    try:
        user_id: int | None = _write_tenant(
            tenant_id=resolved_tenant,
            academy_name=academy_name.strip(),
            email=normalized_email,
            password=password,
            owner_phone=clean_owner_phone,
            whatsapp_number=clean_whatsapp_number,
            days_ahead=days_ahead,
            fallback_marker=marker,
            fallback_label=fallback_label.strip(),
        )
    except psycopg2.errors.UniqueViolation:
        # Someone took the slug between generate_tenant_id() and the INSERT.
        # owners.tenant_id UNIQUE is what actually guarantees uniqueness; the
        # generator only makes a collision unlikely. Regenerate once.
        if not may_regenerate:
            raise
        resolved_tenant = tenants.generate_tenant_id(academy_name)
        logger.warning("Tenant id collided on insert; retrying as '%s'.", resolved_tenant)
        user_id = _write_tenant(
            tenant_id=resolved_tenant,
            academy_name=academy_name.strip(),
            email=normalized_email,
            password=password,
            owner_phone=clean_owner_phone,
            whatsapp_number=clean_whatsapp_number,
            days_ahead=days_ahead,
            fallback_marker=marker,
            fallback_label=fallback_label.strip(),
        )

    # The academy name and the tenant are fine to log; the email, the password
    # and both phone numbers are not — the repository is public.
    logger.info("Tenant '%s' provisioned for academy '%s'.", resolved_tenant, academy_name)

    return {"tenant_id": resolved_tenant, "user_id": user_id, "created": True}


def _write_tenant(
    tenant_id: str,
    academy_name: str,
    email: str,
    password: str,
    owner_phone: str | None,
    whatsapp_number: str | None,
    days_ahead: int,
    fallback_marker: str,
    fallback_label: str,
) -> int | None:
    """Write the five rows a working tenant needs, in one transaction.

    Order is forced by the foreign key (`users.tenant_id` references
    `owners.tenant_id`) and otherwise reads top-down: the gym, how its attendant
    talks, what classes it has, how far ahead to look, and who can log in.

    Returns:
        int | None: The new user's id.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            # 1. The gym itself. `owners` is the tenant registry.
            cur.execute(
                """
                INSERT INTO owners (tenant_id, owner_phone, whatsapp_number, integration_status)
                VALUES (%s, %s, %s, 'disconnected')
                ON CONFLICT (tenant_id) DO NOTHING
                """,
                (tenant_id, owner_phone, whatsapp_number),
            )

            # 2. The customizable prompt layer. Without this row the settings
            #    screen's "IA" section silently refuses every save forever.
            cur.execute(
                """
                INSERT INTO ai_configs
                    (tenant_id, academy_name, assistant_name, tone, business_info, flow_emphasis)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id) DO NOTHING
                """,
                (
                    tenant_id,
                    academy_name,
                    _AI_CONFIG_SEED["assistant_name"],
                    _AI_CONFIG_SEED["tone"],
                    _AI_CONFIG_SEED["business_info"],
                    _AI_CONFIG_SEED["flow_emphasis"],
                ),
            )

            # 3. The fallback class type. THIS IS THE ONE THAT IS EASY TO SKIP
            #    AND EXPENSIVE TO MISS: scheduling._parse_class_type() sends any
            #    event whose title has no recognized [MARKER] to the tenant's
            #    fallback, and both get_available_slots() and book_slot() then
            #    look its capacity up directly. load_class_types() synthesizes
            #    one in memory when the tenant has none, so a tenant without
            #    this row does not crash — it works quietly wrong, ignoring
            #    capacity. Capacity NULL means unlimited, and only NULL.
            cur.execute(
                """
                INSERT INTO class_types
                    (tenant_id, marker, label, capacity, requires_child_name, is_fallback)
                VALUES (%s, %s, %s, NULL, FALSE, TRUE)
                ON CONFLICT (tenant_id, marker) DO NOTHING
                """,
                (tenant_id, fallback_marker, fallback_label),
            )

            # 4. The Calendar search horizon. Written even though
            #    get_scheduling_config() falls back to 14 anyway: without a real
            #    row the settings screen shows a default it cannot save over.
            cur.execute(
                """
                INSERT INTO scheduling_configs (tenant_id, days_ahead)
                VALUES (%s, %s)
                ON CONFLICT (tenant_id) DO NOTHING
                """,
                (tenant_id, days_ahead),
            )

        # 5. The login. Passed the open connection so it joins this transaction
        #    instead of committing on its own.
        user_id: int | None = users.create_user(email, password, tenant_id, conn=conn)

        conn.commit()

    return user_id


def try_set_whatsapp_number(tenant_id: str, raw_number: str | None) -> tuple[bool, str]:
    """Point a tenant's routing at its own Twilio number, saying whether it worked.

    Applies the same shape of guard Module S1 put on `owner_phone`, for the same
    reason: this is a ROUTING KEY, and a number that plays two roles makes
    routing ambiguous. The UNIQUE index added by migration 009 catches
    tenant-versus-tenant; these checks catch a number that is already an owner's
    personal line or already a lead in `sessions`. Since Module S3d the same
    column also decides the "From" of every OUTBOUND message, which raises the
    price of a bad write: a wrong number here no longer just misroutes replies,
    it stops the gym from sending at all.

    TWO CALLERS, ONE RULE. The founder's CLI wants a line to print;
    /dashboard/settings/whatsapp-number needs to know success from refusal to
    pick a notice colour. Hence the pair: this function decides, and
    set_whatsapp_number() below is the CLI's one-line view of it. Re-checking the
    guards in the route is the mistake bot/confirmations.py documents — written
    twice, a rule ends up right in only one of them.

    Args:
        tenant_id (str): The tenant to configure.
        raw_number (str | None): The number as typed, or None/empty to clear it.

    Returns:
        tuple[bool, str]: Whether the database changed, and a Portuguese
        description of what happened, safe to show to a gym owner.
    """
    # As mensagens são lidas por duas plateias — o fundador no terminal e o dono
    # da academia na tela — então nenhuma delas cita o slug do tenant. Quem roda
    # a CLI acabou de digitar --tenant-id, e para o dono o slug é jargão interno.
    if not raw_number:
        store.update_whatsapp_number(None, tenant_id=tenant_id)
        return True, (
            "Número de WhatsApp removido. As mensagens voltam a sair pelo número "
            "de sandbox até você cadastrar outro."
        )

    clean: str | None = store.normalize_owner_phone(raw_number)
    if clean is None:
        return False, "Número inválido: use de 10 a 15 dígitos (DDI + DDD + número)."

    owner: dict | None = store.get_owner_by_phone(clean)
    if owner is not None:
        return False, (
            f"Esse número já é o telefone pessoal do dono do tenant "
            f"'{owner['tenant_id']}'. Um número não pode ser os dois papéis."
        )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM sessions WHERE sender = %s", (clean,))
            is_lead: bool = cur.fetchone() is not None

    if is_lead:
        return False, (
            "Esse número já pertence a uma conversa de lead. "
            "Usá-lo como número da academia sequestraria essa conversa."
        )

    if not store.update_whatsapp_number(clean, tenant_id=tenant_id):
        return False, f"Nenhum tenant '{tenant_id}' encontrado."

    return True, (
        "Número de WhatsApp salvo. É por ele que a IA passa a atender e a "
        "responder os leads."
    )


def set_whatsapp_number(tenant_id: str, raw_number: str | None) -> str:
    """The CLI's view of try_set_whatsapp_number(): just the message.

    Args:
        tenant_id (str): The tenant to configure.
        raw_number (str | None): The number as typed, or None/empty to clear it.

    Returns:
        str: A Portuguese description of what happened.
    """
    return try_set_whatsapp_number(tenant_id, raw_number)[1]


# ============================================================
# CLI
# ============================================================

_WHATSAPP_PENDING_BANNER: str = """
╔══════════════════════════════════════════════════════════════════════════╗
║  📱  FALTA O NÚMERO DO WHATSAPP                                          ║
║                                                                          ║
║  A academia está criada e o dono já consegue logar, mas ela ainda não     ║
║  recebe mensagem nenhuma: sem `whatsapp_number`, todo webhook que chega   ║
║  cai no tenant piloto. É a única etapa que depende de você — aprovar um   ║
║  WhatsApp Sender no Twilio — e é a última linha do checklist que o dono   ║
║  vê em /dashboard/onboarding.                                            ║
║                                                                          ║
║  Quando o número sair:                                                   ║
║    python -m accounts.provision set-whatsapp-number \\                    ║
║        --tenant-id <slug> --number 55XXXXXXXXXXX                          ║
╚══════════════════════════════════════════════════════════════════════════╝
"""


def _read_password(raw: str | None) -> str:
    """Return the password, reading it interactively when asked to.

    `--password -` keeps the real password out of the shell history, which is a
    file that survives the terminal and is rarely treated as a secret.

    Args:
        raw (str | None): The value of --password.

    Returns:
        str: The password to use.
    """
    if raw == "-":
        return getpass.getpass("Senha: ")
    return raw or ""


def _cmd_create(args: argparse.Namespace) -> None:
    """Provision a whole tenant."""
    try:
        result: dict[str, Any] = provision_tenant(
            academy_name=args.name,
            email=args.email,
            password=_read_password(args.password),
            owner_phone=args.owner_phone,
            whatsapp_number=args.whatsapp_number,
            tenant_id=args.tenant_id,
            days_ahead=args.days_ahead,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"Não deu para criar a conta: {exc}")
        return

    if not result["created"]:
        print(
            f"Esse e-mail já tem conta, no tenant '{result['tenant_id']}'. "
            "Nada foi criado."
        )
        return

    print(f"Academia '{args.name}' criada.")
    print(f"  tenant_id : {result['tenant_id']}")
    print(f"  usuário   : #{result['user_id']}")
    print("\nO que foi criado junto:")
    print("  · owners             — a academia, com Google Calendar desconectado")
    print("  · ai_configs         — a camada de prompt, com os textos-guia entre colchetes")
    print("  · class_types        — a turma padrão (ilimitada), que segura eventos sem [MARCADOR]")
    print("  · scheduling_configs — a janela de busca no Calendar")
    print("  · users              — o login, com a senha em hash")
    print("\nPróximos passos: conectar o Google Calendar e preencher a seção IA")
    print("em /dashboard/settings, logando com esse e-mail.")
    print(_WHATSAPP_PENDING_BANNER)


def _cmd_list(args: argparse.Namespace) -> None:
    """List every tenant and the users attached to them."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT o.tenant_id, o.owner_phone, o.whatsapp_number, o.integration_status,
                       c.academy_name
                FROM owners o
                LEFT JOIN ai_configs c ON c.tenant_id = o.tenant_id
                ORDER BY o.tenant_id
                """
            )
            rows: list[dict] = [dict(row) for row in cur.fetchall()]

    if not rows:
        print("Nenhuma academia cadastrada.")
        return

    print(f"{len(rows)} academia(s):\n")
    for row in rows:
        print(f"  {row['tenant_id']}")
        print(f"    nome no prompt  : {row['academy_name'] or '(sem linha em ai_configs)'}")
        print(f"    telefone do dono: {row['owner_phone'] or '—'}")
        print(f"    número da conta : {row['whatsapp_number'] or '— (cai no tenant default)'}")
        print(f"    Google Calendar : {row['integration_status']}")
        for user in users.list_users(row["tenant_id"]):
            print(f"    login           : {user['email']}  (#{user['id']})")
        print()


def _cmd_reset_password(args: argparse.Namespace) -> None:
    """Replace one user's password."""
    password: str = _read_password(args.password)
    error: str | None = users.validate_password(password)
    if error is not None:
        print(error)
        return

    if users.set_password(args.email, password):
        print("Senha trocada. O login antigo deixou de valer imediatamente.")
    else:
        print("Nenhum usuário com esse e-mail.")


def _cmd_set_whatsapp_number(args: argparse.Namespace) -> None:
    """Point a tenant's inbound routing at its own number."""
    print(set_whatsapp_number(args.tenant_id, args.number))


def _cmd_slug(args: argparse.Namespace) -> None:
    """Show the slug a name would generate, without writing anything."""
    candidate: str | None = tenants.slugify_tenant_id(args.name)

    if candidate is None:
        print("Esse nome não gera identificador. Use ao menos uma letra ou número.")
        return

    taken: bool = tenants.is_tenant_id_taken(candidate)
    print(f"  nome  : {args.name}")
    print(f"  slug  : {candidate}")
    print(f"  livre : {'não — receberia sufixo' if taken else 'sim'}")


def main() -> int:
    """Entry point for `python -m accounts.provision`, run from src/."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")

    parser = argparse.ArgumentParser(
        description="Cria e administra contas de academia (Módulo S3a). "
                    "Cadastro fechado: só o fundador roda isto."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="Provisiona uma academia inteira.")
    create.add_argument("--name", required=True, help='Ex.: "Academia Delariva Itaipuaçu"')
    create.add_argument("--email", required=True, help="O login do dono.")
    create.add_argument("--password", required=True,
                        help="A senha. Use '-' para digitar sem deixar no histórico do shell.")
    create.add_argument("--tenant-id", dest="tenant_id", default=None,
                        help="Identificador explícito, se não quiser o gerado a partir do nome.")
    create.add_argument("--owner-phone", dest="owner_phone", default=None,
                        help="WhatsApp pessoal do dono (o 'From' que o identifica).")
    create.add_argument("--whatsapp-number", dest="whatsapp_number", default=None,
                        help="Número da academia no Twilio (o 'To' que identifica o tenant).")
    create.add_argument("--days-ahead", dest="days_ahead", type=int,
                        default=class_types.DEFAULT_DAYS_AHEAD,
                        help="Janela de busca no Calendar, em dias.")
    create.set_defaults(func=_cmd_create)

    sub.add_parser("list", help="Lista as academias e seus logins.").set_defaults(func=_cmd_list)

    reset = sub.add_parser("reset-password", help="Troca a senha de um usuário.")
    reset.add_argument("--email", required=True)
    reset.add_argument("--password", required=True, help="Use '-' para digitar.")
    reset.set_defaults(func=_cmd_reset_password)

    set_number = sub.add_parser("set-whatsapp-number",
                                help="Define o número da academia que roteia o tenant.")
    set_number.add_argument("--tenant-id", dest="tenant_id", required=True)
    set_number.add_argument("--number", default=None,
                            help="Vazio para remover o número.")
    set_number.set_defaults(func=_cmd_set_whatsapp_number)

    slug = sub.add_parser("slug", help="Mostra o slug que um nome geraria. Não escreve nada.")
    slug.add_argument("--name", required=True)
    slug.set_defaults(func=_cmd_slug)

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
