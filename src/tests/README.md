# `src/tests/` — o mapa dos testes do Corujai

Onde estão os testes, o que cada um cobre, e como rodar. Se você só quer saber **qual comando
digitar**, pule para [Rodando tudo](#rodando-tudo).

---

## Como os testes deste projeto funcionam

**Não há pytest, nem unittest, nem `conftest.py`.** Cada módulo do produto tem uma pasta em
`src/tests/` com **três arquivos**, sempre com os mesmos nomes:

| Arquivo | O que é |
|---|---|
| `<modulo>_suite.py` | A **suíte automatizada**. Roda todos os cenários, imprime um relatório PASS/FAIL e sai com código `0` só se tudo passou. É o que você roda. |
| `<modulo>.py` | A **CLI manual**. Uma operação por chamada, para você olhar o banco no DBeaver entre um comando e outro. É o que você usa quando algo falhou e você quer entender por quê. |
| `*_TESTING.md` | O **roteiro**. Descreve cada cenário em português, com o que provaria o contrário. É a documentação de verdade do módulo — a suíte é a execução dele. |

Cada suíte é um script solto, executável direto. Elas localizam a pasta `src/` **pelo nome**
(`next(p for p in ... if p.name == "src")`), nunca contando `.parent` — então mover uma pasta de
teste não quebra o import.

> **Sempre rode de dentro de `src/`.** Todos os imports do projeto são relativos a essa pasta
> (`from config import Config`), como no Railway.

### Os dois requisitos

1. **Virtualenv ativo** — `source venv/bin/activate` na raiz do repositório.
2. **Postgres de pé, com as migrations aplicadas.** As suítes não criam banco nem rodam
   migration: elas leem o banco em `DATABASE_URL`. Se `schema_migrations` estiver desatualizada,
   o passo `P1` de cada suíte falha e ela para ali, de propósito. Suba a app uma vez
   (`python app.py`) para aplicar as migrations.

---

## Rodando tudo

Da pasta `src/`, com o venv ativo:

```bash
python tests/test_scheduling/test_scheduling_suite.py            # ⚠️ escreve no Google Calendar real
python tests/test_ai_action/test_ai_action_suite.py --skip-live
python tests/test_owner_notifications/test_owner_notifications_suite.py
python tests/test_inbox/test_inbox_suite.py
python tests/test_confirmation/test_confirmation_suite.py
python tests/test_settings/test_settings_suite.py
python tests/test_class_types/test_class_types_suite.py
python tests/test_accounts/test_accounts_suite.py
python tests/test_signup/test_signup_suite.py
python tests/test_tenant_isolation/test_tenant_isolation_suite.py
python tests/test_metrics/test_metrics_suite.py
```

Ou, para rodar as **dez determinísticas** de uma vez e ver só o placar de cada uma:

```bash
for s in tests/test_*/test_*_suite.py; do
  case "$s" in *test_scheduling*) continue;; esac          # essa mexe no Calendar real
  args=""; case "$s" in *test_ai_action*) args="--skip-live";; esac
  printf '%-52s ' "$s"
  python "$s" $args --no-color 2>&1 | grep -E "^ [0-9]+ testes" || echo "ERRO"
done
```

**A saída esperada hoje é 171 testes, 0 falhas, 3 pulados** (os pulados são do `test_ai_action`
com `--skip-live`, que são cenários que só rodam contra o Calendar de verdade).

### As flags

Todas as suítes aceitam as três primeiras:

| Flag | O que faz |
|---|---|
| `--keep` | **Não limpa nada no fim.** Para você abrir o DBeaver e olhar o que ficou. Cuidado nas suítes marcadas ⚠️ abaixo: `--keep` deixa a linha real do piloto sobrescrita. |
| `--no-color` | Sem ANSI. Use ao redirecionar para arquivo ou canalizar para `grep`. |
| `--json [caminho]` | Também grava o relatório em JSON. Sem argumento, cai em `tests/outputs/` com nome datado. |
| `--skip-live` | Só `test_ai_action`. Pula os cenários que escrevem no Google Calendar. |
| `--reset-bookings` | Só `test_scheduling`. Apaga reservas de teste antigas antes de começar. |
| `--skip-token-test` | Só `test_scheduling`. Pula o cenário que estraga o `refresh_token` de propósito. |

---

## As onze suítes

| Pasta | Módulo | Cobre | Rede? |
|---|---|---|---|
| `test_scheduling/` | 2 | Motor de agendamento: leitura de horários livres do Calendar e escrita da reserva no Postgres, com o advisory lock | ⚠️ **Google Calendar real** |
| `test_ai_action/` | 3 | A camada de ação da IA: o bloco `<corujai_action>`, o parser tolerante, a aplicação de estado e o `book`/`handoff` | LLM dublado; Calendar só sem `--skip-live` |
| `test_owner_notifications/` | — | A fila `owner_notifications`: enfileirar, o dreno do cron com retentativas, e o registro do `1`/`2` do dono | Não |
| `test_inbox/` | 5 | Inbox do operador e o takeover: pausa, gravação durante a pausa, resposta pelo painel e a devolução para a IA | Não |
| `test_confirmation/` | 6 | O dono decidindo se a aula acontece, pelos dois canais (WhatsApp e painel), e a guarda de transição | Não |
| `test_settings/` | S1 + S3d | A tela `/dashboard/settings`: a camada da IA, o telefone do dono e o número da academia | Não |
| `test_class_types/` | S2 | Tipos de aula e capacidade por academia, o `days_ahead`, e o invariante da turma padrão | Não |
| `test_accounts/` | S3a | Contas, login, geração de slug e `provision_tenant()` numa transação só | Não |
| `test_signup/` | S3c | Cadastro público, honeypot, teto por IP, a checklist de onboarding — e o **CSRF ligado** | Não |
| `test_tenant_isolation/` | S3b + S3d | Nenhuma leitura vaza entre academias, e desde o S3d nenhum **envio** também | Não |
| `test_metrics/` | S4 | O funil de `/dashboard/metrics`: as quatro contagens, as taxas e as janelas de 7/30/90 dias | Não |

Há ainda `GOOGLE_CALENDAR_OAUTH_TESTING.md` na raiz desta pasta: o roteiro do **Módulo 1**
(o fluxo OAuth). Ele é **manual e só manual** — não tem suíte, porque o fluxo passa pelo
navegador e pela tela de consentimento do Google, que não dá para automatizar aqui.

---

## ⚠️ O que as suítes escrevem no banco

Elas rodam contra o **Postgres de verdade** apontado por `DATABASE_URL`. Não há banco de teste
separado. O que as mantém seguras são duas convenções:

### 1. Cada suíte tem um prefixo de telefone só dela

Assim um teardown nunca apaga o lead de outra suíte — nem um lead real.

| Prefixo | Suíte | | Prefixo | Suíte |
|---|---|---|---|---|
| `5521000…` | scheduling | | `5527000…` | class types |
| `5522000…` | ai action | | `5528000…` | accounts |
| `5523000…` | owner notifications | | `5529000…` | signup |
| `5524000…` | inbox | | `5530000…` | tenant isolation |
| `5525000…` | confirmation | | `5531000…` | metrics |
| `5526000…` | settings | | | |

`test_tenant_isolation` e `test_metrics` também têm **um domínio de e-mail cada**
(`@suite-s3b.corujai.test` e `@suite-s4.corujai.test`), que de propósito **não** casam com o
`%@suite.corujai.test` que o `test_accounts` apaga no teardown dele.

### 2. Quem escreve na linha real do piloto faz backup em `/tmp`

A maioria das suítes cria academias-fixture próprias. Quatro não podem, porque a tabela que elas
exercitam tem **uma linha por academia** e o piloto é a única. Essas fazem snapshot antes da
primeira escrita, restauram no teardown, e reparam um backup órfão no começo da execução
seguinte:

| Suíte | Arquivo | Restaura |
|---|---|---|
| `test_settings` | `/tmp/corujai_settings_backup.json` | `ai_configs` (com `updated_at`), `owners.owner_phone` e `owners.whatsapp_number` |
| `test_class_types` | `/tmp/corujai_class_types_backup.json` | `class_types` e `scheduling_configs` |
| `test_confirmation` | `/tmp/corujai_confirmation_owner_backup.json` | `owners.owner_phone` |
| `test_owner_notifications` | `/tmp/corujai_owner_notifications_owner_backup.json` | `owners.owner_phone` |
| `test_scheduling` | `/tmp/corujai_owners_backup.json` | `owners.refresh_token` (o cenário que o invalida de propósito) |

> **Nunca remova o passo de backup, e nunca rode essas com `--keep` num banco que te importa.**
> `test_settings` é a mais perigosa das cinco: ela escreve em `owners.whatsapp_number`, que roteia
> o WhatsApp da academia nas duas direções, e uma sobra ali não aparece em tela nem em log —
> porque um número gravado é exatamente o estado normal de uma academia em produção.

`test_accounts` e `test_signup` **não têm backup, de propósito**: elas nunca tocam nas linhas do
piloto, e a reparação pós-crash é feita por `_drop_orphan_fixtures()`, chamado na largada.

---

## Três armadilhas ao escrever ou alterar um teste

**1. O CSRF não desliga sozinho com `TESTING = True`.** O Flask-WTF olha `WTF_CSRF_ENABLED`, e
sem essa linha ao lado do `TESTING` todo POST volta 400 sem chegar no código sob teste:

```python
app.config["TESTING"] = True
app.config["WTF_CSRF_ENABLED"] = False
```

`test_signup` é a **única** suíte que roda algo com o CSRF **ligado** — é a única cobertura que
essa fiação tem, incluindo o `csrf.exempt(webhook_bp)` sem o qual o Twilio para de ser atendido
em silêncio.

**2. Um dublê não é salvo pelo default da função real.** Quem passa o argumento é quem *chama*,
então acrescentar um parâmetro a uma função dublada quebra o dublê com `TypeError` — e esse
`TypeError` costuma cair num `try/except` do produto, virando um teste que falha longe da causa.
Já aconteceu três vezes neste projeto (S3a, S3b e S3d). Ao mexer numa assinatura, procure os
dublês antes:

```bash
grep -rn "nome_da_funcao" tests/ | grep -E "lambda|def |patched\("
```

**3. Login de verdade, sempre.** Desde o Módulo S3a `session["dashboard_authenticated"] = True`
não autentica nada. Toda suíte que dirige o painel cria a própria linha em `users` e entra pelo
`POST /dashboard/login` real.

---

## Quando algo falha

1. **Leia o relatório.** Cada linha `✖` traz o passo, o título e a asserção que quebrou. Um `○`
   é um skip e não reprova a execução.
2. **Abra o roteiro do módulo** (`*_TESTING.md` da pasta). Cada cenário tem uma coluna dizendo
   *o que provaria o contrário* — normalmente é isso que acabou de acontecer.
3. **Reproduza à mão com a CLI da pasta.** Elas fazem uma operação por chamada:

   ```bash
   python tests/test_ai_action/test_ai_action.py --help
   python tests/test_inbox/test_inbox.py --help
   ```

4. **Olhe as linhas no DBeaver.** As queries que valem a pena estão na seção
   *"Inspecting the database with DBeaver"* do `CLAUDE.md`. Desde o Módulo S3b, **sempre
   selecione `tenant_id`**: uma consulta só por `sender` pode devolver a conversa de duas pessoas
   diferentes lado a lado e parecer uma só.
5. **Se a suíte morreu no meio**, ela não chegou ao teardown. Rode de novo: a execução seguinte
   repara o backup órfão sozinha, na largada.
