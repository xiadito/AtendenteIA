"""Manual CLI for per-tenant class types (Module S2).

One operation per invocation, so each step of CLASS_TYPES_TESTING.md can be run
by hand and inspected in DBeaver between commands. Nothing here touches WhatsApp
or the LLM; only `slots` reaches Google Calendar, and it only reads.

The point of this CLI is to see the class types from the ENGINE's side. The
settings screen shows you the rows; `show` shows you the bundle the scheduling
code actually receives — including the fallback, which may be synthesized and
therefore appear in no row anywhere.

CAREFUL: `add`, `edit`, `delete`, `set-default` and `set-days` write to a real
tenant, defaulting to 'default' (the pilot). Pass --tenant to play in a
throwaway one. Run `backup` first if the pilot's current values matter.

Run from src/:
    # 0. Snapshot the pilot's rows before playing with them
    python tests/test_class_types/test_class_types.py backup

    # 1. See the rows AND the bundle the engine gets
    python tests/test_class_types/test_class_types.py show

    # 2. Check how a marker would be normalized, without writing anything
    python tests/test_class_types/test_class_types.py normalize --marker "Crianças"

    # 3. See which class type a Calendar title would resolve to
    python tests/test_class_types/test_class_types.py parse --title "[ CRIANÇAS ] Aula Experimental"
    python tests/test_class_types/test_class_types.py parse --title "Aula sem marcador"

    # 4. Build a throwaway tenant and read it back
    python tests/test_class_types/test_class_types.py add --tenant box --marker WOD --label WOD --capacity 12
    python tests/test_class_types/test_class_types.py set-default --tenant box --marker WOD
    python tests/test_class_types/test_class_types.py show --tenant box

    # 5. The Armadilha #1 case: a tenant with no fallback at all
    python tests/test_class_types/test_class_types.py add --tenant sem-fb --marker WOD --label WOD --capacity 12
    python tests/test_class_types/test_class_types.py show --tenant sem-fb
    python tests/test_class_types/test_class_types.py parse --tenant sem-fb --title "Aula sem marcador"

    # 6. The search horizon
    python tests/test_class_types/test_class_types.py set-days --days 21
    python tests/test_class_types/test_class_types.py slots

    # 7. Clean up
    python tests/test_class_types/test_class_types.py drop-tenant --tenant box
    python tests/test_class_types/test_class_types.py restore
"""
import argparse
import json
import sys
from pathlib import Path

# Locate src/ by NAME, like app.py and the suites.
SRC_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "src")
sys.path.insert(0, str(SRC_DIR))

import bot.class_types as class_types  # noqa: E402
import bot.scheduling as scheduling  # noqa: E402
import integrations.store as store  # noqa: E402
from database.db import get_connection  # noqa: E402

# Same file the suite uses, on purpose: whichever one you ran last, the other
# can put the pilot's rows back.
BACKUP_PATH = Path("/tmp/corujai_class_types_backup.json")


def _capacity_text(capacity: int | None) -> str:
    return "ilimitado" if capacity is None else str(capacity)


def cmd_show(args: argparse.Namespace) -> int:
    """Print the tenant's rows and the bundle the scheduling engine receives."""
    rows = class_types.list_class_types(args.tenant)
    bundle = class_types.load_class_types(args.tenant)
    config = class_types.get_scheduling_config(args.tenant)

    print(f"\nTenant: {args.tenant}")
    print(f"Janela de busca (days_ahead): {config['days_ahead']} dias\n")

    if not rows:
        print("  (nenhuma turma cadastrada)\n")
    else:
        print(f"  {'MARCADOR':<14}{'NOME':<18}{'VAGAS':<12}{'CRIANÇA':<10}PADRÃO")
        print("  " + "─" * 62)
        for row in rows:
            print(f"  {row['marker']:<14}{row['label']:<18}"
                  f"{_capacity_text(row['capacity']):<12}"
                  f"{'sim' if row['requires_child_name'] else 'não':<10}"
                  f"{'sim' if row['is_fallback'] else ''}")
        print()

    print("O que o motor de agendamento enxerga:")
    print(f"  capacities          : {bundle['capacities']}")
    print(f"  labels              : {bundle['labels']}")
    print(f"  child_name_required : {sorted(bundle['child_name_required'])}")
    print(f"  fallback            : {bundle['fallback']}")

    stored = {row["marker"] for row in rows}
    if bundle["fallback"] not in stored:
        print("\n  ⚠  A turma padrão acima é SINTÉTICA: não existe linha no banco para ela.")
        print("     É a rede de segurança — sem ela, um evento com o título sem marcador")
        print("     levantaria KeyError no meio de um agendamento. Cadastre uma turma e")
        print("     marque-a como padrão para sair desse estado.")

    print()
    return 0


def cmd_normalize(args: argparse.Namespace) -> int:
    """Show the canonical form of a marker, without writing anything."""
    normalized = class_types.normalize_marker(args.marker)

    if normalized is None:
        print(f"\n  {args.marker!r} → RECUSADO")
        print("  Só letras (sem números, espaços ou símbolos) e no máximo 32 caracteres.")
        print("  É o que o regex do título do evento consegue casar — um marcador fora")
        print("  disso viraria uma turma que nenhum evento pode receber.\n")
        return 1

    print(f"\n  {args.marker!r} → {normalized!r}\n")
    return 0


def cmd_parse(args: argparse.Namespace) -> int:
    """Resolve a Calendar event title to a class type, as the engine would."""
    bundle = class_types.load_class_types(args.tenant)
    resolved = scheduling._parse_class_type(args.title, bundle)

    capacity = bundle["capacities"][resolved]
    needs_child = resolved in bundle["child_name_required"]
    fell_back = resolved == bundle["fallback"]

    print(f"\n  Título : {args.title!r}")
    print(f"  Tenant : {args.tenant}")
    print(f"  Turma  : {resolved}  ({bundle['labels'].get(resolved, resolved)})")
    print(f"  Vagas  : {_capacity_text(capacity)}")
    print(f"  Criança: {'exige o nome' if needs_child else 'não exige'}")

    if fell_back:
        print("\n  ↳ caiu na TURMA PADRÃO (o título não trazia um marcador conhecido).")
    print()
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    """Register a class type."""
    marker = class_types.normalize_marker(args.marker)
    if marker is None:
        print(f"  marcador inválido: {args.marker!r} (só letras, até 32 caracteres)")
        return 1

    result = class_types.create_class_type(
        marker, args.label, args.capacity, args.requires_child_name, args.tenant)

    if result == "duplicate":
        print(f"  o tenant '{args.tenant}' já tem a turma [{marker}]")
        return 1

    print(f"  turma [{marker}] cadastrada em '{args.tenant}' "
          f"({_capacity_text(args.capacity)} vaga(s))")
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    """Overwrite a class type's label, capacity and child-name requirement."""
    marker = class_types.normalize_marker(args.marker)
    if marker is None:
        print(f"  marcador inválido: {args.marker!r}")
        return 1

    if not class_types.update_class_type(
            marker, args.label, args.capacity, args.requires_child_name, args.tenant):
        print(f"  o tenant '{args.tenant}' não tem a turma [{marker}]")
        return 1

    print(f"  turma [{marker}] atualizada em '{args.tenant}'")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    """Delete a class type (never the fallback)."""
    marker = class_types.normalize_marker(args.marker)
    if marker is None:
        print(f"  marcador inválido: {args.marker!r}")
        return 1

    result = class_types.delete_class_type(marker, args.tenant)

    if result == "not_found":
        print(f"  o tenant '{args.tenant}' não tem a turma [{marker}]")
        return 1

    if result == "is_fallback":
        print(f"  [{marker}] é a turma padrão de '{args.tenant}' e não pode ser excluída.")
        print("  Marque outra como padrão antes (set-default).")
        return 1

    print(f"  turma [{marker}] excluída de '{args.tenant}'")
    return 0


def cmd_set_default(args: argparse.Namespace) -> int:
    """Point the tenant's fallback at a class type."""
    marker = class_types.normalize_marker(args.marker)
    if marker is None:
        print(f"  marcador inválido: {args.marker!r}")
        return 1

    if not class_types.set_fallback_class_type(marker, args.tenant):
        print(f"  o tenant '{args.tenant}' não tem a turma [{marker}]")
        return 1

    print(f"  [{marker}] agora é a turma padrão de '{args.tenant}': eventos sem")
    print("  marcador reconhecido no título passam a ser tratados como dessa turma.")
    return 0


def cmd_set_days(args: argparse.Namespace) -> int:
    """Set how many days ahead the engine looks for slots."""
    if not class_types.MIN_DAYS_AHEAD <= args.days <= class_types.MAX_DAYS_AHEAD:
        print(f"  use um valor entre {class_types.MIN_DAYS_AHEAD} e {class_types.MAX_DAYS_AHEAD}")
        return 1

    if not class_types.update_days_ahead(args.days, args.tenant):
        # No row to update: create one, so a fixture tenant is usable.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO scheduling_configs (tenant_id, days_ahead) VALUES (%s, %s)",
                    (args.tenant, args.days),
                )
            conn.commit()
        print(f"  '{args.tenant}' não tinha configuração de agendamento; criada com {args.days} dias")
        return 0

    print(f"  janela de '{args.tenant}' ajustada para {args.days} dias")
    return 0


def cmd_slots(args: argparse.Namespace) -> int:
    """List the real available slots, sized by this tenant's class types.

    The only command here that reaches Google Calendar. Read-only.
    """
    try:
        slots = scheduling.get_available_slots(days_ahead=args.days, tenant_id=args.tenant)
    except scheduling.IntegrationNotConnectedError:
        print("  Google Calendar não está conectado para este tenant.")
        return 1
    except scheduling.IntegrationNeedsReconnectError:
        print("  Google Calendar precisa ser reconectado (refresh_token recusado).")
        return 1

    if not slots:
        print("\n  (nenhum horário livre na janela consultada)\n")
        return 0

    print()
    for slot in slots:
        remaining = "ilimitado" if slot["remaining_slots"] is None else slot["remaining_slots"]
        child = "  · exige nome da criança" if slot["requires_child_name"] else ""
        print(f"  [{slot['class_type']}] {slot['label']}")
        print(f"      vagas: {remaining}{child}")
        print(f"      event_id: {slot['event_id']}")
    print()
    return 0


def cmd_drop_tenant(args: argparse.Namespace) -> int:
    """Delete every class type and the scheduling config of a tenant."""
    if args.tenant == store.DEFAULT_TENANT_ID:
        print("  recusado: use `restore` para o tenant do piloto, não drop-tenant.")
        return 1

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM class_types WHERE tenant_id = %s", (args.tenant,))
            removed = cur.rowcount
            cur.execute("DELETE FROM scheduling_configs WHERE tenant_id = %s", (args.tenant,))
        conn.commit()

    print(f"  tenant '{args.tenant}': {removed} turma(s) e a configuração removidas")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """Snapshot the pilot's class types and horizon to BACKUP_PATH."""
    backup = {
        "class_types": class_types.list_class_types(store.DEFAULT_TENANT_ID),
        "days_ahead": class_types.get_scheduling_config()["days_ahead"],
    }
    # updated_at is a datetime; it is not restored, so drop it before writing.
    for row in backup["class_types"]:
        row.pop("updated_at", None)

    BACKUP_PATH.write_text(json.dumps(backup, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  {len(backup['class_types'])} turma(s) do piloto salvas em {BACKUP_PATH}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    """Put the pilot's class types and horizon back from BACKUP_PATH."""
    if not BACKUP_PATH.exists():
        print(f"  nenhum backup em {BACKUP_PATH}")
        return 1

    backup = json.loads(BACKUP_PATH.read_text(encoding="utf-8"))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM class_types WHERE tenant_id = %s", (store.DEFAULT_TENANT_ID,))
            for row in backup["class_types"]:
                cur.execute(
                    """
                    INSERT INTO class_types
                        (tenant_id, marker, label, capacity, requires_child_name, is_fallback)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (store.DEFAULT_TENANT_ID, row["marker"], row["label"], row["capacity"],
                     row["requires_child_name"], row["is_fallback"]),
                )
            cur.execute(
                "UPDATE scheduling_configs SET days_ahead = %s WHERE tenant_id = %s",
                (backup["days_ahead"], store.DEFAULT_TENANT_ID),
            )
        conn.commit()

    print(f"  {len(backup['class_types'])} turma(s) do piloto restauradas de {BACKUP_PATH}")
    return 0


def _add_tenant_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tenant", default=store.DEFAULT_TENANT_ID,
                        help="Tenant a usar (padrão: 'default', o piloto).")


def _add_class_type_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--marker", required=True, help="Marcador, ex.: CRIANCAS (acento e caixa não importam).")
    parser.add_argument("--label", required=True, help="Nome como o aluno lê, ex.: Crianças.")
    parser.add_argument("--capacity", type=int, default=None,
                        help="Vagas. Omita para ilimitado.")
    parser.add_argument("--requires-child-name", action="store_true",
                        help="A turma exige o nome da criança para agendar.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CLI manual dos tipos de aula por tenant (CLASS_TYPES_TESTING.md).")
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show", help="Mostra as turmas e o que o motor enxerga.")
    _add_tenant_argument(show)
    show.set_defaults(func=cmd_show)

    normalize = sub.add_parser("normalize", help="Mostra a forma canônica de um marcador.")
    normalize.add_argument("--marker", required=True)
    normalize.set_defaults(func=cmd_normalize)

    parse = sub.add_parser("parse", help="Resolve um título de evento a uma turma.")
    _add_tenant_argument(parse)
    parse.add_argument("--title", required=True, help='Ex.: "[CRIANCAS] Aula Experimental"')
    parse.set_defaults(func=cmd_parse)

    add = sub.add_parser("add", help="Cadastra uma turma.")
    _add_tenant_argument(add)
    _add_class_type_arguments(add)
    add.set_defaults(func=cmd_add)

    edit = sub.add_parser("edit", help="Edita nome, vagas e exigência de nome da criança.")
    _add_tenant_argument(edit)
    _add_class_type_arguments(edit)
    edit.set_defaults(func=cmd_edit)

    delete = sub.add_parser("delete", help="Exclui uma turma (nunca a padrão).")
    _add_tenant_argument(delete)
    delete.add_argument("--marker", required=True)
    delete.set_defaults(func=cmd_delete)

    set_default = sub.add_parser("set-default", help="Define a turma padrão do tenant.")
    _add_tenant_argument(set_default)
    set_default.add_argument("--marker", required=True)
    set_default.set_defaults(func=cmd_set_default)

    set_days = sub.add_parser("set-days", help="Ajusta a janela de busca (days_ahead).")
    _add_tenant_argument(set_days)
    set_days.add_argument("--days", type=int, required=True)
    set_days.set_defaults(func=cmd_set_days)

    slots = sub.add_parser("slots", help="Lista horários livres reais (lê o Google Calendar).")
    _add_tenant_argument(slots)
    slots.add_argument("--days", type=int, default=None,
                       help="Janela em dias. Omita para usar a configurada do tenant.")
    slots.set_defaults(func=cmd_slots)

    drop = sub.add_parser("drop-tenant", help="Apaga um tenant de teste inteiro.")
    _add_tenant_argument(drop)
    drop.set_defaults(func=cmd_drop_tenant)

    backup = sub.add_parser("backup", help="Salva as turmas do piloto.")
    backup.set_defaults(func=cmd_backup)

    restore = sub.add_parser("restore", help="Restaura as turmas do piloto.")
    restore.set_defaults(func=cmd_restore)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
