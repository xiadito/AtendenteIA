"""Automated end-to-end suite for accounts, login and tenant provisioning (Module S3a).

Runs the scenarios documented in ACCOUNTS_TESTING.md and prints a PASS/FAIL
report boiled down to an exit code, in the same style as
tests/test_class_types/test_class_types_suite.py.

Fully deterministic: no LLM, no WhatsApp, no Google Calendar. Everything here is
Postgres plus the Flask test client.

NO /tmp BACKUP FILE, AND THAT IS DELIBERATE — this is the first suite in the
project without one. test_settings and test_class_types snapshot the pilot's
real rows because the screens they exercise can only write to tenant 'default'.
This suite never writes to the pilot: every scenario runs on fixture tenants
built by provision_tenant() under the prefix below. What replaces the backup is
_drop_orphan_fixtures(), called at the start of main() — the same crash-repair
role, deleting whatever a run that died left behind. Do not "restore" the
missing backup step; there is nothing of the pilot's to restore.

⛔ THE SUITE CREATES SEVERAL TENANTS. That is safe here and NOT safe in
production: until Module S3b merges, the reads do not filter by tenant, so a
second tenant in a production database would see the pilot's conversations and
bookings. Run this against a development database.

Run from src/:
    python tests/test_accounts/test_accounts_suite.py
    python tests/test_accounts/test_accounts_suite.py --keep
    python tests/test_accounts/test_accounts_suite.py --json

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

import accounts.provision as provision  # noqa: E402
import accounts.tenants as tenants  # noqa: E402
import accounts.users as accounts_users  # noqa: E402
import bot.ai_configs as ai_configs  # noqa: E402
import bot.class_types as class_types  # noqa: E402
import integrations.store as store  # noqa: E402
import webhook.routes as routes  # noqa: E402
from database.db import get_connection  # noqa: E402

# Fixture tenants. Hyphens rather than test_class_types' underscores because
# these ids come out of the real slug generator, which never produces "_".
TENANT_PREFIX = "suite-s3a-"

# Every user this suite creates lives on one domain, so teardown can scope its
# DELETE and can never touch the founder's own account — including the one
# create_app() may have bootstrapped from .env while this suite ran.
EMAIL_DOMAIN = "@suite.corujai.test"

# Leads, if a scenario needs one. Reserved so the prefix registry stays
# collision-free (5521000...-5527000... are taken by the other suites).
SENDER_PREFIX = "5528000"

SUITE_PASSWORD = "suite-password-s3a"

MIGRATION_OWNERS = "003_create_owners"
MIGRATION_AI_CONFIGS = "005_create_ai_configs"
MIGRATION_CLASS_TYPES = "008_create_class_types"
MIGRATION_USERS = "009_create_users"

# The five tables provision_tenant() writes, in the order it writes them.
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
            print(self.console.green("\n Contas e provisionamento OK — todos os testes passaram.\n"))
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


class WarningCapture(logging.Handler):
    """Collect WARNING records from one logger, to assert a warning was emitted.

    Same idea as the capture in tests/test_class_types: several behaviours in
    this module are "degrade, but say so out loud", and a test that only checks
    the degradation would pass even if the warning disappeared.
    """

    def __init__(self, logger_name: str) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[str] = []
        self.logger = logging.getLogger(logger_name)

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())

    def __enter__(self) -> "WarningCapture":
        self.logger.addHandler(self)
        return self

    def __exit__(self, *args: Any) -> None:
        self.logger.removeHandler(self)


def table_counts() -> dict[str, int]:
    """Snapshot the row count of every table provisioning writes to."""
    counts: dict[str, int] = {}
    with get_connection() as conn:
        with conn.cursor() as cur:
            for table in PROVISIONED_TABLES:
                cur.execute(f"SELECT COUNT(*) AS total FROM {table}")
                counts[table] = int(cur.fetchone()["total"])
    return counts


def read_class_type_rows(tenant_id: str) -> list[dict]:
    """Read a tenant's class types STRAIGHT FROM THE TABLE.

    Deliberately not load_class_types(): that function synthesizes a fallback in
    memory when the tenant has none, which is exactly the bug this suite has to
    be able to see. Reading the table is the only way to prove provisioning
    wrote a real row.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT marker, label, capacity, requires_child_name, is_fallback
                FROM class_types WHERE tenant_id = %s ORDER BY marker
                """,
                (tenant_id,),
            )
            return [dict(row) for row in cur.fetchall()]


def read_owner_row(tenant_id: str) -> dict | None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM owners WHERE tenant_id = %s", (tenant_id,))
            row = cur.fetchone()
    return dict(row) if row else None


def read_password_hash(email: str) -> str | None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT password_hash FROM users WHERE email = %s", (email,))
            row = cur.fetchone()
    return row["password_hash"] if row else None


def drop_tenant(tenant_id: str) -> None:
    """Delete a fixture tenant and everything hanging off it.

    Order matters: `users` has a foreign key to `owners` (ON DELETE CASCADE, so
    the cascade would handle it, but being explicit documents the dependency),
    and the other three tables have NO foreign key at all — nothing deletes them
    for us.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE tenant_id = %s", (tenant_id,))
            cur.execute("DELETE FROM class_types WHERE tenant_id = %s", (tenant_id,))
            cur.execute("DELETE FROM ai_configs WHERE tenant_id = %s", (tenant_id,))
            cur.execute("DELETE FROM scheduling_configs WHERE tenant_id = %s", (tenant_id,))
            cur.execute("DELETE FROM owners WHERE tenant_id = %s", (tenant_id,))
        conn.commit()


def _drop_orphan_fixtures() -> None:
    """Delete anything a previous run left behind, before this one starts.

    This is what stands in for the /tmp backup the other suites keep: a run that
    dies mid-suite leaves fixture tenants in the database, and the next run's
    slug-collision test would then see a world it did not create.

    Scoped to the fixture prefix and the fixture email domain, so it can never
    touch the pilot or the founder's own account.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE email LIKE %s", ("%" + EMAIL_DOMAIN,))
            removed_users: int = cur.rowcount
            for table in ("users", "class_types", "ai_configs", "scheduling_configs", "owners"):
                cur.execute(
                    f"DELETE FROM {table} WHERE tenant_id LIKE %s", (TENANT_PREFIX + "%",)
                )
            cur.execute("DELETE FROM sessions WHERE sender LIKE %s", (SENDER_PREFIX + "%",))
        conn.commit()

    if removed_users:
        print(f"  reparo: {removed_users} usuário(s) órfão(s) de uma run anterior removido(s)")


class AccountsSuite:
    """Owns fixtures, tests and teardown for accounts and provisioning."""

    def __init__(self, report: Report, keep: bool) -> None:
        self.report = report
        self.keep = keep
        self._n = 0
        self.app: Any = None
        self.baseline: dict[str, int] = {}
        self.tenants: list[str] = []
        self.emails: list[str] = []
        # Filled by test 2, reused by later tests.
        self.tenant_a: str = ""
        self.email_a: str = ""

    # -- infrastructure -----------------------------------------------------

    def next_sender(self) -> str:
        self._n += 1
        return f"{SENDER_PREFIX}{self._n:06d}"

    def next_email(self, label: str) -> str:
        email: str = f"{TENANT_PREFIX}{label}{EMAIL_DOMAIN}"
        self.emails.append(email)
        return email

    def application(self) -> Any:
        """Build the Flask app once, reusing it across tests."""
        if self.app is None:
            import app as flask_app

            self.app = flask_app.create_app()
            self.app.config["TESTING"] = True
        return self.app

    def provision(self, name: str, label: str, **kwargs: Any) -> dict[str, Any]:
        """Provision a fixture tenant and remember it for teardown."""
        result: dict[str, Any] = provision.provision_tenant(
            academy_name=name,
            email=self.next_email(label),
            password=SUITE_PASSWORD,
            **kwargs,
        )
        if result["tenant_id"] not in self.tenants:
            self.tenants.append(result["tenant_id"])
        return result

    # -- prerequisites ------------------------------------------------------

    def check_schema(self) -> str:
        """Check every table this module touches is in place."""
        required = (MIGRATION_OWNERS, MIGRATION_AI_CONFIGS,
                    MIGRATION_CLASS_TYPES, MIGRATION_USERS)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT version FROM schema_migrations WHERE version = ANY(%s)",
                    (list(required),),
                )
                applied = {row["version"] for row in cur.fetchall()}

                cur.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'owners' AND column_name = 'whatsapp_number'
                    """
                )
                has_column: bool = cur.fetchone() is not None

                cur.execute(
                    "SELECT indexname FROM pg_indexes WHERE tablename = 'owners'"
                )
                indexes = {row["indexname"] for row in cur.fetchall()}

        missing = [version for version in required if version not in applied]
        expect(not missing,
               f"migrações não aplicadas: {', '.join(missing)} (suba a app para rodar init_db)")
        expect(has_column, "owners.whatsapp_number não existe — a migration 009 não rodou inteira")
        expect("idx_owners_whatsapp_number" in indexes,
               "falta o índice único de owners.whatsapp_number")
        expect("idx_owners_owner_phone" in indexes,
               "falta o índice único de owners.owner_phone (o que o S1 adiou)")

        return "migrations 003, 005, 008 e 009 aplicadas · coluna e índices no lugar"

    def prepare_fixtures(self) -> str:
        """Snapshot the row counts, so teardown can prove it put everything back."""
        self.baseline = table_counts()

        pilot = read_owner_row(store.DEFAULT_TENANT_ID)
        expect(pilot is not None, "o tenant 'default' não existe — banco fora do esperado")
        self.pilot_has_number: bool = pilot["whatsapp_number"] is not None

        counts = " · ".join(f"{table}={n}" for table, n in self.baseline.items())
        return f"contagens iniciais: {counts}"

    # -- tests --------------------------------------------------------------

    def test_01_slugify_rules(self) -> str:
        """slugify_tenant_id é puro e mecânico — sem lista de palavras genéricas.

        A decisão 11A dava o exemplo "Academia Delariva Itaipuaçu" →
        "delariva-itaipuacu", mas nenhuma regra mecânica derruba "Academia"
        sozinha. A regra vale; o exemplo não. Quem quiser o slug curto passa
        --tenant-id.
        """
        cases: list[tuple[str, str | None]] = [
            ("Academia Delariva Itaipuaçu", "academia-delariva-itaipuacu"),
            ("CrossFit Niterói - Unidade 2", "crossfit-niteroi-unidade-2"),
            ("  Espaço   Físico  ", "espaco-fisico"),
            ("Gracie Barra!!!", "gracie-barra"),
            ("---", None),
            ("🥋🥋", None),
            ("", None),
            (None, None),
        ]
        for raw, expected in cases:
            expect_equal(tenants.slugify_tenant_id(raw), expected, f"slug de {raw!r}")

        # Truncagem: sobra espaço para o sufixo de colisão e não termina em "-".
        long_name: str = "Academia " + ("muito longa " * 12)
        truncated: str | None = tenants.slugify_tenant_id(long_name)
        expect(truncated is not None, "nome longo deveria gerar slug")
        expect(len(truncated) <= 59, f"slug longo deveria ser truncado em 59, veio {len(truncated)}")
        expect(not truncated.endswith("-"), "o corte não pode deixar hífen no final")

        return f"{len(cases)} casos + truncagem em {len(truncated)} chars sem hífen final"

    def test_02_provision_seeds_all_five_tables(self) -> str:
        """provision_tenant escreve as CINCO tabelas — é o coração do módulo."""
        result = self.provision("Suite S3A Alpha", "alpha")
        self.tenant_a = result["tenant_id"]
        self.email_a = self.emails[-1]

        expect(result["created"], "a primeira chamada deveria criar")
        # As academias de fixture se chamam "Suite S3A ...' exatamente para o
        # slug real cair sob TENANT_PREFIX — é assim que teardown e
        # _drop_orphan_fixtures conseguem escopar seus DELETEs por LIKE.
        expect(result["tenant_id"].startswith(TENANT_PREFIX),
               f"o slug de fixture deveria começar com '{TENANT_PREFIX}', "
               f"veio {result['tenant_id']!r} — a limpeza não o encontraria")

        owner = read_owner_row(self.tenant_a)
        expect(owner is not None, "faltou a linha em owners")
        expect_equal(owner["integration_status"], "disconnected", "status inicial do Calendar")

        config = ai_configs.get_ai_config(self.tenant_a)
        expect_equal(config["academy_name"], "Suite S3A Alpha", "nome da academia em ai_configs")

        rows = read_class_type_rows(self.tenant_a)
        expect_equal(len(rows), 1, "o tenant novo deveria ter exatamente uma turma")

        scheduling_days = class_types.get_scheduling_config(self.tenant_a)["days_ahead"]
        expect_equal(scheduling_days, class_types.DEFAULT_DAYS_AHEAD, "janela de busca")

        user = accounts_users.get_user_by_email(self.email_a)
        expect(user is not None, "faltou a linha em users")
        expect_equal(user["tenant_id"], self.tenant_a, "o usuário aponta para o tenant novo")

        return f"tenant '{self.tenant_a}': owners, ai_configs, class_types, scheduling_configs, users"

    def test_03_fallback_row_is_real(self) -> str:
        """Armadilha #4: a turma padrão precisa existir NO BANCO, não só em memória.

        load_class_types() sintetiza um fallback ilimitado quando o tenant não
        tem nenhum, então um tenant sem linha nenhuma NÃO quebra — ele funciona
        silenciosamente errado, ignorando capacidade. Por isso este teste lê a
        tabela direto, e só depois confere a invariante do S2.
        """
        rows = read_class_type_rows(self.tenant_a)
        expect_equal(len(rows), 1, "uma turma padrão")

        fallback = rows[0]
        expect(fallback["is_fallback"], "a turma criada tem de ser a padrão")
        expect(fallback["capacity"] is None, "capacidade da padrão é NULL = ilimitada")
        expect(not fallback["requires_child_name"],
               "a padrão não pode exigir nome de criança: um título com typo cairia nela")

        # Espelha o sintetizador. Se alguém renomear a constante, isto falha.
        expect_equal(fallback["marker"], class_types._SYNTHETIC_FALLBACK_MARKER,
                     "o marcador provisionado espelha _SYNTHETIC_FALLBACK_MARKER")
        expect_equal(fallback["label"], class_types._SYNTHETIC_FALLBACK_LABEL,
                     "o label provisionado espelha _SYNTHETIC_FALLBACK_LABEL")

        # E a invariante central do S2 vale para o tenant novo.
        bundle = class_types.load_class_types(self.tenant_a)
        expect(bundle["fallback"] in bundle["capacities"],
               "invariante do S2 quebrada: fallback fora de capacities")

        return f"[{fallback['marker']}] {fallback['label']}, ilimitada, real no banco"

    def test_04_ai_config_row_is_updatable(self) -> str:
        """A armadilha do UPDATE: sem linha em ai_configs, a tela recusa todo save.

        update_ai_config() é UPDATE, não upsert — devolve False para sempre num
        tenant sem linha, e a tela diz que não achou o cadastro. Este teste é o
        que falha se o passo 2 do provisionamento for removido.
        """
        updated: bool = ai_configs.update_ai_config(
            academy_name="Suite S3A Alpha",
            assistant_name="Corujinha",
            tone="objetiva",
            business_info="Jiu-Jitsu",
            flow_emphasis="agendar rápido",
            tenant_id=self.tenant_a,
        )
        expect(updated, "update_ai_config devolveu False — o tenant não tem linha em ai_configs")

        config = ai_configs.get_ai_config(self.tenant_a)
        expect_equal(config["assistant_name"], "Corujinha", "o save chegou ao banco")

        return "update_ai_config devolve True: a linha existe e a tela consegue salvar"

    def test_05_ai_seed_matches_migration_005(self) -> str:
        """Os textos-guia do provisionamento têm de bater com os da migration 005.

        A migration só semeia o 'default' (não dá para semear um tenant que
        ainda não existe), então os mesmos textos vivem em dois lugares. Este
        teste é o que faz a duplicação falhar alto em vez de divergir calada.
        """
        pilot = ai_configs.get_ai_config(store.DEFAULT_TENANT_ID)

        for field, seeded in provision._AI_CONFIG_SEED.items():
            if pilot[field].startswith("["):
                expect_equal(seeded, pilot[field],
                             f"o seed de '{field}' divergiu da migration 005")

        # academy_name não entra: o provisionamento usa o nome real da academia.
        expect("academy_name" not in provision._AI_CONFIG_SEED,
               "academy_name não deveria estar no seed — ele vem do nome digitado")

        return f"{len(provision._AI_CONFIG_SEED)} textos-guia conferidos contra o 'default'"

    def test_06_slug_collision_gets_suffix(self) -> str:
        """Duas academias com o mesmo nome recebem slugs diferentes."""
        first = self.provision("Suite S3A Repetida", "rep1")
        second = self.provision("Suite S3A Repetida", "rep2")

        expect(first["tenant_id"] != second["tenant_id"],
               "dois tenants não podem compartilhar identificador")
        expect_equal(second["tenant_id"], f"{first['tenant_id']}-2",
                     "a segunda deveria receber o sufixo -2")
        expect(read_owner_row(second["tenant_id"]) is not None,
               "o tenant com sufixo precisa ter linha real")

        return f"'{first['tenant_id']}' e '{second['tenant_id']}'"

    def test_07_reserved_slug_is_never_generated(self) -> str:
        """Um nome que vira 'default' nunca pode devolver 'default'.

        O tenant do piloto é semeado à mão pelas migrations; uma academia nova
        caindo nesse id herdaria em silêncio a configuração da Delariva.
        """
        generated: str = tenants.generate_tenant_id("Default")
        expect(generated != store.DEFAULT_TENANT_ID,
               f"generate_tenant_id devolveu o tenant reservado: {generated!r}")

        try:
            tenants.validate_explicit_tenant_id("default")
            raise AssertionError("--tenant-id default deveria ser recusado")
        except ValueError:
            pass

        # E um nome sem nada aproveitável não vira slug vazio.
        try:
            tenants.generate_tenant_id("🥋")
            raise AssertionError("um nome só de emoji deveria levantar ValueError")
        except ValueError:
            pass

        return f"'Default' virou '{generated}'; 'default' explícito recusado"

    def test_08_provisioning_is_idempotent(self) -> str:
        """O e-mail é a identidade do pedido: repetir não cria nada."""
        before = table_counts()

        repeated = provision.provision_tenant(
            academy_name="Outro Nome Qualquer",
            email=self.email_a,
            password=SUITE_PASSWORD,
        )

        expect(not repeated["created"], "a segunda chamada não deveria criar")
        expect_equal(repeated["tenant_id"], self.tenant_a,
                     "deveria reportar o tenant que a primeira chamada criou")
        expect_equal(table_counts(), before, "nenhuma tabela pode ter crescido")

        return "e-mail repetido: nada criado, tenant original reportado"

    def test_09_password_is_hashed(self) -> str:
        """A senha nunca vai para o banco em texto puro."""
        stored: str | None = read_password_hash(self.email_a)
        expect(stored is not None, "o usuário deveria existir")
        expect(stored != SUITE_PASSWORD, "A SENHA FOI GRAVADA EM TEXTO PURO")
        expect(SUITE_PASSWORD not in stored, "a senha aparece dentro do hash")
        expect(stored.startswith("scrypt:"),
               f"esperado hash scrypt do werkzeug, veio {stored[:20]!r} — "
               "se o Werkzeug mudou o algoritmo padrão, isto é uma falha esperada")

        expect(accounts_users.authenticate(self.email_a, SUITE_PASSWORD) is not None,
               "senha certa deveria autenticar")
        expect(accounts_users.authenticate(self.email_a, "senha-errada") is None,
               "senha errada não pode autenticar")
        expect(accounts_users.authenticate("nao-existe" + EMAIL_DOMAIN, SUITE_PASSWORD) is None,
               "e-mail inexistente não pode autenticar")

        # Normalização: o e-mail é comparado na forma canônica dos dois lados.
        messy: str = f"  {self.email_a.upper()}  "
        expect(accounts_users.authenticate(messy, SUITE_PASSWORD) is not None,
               "e-mail com maiúsculas e espaços deveria autenticar igual")

        return "hash scrypt · senha certa entra, errada não · e-mail normalizado nos dois lados"

    def test_10_login_route(self) -> str:
        """A rota de login valida contra users e devolve uma mensagem genérica."""
        client = self.application().test_client()

        page = client.get("/dashboard/login")
        expect_equal(page.status_code, 200, "GET do login")
        expect('type="email"' in page.get_data(as_text=True),
               "o formulário deveria ter campo de e-mail")

        ok = client.post("/dashboard/login",
                         data={"email": self.email_a, "password": SUITE_PASSWORD})
        expect_equal(ok.status_code, 302, "login válido deveria redirecionar")
        expect("/dashboard/menu" in ok.headers.get("Location", ""),
               "login válido deveria cair no menu")

        bad_client = self.application().test_client()
        bad = bad_client.post("/dashboard/login",
                              data={"email": self.email_a, "password": "errada"})
        body = bad.get_data(as_text=True)
        expect_equal(bad.status_code, 200, "login inválido re-renderiza a página")
        expect("E-mail ou senha incorretos." in body, "faltou a mensagem de erro")

        unknown = self.application().test_client().post(
            "/dashboard/login",
            data={"email": "ninguem" + EMAIL_DOMAIN, "password": SUITE_PASSWORD},
        )
        expect("E-mail ou senha incorretos." in unknown.get_data(as_text=True),
               "e-mail inexistente e senha errada precisam dar a MESMA mensagem")

        # Sessão de verdade: uma rota protegida abre depois do login.
        expect_equal(client.get("/dashboard/menu").status_code, 200,
                     "o menu deveria abrir com a sessão do login")

        return "login entra, erro é genérico nos dois casos, sessão abre o menu"

    def test_11_logout_ends_the_session(self) -> str:
        """Sair devolve o painel ao estado anônimo."""
        client = self.application().test_client()
        client.post("/dashboard/login",
                    data={"email": self.email_a, "password": SUITE_PASSWORD})
        expect_equal(client.get("/dashboard/menu").status_code, 200, "logado antes do logout")

        client.get("/dashboard/logout")
        after = client.get("/dashboard/menu")
        expect_equal(after.status_code, 302, "depois do logout o menu deveria redirecionar")
        expect("/dashboard/login" in after.headers.get("Location", ""),
               "deveria redirecionar para o login")

        return "logout encerra a sessão; o menu volta a redirecionar"

    def test_12_tenant_id_reaches_current_user(self) -> str:
        """A costura do S3b: current_user.tenant_id existe e é o tenant certo.

        É tudo que o S3a promete nessa direção. NENHUMA leitura filtra por esse
        valor ainda — isso é o S3b.
        """
        from flask_login import current_user, login_user
        from accounts.auth import User

        row = accounts_users.get_user_by_email(self.email_a)
        with self.application().test_request_context():
            login_user(User(row))
            expect(current_user.is_authenticated, "o usuário deveria estar autenticado")
            expect_equal(current_user.tenant_id, self.tenant_a,
                         "current_user.tenant_id deveria ser o tenant provisionado")
            expect_equal(current_user.email, self.email_a, "e-mail no current_user")

        return f"current_user.tenant_id == '{self.tenant_a}' (recebido, ainda não propagado)"

    def test_13_resolve_tenant_by_whatsapp_number(self) -> str:
        """O 'To' resolve o tenant, em qualquer formato — e degrada avisando."""
        number: str = "5528000777777"
        expect(store.update_whatsapp_number(number, tenant_id=self.tenant_a),
               "deveria gravar o número do tenant")

        for raw in (f"whatsapp:+{number}", f"+55 28000 77-7777", number):
            expect_equal(store.find_tenant_by_whatsapp_number(raw), self.tenant_a,
                         f"o formato {raw!r} deveria casar")

        expect(store.find_tenant_by_whatsapp_number("5529999999999") is None,
               "número desconhecido deveria devolver None, não um tenant")
        expect(store.find_tenant_by_whatsapp_number(None) is None, "None deveria devolver None")
        expect(store.find_tenant_by_whatsapp_number("abc") is None,
               "lixo deveria devolver None")

        # E o wrapper degrada para 'default' AVISANDO — o aviso é o contrato.
        with WarningCapture("integrations.store") as captured:
            resolved: str = store.resolve_tenant_by_whatsapp_number("5529999999999")
        expect_equal(resolved, store.DEFAULT_TENANT_ID, "desconhecido cai no tenant padrão")
        expect(any("No tenant registered" in message for message in captured.records),
               "a queda para o padrão precisa emitir WARNING")

        store.update_whatsapp_number(None, tenant_id=self.tenant_a)
        return "3 formatos casam · desconhecido → 'default' com WARNING"

    def test_14_sandbox_routing_is_unchanged(self) -> str:
        """Cenário do piloto HOJE: sem whatsapp_number, tudo é como antes do S3a.

        Se o piloto já tiver número próprio, este teste perde o sentido e é
        pulado em vez de falhar — o ambiente passou do sandbox.
        """
        if self.pilot_has_number:
            raise SkipTest("o tenant 'default' já tem whatsapp_number; o cenário sandbox acabou")

        owner_phone: str = "5528000444444"
        store.update_owner_phone(owner_phone, tenant_id=self.tenant_a)

        calls: dict[str, list] = {"owner": [], "lead": []}

        def fake_owner(phone: str, body: str, tenant_id: str = "default") -> None:
            calls["owner"].append((phone, tenant_id))

        def fake_lead(sender: str, body: str, tenant_id: str = "default") -> None:
            calls["lead"].append((sender, tenant_id))

        client = self.application().test_client()
        sandbox_number: str = "whatsapp:+14155238886"

        with patched(routes, "receive_twilio_owner", fake_owner), \
                patched(routes, "handle_text_message", fake_lead):
            client.post("/webhook", data={"From": f"whatsapp:+{owner_phone}",
                                          "Body": "1", "To": sandbox_number})
            client.post("/webhook", data={"From": f"whatsapp:+{self.next_sender()}",
                                          "Body": "oi", "To": sandbox_number})

        expect_equal(len(calls["owner"]), 1, "o dono deveria cair no handler do dono")
        expect_equal(len(calls["lead"]), 1, "o desconhecido deveria cair no handler do lead")
        expect_equal(calls["owner"][0][1], self.tenant_a,
                     "a varredura global ainda descobre o tenant pelo telefone do dono")
        expect_equal(calls["lead"][0][1], store.DEFAULT_TENANT_ID,
                     "sem 'To' registrado, o lead cai no tenant padrão")

        return "sandbox: dono → handler do dono, lead → tenant padrão (idêntico ao pré-S3a)"

    def test_15_cross_tenant_owner_is_a_lead(self) -> str:
        """ARMADILHA #2 — o teste mais importante da suíte.

        Com número próprio por academia, o dono da academia B escrevendo para o
        número da academia A NÃO pode ser tratado como dono. Se fosse, o "1"
        dele confirmaria uma aula da academia A. Uma varredura global por
        telefone faria exatamente isso.
        """
        tenant_b = self.provision("Suite S3A Bravo", "bravo")["tenant_id"]
        self.tenants.append(tenant_b) if tenant_b not in self.tenants else None

        number_a: str = "5528000555555"
        owner_a: str = "5528000666666"
        owner_b: str = "5528000888888"

        store.update_whatsapp_number(number_a, tenant_id=self.tenant_a)
        store.update_owner_phone(owner_a, tenant_id=self.tenant_a)
        store.update_owner_phone(owner_b, tenant_id=tenant_b)

        calls: dict[str, list] = {"owner": [], "lead": []}

        def fake_owner(phone: str, body: str, tenant_id: str = "default") -> None:
            calls["owner"].append((phone, tenant_id))

        def fake_lead(sender: str, body: str, tenant_id: str = "default") -> None:
            calls["lead"].append((sender, tenant_id))

        client = self.application().test_client()
        with patched(routes, "receive_twilio_owner", fake_owner), \
                patched(routes, "handle_text_message", fake_lead):
            # O dono de A escrevendo para o número de A: é dono.
            client.post("/webhook", data={"From": f"whatsapp:+{owner_a}",
                                          "Body": "1", "To": f"whatsapp:+{number_a}"})
            # O dono de B escrevendo para o número de A: é LEAD de A.
            client.post("/webhook", data={"From": f"whatsapp:+{owner_b}",
                                          "Body": "1", "To": f"whatsapp:+{number_a}"})

        expect_equal(len(calls["owner"]), 1,
                     "só o dono da própria academia pode cair no handler do dono")
        expect_equal(calls["owner"][0], (owner_a, self.tenant_a), "dono de A reconhecido em A")
        expect_equal(len(calls["lead"]), 1, "o dono de B deveria virar lead")
        expect_equal(calls["lead"][0], (owner_b, self.tenant_a),
                     "SEQUESTRO ENTRE TENANTS: o dono de B foi tratado como dono em A")

        store.update_whatsapp_number(None, tenant_id=self.tenant_a)
        return "dono de A → dono; dono de B no número de A → lead de A"

    def test_16_every_dashboard_route_requires_login(self) -> str:
        """Nenhuma rota de painel pode ficar sem autenticação.

        As rotas são DERIVADAS do url_map em vez de listadas à mão: uma lista
        literal envelhece, e uma rota nova sem decorator passaria despercebida.
        Derivando, a rota nova entra automaticamente neste teste e falha.
        """
        app = self.application()
        anonymous = app.test_client()

        # Login e logout são as duas exceções legítimas do blueprint.
        exempt: set[str] = {"dashboard.login", "dashboard.logout"}

        checked: int = 0
        for rule in app.url_map.iter_rules():
            if not rule.endpoint.startswith(("dashboard.", "integrations.")):
                continue
            if rule.endpoint in exempt:
                continue

            method: str = "POST" if "POST" in rule.methods else "GET"
            # Preenche <sender>, <marker>, <booking_id> com um valor qualquer:
            # a rota tem de redirecionar ANTES de olhar o parâmetro.
            path: str = rule.rule
            for argument in rule.arguments:
                path = path.replace(f"<{argument}>", "x").replace(f"<int:{argument}>", "1")

            response = anonymous.open(path, method=method)
            expect_equal(response.status_code, 302,
                         f"{method} {path} ({rule.endpoint}) deveria redirecionar")
            expect("/dashboard/login" in response.headers.get("Location", ""),
                   f"{method} {path} deveria redirecionar para o login")
            checked += 1

        expect(checked >= 24,
               f"esperava ao menos 24 rotas protegidas, encontrei {checked} — "
               "alguma rota sumiu ou deixou de estar no blueprint")

        return f"{checked} rotas de painel exigem sessão autenticada"

    def test_17_webhook_is_not_behind_login(self) -> str:
        """O webhook é chamado pelo Twilio, não por gente logada — e continua aberto.

        Pôr @require_auth aqui derrubaria o produto inteiro em silêncio: o
        Twilio receberia 302 e o lead nunca seria respondido.
        """
        anonymous = self.application().test_client()

        checks: list[tuple[str, str]] = [
            ("GET", "/status"),
            ("GET", "/webhook"),
            ("POST", "/webhook"),
            ("GET", "/dashboard/login"),
        ]
        for method, path in checks:
            response = anonymous.open(path, method=method)
            location: str = response.headers.get("Location", "")
            expect("/dashboard/login" not in location,
                   f"{method} {path} NÃO pode exigir login")

        # GET / redireciona para o menu, que aí sim manda para o login.
        root = anonymous.get("/")
        expect_equal(root.status_code, 302, "GET / redireciona")
        expect("/dashboard/menu" in root.headers.get("Location", ""),
               "GET / deveria apontar para o menu")

        return "webhook, status e login abertos; GET / segue apontando para o menu"

    def test_18_bootstrap_is_idempotent(self) -> str:
        """O primeiro usuário nasce uma vez só, e nunca ressuscita depois.

        A checagem count_users() é o que impede um restart de re-semear a senha
        do .env depois que o fundador já a trocou.
        """
        from accounts.bootstrap import bootstrap_first_user

        expect(accounts_users.count_users() > 0,
               "a suíte já criou usuários; o bootstrap tem de recusar")
        expect(not bootstrap_first_user(),
               "com usuários no banco, o bootstrap não pode criar nada")

        before = table_counts()["users"]
        bootstrap_first_user()
        expect_equal(table_counts()["users"], before, "o bootstrap não pode ter criado ninguém")

        return "com a tabela populada o bootstrap é no-op"

    def test_19_set_whatsapp_number_guards(self) -> str:
        """O número da academia não pode ser o telefone de um dono nem de um lead.

        Mesma família de guardas que o S1 pôs no owner_phone, e pela mesma razão:
        um número com dois papéis torna o roteamento ambíguo.
        """
        # Lido do banco, não fixo: os testes 14 e 15 já mexeram no owner_phone
        # deste tenant, e um número velho aqui passaria pela guarda sem exercê-la.
        owner_phone: str | None = read_owner_row(self.tenant_a)["owner_phone"]
        expect(owner_phone is not None,
               "o tenant de teste deveria ter owner_phone a esta altura")

        result: str = provision.set_whatsapp_number(self.tenant_a, owner_phone)
        expect("telefone pessoal do dono" in result,
               f"deveria recusar o telefone de um dono, veio: {result}")

        lead_sender: str = self.next_sender()
        import bot.session as session_store
        session_store.get_session(lead_sender)

        result = provision.set_whatsapp_number(self.tenant_a, lead_sender)
        expect("conversa de lead" in result,
               f"deveria recusar um número que já é lead, veio: {result}")

        expect(read_owner_row(self.tenant_a)["whatsapp_number"] is None,
               "nenhuma das recusas podia ter gravado")

        result = provision.set_whatsapp_number(self.tenant_a, "123")
        expect("inválido" in result, f"deveria recusar número curto, veio: {result}")

        return "3 recusas (dono, lead, formato) sem gravar nada"

    def test_20_teardown_restores_the_counts(self) -> str:
        """Depois da limpeza tem de sobrar só o que já existia — e o 'default'.

        Rodado antes do teardown de verdade: apaga os fixtures e confere as
        contagens, para uma sobra ser uma FALHA e não uma descoberta futura.
        """
        for tenant_id in list(self.tenants):
            drop_tenant(tenant_id)
        for email in list(self.emails):
            accounts_users.delete_user(email)

        after = table_counts()
        expect_equal(after, self.baseline,
                     "alguma linha de fixture sobrou depois da limpeza")

        pilot = read_owner_row(store.DEFAULT_TENANT_ID)
        expect(pilot is not None, "o tenant do piloto tem de continuar existindo")

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM owners WHERE tenant_id LIKE %s",
                            (TENANT_PREFIX + "%",))
                leftovers = int(cur.fetchone()["n"])
        expect_equal(leftovers, 0, "sobrou tenant de fixture")

        self.tenants.clear()
        self.emails.clear()
        return "contagens de volta ao ponto inicial; só o 'default' de pé"

    # -- teardown -----------------------------------------------------------

    def teardown(self) -> None:
        if self.keep:
            print("  --keep: tenants e usuários de teste preservados.")
            print(f"  ATENÇÃO: há tenants além do 'default' no banco. "
                  f"Apague-os antes de usar isto em produção.")
            return

        for tenant_id in list(self.tenants):
            drop_tenant(tenant_id)

        removed_users: int = 0
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Escopado ao domínio de fixture: nunca toca na conta do fundador.
                cur.execute("DELETE FROM users WHERE email LIKE %s", ("%" + EMAIL_DOMAIN,))
                removed_users = cur.rowcount
                cur.execute("DELETE FROM sessions WHERE sender LIKE %s", (SENDER_PREFIX + "%",))
                removed_sessions = cur.rowcount
            conn.commit()

        print(f"  limpeza: {len(self.tenants)} tenant(s), {removed_users} usuário(s) e "
              f"{removed_sessions} sessão(ões) de teste removidos.")


def _resolve_report_path(path: Path) -> Path:
    if path.is_dir() or path.suffix.lower() != ".json":
        path = path / f"accounts-{datetime.now():%Y%m%d_%H%M%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Suíte automatizada de contas e provisionamento (ACCOUNTS_TESTING.md).")
    parser.add_argument("--keep", action="store_true",
                        help="Não desfaz nada ao final (para depurar à mão).")
    parser.add_argument("--no-color", action="store_true", help="Saída sem cores ANSI.")
    parser.add_argument("--json", nargs="?", type=Path, const=DEFAULT_REPORT_DIR, default=None,
                        metavar="ARQUIVO",
                        help="Também grava o relatório em JSON (sozinha: tests/outputs/ com nome datado).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.ERROR, format="%(levelname)s - %(message)s")

    console = Console(color=not args.no_color and sys.stdout.isatty())
    report = Report(console)

    print(console.bold("\n═══ Corujai · suíte de contas e provisionamento ═══"))
    print(console.dim(" Roteiro: ACCOUNTS_TESTING.md"))

    # Uma run anterior interrompida pode ter deixado tenants de fixture no banco.
    _drop_orphan_fixtures()

    report.section("Pré-requisitos")
    suite = AccountsSuite(report, keep=args.keep)
    if not report.run("P1", "Migration 009 aplicada, coluna e índices no lugar",
                      suite.check_schema):
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
        ("1", "slugify_tenant_id é mecânico, sem lista de palavras genéricas",
         suite.test_01_slugify_rules),
        ("2", "provision_tenant semeia as cinco tabelas",
         suite.test_02_provision_seeds_all_five_tables),
        ("3", "A turma padrão existe no banco, não só em memória (Armadilha #4)",
         suite.test_03_fallback_row_is_real),
        ("4", "A tela de IA consegue salvar no tenant novo (armadilha do UPDATE)",
         suite.test_04_ai_config_row_is_updatable),
        ("5", "Os textos-guia batem com a migration 005",
         suite.test_05_ai_seed_matches_migration_005),
        ("6", "Colisão de slug recebe sufixo", suite.test_06_slug_collision_gets_suffix),
        ("7", "O identificador 'default' nunca é gerado nem aceito",
         suite.test_07_reserved_slug_is_never_generated),
        ("8", "Provisionar duas vezes com o mesmo e-mail não cria nada",
         suite.test_08_provisioning_is_idempotent),
        ("9", "A senha é gravada em hash, e o login confere",
         suite.test_09_password_is_hashed),
        ("10", "A rota de login valida contra users, com erro genérico",
         suite.test_10_login_route),
        ("11", "Logout encerra a sessão", suite.test_11_logout_ends_the_session),
        ("12", "current_user.tenant_id existe — a costura do S3b",
         suite.test_12_tenant_id_reaches_current_user),
        ("13", "O 'To' resolve o tenant e degrada para 'default' avisando",
         suite.test_13_resolve_tenant_by_whatsapp_number),
        ("14", "Sem whatsapp_number, o roteamento é idêntico ao pré-S3a",
         suite.test_14_sandbox_routing_is_unchanged),
        ("15", "O dono de outra academia é tratado como lead (Armadilha #2)",
         suite.test_15_cross_tenant_owner_is_a_lead),
        ("16", "Toda rota de painel exige login",
         suite.test_16_every_dashboard_route_requires_login),
        ("17", "O webhook NÃO exige login", suite.test_17_webhook_is_not_behind_login),
        ("18", "O bootstrap do primeiro usuário é idempotente",
         suite.test_18_bootstrap_is_idempotent),
        ("19", "O número da academia recusa telefone de dono e de lead",
         suite.test_19_set_whatsapp_number_guards),
        ("20", "A limpeza devolve o banco ao estado inicial",
         suite.test_20_teardown_restores_the_counts),
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
