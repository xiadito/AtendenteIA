"""Automated end-to-end suite for the settings screen (Module S1).

Runs the scenarios documented in SETTINGS_TESTING.md and prints a PASS/FAIL
report boiled down to an exit code, in the same style as
tests/test_confirmation/test_confirmation_suite.py.

Everything here is fully deterministic: no LLM, no WhatsApp, no Google Calendar.
The screen only reads and writes two Postgres rows, and this suite drives it
through the Flask test client exactly as a logged-in owner would.

DESTRUCTIVE, AND ON THE PILOT'S OWN ROWS. Unlike the other suites, which write
fixture rows under their own sender prefix, this one has to overwrite the single
real row of `ai_configs` and the single real `owners.owner_phone` — those tables
hold one row per tenant and the pilot is the only tenant. Both are snapshotted to
BACKUP_PATH before the first write and restored in teardown; a run that dies
mid-suite is repaired at the start of the next one. Never remove that backup.

Teardown also deletes the sessions rows this run created (prefix 5526000...,
messages cascade off them).

Run from src/:
    python tests/test_settings/test_settings_suite.py
    python tests/test_settings/test_settings_suite.py --keep
    python tests/test_settings/test_settings_suite.py --json

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

import bot.ai_configs as ai_configs  # noqa: E402
import bot.session as session_store  # noqa: E402
import integrations.store as store  # noqa: E402
from database.db import get_connection  # noqa: E402

# Leads this suite creates. Own prefix so teardown can scope its DELETEs and
# never touch a real lead or the other suites' senders (5521000...-5525000...).
SENDER_PREFIX = "5526000"

# Fixture number the suite writes into owners.owner_phone.
OWNER_PHONE_TEST = "5526099999999"

# Where the pilot's real ai_configs row and owner_phone are snapshotted before
# the suite overwrites them. Outside the repo so a crashed run never leaves
# fixture text in git.
BACKUP_PATH = Path("/tmp/corujai_settings_backup.json")

MIGRATION_SESSIONS = "001_create_sessions"
MIGRATION_OWNERS = "003_create_owners"
MIGRATION_AI_CONFIGS = "005_create_ai_configs"

# The five editable columns, in the order the form posts them.
AI_FIELDS: tuple[str, ...] = (
    "academy_name",
    "assistant_name",
    "tone",
    "business_info",
    "flow_emphasis",
)

DEFAULT_REPORT_DIR = SRC_DIR / "tests" / "outputs"


# ---------------------------------------------------------------------------
# Report / console output (same shape as the other suites)
# ---------------------------------------------------------------------------

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
            "step": step,
            "title": title,
            "status": status,
            "detail": detail,
            "traceback": trace,
            "seconds": round(time.monotonic() - started, 2),
        })
        self._print_line(step, title, status, detail)
        return status in ("PASS", "SKIP")

    def _print_line(self, step: str, title: str, status: str, detail: str) -> None:
        painter = {
            "PASS": self.console.green,
            "FAIL": self.console.red,
            "ERROR": self.console.red,
            "SKIP": self.console.yellow,
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
            print(self.console.green("\n Tela de configurações OK — todos os testes passaram.\n"))
        return not failed

    def to_json(self, path: Path) -> None:
        path.write_text(json.dumps({
            "passed": sum(1 for r in self.results if r["status"] == "PASS"),
            "failed": len(self.failed),
            "skipped": sum(1 for r in self.results if r["status"] == "SKIP"),
            "results": self.results,
        }, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Direct database reads (what the screen wrote, straight from the source)
# ---------------------------------------------------------------------------

def read_ai_config_row() -> dict:
    """Read the tenant's ai_configs row, including updated_at.

    get_ai_config() deliberately does not select updated_at, so the freshness
    assertions need their own query.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM ai_configs WHERE tenant_id = %s",
                (store.DEFAULT_TENANT_ID,),
            )
            row = cur.fetchone()
    expect(row is not None, "nenhuma linha em ai_configs para tenant_id='default'")
    return dict(row)


def read_owner_phone() -> str | None:
    owner = store.get_owner_for_notification()
    expect(owner is not None, "nenhuma linha em owners para tenant_id='default'")
    return owner["owner_phone"]


# ---------------------------------------------------------------------------
# Suite
# ---------------------------------------------------------------------------

class SettingsSuite:
    """Owns fixtures, tests and teardown for the settings screen."""

    def __init__(self, report: Report, keep: bool) -> None:
        self.report = report
        self.keep = keep
        self._n = 0
        self.app: Any = None
        self.client: Any = None
        self._backup: dict[str, Any] | None = None

    # -- infrastructure -----------------------------------------------------

    def next_sender(self) -> str:
        self._n += 1
        return f"{SENDER_PREFIX}{self._n:06d}"

    def _authenticated_client(self) -> Any:
        """Return a test client with the dashboard session already logged in.

        Every settings route sits behind @_require_auth, so without this each
        request would 302 to the login page and the tests would pass on a
        redirect that never reached the code under test.
        """
        if self.client is None:
            import app as flask_app

            self.app = flask_app.create_app()
            self.app.config["TESTING"] = True
            self.client = self.app.test_client()
            with self.client.session_transaction() as flask_session:
                flask_session["dashboard_authenticated"] = True
        return self.client

    def _post_ai(self, **overrides: str) -> Any:
        """POST the AI form with valid defaults, overridden field by field."""
        form: dict[str, str] = {
            "academy_name": "Academia da Suíte",
            "assistant_name": "Suitinha",
            "tone": "objetiva e simpática",
            "business_info": "Jiu-Jitsu e musculação, Itaipuaçu, aula experimental gratuita",
            "flow_emphasis": "agendar a experimental o quanto antes",
        }
        form.update(overrides)
        return self._authenticated_client().post("/dashboard/settings/ai", data=form)

    def _post_phone(self, owner_phone: str) -> Any:
        return self._authenticated_client().post(
            "/dashboard/settings/account", data={"owner_phone": owner_phone}
        )

    # -- prerequisites ------------------------------------------------------

    def check_schema(self) -> str:
        """Check every table this screen touches is in place."""
        required = (MIGRATION_SESSIONS, MIGRATION_OWNERS, MIGRATION_AI_CONFIGS)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT version FROM schema_migrations WHERE version = ANY(%s)",
                    (list(required),),
                )
                applied = {row["version"] for row in cur.fetchall()}

        missing = [version for version in required if version not in applied]
        expect(not missing,
               f"migrações não aplicadas: {', '.join(missing)} (suba a app para rodar init_db)")
        return "sessions, owners e ai_configs aplicadas"

    # -- fixtures -----------------------------------------------------------

    def prepare_fixtures(self) -> str:
        """Snapshot BOTH rows this suite overwrites, before touching either.

        This is the step that makes the suite safe to run against the pilot's
        real database: without it, running the tests would silently replace the
        gym's AI configuration with the fixture text above.
        """
        config = read_ai_config_row()
        self._backup = {
            "ai_config": {field: config[field] for field in AI_FIELDS},
            "ai_updated_at": config["updated_at"].isoformat(),
            "owner_phone": read_owner_phone(),
        }
        BACKUP_PATH.write_text(json.dumps(self._backup, ensure_ascii=False), encoding="utf-8")
        return f"ai_configs e owner_phone salvos em {BACKUP_PATH}"

    # -- tests --------------------------------------------------------------

    def test_01_update_ai_config_persists(self) -> str:
        """Os cinco campos são gravados e updated_at avança."""
        before = read_ai_config_row()

        updated: bool = ai_configs.update_ai_config(
            academy_name="Delariva Itaipuaçu",
            assistant_name="Corujinha",
            tone="acolhedora, trata o lead pelo nome",
            business_info="Jiu-Jitsu, CrossFit e musculação",
            flow_emphasis="fechar a aula experimental",
        )
        expect(updated, "update_ai_config deveria ter atualizado uma linha")

        after = read_ai_config_row()
        expect_equal(after["academy_name"], "Delariva Itaipuaçu", "academy_name")
        expect_equal(after["assistant_name"], "Corujinha", "assistant_name")
        expect_equal(after["tone"], "acolhedora, trata o lead pelo nome", "tone")
        expect_equal(after["business_info"], "Jiu-Jitsu, CrossFit e musculação", "business_info")
        expect_equal(after["flow_emphasis"], "fechar a aula experimental", "flow_emphasis")
        expect(after["updated_at"] > before["updated_at"],
               f"updated_at deveria ter avançado (era {before['updated_at']}, ficou {after['updated_at']})")

        return "os cinco campos gravados e updated_at avançou"

    def test_02_get_ai_config_sees_the_write(self) -> str:
        """A leitura seguinte enxerga o valor novo — prova de que não há cache.

        É o teste que sustenta a decisão de não invalidar nada ao salvar: se um
        dia alguém puser um cache em get_ai_config() sem dar à tela um jeito de
        limpá-lo, este teste falha, e é assim que o dono descobre antes do lead.
        """
        ai_configs.update_ai_config(
            academy_name="Primeira Escrita",
            assistant_name="A1",
            tone="t1",
            business_info="b1",
            flow_emphasis="f1",
        )
        expect_equal(ai_configs.get_ai_config()["academy_name"], "Primeira Escrita",
                     "get_ai_config após a primeira escrita")

        ai_configs.update_ai_config(
            academy_name="Segunda Escrita",
            assistant_name="A2",
            tone="t2",
            business_info="b2",
            flow_emphasis="f2",
        )
        config = ai_configs.get_ai_config()
        expect_equal(config["academy_name"], "Segunda Escrita",
                     "get_ai_config após a segunda escrita (cache serviria a primeira)")
        expect_equal(config["tone"], "t2", "tone na segunda leitura")

        return "duas escrituras seguidas, duas leituras corretas: sem cache no caminho"

    def test_03_normalize_owner_phone_table(self) -> str:
        """normalize_owner_phone é pura: uma tabela de casos cobre o contrato."""
        accepted: list[tuple[str, str]] = [
            ("5521999999999", "5521999999999"),
            ("whatsapp:+5521999999999", "5521999999999"),
            ("+55 21 99999-9999", "5521999999999"),
            ("(21) 99999-9999", "21999999999"),
            ("  5521999999999  ", "5521999999999"),
            ("whatsapp:+55 (21) 9.9999-9999", "5521999999999"),
        ]
        for raw, expected in accepted:
            expect_equal(store.normalize_owner_phone(raw), expected, f"normalizar {raw!r}")

        rejected: list[Any] = ["", None, "   ", "123", "abcdefghij", "55219999", "1" * 16]
        for raw in rejected:
            expect_equal(store.normalize_owner_phone(raw), None, f"recusar {raw!r}")

        return f"{len(accepted)} formatos normalizados, {len(rejected)} recusados"

    def test_04_saving_phone_normalizes_and_persists(self) -> str:
        """O POST grava no mesmo formato que o webhook compara."""
        response = self._post_phone(OWNER_PHONE_TEST)
        expect_equal(response.status_code, 200, "status do POST de telefone")
        expect_equal(read_owner_phone(), OWNER_PHONE_TEST, "owner_phone gravado")

        # get_owner_by_phone é exatamente o que o webhook chama a cada mensagem:
        # se o formato gravado divergir, ela devolve None e o dono deixa de ser
        # reconhecido, sem erro nenhum em lugar nenhum.
        owner = store.get_owner_by_phone(OWNER_PHONE_TEST)
        expect(owner is not None,
               "get_owner_by_phone não achou o número recém-salvo — o roteamento do dono quebraria")

        return "número salvo e reconhecido por get_owner_by_phone"

    def test_05_dirty_input_is_stored_clean(self) -> str:
        """Formato sujo digitado na tela chega limpo ao banco."""
        response = self._post_phone("whatsapp:+55 26 09999-8888")
        expect_equal(response.status_code, 200, "status do POST de telefone sujo")
        expect_equal(read_owner_phone(), "5526099998888", "owner_phone normalizado")
        expect(store.get_owner_by_phone("5526099998888") is not None,
               "o número sujo, depois de limpo, precisa ser encontrável pelo webhook")

        return "'whatsapp:+55 26 09999-8888' virou '5526099998888'"

    def test_06_guard_rejects_a_lead_number(self) -> str:
        """Guarda (b): um número que já é lead não pode virar o número do dono.

        É a guarda mais importante da tela. Se passasse, o webhook passaria a
        rotear as mensagens daquele lead para receive_twilio_owner(), e o "1" que
        a pessoa mandasse numa conversa comum viraria uma confirmação de aula.
        """
        lead_sender = self.next_sender()
        session_store.get_session(lead_sender)  # cria a linha em sessions

        before = read_owner_phone()
        response = self._post_phone(lead_sender)
        body = response.get_data(as_text=True)

        expect_equal(response.status_code, 200, "status do POST recusado")
        expect("já está em uso" in body,
               "a tela deveria explicar que o número já pertence a uma conversa")
        expect_equal(read_owner_phone(), before, "o owner_phone não podia ter mudado")

        return "número de lead recusado; owner_phone intacto"

    def test_07_invalid_phone_is_refused(self) -> str:
        """Entrada vazia ou curta demais é recusada sem gravar."""
        before = read_owner_phone()

        for raw in ("", "   ", "123", "não é telefone"):
            response = self._post_phone(raw)
            expect_equal(response.status_code, 200, f"status do POST com {raw!r}")
            expect("Número inválido" in response.get_data(as_text=True),
                   f"a tela deveria recusar {raw!r} com aviso de número inválido")
            expect_equal(read_owner_phone(), before, f"o banco não podia mudar com {raw!r}")

        return "quatro entradas inválidas recusadas; owner_phone intacto"

    def test_08_empty_ai_field_is_refused(self) -> str:
        """Campo vazio da IA é recusado, e o que foi digitado volta na tela.

        Devolver o texto digitado importa: recarregar do banco apagaria a edição
        do dono e ele teria de reescrever tudo por causa de um campo em branco.
        """
        before = read_ai_config_row()

        response = self._post_ai(tone="   ")
        body = response.get_data(as_text=True)

        expect_equal(response.status_code, 200, "status do POST recusado")
        expect("Preencha todos os campos" in body, "a tela deveria pedir os campos faltantes")
        expect("Academia da Suíte" in body,
               "o valor digitado deveria voltar preenchido, não o do banco")

        after = read_ai_config_row()
        for field in AI_FIELDS:
            expect_equal(after[field], before[field], f"{field} não podia ter mudado")

        return "campo vazio recusado; banco intacto e texto digitado devolvido"

    def test_09_get_shows_current_state(self) -> str:
        """A tela mostra o que está gravado, nas duas seções."""
        ai_configs.update_ai_config(
            academy_name="Academia Visível",
            assistant_name="Atendente Visível",
            tone="tom visível",
            business_info="info visível",
            flow_emphasis="ênfase visível",
        )
        self._post_phone(OWNER_PHONE_TEST)

        response = self._authenticated_client().get("/dashboard/settings")
        body = response.get_data(as_text=True)

        expect_equal(response.status_code, 200, "status do GET")
        for expected in ("Academia Visível", "Atendente Visível", "tom visível",
                         "info visível", "ênfase visível", OWNER_PHONE_TEST):
            expect(expected in body, f"a tela deveria mostrar {expected!r}")

        # As duas seções são formulários independentes — se virarem um só, salvar
        # o telefone passa a reescrever a config da IA junto.
        expect('action="/dashboard/settings/ai"' in body, "formulário da IA ausente")
        expect('action="/dashboard/settings/account"' in body, "formulário da conta ausente")

        return "as duas seções mostram o estado atual, cada uma com seu próprio POST"

    def test_10_routes_require_auth(self) -> str:
        """As três rotas novas não podem ficar de fora do @_require_auth."""
        if self.app is None:
            self._authenticated_client()
        anonymous = self.app.test_client()

        paths = [
            ("get", "/dashboard/settings"),
            ("post", "/dashboard/settings/ai"),
            ("post", "/dashboard/settings/account"),
        ]
        for method, path in paths:
            response = getattr(anonymous, method)(path)
            expect_equal(response.status_code, 302, f"{method.upper()} {path} deveria redirecionar")
            expect("/dashboard/login" in response.headers.get("Location", ""),
                   f"{method.upper()} {path} deveria redirecionar para o login")

        return "as três rotas de configurações exigem sessão do painel"

    # -- teardown -----------------------------------------------------------

    def teardown(self) -> None:
        if self.keep:
            print("  --keep: config da IA, owner_phone e sessões de teste preservados.")
            print(f"  ATENÇÃO: a config real do piloto continua sobrescrita. Backup em {BACKUP_PATH}.")
            return

        with get_connection() as conn:
            with conn.cursor() as cur:
                # As mensagens somem junto, por ON DELETE CASCADE em messages.sender.
                cur.execute("DELETE FROM sessions WHERE sender LIKE %s", (SENDER_PREFIX + "%",))
                removed_sessions = cur.rowcount
            conn.commit()

        restored = _restore_backup(self._backup)
        BACKUP_PATH.unlink(missing_ok=True)

        print(f"  limpeza: {removed_sessions} sessão(ões) de teste removida(s); "
              f"{'config da IA e owner_phone originais restaurados' if restored else 'nada a restaurar'}.")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _resolve_report_path(path: Path) -> Path:
    if path.is_dir() or path.suffix.lower() != ".json":
        path = path / f"settings-{datetime.now():%Y%m%d_%H%M%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _restore_backup(backup: dict[str, Any] | None) -> bool:
    """Put the pilot's real ai_configs row and owner_phone back.

    updated_at is restored too, so a suite run leaves no trace at all — an owner
    reading the screen afterwards should not see a save they never made.

    Args:
        backup (dict[str, Any] | None): Snapshot from prepare_fixtures, or None
            if the suite never got that far.

    Returns:
        bool: True if anything was written back.
    """
    if not backup:
        return False

    config: dict[str, str] = backup["ai_config"]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ai_configs
                SET academy_name = %s,
                    assistant_name = %s,
                    tone = %s,
                    business_info = %s,
                    flow_emphasis = %s,
                    updated_at = %s
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

    return True


def _restore_orphan_backup() -> None:
    """Repair the tenant rows if a previous run died before its teardown."""
    if not BACKUP_PATH.exists():
        return

    backup = json.loads(BACKUP_PATH.read_text(encoding="utf-8"))
    _restore_backup(backup)
    BACKUP_PATH.unlink(missing_ok=True)
    print(f"  config da IA e owner_phone restaurados a partir de {BACKUP_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Suíte automatizada da tela de configurações (SETTINGS_TESTING.md).")
    parser.add_argument("--keep", action="store_true", help="Não desfaz nada ao final (para depurar à mão).")
    parser.add_argument("--no-color", action="store_true", help="Saída sem cores ANSI.")
    parser.add_argument("--json", nargs="?", type=Path, const=DEFAULT_REPORT_DIR, default=None,
                        metavar="ARQUIVO",
                        help="Também grava o relatório em JSON (sozinha: tests/outputs/ com nome datado).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.ERROR, format="%(levelname)s - %(message)s")

    console = Console(color=not args.no_color and sys.stdout.isatty())
    report = Report(console)

    print(console.bold("\n═══ Corujai · suíte da tela de configurações ═══"))
    print(console.dim(" Roteiro: SETTINGS_TESTING.md"))

    # A crashed previous run may have left fixture text in the tenant's rows.
    _restore_orphan_backup()

    report.section("Pré-requisitos")
    suite = SettingsSuite(report, keep=args.keep)
    if not report.run("P1", "Schema de sessions, owners e ai_configs aplicado", suite.check_schema):
        print(console.red("\n Pré-requisitos falharam — a suíte não pode continuar."))
        report.summary()
        sys.exit(1)

    report.section("Preparo")
    atexit.register(suite.teardown)
    if not report.run("F1", "Config da IA e owner_phone reais salvos para restauração",
                      suite.prepare_fixtures):
        print(console.red("\n Sem backup a suíte não pode rodar: ela sobrescreve os dados reais."))
        atexit.unregister(suite.teardown)
        report.summary()
        sys.exit(1)

    report.section("Roteiro de testes")
    tests: list[tuple[str, str, Callable[[], str | None]]] = [
        ("1", "update_ai_config grava os cinco campos e toca updated_at",
         suite.test_01_update_ai_config_persists),
        ("2", "get_ai_config enxerga a escrita seguinte (sem cache)",
         suite.test_02_get_ai_config_sees_the_write),
        ("3", "normalize_owner_phone aceita e recusa o que deve",
         suite.test_03_normalize_owner_phone_table),
        ("4", "Salvar telefone grava no formato que o webhook compara",
         suite.test_04_saving_phone_normalizes_and_persists),
        ("5", "Telefone com formato sujo chega limpo ao banco",
         suite.test_05_dirty_input_is_stored_clean),
        ("6", "Guarda (b): número de lead é recusado como número do dono",
         suite.test_06_guard_rejects_a_lead_number),
        ("7", "Telefone inválido é recusado sem gravar",
         suite.test_07_invalid_phone_is_refused),
        ("8", "Campo vazio da IA é recusado e devolve o que foi digitado",
         suite.test_08_empty_ai_field_is_refused),
        ("9", "A tela mostra o estado atual das duas seções",
         suite.test_09_get_shows_current_state),
        ("10", "As rotas de configurações exigem autenticação",
         suite.test_10_routes_require_auth),
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
