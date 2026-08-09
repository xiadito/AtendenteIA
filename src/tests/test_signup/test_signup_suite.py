"""Automated end-to-end suite for the public signup screen (Module S3c).

Runs the scenarios documented in SIGNUP_TESTING.md and prints a PASS/FAIL report
boiled down to an exit code, in the same style as
tests/test_accounts/test_accounts_suite.py.

Fully deterministic: no LLM, no WhatsApp, no Google Calendar. Postgres plus the
Flask test client.

TWO THINGS THIS SUITE DOES THAT NO OTHER ONE DOES:

1. It flips Config.SIGNUP_ENABLED at runtime, because the flag being OFF is
   half of what the module promises. `patched()` puts it back.
2. It runs some tests with CSRF *ON*. Every other suite disables it, which is
   right for them — they are testing other things — but it leaves the CSRF
   wiring itself untested, including the exemption that keeps Twilio working.
   Scenarios 12 and 13 are the only coverage that exists for that.

NO /tmp BACKUP FILE, same reason as tests/test_accounts: nothing here writes to
the pilot's rows. Every scenario runs on fixture tenants created through the
signup form under the prefix below, and _drop_orphan_fixtures() at the start of
main() plays the crash-repair role the backup file plays elsewhere.

⛔ THE SUITE CREATES TENANTS THROUGH A PUBLIC SIGNUP FORM. Safe here, not safe
in production: until Module S3b merges the reads do not filter by tenant. Run
this against a development database.

Run from src/:
    python tests/test_signup/test_signup_suite.py
    python tests/test_signup/test_signup_suite.py --keep
    python tests/test_signup/test_signup_suite.py --json

Exit code is 0 only when every test passed.
"""

import argparse
import atexit
import contextlib
import json
import logging
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

# Locate src/ by NAME, like app.py and the other suites.
SRC_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "src")
sys.path.insert(0, str(SRC_DIR))

import accounts.onboarding as onboarding_steps  # noqa: E402
import accounts.signup as signup_guard  # noqa: E402
import accounts.users as accounts_users  # noqa: E402
import bot.ai_configs as ai_configs  # noqa: E402
import integrations.store as store  # noqa: E402
import webhook.routes as routes  # noqa: E402
from config import Config  # noqa: E402
from database.db import get_connection  # noqa: E402

# Fixture tenants. The slug comes out of the real generator, from the academy
# names below — every one of them starts with "Suite S3C", so every slug starts
# with this prefix and teardown can scope its DELETEs.
TENANT_PREFIX = "suite-s3c-"

# Every user this suite creates lives on one domain, so teardown can never touch
# the founder's own account.
EMAIL_DOMAIN = "@suite.corujai.test"

# Reserved in the prefix registry (5521000...-5528000... are taken).
SENDER_PREFIX = "5529000"

SUITE_PASSWORD = "suite-password-s3c"

MIGRATION_USERS = "009_create_users"
MIGRATION_SIGNUP = "010_create_signup_attempts"

PROVISIONED_TABLES: tuple[str, ...] = (
    "owners", "ai_configs", "class_types", "scheduling_configs", "users",
)

DEFAULT_REPORT_DIR = SRC_DIR / "tests" / "outputs"


class SkipTest(Exception):
    """Raised by a test that cannot run in the current environment."""


class Console:
    """Tiny ANSI helper that degrades to plain text when piped to a file."""

    def __init__(self, color: bool) -> None:
        self.color = color

    def paint(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def green(self, text: str) -> str:
        return self.paint(text, "32")

    def red(self, text: str) -> str:
        return self.paint(text, "31")

    def yellow(self, text: str) -> str:
        return self.paint(text, "33")

    def dim(self, text: str) -> str:
        return self.paint(text, "2")

    def bold(self, text: str) -> str:
        return self.paint(text, "1")


class Report:
    """Collects test outcomes and renders the final console report."""

    _SYMBOLS = {"PASS": "✔", "FAIL": "✖", "ERROR": "✖", "SKIP": "○"}

    def __init__(self, console: Console) -> None:
        self.console = console
        self.results: list[dict[str, Any]] = []
        self.started_at = time.monotonic()

    def section(self, title: str) -> None:
        print(f"\n{self.console.bold('▸ ' + title)}")

    def run(self, step: str, title: str, test: Callable[[], str | None]) -> bool:
        started = time.monotonic()
        detail, trace, status = "", None, "PASS"

        try:
            detail = test() or ""
        except SkipTest as exc:
            status, detail = "SKIP", str(exc)
        except AssertionError as exc:
            status, detail = "FAIL", str(exc)
        except Exception as exc:  # noqa: BLE001 - any crash is a test failure
            status = "ERROR"
            detail = f"{type(exc).__name__}: {exc}"
            trace = traceback.format_exc()

        self.results.append({
            "step": step, "title": title, "status": status,
            "detail": detail, "traceback": trace,
            "seconds": round(time.monotonic() - started, 2),
        })
        self._print_line(step, title, status, detail)
        return status in ("PASS", "SKIP")

    def _print_line(self, step: str, title: str, status: str, detail: str) -> None:
        painter = {
            "PASS": self.console.green, "FAIL": self.console.red,
            "ERROR": self.console.red, "SKIP": self.console.yellow,
        }[status]
        symbol = painter(self._SYMBOLS[status])
        print(f"  {symbol} {step:>3}  {title}")
        if detail:
            print(f"         {self.console.dim(detail)}")

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [r for r in self.results if r["status"] in ("FAIL", "ERROR")]

    def summary(self) -> bool:
        passed = sum(1 for r in self.results if r["status"] == "PASS")
        skipped = sum(1 for r in self.results if r["status"] == "SKIP")
        failed = len(self.failed)
        elapsed = time.monotonic() - self.started_at

        print("\n" + "─" * 72)
        parts = [
            f"{len(self.results)} testes",
            self.console.green(f"{passed} passaram"),
            self.console.red(f"{failed} falharam") if failed else "0 falharam",
            self.console.yellow(f"{skipped} pulados") if skipped else "0 pulados",
        ]
        print(f" {' · '.join(parts)}{self.console.dim(f'   ({elapsed:.1f}s)')}")
        print("─" * 72)

        for result in self.failed:
            print(f"\n{self.console.red('FALHOU')} passo {result['step']} — {result['title']}")
            print(f"  {result['detail']}")
            if result["traceback"]:
                print(self.console.dim("".join(f"  {line}" for line in
                                              result["traceback"].splitlines(keepends=True))))

        if not failed:
            print(self.console.green("\n Cadastro público OK — todos os testes passaram.\n"))
        return not failed

    def to_json(self, path: Path) -> None:
        path.write_text(json.dumps({
            "passed": sum(1 for r in self.results if r["status"] == "PASS"),
            "failed": len(self.failed),
            "skipped": sum(1 for r in self.results if r["status"] == "SKIP"),
            "results": self.results,
        }, indent=2, ensure_ascii=False), encoding="utf-8")


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def expect_equal(actual: Any, expected: Any, what: str) -> None:
    if actual != expected:
        raise AssertionError(f"{what}: esperado {expected!r}, veio {actual!r}")


@contextlib.contextmanager
def patched(obj: Any, attr: str, value: Any) -> Iterator[None]:
    """Temporarily set obj.attr = value, restoring the original afterward."""
    original = getattr(obj, attr)
    setattr(obj, attr, value)
    try:
        yield
    finally:
        setattr(obj, attr, original)


def table_counts() -> dict[str, int]:
    """Snapshot the row count of every table signup writes to."""
    counts: dict[str, int] = {}
    with get_connection() as conn:
        with conn.cursor() as cur:
            for table in PROVISIONED_TABLES:
                cur.execute(f"SELECT COUNT(*) AS total FROM {table}")
                counts[table] = int(cur.fetchone()["total"])
    return counts


def read_class_type_rows(tenant_id: str) -> list[dict]:
    """Read a tenant's class types straight from the table."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT marker, label, capacity, is_fallback
                FROM class_types WHERE tenant_id = %s ORDER BY marker
                """,
                (tenant_id,),
            )
            return [dict(row) for row in cur.fetchall()]


def clear_attempts() -> None:
    """Wipe the throttle's memory. Between tests, so one does not throttle the next."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM signup_attempts")
        conn.commit()


def drop_tenant(tenant_id: str) -> None:
    """Delete a fixture tenant and everything hanging off it."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            for table in ("users", "class_types", "ai_configs",
                          "scheduling_configs", "owners"):
                cur.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant_id,))
        conn.commit()


def _drop_orphan_fixtures() -> None:
    """Delete anything a previous run left behind, before this one starts.

    Stands in for the /tmp backup the older suites keep. Scoped to the fixture
    prefix and the fixture email domain, so it can never touch the pilot or the
    founder's own account.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE email LIKE %s", ("%" + EMAIL_DOMAIN,))
            removed_users: int = cur.rowcount
            for table in ("users", "class_types", "ai_configs",
                          "scheduling_configs", "owners"):
                cur.execute(f"DELETE FROM {table} WHERE tenant_id LIKE %s",
                            (TENANT_PREFIX + "%",))
            cur.execute("DELETE FROM sessions WHERE sender LIKE %s", (SENDER_PREFIX + "%",))
            cur.execute("DELETE FROM signup_attempts")
        conn.commit()

    if removed_users:
        print(f"  reparo: {removed_users} usuário(s) órfão(s) de uma run anterior removido(s)")


class SignupSuite:
    """Owns fixtures, tests and teardown for the public signup screen."""

    def __init__(self, report: Report, keep: bool) -> None:
        self.report = report
        self.keep = keep
        self._n = 0
        self.app: Any = None
        self.baseline: dict[str, int] = {}
        self.tenants: list[str] = []
        self.tenant_a: str = ""
        self.email_a: str = ""

    # -- infrastructure -----------------------------------------------------

    def next_email(self, label: str) -> str:
        return f"suite-s3c-{label}{EMAIL_DOMAIN}"

    def application(self) -> Any:
        """Build the Flask app once, with CSRF disabled by default.

        Scenarios 12 and 13 build their own app WITH CSRF on — that is the only
        place in the project where the CSRF wiring itself is exercised.
        """
        if self.app is None:
            import app as flask_app

            self.app = flask_app.create_app()
            self.app.config["TESTING"] = True
            self.app.config["WTF_CSRF_ENABLED"] = False
        return self.app

    def signup(self, client: Any, name: str, email: str, password: str | None = None,
               confirm: str | None = None, **extra: str) -> Any:
        """POST the signup form with sane defaults, overridable field by field.

        EACH CALL GETS ITS OWN CLIENT IP. Without this, the per-IP ceiling — which
        is real code doing its job — would fire partway through the suite and
        every later test would read as a failure. The tests that are ABOUT the
        throttle (9 and 10) post directly with a fixed address instead.
        """
        password = SUITE_PASSWORD if password is None else password
        data: dict[str, str] = {
            "academy_name": name,
            "email": email,
            "password": password,
            "password_confirm": password if confirm is None else confirm,
        }
        data.update(extra)

        self._n += 1
        return client.post(
            "/dashboard/signup",
            data=data,
            headers={"X-Forwarded-For": f"203.0.113.{self._n % 250}"},
        )

    def remember(self, tenant_id: str) -> None:
        if tenant_id and tenant_id not in self.tenants:
            self.tenants.append(tenant_id)

    # -- prerequisites ------------------------------------------------------

    def check_schema(self) -> str:
        """Check the tables this module needs are in place."""
        required = (MIGRATION_USERS, MIGRATION_SIGNUP)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT version FROM schema_migrations WHERE version = ANY(%s)",
                    (list(required),),
                )
                applied = {row["version"] for row in cur.fetchall()}
                cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'signup_attempts'")
                indexes = {row["indexname"] for row in cur.fetchall()}

        missing = [v for v in required if v not in applied]
        expect(not missing,
               f"migrações não aplicadas: {', '.join(missing)} (suba a app para rodar init_db)")
        expect("idx_signup_attempts_ip_created" in indexes,
               "falta o índice (ip_hash, created_at) de signup_attempts")

        return "migrations 009 e 010 aplicadas · índice do throttle no lugar"

    def prepare_fixtures(self) -> str:
        """Snapshot the row counts, so teardown can prove it put everything back."""
        self.baseline = table_counts()
        clear_attempts()
        counts = " · ".join(f"{t}={n}" for t, n in self.baseline.items())
        return f"contagens iniciais: {counts}"

    # -- tests --------------------------------------------------------------

    def test_01_flag_off_is_404(self) -> str:
        """Com SIGNUP_ENABLED desligada a rota não existe — 404, não 403.

        403 anuncia que há algo ali para voltar depois. E o `abort` vem antes de
        qualquer leitura do formulário, então a rota desligada não tem caminho de
        execução nenhum.
        """
        app = self.application()
        with patched(Config, "SIGNUP_ENABLED", False):
            client = app.test_client()
            expect_equal(client.get("/dashboard/signup").status_code, 404,
                         "GET com a flag desligada")
            expect_equal(
                self.signup(client, "Suite S3C Proibida", self.next_email("off")).status_code,
                404, "POST com a flag desligada")

            login_body = client.get("/dashboard/login").get_data(as_text=True)
            expect("/dashboard/signup" not in login_body,
                   "o login não pode mostrar link para uma rota que dá 404")

        expect_equal(table_counts(), self.baseline, "nada podia ter sido criado")
        return "GET e POST dão 404 · o login não mostra o link"

    def test_02_flag_on_renders_the_form(self) -> str:
        """Com a flag ligada, a tela aparece — com os quatro campos e o honeypot."""
        with patched(Config, "SIGNUP_ENABLED", True):
            body = self.application().test_client().get("/dashboard/signup").get_data(as_text=True)

        for field in ("academy_name", "email", "password", "password_confirm"):
            expect(f'name="{field}"' in body, f"faltou o campo {field}")
        expect(f'name="{signup_guard.HONEYPOT_FIELD}"' in body, "faltou o honeypot")
        expect('name="csrf_token"' in body, "o formulário precisa do token CSRF")
        expect("/dashboard/login" in body, "faltou o caminho de volta para o login")

        return "4 campos + honeypot + token CSRF + link para o login"

    def test_03_signup_creates_the_whole_tenant(self) -> str:
        """Um cadastro válido cria as cinco tabelas, loga, e cai no onboarding."""
        self.email_a = self.next_email("alpha")
        client = self.application().test_client()

        with patched(Config, "SIGNUP_ENABLED", True):
            response = self.signup(client, "Suite S3C Alpha", self.email_a)

        expect_equal(response.status_code, 302, "cadastro válido deveria redirecionar")
        expect("/dashboard/onboarding" in response.headers.get("Location", ""),
               "deveria cair na tela de primeiros passos")

        user = accounts_users.get_user_by_email(self.email_a)
        expect(user is not None, "faltou a linha em users")
        self.tenant_a = user["tenant_id"]
        self.remember(self.tenant_a)

        expect(self.tenant_a.startswith(TENANT_PREFIX),
               f"o slug de fixture deveria começar com '{TENANT_PREFIX}', veio {self.tenant_a!r}")

        owner = store.get_owner_credentials(self.tenant_a)
        expect(owner is not None, "faltou a linha em owners")
        expect_equal(ai_configs.get_ai_config(self.tenant_a)["academy_name"],
                     "Suite S3C Alpha", "nome da academia em ai_configs")

        rows = read_class_type_rows(self.tenant_a)
        expect_equal(len(rows), 1, "o tenant novo deveria ter exatamente a turma padrão")
        expect(rows[0]["is_fallback"], "a turma criada tem de ser a padrão")

        # A sessão é real: uma rota protegida abre.
        expect_equal(client.get("/dashboard/menu").status_code, 200,
                     "o cadastro deveria ter logado a pessoa")

        return f"tenant '{self.tenant_a}' criado nas cinco tabelas, já logado"

    def test_04_slug_is_not_accepted_from_the_form(self) -> str:
        """Mandar tenant_id no POST não muda o slug — ele sai do nome.

        provision_tenant() aceita o parâmetro (o CLI do fundador usa), e lê-lo de
        um formulário público deixaria um estranho escolher uma chave primária e
        disputar nomes bons com outras academias.
        """
        email = self.next_email("slug")
        client = self.application().test_client()

        with patched(Config, "SIGNUP_ENABLED", True):
            self.signup(client, "Suite S3C Slug", email, tenant_id="escolhido-por-mim")

        user = accounts_users.get_user_by_email(email)
        expect(user is not None, "o cadastro deveria ter funcionado")
        self.remember(user["tenant_id"])

        expect(user["tenant_id"] != "escolhido-por-mim",
               "O SLUG DO FORMULÁRIO FOI ACEITO — um estranho escolheria a chave primária")
        expect_equal(user["tenant_id"], "suite-s3c-slug", "o slug deveria sair do nome")

        return "tenant_id do formulário ignorado; slug gerado do nome"

    def test_05_password_confirmation(self) -> str:
        """Senhas divergentes: 200 com erro, e nada escrito."""
        before = table_counts()
        with patched(Config, "SIGNUP_ENABLED", True):
            response = self.signup(
                self.application().test_client(),
                "Suite S3C Divergente", self.next_email("mismatch"),
                password="senha-boa-123", confirm="senha-outra-456",
            )

        expect_equal(response.status_code, 200, "deveria re-renderizar o formulário")
        expect("não coincidem" in response.get_data(as_text=True), "faltou a mensagem")
        expect_equal(table_counts(), before, "nada podia ter sido criado")

        return "senhas divergentes recusadas sem escrever nada"

    def test_06_invalid_input_never_500s(self) -> str:
        """E-mail inválido e senha curta devolvem a mensagem do provision_tenant."""
        before = table_counts()
        cases: list[tuple[str, str, str, str]] = [
            ("Suite S3C Ruim", "nao-e-email", SUITE_PASSWORD, "E-mail inválido"),
            ("Suite S3C Curta", self.next_email("short"), "abc", "pelo menos"),
            ("", self.next_email("noname"), SUITE_PASSWORD, "nome da academia"),
        ]

        with patched(Config, "SIGNUP_ENABLED", True):
            for name, email, password, expected in cases:
                response = self.signup(self.application().test_client(), name, email,
                                       password=password)
                expect_equal(response.status_code, 200, f"{expected}: deveria devolver 200")
                expect(expected in response.get_data(as_text=True),
                       f"esperava '{expected}' na tela")

        expect_equal(table_counts(), before, "nenhum dos casos podia ter escrito")
        return f"{len(cases)} entradas inválidas recusadas com mensagem, sem 500"

    def test_07_duplicate_email_does_not_enumerate(self) -> str:
        """E-mail repetido não confirma que o e-mail existe.

        O /login do S3a é genérico de propósito, para não servir de oráculo de
        contas. Um cadastro que responde "esse e-mail já está cadastrado"
        devolveria exatamente esse oráculo.
        """
        before = table_counts()
        with patched(Config, "SIGNUP_ENABLED", True):
            response = self.signup(self.application().test_client(),
                                   "Suite S3C Outra", self.email_a)

        body = response.get_data(as_text=True)
        expect_equal(response.status_code, 200, "deveria re-renderizar")
        expect("Não foi possível criar a conta" in body, "faltou a mensagem genérica")
        expect("já está cadastrado" not in body and "já existe" not in body,
               "a mensagem não pode confirmar que o e-mail existe")
        expect_equal(table_counts(), before, "nada podia ter sido criado")

        return "e-mail repetido: mensagem genérica, nenhuma linha nova"

    def test_08_honeypot_is_silent(self) -> str:
        """Honeypot preenchido: a mesma tela de sucesso, e zero linhas.

        Responder um erro ensinaria o próximo bot a pular o campo — que é
        exatamente o que faz a armadilha deixar de funcionar.
        """
        before = table_counts()
        email = self.next_email("bot")

        with patched(Config, "SIGNUP_ENABLED", True):
            response = self.signup(self.application().test_client(),
                                   "Suite S3C Bot", email,
                                   **{signup_guard.HONEYPOT_FIELD: "http://spam.example"})

        expect_equal(response.status_code, 200, "o bot deveria receber 200, como um humano")
        expect("Conta criada" in response.get_data(as_text=True),
               "o bot deveria ver a mesma tela de sucesso")
        expect(accounts_users.get_user_by_email(email) is None,
               "O HONEYPOT DEIXOU PASSAR — o usuário foi criado")
        expect_equal(table_counts(), before, "nada podia ter sido criado")

        return "bot recebe a tela de sucesso; nada é escrito"

    def test_09_ip_ceiling(self) -> str:
        """A partir do teto, o mesmo IP é recusado — e outro IP continua passando."""
        clear_attempts()
        before = table_counts()
        headers = {"X-Forwarded-For": "203.0.113.7"}

        with patched(Config, "SIGNUP_ENABLED", True):
            client = self.application().test_client()
            # Gasta o teto com tentativas inválidas (não criam tenant, mas contam).
            for i in range(signup_guard.MAX_ATTEMPTS_PER_WINDOW):
                client.post("/dashboard/signup",
                            data={"academy_name": "Suite S3C Flood", "email": "x@y",
                                  "password": "abc", "password_confirm": "abc"},
                            headers=headers)

            blocked = client.post(
                "/dashboard/signup",
                data={"academy_name": "Suite S3C Flood", "email": self.next_email("flood"),
                      "password": SUITE_PASSWORD, "password_confirm": SUITE_PASSWORD},
                headers=headers)

            expect_equal(blocked.status_code, 429, "a tentativa acima do teto deveria dar 429")
            expect("Muitas tentativas" in blocked.get_data(as_text=True), "faltou a mensagem")

            # Outro IP não herda o bloqueio.
            other = self.application().test_client().post(
                "/dashboard/signup",
                data={"academy_name": "Suite S3C Outro IP", "email": self.next_email("otherip"),
                      "password": SUITE_PASSWORD, "password_confirm": SUITE_PASSWORD},
                headers={"X-Forwarded-For": "198.51.100.42"})

        expect_equal(other.status_code, 302, "um IP diferente deveria conseguir se cadastrar")
        created = accounts_users.get_user_by_email(self.next_email("otherip"))
        expect(created is not None, "o cadastro do outro IP deveria ter criado o usuário")
        self.remember(created["tenant_id"])

        expect(table_counts()["owners"] == before["owners"] + 1,
               "só o cadastro do segundo IP podia ter criado tenant")

        clear_attempts()
        return f"teto de {signup_guard.MAX_ATTEMPTS_PER_WINDOW} por IP aplicado; outro IP passa"

    def test_10_throttle_counts_in_the_database(self) -> str:
        """O contador vive no Postgres, não em memória de processo.

        O gunicorn roda vários workers, cada um com seus próprios globais: um
        contador em processo veria ~1/N das tentativas e deixaria passar N vezes
        a taxa pretendida.
        """
        clear_attempts()
        ip = "203.0.113.99"

        expect(not signup_guard.too_many_attempts(ip), "começa liberado")
        for _ in range(signup_guard.MAX_ATTEMPTS_PER_WINDOW):
            signup_guard.record_attempt(ip)
        expect(signup_guard.too_many_attempts(ip), "deveria bloquear ao atingir o teto")
        expect(not signup_guard.too_many_attempts("198.51.100.1"), "outro IP é outro balde")

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT ip_hash FROM signup_attempts LIMIT 1")
                stored = cur.fetchone()["ip_hash"]

        expect(ip not in stored, "O IP FOI GRAVADO EM CLARO — deveria ser hash")
        expect_equal(len(stored), 64, "o hash deveria ter 64 caracteres hex")

        clear_attempts()
        return "contagem no banco · IP gravado como hash de 64 chars"

    def test_11_onboarding_reflects_the_database(self) -> str:
        """A checklist é derivada: mexer no banco muda a tela, sem estado próprio."""
        steps = onboarding_steps.get_steps(self.tenant_a)
        by_key = {step["key"]: step for step in steps}

        expect(by_key["account"]["done"], "conta criada deveria estar marcada")
        expect(not by_key["calendar"]["done"], "o Calendar não foi conectado")
        expect(not by_key["ai"]["done"], "a IA está com os textos-guia")
        expect(not by_key["classes"]["done"], "só existe a turma padrão")
        expect(not by_key["whatsapp"]["done"], "o número ainda não foi atribuído")
        # DESDE O MÓDULO S3d ESTE PASSO TEM BOTÃO, e a mudança é de produto, não
        # de implementação: o dono digita o próprio número na tela de
        # configurações. O que ele continua não podendo fazer sozinho é a metade
        # que vem ANTES — a liberação do Sender no Twilio —, e é isso que a
        # descrição do passo precisa dizer. Um botão sem esse aviso faria o dono
        # cadastrar um número que ainda não existe e o envio falharia calado.
        expect_equal(by_key["whatsapp"]["action_endpoint"], "dashboard.settings",
                     "o passo do WhatsApp deveria levar à tela de configurações")
        expect("equipe" in by_key["whatsapp"]["description"],
               "o passo precisa dizer que a liberação do número não é do dono")
        expect_equal(onboarding_steps.pending_count(self.tenant_a), 4, "pendências iniciais")

        # Conecta o Calendar por baixo, e a tela tem de mudar sozinha.
        store.save_owner_credentials("dono@suite.corujai.test", "fake-refresh",
                                     "cal-id", tenant_id=self.tenant_a)
        expect_equal(onboarding_steps.pending_count(self.tenant_a), 3,
                     "conectar o Calendar deveria marcar um passo")

        # Preenche a IA.
        ai_configs.update_ai_config(
            academy_name="Suite S3C Alpha", assistant_name="Corujinha",
            tone="objetiva", business_info="Jiu-Jitsu", flow_emphasis="agendar rápido",
            tenant_id=self.tenant_a)
        expect_equal(onboarding_steps.pending_count(self.tenant_a), 2,
                     "preencher a IA deveria marcar outro")

        # E o número, que só o fundador atribui.
        store.update_whatsapp_number("5529000123456", tenant_id=self.tenant_a)
        expect_equal(onboarding_steps.pending_count(self.tenant_a), 1,
                     "o número deveria marcar o último que não depende do dono")
        store.update_whatsapp_number(None, tenant_id=self.tenant_a)

        return "4 → 3 → 2 → 1 pendências, tudo derivado do banco"

    def test_12_onboarding_requires_login(self) -> str:
        """A tela de primeiros passos é do painel, e exige sessão."""
        anonymous = self.application().test_client()
        response = anonymous.get("/dashboard/onboarding")
        expect_equal(response.status_code, 302, "deveria redirecionar")
        expect("/dashboard/login" in response.headers.get("Location", ""),
               "deveria redirecionar para o login")
        return "/dashboard/onboarding exige sessão autenticada"

    def test_13_csrf_blocks_a_tokenless_post(self) -> str:
        """Com o CSRF LIGADO, um POST sem token é recusado.

        É o único teste do projeto que liga o CSRF — todas as outras suítes o
        desligam, o que é certo para elas e deixaria esta fiação sem cobertura
        nenhuma.
        """
        import app as flask_app

        app = flask_app.create_app()          # CSRF ligado, como em produção
        app.config["TESTING"] = True
        client = app.test_client()

        with patched(Config, "SIGNUP_ENABLED", True):
            response = self.signup(client, "Suite S3C CSRF", self.next_email("csrf"))

        expect_equal(response.status_code, 400, "POST sem token CSRF deveria ser recusado")
        expect(accounts_users.get_user_by_email(self.next_email("csrf")) is None,
               "nada podia ter sido criado sem token")

        # E o login também está protegido.
        expect_equal(
            client.post("/dashboard/login", data={"email": "a@b.com", "password": "x"}).status_code,
            400, "o login também deveria exigir token")

        return "POST sem token CSRF recusado no cadastro e no login"

    def test_14_webhook_is_exempt_from_csrf(self) -> str:
        """ARMADILHA CENTRAL DO MÓDULO: o webhook do Twilio NÃO pode exigir token.

        CSRFProtect intercepta todo POST da aplicação. Sem csrf.exempt(webhook_bp)
        o Twilio passaria a receber 400 em cada mensagem, nenhum lead seria
        respondido, e nada no log pareceria um erro — o bot simplesmente ficaria
        mudo.
        """
        import app as flask_app

        app = flask_app.create_app()          # CSRF ligado
        app.config["TESTING"] = True
        client = app.test_client()

        calls: list[tuple] = []

        def fake_lead(sender: str, body: str, tenant_id: str = "default") -> None:
            calls.append((sender, tenant_id))

        with patched(routes, "handle_text_message", fake_lead):
            response = client.post("/webhook", data={
                "From": f"whatsapp:+{SENDER_PREFIX}000001",
                "Body": "oi",
                "To": "whatsapp:+14155238886",
            })

        expect_equal(response.status_code, 200,
                     "O WEBHOOK DO TWILIO ESTÁ EXIGINDO CSRF — nenhum lead seria respondido")
        expect_equal(len(calls), 1, "a mensagem deveria ter chegado ao handler")

        return "POST /webhook sem token continua 200 e chega ao handler"

    def test_15_teardown_restores_the_counts(self) -> str:
        """Depois da limpeza sobra só o que já existia."""
        for tenant_id in list(self.tenants):
            drop_tenant(tenant_id)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE email LIKE %s", ("%" + EMAIL_DOMAIN,))
            conn.commit()
        clear_attempts()

        expect_equal(table_counts(), self.baseline,
                     "alguma linha de fixture sobrou depois da limpeza")

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM owners WHERE tenant_id LIKE %s",
                            (TENANT_PREFIX + "%",))
                leftovers = int(cur.fetchone()["n"])
        expect_equal(leftovers, 0, "sobrou tenant de fixture")

        self.tenants.clear()
        return "contagens de volta ao ponto inicial"

    # -- teardown -----------------------------------------------------------

    def teardown(self) -> None:
        if self.keep:
            print("  --keep: tenants e usuários de teste preservados.")
            print("  ATENÇÃO: há tenants além do 'default' no banco.")
            return

        for tenant_id in list(self.tenants):
            drop_tenant(tenant_id)

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE email LIKE %s", ("%" + EMAIL_DOMAIN,))
                removed_users = cur.rowcount
                cur.execute("DELETE FROM sessions WHERE sender LIKE %s", (SENDER_PREFIX + "%",))
                removed_sessions = cur.rowcount
                cur.execute("DELETE FROM signup_attempts")
            conn.commit()

        print(f"  limpeza: {len(self.tenants)} tenant(s), {removed_users} usuário(s) e "
              f"{removed_sessions} sessão(ões) de teste removidos.")


def _resolve_report_path(path: Path) -> Path:
    if path.is_dir() or path.suffix.lower() != ".json":
        path = path / f"signup-{datetime.now():%Y%m%d_%H%M%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Suíte automatizada do cadastro público (SIGNUP_TESTING.md).")
    parser.add_argument("--keep", action="store_true",
                        help="Não desfaz nada ao final (para depurar à mão).")
    parser.add_argument("--no-color", action="store_true", help="Saída sem cores ANSI.")
    parser.add_argument("--json", nargs="?", type=Path, const=DEFAULT_REPORT_DIR, default=None,
                        metavar="ARQUIVO",
                        help="Também grava o relatório em JSON (sozinha: tests/outputs/).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.ERROR, format="%(levelname)s - %(message)s")

    console = Console(color=not args.no_color and sys.stdout.isatty())
    report = Report(console)

    print(console.bold("\n═══ Corujai · suíte do cadastro público ═══"))
    print(console.dim(" Roteiro: SIGNUP_TESTING.md"))

    _drop_orphan_fixtures()

    report.section("Pré-requisitos")
    suite = SignupSuite(report, keep=args.keep)
    if not report.run("P1", "Migrations 009 e 010 aplicadas", suite.check_schema):
        print(console.red("\n Pré-requisitos falharam — a suíte não pode continuar."))
        report.summary()
        sys.exit(1)

    report.section("Preparo")
    atexit.register(suite.teardown)
    if not report.run("F1", "Contagens iniciais registradas", suite.prepare_fixtures):
        print(console.red("\n Sem as contagens a suíte não consegue provar que limpou."))
        atexit.unregister(suite.teardown)
        report.summary()
        sys.exit(1)

    report.section("Roteiro de testes")
    tests: list[tuple[str, str, Callable[[], str | None]]] = [
        ("1", "Com a flag desligada a rota dá 404", suite.test_01_flag_off_is_404),
        ("2", "Com a flag ligada o formulário aparece completo",
         suite.test_02_flag_on_renders_the_form),
        ("3", "Um cadastro válido cria as cinco tabelas e já loga",
         suite.test_03_signup_creates_the_whole_tenant),
        ("4", "O slug não é aceito do formulário",
         suite.test_04_slug_is_not_accepted_from_the_form),
        ("5", "Senhas divergentes são recusadas", suite.test_05_password_confirmation),
        ("6", "Entrada inválida devolve mensagem, nunca 500",
         suite.test_06_invalid_input_never_500s),
        ("7", "E-mail repetido não confirma que o e-mail existe",
         suite.test_07_duplicate_email_does_not_enumerate),
        ("8", "O honeypot descarta em silêncio", suite.test_08_honeypot_is_silent),
        ("9", "O teto por IP bloqueia, e outro IP passa", suite.test_09_ip_ceiling),
        ("10", "O throttle conta no banco e grava o IP em hash",
         suite.test_10_throttle_counts_in_the_database),
        ("11", "A checklist do onboarding é derivada do banco",
         suite.test_11_onboarding_reflects_the_database),
        ("12", "A tela de primeiros passos exige login",
         suite.test_12_onboarding_requires_login),
        ("13", "Com CSRF ligado, POST sem token é recusado",
         suite.test_13_csrf_blocks_a_tokenless_post),
        ("14", "O webhook do Twilio é isento de CSRF",
         suite.test_14_webhook_is_exempt_from_csrf),
        ("15", "A limpeza devolve o banco ao estado inicial",
         suite.test_15_teardown_restores_the_counts),
    ]
    for step, title, test in tests:
        report.run(step, title, test)

    atexit.unregister(suite.teardown)
    report.section("Limpeza")
    suite.teardown()

    if args.json is not None:
        report_path = _resolve_report_path(args.json)
        report.to_json(report_path)
        print(f"  relatório JSON: {report_path}")

    sys.exit(0 if report.summary() else 1)


if __name__ == "__main__":
    main()
