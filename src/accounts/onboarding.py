"""What a freshly signed-up gym still has to do before the bot can work (Module S3c).

NO STATE OF ITS OWN. Every step below is DERIVED from rows that already exist —
`owners.integration_status`, `ai_configs`' seed placeholders, how many
`class_types` the tenant has, `owners.whatsapp_number`. Nothing is written, no
"onboarding_completed" column exists, and none should: a checklist with its own
state is a second record of a fact the tables already hold, and the two drift.
Tick a step by doing the work; untick it by undoing the work.

The steps are ordered by what unblocks the most, and the last one is the only
one the gym owner cannot FINISH alone — which is exactly why it needs saying out
loud on the screen instead of leaving them wondering why the bot is silent. Until
Module S3d it had no button at all; now it has one, because the owner types the
number, but the Twilio Sender approval that has to come first is still ours.
"""

import logging
from typing import Any

import bot.ai_configs as ai_configs
import bot.class_types as class_types
import integrations.store as store

logger = logging.getLogger(__name__)


def _ai_config_is_filled(tenant_id: str) -> bool:
    """Say whether the owner has replaced the AI layer's placeholder text.

    A new tenant's `ai_configs` row carries the bracketed guide texts copied
    from migration 005 ("[TOM/PERSONALIDADE — ex.: ...]"). Any of the four text
    fields still starting with "[" means untouched.

    `academy_name` is excluded on purpose: signup fills it with the real name,
    so it is never a placeholder and would make this always look done.

    Args:
        tenant_id (str): The tenant to inspect.

    Returns:
        bool: True once every guide text has been replaced.
    """
    config: dict[str, str] = ai_configs.get_ai_config(tenant_id)
    guided: tuple[str, ...] = ("assistant_name", "tone", "business_info", "flow_emphasis")
    return all(not (config.get(field) or "").lstrip().startswith("[") for field in guided)


def get_steps(tenant_id: str) -> list[dict[str, Any]]:
    """Build the onboarding checklist for one tenant.

    Args:
        tenant_id (str): Normally current_user.tenant_id.

    Returns:
        list[dict[str, Any]]: One dict per step, each with:
            key (str), title (str), description (str), done (bool),
            action_endpoint (str | None) and action_label (str | None).
            A step with no endpoint is one the owner cannot act on.
    """
    owner: dict | None = store.get_owner_credentials(tenant_id)

    calendar_connected: bool = bool(
        owner and owner.get("integration_status") == "connected"
    )

    # More than one class type means the owner added something of their own:
    # provisioning seeds exactly one, the unlimited fallback.
    class_type_count: int = len(class_types.list_class_types(tenant_id))

    # The gym's own line. Since Module S3d this step HAS a button: the owner
    # types the number themselves on the settings screen. What they still cannot
    # do alone is the half that comes first — the Twilio Sender approval — so the
    # description says which part is whose, instead of pretending the button is
    # the whole step.
    has_whatsapp_number: bool = bool(store.get_whatsapp_number(tenant_id))

    return [
        {
            "key": "account",
            "title": "Conta criada",
            "description": "Sua academia já existe no Corujai.",
            "done": True,
            "action_endpoint": None,
            "action_label": None,
        },
        {
            "key": "calendar",
            "title": "Conectar o Google Calendar",
            "description": "É de onde a IA lê os horários livres para oferecer ao lead. "
                           "Sem isso ela não tem o que agendar.",
            "done": calendar_connected,
            "action_endpoint": "integrations.google_status",
            "action_label": "Conectar",
        },
        {
            "key": "ai",
            "title": "Descrever sua academia para a IA",
            "description": "Nome da atendente, tom da conversa, modalidades, endereço e "
                           "valores. Enquanto estiver com os textos de exemplo, a IA "
                           "responde de forma genérica.",
            "done": _ai_config_is_filled(tenant_id),
            "action_endpoint": "dashboard.settings",
            "action_label": "Preencher",
        },
        {
            "key": "classes",
            "title": "Cadastrar suas turmas",
            "description": "Cada turma tem um marcador, um nome e o número de vagas. "
                           "Sua conta começa só com a turma padrão.",
            "done": class_type_count > 1,
            "action_endpoint": "dashboard.settings",
            "action_label": "Cadastrar",
        },
        {
            "key": "whatsapp",
            "title": "Número de WhatsApp",
            "description": "Nossa equipe libera o número da sua academia no WhatsApp e "
                           "avisa você. Com ele em mãos, grave-o em Configurações → "
                           "Conta: é por esse número que a IA vai atender e responder "
                           "seus leads.",
            "done": has_whatsapp_number,
            "action_endpoint": "dashboard.settings",
            "action_label": "Configurar",
        },
    ]


def pending_count(tenant_id: str) -> int:
    """Count the steps still open, for the menu badge.

    Args:
        tenant_id (str): The tenant to inspect.

    Returns:
        int: How many steps are not done. Zero means the checklist can be hidden.
    """
    return sum(1 for step in get_steps(tenant_id) if not step["done"])
