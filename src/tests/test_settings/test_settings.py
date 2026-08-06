"""Manual CLI for the settings screen (Module S1).

One operation per invocation, so each step of SETTINGS_TESTING.md can be run by
hand and inspected in DBeaver between commands. Nothing here touches WhatsApp,
the LLM or Google Calendar — the screen only reads and writes two Postgres rows.

CAREFUL: `set-ai` and `set-phone` overwrite the pilot's REAL rows (one row per
tenant, and the pilot is the only tenant). Run `backup` first if the current
values matter, and `restore` to put them back.

Run from src/:
    # 0. Snapshot the real values before playing with them
    python tests/test_settings/test_settings.py backup

    # 1. See what the screen would show
    python tests/test_settings/test_settings.py show

    # 2. Edit the AI's customizable layer (only the flags you pass change)
    python tests/test_settings/test_settings.py set-ai --academy-name "Delariva Itaipuaçu" \
        --assistant-name "Corujinha"

    # 3. Edit the owner's number, with the same guards the screen applies
    python tests/test_settings/test_settings.py set-phone --phone "whatsapp:+55 21 99999-9999"

    # 4. Check how a raw input would be normalized, without writing anything
    python tests/test_settings/test_settings.py normalize --phone "(21) 99999-9999"

    # 5. Create a lead session, to see guard (b) refuse its number
    python tests/test_settings/test_settings.py seed-lead --sender 5526000000001
    python tests/test_settings/test_settings.py set-phone --phone 5526000000001

    # 6. Put the real values back
    python tests/test_settings/test_settings.py restore
    python tests/test_settings/test_settings.py reset --sender 5526000000001
"""
import argparse
import json
import sys
from pathlib import Path

# Locate src/ by NAME, like app.py and the suites.
SRC_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "src")
sys.path.insert(0, str(SRC_DIR))

import bot.ai_configs as ai_configs  # noqa: E402
import bot.session as session_store  # noqa: E402
import integrations.store as store  # noqa: E402
from database.db import get_connection  # noqa: E402

# Same file the suite uses, on purpose: whichever one you ran last, the other
# can put the real values back.
BACKUP_PATH = Path("/tmp/corujai_settings_backup.json")

AI_FIELDS: tuple[str, ...] = (
    "academy_name",
    "assistant_name",
    "tone",
    "business_info",
    "flow_emphasis",
)

_STATUS_LABELS = {
    "connected": "conectado",
    "disconnected": "desconectado",
    "needs_reconnect": "precisa reconectar",
}


def _read_ai_row() -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM ai_configs WHERE tenant_id = %s",
                (store.DEFAULT_TENANT_ID,),
            )
            row = cur.fetchone()
    return dict(row) if row else {}


def _cmd_show(args: argparse.Namespace) -> None:
    """Print both sections exactly as the screen would fill them."""
    config = _read_ai_row()
    if not config:
        print(f"Nenhuma linha em ai_configs para tenant_id='{store.DEFAULT_TENANT_ID}'.")
        return

    print("\n── IA " + "─" * 60)
    for field in AI_FIELDS:
        print(f"  {field:<15} {config[field]}")
    print(f"  {'updated_at':<15} {config['updated_at']}")

    owner = store.get_owner_for_notification()
    credentials = store.get_owner_credentials()
    print("\n── Conta " + "─" * 57)
    print(f"  {'owner_phone':<15} {(owner or {}).get('owner_phone') or '(vazio)'}")
    status = (credentials or {}).get("integration_status", "disconnected")
    print(f"  {'Google Calendar':<15} {_STATUS_LABELS.get(status, status)}")
    if (credentials or {}).get("google_email"):
        print(f"  {'conta Google':<15} {credentials['google_email']}")
    print()


def _cmd_set_ai(args: argparse.Namespace) -> None:
    """Update the AI layer, keeping whatever flags were not passed.

    update_ai_config writes all five columns at once (the form submits all
    five), so anything omitted here is read back from the row first.
    """
    current = _read_ai_row()
    if not current:
        print(f"Nenhuma linha em ai_configs para tenant_id='{store.DEFAULT_TENANT_ID}'.")
        return

    values: dict[str, str] = {
        field: getattr(args, field) or current[field] for field in AI_FIELDS
    }

    if ai_configs.update_ai_config(**values):
        print("Configuração da IA salva:")
        for field in AI_FIELDS:
            marker = "*" if getattr(args, field) else " "
            print(f" {marker} {field:<15} {values[field]}")
        print("\n(* = alterado nesta chamada. Vale já na próxima mensagem: não há cache.)")
    else:
        print("Nada foi atualizado.")


def _cmd_set_phone(args: argparse.Namespace) -> None:
    """Apply the same two guards the route applies, then write.

    Deliberately duplicates the route's flow instead of calling it: the point of
    the CLI is to exercise the guards one at a time and see which one refused.
    """
    normalized: str | None = store.normalize_owner_phone(args.phone)

    if normalized is None:
        print(f"Recusado (guarda a): {args.phone!r} não vira um telefone válido.")
        return
    print(f"Normalizado: {args.phone!r} → {normalized}")

    if session_store.session_exists(normalized):
        print(f"Recusado (guarda b): {normalized} já é o número de um lead com conversa.")
        print("Salvá-lo faria o webhook tratar as mensagens dessa pessoa como respostas do dono.")
        return

    if store.update_owner_phone(normalized):
        print(f"owner_phone do tenant '{store.DEFAULT_TENANT_ID}' definido para {normalized}.")
        found = store.get_owner_by_phone(normalized)
        print(f"get_owner_by_phone reconhece o número: {'sim' if found else 'NÃO — roteamento quebrado'}")
    else:
        print(f"Nenhuma linha em owners para tenant_id='{store.DEFAULT_TENANT_ID}'.")


def _cmd_normalize(args: argparse.Namespace) -> None:
    """Dry run of guard (a): show the stored form without writing."""
    normalized: str | None = store.normalize_owner_phone(args.phone)
    if normalized is None:
        print(f"{args.phone!r} → recusado (nada entre 10 e 15 dígitos).")
    else:
        print(f"{args.phone!r} → {normalized}")


def _cmd_seed_lead(args: argparse.Namespace) -> None:
    """Create a lead session, so guard (b) has something to collide with."""
    session_store.get_session(args.sender)
    print(f"Sessão criada para {args.sender}.")
    print(f"Agora `set-phone --phone {args.sender}` deve ser recusado pela guarda (b).")


def _cmd_backup(args: argparse.Namespace) -> None:
    config = _read_ai_row()
    owner = store.get_owner_for_notification()
    if not config or owner is None:
        print("Faltam as linhas do tenant em ai_configs/owners — nada a salvar.")
        return

    BACKUP_PATH.write_text(json.dumps({
        "ai_config": {field: config[field] for field in AI_FIELDS},
        "ai_updated_at": config["updated_at"].isoformat(),
        "owner_phone": owner["owner_phone"],
    }, ensure_ascii=False), encoding="utf-8")
    print(f"Valores reais salvos em {BACKUP_PATH}.")


def _cmd_restore(args: argparse.Namespace) -> None:
    if not BACKUP_PATH.exists():
        print(f"Nenhum backup em {BACKUP_PATH}.")
        return

    backup = json.loads(BACKUP_PATH.read_text(encoding="utf-8"))
    config: dict[str, str] = backup["ai_config"]

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ai_configs
                SET academy_name = %s, assistant_name = %s, tone = %s,
                    business_info = %s, flow_emphasis = %s, updated_at = %s
                WHERE tenant_id = %s
                """,
                (*(config[field] for field in AI_FIELDS),
                 backup["ai_updated_at"], store.DEFAULT_TENANT_ID),
            )
            cur.execute(
                "UPDATE owners SET owner_phone = %s, updated_at = NOW() WHERE tenant_id = %s",
                (backup["owner_phone"], store.DEFAULT_TENANT_ID),
            )
        conn.commit()

    BACKUP_PATH.unlink(missing_ok=True)
    print("Config da IA e owner_phone reais restaurados.")


def _cmd_reset(args: argparse.Namespace) -> None:
    """Delete one test lead (its messages cascade off the session)."""
    session_store.clear_session(args.sender)
    print(f"Sessão e mensagens de {args.sender} removidas.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CLI manual da tela de configurações (ver SETTINGS_TESTING.md).")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("show", help="Mostra as duas seções como a tela as exibiria.").set_defaults(
        func=_cmd_show)

    set_ai = sub.add_parser("set-ai", help="Edita a camada customizável do prompt.")
    set_ai.add_argument("--academy-name", dest="academy_name")
    set_ai.add_argument("--assistant-name", dest="assistant_name")
    set_ai.add_argument("--tone", dest="tone")
    set_ai.add_argument("--business-info", dest="business_info")
    set_ai.add_argument("--flow-emphasis", dest="flow_emphasis")
    set_ai.set_defaults(func=_cmd_set_ai)

    set_phone = sub.add_parser("set-phone", help="Edita o telefone do dono, com as duas guardas.")
    set_phone.add_argument("--phone", required=True, help="Aceita formato sujo: 'whatsapp:+55 21 9...'")
    set_phone.set_defaults(func=_cmd_set_phone)

    normalize = sub.add_parser("normalize", help="Só mostra como um número seria gravado.")
    normalize.add_argument("--phone", required=True)
    normalize.set_defaults(func=_cmd_normalize)

    seed_lead = sub.add_parser("seed-lead", help="Cria uma sessão de lead para testar a guarda (b).")
    seed_lead.add_argument("--sender", required=True, help="Ex.: 5526000000001")
    seed_lead.set_defaults(func=_cmd_seed_lead)

    sub.add_parser("backup", help="Salva os valores reais antes de mexer.").set_defaults(
        func=_cmd_backup)
    sub.add_parser("restore", help="Devolve os valores reais salvos pelo backup.").set_defaults(
        func=_cmd_restore)

    reset = sub.add_parser("reset", help="Apaga a sessão (e as mensagens) de um lead de teste.")
    reset.add_argument("--sender", required=True)
    reset.set_defaults(func=_cmd_reset)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
