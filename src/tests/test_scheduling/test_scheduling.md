# `src/tests/test_scheduling/` — ferramentas de teste do motor de agendamento

Dois scripts que exercitam o motor do Módulo 2 (`bot/scheduling.py` + `bot/bookings.py`)
sem passar pela IA nem pelo WhatsApp. Nenhum dos dois é importado pela aplicação — são
ferramentas de linha de comando, rodadas à mão.

| Script | Para quê |
|---|---|
| `test_scheduling.py` | CLI manual: uma chamada por vez, você lê a saída e julga |
| `test_scheduling_suite.py` | Suíte automatizada: roda o roteiro inteiro e devolve um *exit code* |

Os dois localizam `src/` **pelo nome** (subindo por `Path(__file__).parents` até achar o
diretório chamado `src`) e o inserem no `sys.path`, para importar `bot`, `config`,
`integrations` e `database` do mesmo jeito que o `app.py` faz.

Como essa resolução é absoluta — e o `load_dotenv()` do `config.py` também procura o `.env`
a partir do próprio arquivo, não do diretório atual — **os scripts rodam de qualquer lugar**:

```bash
source venv/bin/activate
cd src
python tests/test_scheduling/test_scheduling.py list          # o caminho dos exemplos
```

```bash
python src/tests/test_scheduling/test_scheduling.py list      # da raiz, funciona igual
```

Os exemplos deste documento assumem `cd src` só por serem mais curtos, e para bater com o
`SCHEDULING_ENGINE_TESTING.md`.

> **Aviso:** os dois escrevem no Google Calendar real e no Postgres apontado por
> `DATABASE_URL`. Não aponte para produção.

## Pré-requisitos

Ambos falham cedo e com mensagem limpa se faltar algo:

1. **Integração Google Calendar conectada** (Módulo 1) — `owners.integration_status = 'connected'`
   e `owners.calendar_id` preenchido. Roteiro em `GOOGLE_CALENDAR_OAUTH_TESTING.md`.
2. **Migration `004_create_trial_bookings` aplicada** — suba o app uma vez (`init_db()` roda
   as migrations pendentes) ou `python -c "from database.db import init_db; init_db()"`.

---

## `test_scheduling.py` — CLI manual

Exercita o motor uma chamada por vez. É o que o `SCHEDULING_ENGINE_TESTING.md` usa nos
passos do roteiro.

### `list` — quais vagas existem

```bash
python tests/test_scheduling/test_scheduling.py list
python tests/test_scheduling/test_scheduling.py list --days 60
```

Chama `get_available_slots(days_ahead=...)` e imprime uma linha por vaga:

```
<event_id>  |  Quinta-feira, 23/07 às 07:00 — Adultos  |  vagas restantes: ilimitado
```

`--days` controla a janela (padrão **14**, o mesmo default de `get_available_slots()`, para
que o CLI mostre exatamente o que a IA verá no Módulo 3). Não há teto — 60, 90, 365 funcionam.

Se não houver nenhuma vaga: `Nenhuma vaga disponível.`, sem erro.

O `event_id` da primeira coluna é o que você passa para o `book`.

### `book` — reservar uma vaga

```bash
python tests/test_scheduling/test_scheduling.py book <event_id> --sender 5521999999999 --name "Ana"
```

Chama `book_slot()` e imprime o dicionário de retorno cru:

| Retorno | Significado |
|---|---|
| `{'status': 'created', 'booking_id': ..., 'active_count': N, 'calendar_synced': True}` | Reservado, e o evento no Calendar foi atualizado |
| `{'status': 'created', ..., 'calendar_synced': False}` | Reservado no Postgres, mas o patch no Calendar falhou. A reserva **vale** — só o evento está dessincronizado |
| `{'status': 'full', 'active_count': N}` | Lotado. Nenhuma chamada ao Google foi feita |
| `{'status': 'duplicate'}` | Este `sender` já tem reserva ativa neste evento |
| `{'status': 'integration_not_connected'}` / `{'status': 'needs_reconnect'}` | Problema de integração |

**`book` não tem `--days` e não precisa.** A janela só existe na *descoberta* de vagas;
`book_slot()` recebe um `event_id` e vai direto nele. Um `event_id` de daqui a 80 dias
funciona mesmo que um `list` sem flag não o mostre mais.

### `cleanup` — zerar o ambiente de teste

```bash
python tests/test_scheduling/test_scheduling.py cleanup --dry-run   # só lista, não apaga
python tests/test_scheduling/test_scheduling.py cleanup             # lista e pede confirmação
python tests/test_scheduling/test_scheduling.py cleanup --yes       # sem confirmação (scripts/CI)
```

Apaga **todos** os eventos do calendário "Aulas Experimentais" — não importa se vieram do
Apps Script, da suíte ou da sua mão — mais as linhas de `trial_bookings` que apontavam para
eles. Devolve o ambiente ao estado "calendário vazio" que o passo 1 do roteiro assume.

| Flag | Padrão | O que faz |
|---|---|---|
| `--days-back` | 365 | Quantos dias para trás varrer |
| `--days-ahead` | 365 | Quantos dias para frente varrer |
| `--dry-run` | — | Só lista o que seria apagado |
| `--yes` | — | Pula a confirmação interativa |

Sem `--yes`, ele lista tudo e exige que você digite `sim`. Sem terminal interativo (pipe,
CI), ele se recusa a apagar e sai com código 1 — `--yes` é obrigatório nesse caso.

Isso só é seguro porque **hoje todo evento naquele calendário é descartável**: o Módulo 3
ainda não ligou a IA ao motor, então nenhum lead real jamais reservou por ali. Quando o
Módulo 3 entrar, este comando precisa de um filtro (ou de aposentadoria).

---

## `test_scheduling_suite.py` — suíte automatizada

Executa todos os cenários do `SCHEDULING_ENGINE_TESTING.md` e reduz o roteiro a um
*exit code*: **0 só se tudo passou** (testes pulados não reprovam a execução).

```bash
python tests/test_scheduling/test_scheduling_suite.py
```

Pelo VSCode: `Ctrl+Shift+P` → **Tasks: Run Test Task** (tarefas definidas em
`.vscode/tasks.json`, já com o `cwd` e o Python do venv corretos).

### Fases da execução

```
1. Pré-requisitos   P1 integração conectada · P2 migration 004 aplicada
2. Preparo          F1 localiza/cria os 7 fixtures · F2 exige ledger de reservas limpo
3. Roteiro          14 testes, cada um imprime ✔ PASS / ✖ FAIL / ○ SKIP
4. Limpeza          desfaz tudo (registrado em atexit desde a fase 2)
```

Se a fase 1 ou 2 falhar, ele para ali — não adianta rodar 14 testes num ambiente errado.

### Fixtures: reaproveitados, não recriados

A suíte lê o calendário e classifica o que já existe em **sete papéis**. Só cria os que
estiverem genuinamente faltando.

| Papel | Como é reconhecido | Usado por |
|---|---|---|
| `baby` | marcador `[BABY]`, futuro | passos 3–6 |
| `criancas` | marcador `[CRIANCAS]`, futuro | passo 7 |
| `adultos` | marcador `[ADULTOS]`, futuro | passos 8, 13 |
| `sem_marcador` | título sem marcador reconhecível | passo 9 |
| `dia_inteiro` | tem `start.date` em vez de `start.dateTime` | passo 10 |
| `recorrente` | tem `recurrence` (é a série mestra) | passo 11 |
| `passada` | começa no passado, com marcador válido | passo 10 |

A classificação é por **forma**, não por título exato: pergunta se o evento é recorrente,
de dia inteiro, no passado, e só então olha o marcador — usando o mesmo
`_TITLE_MARKER_PATTERN` do motor. Assim eventos criados à mão, pelo Apps Script ou por uma
execução anterior classificam igual, independente de redação, acento ou sufixo.

Eventos criados pela suíte levam o sufixo `~ SUITE AUTOMATIZADA` no título, para quem
esbarrar neles na interface do Calendar saber que são descartáveis.

**Instâncias de série nunca ocupam papel.** Uma instância carrega `recurringEventId`; a
mestra não. Sem essa distinção, uma instância recorrente pode ocupar o papel `adultos` e
dois testes acabam escrevendo no mesmo `event_id`.

### Os 14 testes

| # | Afirma | Passo do roteiro |
|---|---|---|
| 1 | Janela de largura zero → `[]` sem exceção | 1 |
| 2 | baby=2, crianças=4, adultos=ilimitado, sem-marcador→adultos | 2 |
| 3 | 1ª reserva: slot continua listado com 1 vaga + linha no Postgres | 3 |
| 4 | 2ª reserva lota → slot some da lista | 4 |
| 5 | 3ª reserva → `status='full'`, contagem intacta | 5 |
| 6 | **Corrida:** 2 threads na última vaga → 1 `created` + 1 `full`, nunca 3 reservas | 6 |
| 7 | Mesmo sender 2× no mesmo slot → `duplicate` | 7 |
| 8 | 5 reservas em `[ADULTOS]` e o slot segue ilimitado | 8 |
| 9 | Título sem marcador → Adultos + WARNING no log | 9 |
| 10 | Dia inteiro e evento passado nunca aparecem | 10 |
| 11 | Recorrência expande em instâncias com capacidade independente | 11 |
| 12 | `invalid_grant` → `IntegrationNeedsReconnectError` + `needs_reconnect` | 12 |
| 13 | `disconnected` → erro limpo, **sem chamar o Google** | 13 |
| 14 | Descrição do evento recebe *append* + `corujai_booked_count` | extra |

O **teste 6 é o mais valioso**: é o único que não dá para verificar olhando. Prova que o
`pg_advisory_xact_lock` em `bookings.py::create_booking_with_lock()` impede que dois leads
ocupem a mesma última vaga. Os outros 13 você conferiria à mão em 20 minutos; esse não.

O **teste 1** usa `days_ahead=0`. `get_available_slots()` calcula `timeMin` e `timeMax` a
partir do mesmo `now`, então a janela tem largura zero e o Google devolve `items: []`
sempre — o mesmo caminho de código do "calendário vazio" do roteiro, só que determinístico.
Não dá para reproduzir um calendário genuinamente vazio aqui, porque o preparo já povoou
o calendário antes de qualquer teste rodar.

### Flags

| Flag | Efeito |
|---|---|
| `--reset-bookings` | Apaga reservas pré-existentes nos eventos reaproveitados, em vez de abortar |
| `--keep` | Não desfaz nada ao final; imprime os `event_id` para depurar à mão |
| `--skip-token-test` | Pula o passo 12 (o que mexe no `refresh_token`) |
| `--no-color` | Saída sem ANSI |
| `--json ARQUIVO` | Também grava o relatório em JSON |

### O que ele mexe — e como desfaz

Três tipos de rastro, três reversões diferentes:

| Rastro | Como é revertido |
|---|---|
| Eventos que **ele criou** | `events().delete()` |
| Eventos **seus**, reaproveitados | `patch()` restaurando a descrição original e zerando `corujai_booked_count` |
| Linhas em `trial_bookings` | `DELETE`, nos dois casos |
| `owners.refresh_token` (passo 12) | Backup em `/tmp/corujai_owners_backup.json`, restauro no `finally` + auto-restauro no início da execução seguinte |

A regra que atravessa o arquivo: **só desfaço o que eu fiz**. `created_event_ids` é
subconjunto de `touched_event_ids`, e só o primeiro autoriza `delete`. Um teste que apaga
dado do usuário quando falha é pior que não ter teste.

O teardown é registrado em `atexit` logo após a conexão, então roda mesmo se um teste
explodir no meio.

Os leads de teste usam telefones determinísticos no formato `5521000000001`,
`5521000000002`, … (`_sender(n)`).

### Ledger sujo aborta antes de testar

O pré-check `F2` verifica se os eventos reaproveitados já têm reservas ativas. Se tiverem,
a suíte **aborta** em vez de rodar: o passo 5 espera `active_count=2`, e uma reserva
sobrando de um teste manual anterior faria o passo 4 lotar cedo e o passo 5 falhar —
apontando para um bug que não existe. Rode com `--reset-bookings` para limpar.

---

## Fluxo recomendado

O caminho mais limpo, e totalmente autocontido:

```bash
cd src
python tests/test_scheduling/test_scheduling.py cleanup --yes    # zera o calendário
python tests/test_scheduling/test_scheduling_suite.py            # cria os 7 fixtures, testa, apaga tudo
```

Depois do `cleanup` os sete papéis estão faltando, então a suíte cria todos — e como
passam a ser dela, o teardown os apaga no final. Não sobra nada.

Para explorar à mão, o caminho longo continua valendo: crie os eventos da tabela de preparo
do `SCHEDULING_ENGINE_TESTING.md` no Google Calendar — à mão, ou pelo script
`seed_calendar_fixtures.gs`, que vive no Google Apps Script (script.google.com) e não neste
repositório — e siga o roteiro com `list` e `book`.

## Troubleshooting

| Sintoma | Causa | Solução |
|---|---|---|
| `ModuleNotFoundError: No module named 'bot'` (ou `integrations`) | O script foi movido para fora da árvore de `src/`, então a âncora por nome não acha mais o diretório | Mantenha os scripts em algum lugar sob `src/`; o cwd não importa |
| `Integração com o Google Calendar não está conectada.` | `integration_status != 'connected'` ou `calendar_id` vazio | Conecte em `/integrations/google` |
| `O Google recusou o token salvo` | `refresh_token` revogado | Reconecte em `/integrations/google` |
| `F2` aborta com "já têm reservas ativas" | Reservas de testes anteriores | `--reset-bookings` |
| Passo 11 falha com contagem inesperada | Instância recorrente ocupando outro papel | Confira `_classify()`; instâncias têm `recurringEventId` |
| Uma chamada concorrente do passo 6 demora | Esperado: o advisory lock bloqueia a segunda até a primeira commitar | Não é deadlock |
| Sobrou fixture no calendário | Execução com `--keep`, ou morreu antes do teardown | `cleanup` |
| Integração quebrada depois de um crash no passo 12 | O `refresh_token` falso ficou gravado | Rode a suíte de novo (auto-restaura de `/tmp/corujai_owners_backup.json`) ou reconecte |

## Notas de manutenção

- **`src/` é encontrado por nome, não contando `.parent`.** Estes arquivos já mudaram de
  lugar uma vez (`src/scripts/` → `src/tests/test_scheduling/`), e o
  `.parent.parent` original passou a apontar para `src/tests/` — um caminho perfeitamente
  válido, só que errado, então todo import quebrava sem que nada avisasse. A âncora por
  nome sobrevive à próxima reorganização; só não sobrevive a sair de dentro de `src/`.
- **Não há framework de teste.** `requirements.txt` é curado à mão e não inclui pytest;
  a suíte traz seu próprio mini-runner (`Report.run()` traduz `AssertionError` → FAIL,
  `SkipTest` → SKIP, qualquer outra exceção → ERROR com traceback). Adicionar pytest é
  uma decisão de arquitetura, não um detalhe.
- **Capturar log exige mexer no nível do *logger*, não só do handler.** `main()` configura
  a raiz em `ERROR`; `logger.warning()` descarta a chamada antes de criar o `LogRecord`
  quando o nível efetivo está acima de `WARNING`. É o que `WarningCapture` resolve.
- **`bot.scheduling` importa `get_calendar_service` por nome**, então o teste 13 precisa
  aplicar o *patch* em `scheduling.get_calendar_service`, não no módulo de origem.
- **`events().instances()` não devolve em ordem cronológica.** Ordene antes de falar em
  "1ª" e "2ª" instância.
- **`singleEvents=False` não garante só séries mestras**: o Google também devolve
  instâncias que tenham alguma exceção. `recurringEventId` é o discriminador confiável.
