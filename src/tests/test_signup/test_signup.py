"""Manual CLI for the public signup screen (Module S3c).

Where the suite asserts, this one lets you SEE — and, unlike the suite, it lets
you flip the flag and poke the guards one at a time.

    0. Ver o estado da flag e do throttle
       python tests/test_signup/test_signup.py status

    1. Cadastrar uma academia como se fosse um visitante
       python tests/test_signup/test_signup.py signup \\
           --name "Suite Manual S3C" --email manual-s3c@suite.corujai.test --password senha-boa-123

    2. Ver a checklist de primeiros passos daquele tenant
       python tests/test_signup/test_signup.py onboarding --tenant suite-manual-s3c

    3. Provar o honeypot (nada deve ser criado)
       python tests/test_signup/test_signup.py honeypot --email bot@suite.corujai.test

    4. Provar o teto por IP
       python tests/test_signup/test_signup.py flood --ip 203.0.113.10

    5. Provar que o webhook do Twilio continua isento de CSRF
       python tests/test_signup/test_signup.py csrf

    6. Limpar
       python tests/test_signup/test_signup.py drop-tenant --tenant suite-manual-s3c
       python tests/test_signup/test_signup.py clear-attempts

⛔ Isto cria tenants de verdade, por um formulário público. Rode contra um banco
de DESENVOLVIMENTO: até o Módulo S3b as leituras não filtram por tenant.

Run from src/.
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Any

# Locate src/ by NAME, like app.py and the suites.
SRC_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "src")
sys.path.insert(0, str(SRC_DIR))

import accounts.onboarding as onboarding_steps  # noqa: E402
import accounts.signup as signup_guard  # noqa: E402
import accounts.users as accounts_users  # noqa: E402
from config import Config  # noqa: E402
from database.db import get_connection  # noqa: E402

RULE: str = "─" * 68


def _client(csrf: bool = False, signup_enabled: bool = True) -> tuple[Any, Any]:
    """Build an app and a test client, with the flag and CSRF as asked.

    Args:
        csrf (bool): Leave CSRF on (as in production) or disable it.
        signup_enabled (bool): Value forced onto Config.SIGNUP_ENABLED.

    Returns:
        tuple[Any, Any]: (app, client).
    """
    import app as flask_app

    Config.SIGNUP_ENABLED = signup_enabled
    app = flask_app.create_app()
    app.config["TESTING"] = True
    if not csrf:
        app.config["WTF_CSRF_ENABLED"] = False
    return app, app.test_client()


def _error_from(html: str) -> str | None:
    """Pull the error message out of a re-rendered signup page."""
    match = re.search(r'<div class="error">(.*?)</div>', html, re.S)
    return match.group(1).strip() if match else None


def _cmd_status(args: argparse.Namespace) -> None:
    """Show the flag, the throttle settings and what exists right now."""
    print(RULE)
    print(f" SIGNUP_ENABLED : {Config.SIGNUP_ENABLED}")
    if not Config.SIGNUP_ENABLED:
        print("   → /dashboard/signup responde 404 e o login não mostra o link.")
        print("   → Para ligar: SIGNUP_ENABLED=true no src/.env (NÃO faça isso em")
        print("     produção antes do Módulo S3b).")
    print(f" teto por IP    : {signup_guard.MAX_ATTEMPTS_PER_WINDOW} "
          f"tentativas / {signup_guard.ATTEMPT_WINDOW_MINUTES} min")
    print(f" campo honeypot : {signup_guard.HONEYPOT_FIELD}")
    print(RULE)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM signup_attempts")
            attempts = cur.fetchone()["n"]
            cur.execute("SELECT tenant_id FROM owners ORDER BY tenant_id")
            tenants = [row["tenant_id"] for row in cur.fetchall()]

    print(f"\n tentativas registradas : {attempts}")
    print(f" tenants                : {', '.join(tenants)}")
    if len(tenants) > 1:
        print("\n ⛔ Há mais de um tenant. Seguro em desenvolvimento; em produção,")
        print("    até o S3b, uma academia enxerga os dados da outra.")


def _cmd_signup(args: argparse.Namespace) -> None:
    """Sign up through the real route, as a visitor would."""
    app, client = _client(signup_enabled=True)
    response = client.post("/dashboard/signup", data={
        "academy_name": args.name,
        "email": args.email,
        "password": args.password,
        "password_confirm": args.confirm or args.password,
    }, headers={"X-Forwarded-For": args.ip})

    if response.status_code == 302:
        user = accounts_users.get_user_by_email(args.email)
        print("Cadastro aceito.")
        print(f"  redirecionou para : {response.headers.get('Location')}")
        print(f"  tenant gerado     : {user['tenant_id']}")
        print(f"  usuário           : #{user['id']}")
        print(f"\nConfira a checklist: python tests/test_signup/test_signup.py "
              f"onboarding --tenant {user['tenant_id']}")
        return

    if response.status_code == 404:
        print("404 — SIGNUP_ENABLED está desligada. É o comportamento correto por padrão.")
        return

    if response.status_code == 429:
        print("429 — o teto por IP foi atingido para esse endereço.")
        print("Limpe com: python tests/test_signup/test_signup.py clear-attempts")
        return

    print(f"Recusado (status {response.status_code}).")
    error = _error_from(response.get_data(as_text=True))
    if error:
        print(f"  mensagem na tela: {error}")


def _cmd_onboarding(args: argparse.Namespace) -> None:
    """Print the onboarding checklist exactly as the screen computes it."""
    steps = onboarding_steps.get_steps(args.tenant)

    print(RULE)
    print(f" PRIMEIROS PASSOS  {args.tenant}")
    print(RULE)
    for step in steps:
        mark = "✓" if step["done"] else "○"
        action = ""
        if not step["done"]:
            action = f"  [{step['action_label']}]" if step["action_endpoint"] else "  (aguardando)"
        print(f"\n  {mark} {step['title']}{action}")
        print(f"      {step['description']}")

    pending = sum(1 for s in steps if not s["done"])
    print(f"\n  {pending} pendência(s).")
    print("\n  Nada disto é gravado: cada passo é derivado das tabelas. Conecte o")
    print("  Calendar ou preencha a IA e rode de novo — a lista muda sozinha.")


def _cmd_honeypot(args: argparse.Namespace) -> None:
    """Fill the hidden field, like a bot would, and show that nothing is written."""
    app, client = _client(signup_enabled=True)

    before = accounts_users.count_users()
    response = client.post("/dashboard/signup", data={
        "academy_name": "Academia do Bot",
        "email": args.email,
        "password": "senha-boa-123",
        "password_confirm": "senha-boa-123",
        signup_guard.HONEYPOT_FIELD: "http://spam.example",
    }, headers={"X-Forwarded-For": args.ip})
    after = accounts_users.count_users()

    print(f"  status              : {response.status_code}")
    print(f"  tela de sucesso     : {'Conta criada' in response.get_data(as_text=True)}")
    print(f"  usuários antes/depois: {before} / {after}")
    print(f"  usuário criado      : {accounts_users.get_user_by_email(args.email) is not None}")
    print("\n  O bot recebe exatamente o que um humano receberia. Responder um erro")
    print("  ensinaria o próximo bot a pular o campo — e a armadilha deixaria de")
    print("  funcionar.")


def _cmd_flood(args: argparse.Namespace) -> None:
    """Burn the per-IP ceiling and show the refusal."""
    app, client = _client(signup_enabled=True)
    headers = {"X-Forwarded-For": args.ip}

    print(f"  disparando {signup_guard.MAX_ATTEMPTS_PER_WINDOW + 1} tentativas de {args.ip}:\n")
    for i in range(signup_guard.MAX_ATTEMPTS_PER_WINDOW + 1):
        response = client.post("/dashboard/signup", data={
            "academy_name": "Flood", "email": f"flood{i}@suite.corujai.test",
            "password": "x", "password_confirm": "x",
        }, headers=headers)
        note = " ← bloqueado pelo teto" if response.status_code == 429 else ""
        print(f"    tentativa {i + 1}: {response.status_code}{note}")

    print(f"\n  O contador vive no Postgres, não em memória: o gunicorn roda vários")
    print(f"  workers e um contador em processo veria só uma fração das tentativas.")
    print(f"  Limpe com: python tests/test_signup/test_signup.py clear-attempts")


def _cmd_csrf(args: argparse.Namespace) -> None:
    """Show the CSRF split: the dashboard is protected, the Twilio webhook is not."""
    import webhook.routes as routes
    import contextlib

    app, client = _client(csrf=True, signup_enabled=True)

    print("  Com o CSRF LIGADO, como em produção:\n")

    r = client.post("/dashboard/signup", data={"academy_name": "X", "email": "a@b.com",
                                               "password": "senha-boa-123",
                                               "password_confirm": "senha-boa-123"})
    print(f"    POST /dashboard/signup sem token : {r.status_code}  (tem que ser 400)")

    r = client.post("/dashboard/login", data={"email": "a@b.com", "password": "x"})
    print(f"    POST /dashboard/login  sem token : {r.status_code}  (tem que ser 400)")

    calls: list = []

    @contextlib.contextmanager
    def patched(obj, attr, value):
        o = getattr(obj, attr); setattr(obj, attr, value)
        try: yield
        finally: setattr(obj, attr, o)

    def fake_lead(sender: str, body: str, tenant_id: str = "default") -> None:
        calls.append(sender)

    with patched(routes, "handle_text_message", fake_lead):
        r = client.post("/webhook", data={"From": "whatsapp:+5529000000001",
                                          "Body": "oi", "To": "whatsapp:+14155238886"})
    print(f"    POST /webhook          sem token : {r.status_code}  (TEM que ser 200)")
    print(f"    a mensagem chegou ao handler     : {len(calls) == 1}")

    print("\n  Se o webhook der 400, csrf.exempt(webhook_bp) sumiu do app.py — e o")
    print("  bot pararia de responder qualquer lead, sem nada parecer um erro.")


def _cmd_drop_tenant(args: argparse.Namespace) -> None:
    """Delete a tenant created by this CLI."""
    if args.tenant == "default":
        print("Recusado: 'default' é o tenant do piloto.")
        return

    with get_connection() as conn:
        with conn.cursor() as cur:
            for table in ("users", "class_types", "ai_configs",
                          "scheduling_configs", "owners"):
                cur.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (args.tenant,))
        conn.commit()
    print(f"Tenant '{args.tenant}' removido das cinco tabelas.")


def _cmd_clear_attempts(args: argparse.Namespace) -> None:
    """Wipe the throttle's memory."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM signup_attempts")
            removed = cur.rowcount
        conn.commit()
    print(f"{removed} tentativa(s) removida(s). Todo IP volta a poder se cadastrar.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CLI manual do cadastro público (ver SIGNUP_TESTING.md).")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Mostra a flag, o throttle e o que existe.").set_defaults(
        func=_cmd_status)

    signup = sub.add_parser("signup", help="Cadastra pela rota, como um visitante.")
    signup.add_argument("--name", required=True)
    signup.add_argument("--email", required=True)
    signup.add_argument("--password", required=True)
    signup.add_argument("--confirm", default=None, help="Para testar senhas divergentes.")
    signup.add_argument("--ip", default="203.0.113.1")
    signup.set_defaults(func=_cmd_signup)

    onboarding = sub.add_parser("onboarding", help="Mostra a checklist de um tenant.")
    onboarding.add_argument("--tenant", required=True)
    onboarding.set_defaults(func=_cmd_onboarding)

    honeypot = sub.add_parser("honeypot", help="Preenche o campo escondido, como um bot.")
    honeypot.add_argument("--email", default="bot@suite.corujai.test")
    honeypot.add_argument("--ip", default="203.0.113.2")
    honeypot.set_defaults(func=_cmd_honeypot)

    flood = sub.add_parser("flood", help="Estoura o teto por IP.")
    flood.add_argument("--ip", default="203.0.113.10")
    flood.set_defaults(func=_cmd_flood)

    sub.add_parser("csrf", help="Painel protegido, webhook isento.").set_defaults(func=_cmd_csrf)

    drop = sub.add_parser("drop-tenant", help="Apaga um tenant de teste inteiro.")
    drop.add_argument("--tenant", required=True)
    drop.set_defaults(func=_cmd_drop_tenant)

    sub.add_parser("clear-attempts", help="Zera o throttle.").set_defaults(
        func=_cmd_clear_attempts)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
