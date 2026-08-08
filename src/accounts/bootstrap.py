"""The first dashboard user, created once at app start (Module S3a).

THE PROBLEM THIS SOLVES: migration 009 cannot seed a user, because a password
hash must not be committed to a public repository. Without something at runtime,
a fresh deploy would come up with an empty `users` table and nobody able to log
in — including the founder, who would have to run a command against production
before the dashboard existed at all.

So: if `users` is empty and DASHBOARD_USER / DASHBOARD_PASSWORD are set, create
the pilot tenant's login from them, once. That also finally gives DASHBOARD_USER
a job — it was read by config.py and used by nothing — and takes
DASHBOARD_PASSWORD out of the authentication path entirely. After S3a the
password is a SEED, never a credential compared at login.

NO FLASK objects are touched here, only Config.
"""

import logging

from config import Config
from accounts import users
from integrations.store import DEFAULT_TENANT_ID

logger = logging.getLogger(__name__)


def bootstrap_first_user() -> bool:
    """Create the pilot tenant's login from the environment, if none exists.

    IDEMPOTENT IN TWO LAYERS, and it needs both. The `count_users()` check makes
    a restart a no-op — once ANY user exists, including after the founder
    changes this password, the bootstrap never fires again and cannot resurrect
    the old one. But gunicorn calls create_app() once PER WORKER, so several
    workers can pass that check in the same instant; what actually prevents a
    duplicate is the ON CONFLICT (email) DO NOTHING inside create_user().

    Deliberately calls create_user() and NOT provision_tenant(): tenant
    'default' already has its `owners`, `ai_configs`, `class_types` and
    `scheduling_configs` rows from migrations 003/005/008, and the pilot's rows
    are never to be touched.

    Every failure is swallowed by the caller, which is why this returns a bool
    rather than raising: a missing `users` table (init_db() failed and only
    printed), an unreachable database, or a missing owners('default') row must
    not stop the application from booting.

    Returns:
        bool: True only if a user was actually created by this call.
    """
    raw_email: str | None = Config.DASHBOARD_USER
    raw_password: str | None = Config.DASHBOARD_PASSWORD

    if not raw_email or not raw_password:
        logger.info(
            "DASHBOARD_USER/DASHBOARD_PASSWORD not set; skipping the first-user bootstrap."
        )
        return False

    email: str | None = users.normalize_email(raw_email)
    if email is None:
        # The single most likely lockout in this module, so it PRINTS as well as
        # logs: create_app() reports its other boot steps with print(), and a
        # warning in a log nobody tails would leave the founder staring at a
        # login screen no password opens.
        message: str = (
            "DASHBOARD_USER não é um e-mail. Desde o Módulo S3a o login é por e-mail: "
            "coloque um endereço em src/.env (ex.: voce@exemplo.com) e reinicie, "
            "ou crie o usuário com `python -m accounts.provision`."
        )
        logger.warning(message)
        print(f"AVISO: {message}")
        return False

    password_error: str | None = users.validate_password(raw_password)
    if password_error is not None:
        # The value itself is never echoed.
        logger.warning("DASHBOARD_PASSWORD is not acceptable: %s", password_error)
        print(f"AVISO: DASHBOARD_PASSWORD recusada — {password_error}")
        return False

    if users.count_users() != 0:
        return False

    user_id: int | None = users.create_user(email, raw_password, DEFAULT_TENANT_ID)

    if user_id is None:
        # Another gunicorn worker won the race. Correct, not an error.
        return False

    # The email is never logged; the tenant is.
    logger.warning(
        "Bootstrapped the first dashboard user for tenant '%s' from DASHBOARD_USER. "
        "Change this password with `python -m accounts.provision reset-password`.",
        DEFAULT_TENANT_ID,
    )
    print(
        f"Primeiro usuário do painel criado para o tenant '{DEFAULT_TENANT_ID}' "
        "a partir do .env. Troque a senha com "
        "`python -m accounts.provision reset-password`."
    )
    return True
