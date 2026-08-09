# Módulo S3b — Isolamento por tenant e chave composta de `sessions`

Este arquivo é as duas coisas: o **roteiro de teste** do módulo e o **documento das modificações**
que ele fez. Se você voltar aqui em seis meses querendo saber o que o S3b mudou e por quê, é este
o arquivo — não há outro.

---

## O que este módulo resolveu

O Módulo S3a deu identidade ao projeto (contas, login, `provision_tenant()`) e passou a **resolver**
o tenant de cada mensagem pelo campo `To` do Twilio. Só que o `tenant_id` parava no ponto de
entrada: `handle_text_message()` e `receive_twilio_owner()` recebiam o tenant e não passavam para
ninguém. Cada ponto ficou marcado no código com um comentário `# S3b:` — nove ao todo.

Sem isso, o banco não tinha como separar duas academias:

| Tabela | Como estava | Consequência |
|---|---|---|
| `sessions` | `sender VARCHAR(20) PRIMARY KEY`, **sem coluna `tenant_id`** | O mesmo lead não podia existir em duas academias. A segunda simplesmente não conseguia registrá-lo. |
| `messages` | `sender REFERENCES sessions(sender)` | Uma mensagem pertencia a um número de telefone, não a uma conversa numa academia. |
| `trial_bookings` | `UNIQUE (calendar_event_id, sender)` | Tinha `tenant_id`, mas a unicidade era global. |

E as leituras (`get_session`, `get_conversation`, `list_conversations`, `count_unread`,
`list_bookings_for_review`, …) varriam todas as linhas de todos os tenants. Uma segunda academia
enxergaria as conversas e os agendamentos do piloto, e o piloto enxergaria os dela.

**É isso que este módulo consertou.** Com ele, criar uma segunda conta e ligar `SIGNUP_ENABLED`
deixam de ser perigosos — *quando* ligar a flag continua sendo decisão do fundador, porque toda
academia nova ainda depende de um WhatsApp Sender aprovado à mão no Twilio.

---

## 1. A migration 011, passo a passo

`src/database/migrations/011_tenant_isolation.sql`. Ela mexe em chave primária e em chave
estrangeira de tabelas que **já têm dado do piloto**, então a ordem não é estilo — é o que faz a
coisa funcionar:

| # | Operação | Por que nesta posição |
|---|---|---|
| 1 | `ALTER TABLE sessions ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'default'` | O `DEFAULT` preenche as linhas existentes **na mesma instrução**. Não há `UPDATE` separado nem janela em que a coluna esteja nula. |
| 2 | `UPDATE messages … SET tenant_id = s.tenant_id … WHERE m.sender = s.sender AND m.tenant_id IS DISTINCT FROM s.tenant_id` | Realinhamento defensivo **antes** da FK do passo 5. Hoje é um no-op (tudo é `'default'`), mas é o que garante que a FK composta não possa ser recusada por dado divergente. Antes da 011 a FK simples já garantia que toda mensagem tem sessão, então o `UPDATE` cobre todas. |
| 3 | **Dropar** a FK de `messages` → `sessions` | Tem que sair **antes** de mexer na PK de que ela depende, senão o passo 4 falha. O nome é **descoberto** em `pg_constraint`, nunca escrito à mão: a 007 escreveu o `REFERENCES` inline e deixou o Postgres nomear. |
| 4 | Dropar a PK atual e `ADD PRIMARY KEY (tenant_id, sender)` | O coração da migration. |
| 5 | Recriar a FK **composta**, `(tenant_id, sender) → sessions (tenant_id, sender) ON DELETE CASCADE` | Depois da PK, porque é ela que a FK referencia. O `CASCADE` é load-bearing: `clear_session()` e o teardown de toda suíte dependem dele. |
| 6 | Dropar o `UNIQUE (calendar_event_id, sender)` e criar `idx_trial_bookings_tenant_event_sender` em `(tenant_id, calendar_event_id, sender)` | Índice único, e não constraint, seguindo o idioma que a 008 e a 009 já usam. Mesma unicidade, inferível por `ON CONFLICT`, e continua levantando o `UniqueViolation` que `create_booking_with_lock()` captura para responder `"duplicate"`. |
| 7 | Índices refeitos com o tenant à frente | Toda leitura depois do S3b filtra por tenant primeiro, então um índice que começa em `sender` não serve mais a consulta para a qual foi criado. |

**Por que ela é segura contra o dado do piloto:** nenhuma linha é apagada nem reescrita, com a
única exceção do realinhamento do passo 2. A coluna nova nasce preenchida pelo próprio `DEFAULT`.
A FK sai antes da PK e volta depois. E `database/db.py` executa o arquivo inteiro num único
`cur.execute()` seguido de um `commit()` — ou seja, **uma transação**: ou aplica tudo, ou o banco
fica exatamente como estava.

**Ela é convergente, não só idempotente.** Cada passo é guardado por uma consulta a
`pg_constraint`/`pg_indexes`, então o arquivo produz o mesmo schema partindo de um banco parado na
010 (produção) e de um banco que já tenha essas chaves. Isso é obrigatório neste projeto: `version`
é o nome do arquivo, e renomear um arquivo faz a migration rodar de novo em silêncio.

> A regra de nunca editar uma migration aplicada continua valendo. A 011 é nova; 001–010 não foram
> tocadas.

### Como isso foi verificado

Antes de rodar contra qualquer banco de verdade, a 011 foi aplicada em **dois bancos
descartáveis** (schemas temporários, com `search_path` sem o `public`, para que um erro não pudesse
alcançar as tabelas reais):

1. **Do zero** — 001→011 num schema vazio. Confere PK, FK, `UNIQUE` e todos os índices, e depois
   roda a 011 **outra vez** para provar que ela converge em vez de estourar.
2. **Com dado semeado** — 001→010 apenas, depois duas sessões, oito mensagens, uma reserva e uma
   notificação no formato do piloto, e **só então** a 011. Confere que nenhuma linha se perdeu, que
   tudo ficou em `'default'`, que o mesmo lead passa a caber em dois tenants, que o `(evento,
   lead)` repetido é barrado dentro de um tenant e permitido entre tenants, e que o `CASCADE`
   composto apaga só o lado certo.

---

## 2. O que ganhou `tenant_id`

O padrão é um só, copiado de `bot/class_types.py` (Módulo S2): **o parâmetro entra por último, com
`DEFAULT_TENANT_ID` como default, e toda consulta que buscava por `sender` passa a buscar pelo
par.** O default preserva o comportamento do piloto e mantém as suítes antigas chamando com os
mesmos argumentos.

O tenant é sempre **passado como argumento** — nunca lido de `flask.g` ou de `current_user` dentro
da camada de dados (decisão 14A). Não é preferência de estilo: `bot/` e `integrations/` também
rodam sob o cron e sob o webhook, onde não existe request nenhum.

| Arquivo | Funções |
|---|---|
| `bot/session.py` | `get_session` (o `SELECT` **e** o `INSERT (tenant_id, sender)`), `session_exists`, `save_session`, `clear_session`, `get_all_sessions` |
| `bot/messages.py` | `get_recent_messages`, `get_conversation`, `mark_conversation_read`, `count_unread`, `list_conversations` |
| `bot/bookings.py` | `count_active_bookings`, `list_active_bookings_by_sender`, `get_booking`, `list_bookings_by_status`, `list_bookings_for_review`, `update_booking_status`, e o `COUNT` interno de `create_booking_with_lock` |
| `bot/owner_notifications.py` | `register_owner_response`, `register_response_for_booking` |
| `bot/confirmations.py` | `confirm_or_cancel_booking`, e daí para o `get_booking`, o `update_booking_status`, os rótulos de turma e o aviso ao lead |
| `bot/scheduling.py` | `_get_service_or_raise` — ver abaixo |
| `bot/ai_context.py` | nada: `get_cached_slots` **já** era por tenant desde o S2. O que faltava era o chamador passar |
| `integrations/store.py` | nada: já era inteiramente por tenant desde o S3a |

Três decisões dentro dessa lista merecem nome:

**Onde a chave já era global, o tenant entrou como GUARDA.** `get_booking(id)` e
`update_booking_status(id)` acham a linha sozinhos — `id` é um uuid4. O tenant está ali porque a
rota do painel monta essas chamadas a partir de um pedaço da URL: sem ele, a academia A abriria e
decidiria a reserva da B colando o id. Uma reserva de outro tenant responde `None`, que é
exatamente a mesma resposta de um id inexistente — e as rotas já sabiam desenhar essa tela.

**`list_pending_notifications` continua global, de propósito.** É a fila de saída do sistema, e o
cron drena todas as academias numa passada só. Cada linha carrega o `tenant_id` dela, e é dele que
o envio se resolve. Filtrar aqui significaria ou N consultas por execução, ou um tenant que o cron
não tem de onde escolher.

**O advisory lock não mudou.** `pg_advisory_xact_lock(hashtext(calendar_event_id))` continua
correto: o id do evento do Google é único no mundo inteiro, e duas academias leem calendários
diferentes. O que virou por tenant foi a **contagem de vagas** feita dentro do lock — que é outra
pergunta ("quantas vagas ESTA academia já vendeu?").

### Dois vazamentos encontrados durante a implementação

Nenhum dos dois estava na lista de armadilhas do plano, e os dois são do mesmo tipo: código que
já sabia para qual academia estava trabalhando, mas lia a linha do piloto.

- **`bot/scheduling.py::_get_service_or_raise()` abria sempre o Google Calendar do piloto.** Uma
  segunda academia receberia os horários livres **do piloto** e gravaria as reservas dela **na
  agenda do piloto**. Nada mais abaixo pegaria isso: os horários voltam bem-formados, só que da
  agenda errada.
- **`integrations/routes.py` gravava a conexão do Google no tenant `'default'`.** O dono da
  academia B conectando a agenda dele **sobrescreveria as credenciais do piloto** — e o piloto
  pararia de funcionar sem nenhum erro que apontasse para a causa.

Ambos agora recebem o tenant (`current_user.tenant_id` no painel, o tenant resolvido no webhook).

---

## 3. A costura `# S3b:` — fechada

Os nove marcadores foram preenchidos e removidos:

- **`bot/handlers.py` (7)** — `get_session`, `add_message` (nas três chamadas), `get_ai_config`,
  `get_cached_slots`, `list_active_bookings_by_sender`, `get_recent_messages`, `save_session`,
  `get_owner_for_notification` e `enqueue_notification`. O `tenant_id` também desce para
  `_execute_action` → `_execute_booking` → `book_slot`.
- **`webhook/routes.py` (2)** — `register_owner_response(..., tenant_id=…)` e
  `confirm_or_cancel_booking(..., tenant_id=…)` no handler do dono.

Além deles, **as rotas de painel passaram a mandar `current_user.tenant_id`**. É a parte mais fácil
de esquecer do módulo inteiro: dá para parametrizar a função direitinho e não passar o valor na
rota — e aí tudo continua lendo `'default'` e o isolamento não acontece de fato, sem nada quebrar.
Foram varridos o inbox (6 rotas + 2 helpers), os agendamentos (4 rotas + 2 helpers), as
configurações (8 rotas + 2 helpers) e as 4 rotas do Google Calendar.

O CSRF do S3c ficou intacto: nenhuma rota mudou de blueprint e `csrf.exempt(webhook_bp)` está onde
sempre esteve.

**Critério de pronto:** `grep -rn "# S3b:" src/` sai vazio. O passo 16 da suíte falha se sobrar
algum.

---

## 4. Rodando a suíte

Da pasta `src/`:

```bash
python tests/test_tenant_isolation/test_tenant_isolation_suite.py
python tests/test_tenant_isolation/test_tenant_isolation_suite.py --keep    # não limpa no fim
python tests/test_tenant_isolation/test_tenant_isolation_suite.py --json    # grava o relatório
```

Sai `0` só se tudo passou. Totalmente determinística: sem LLM, sem WhatsApp, sem Google Calendar —
só Postgres e o cliente de teste do Flask.

**A forma de todo teste é a mesma, e é o ponto:** construir o **mesmo fato** em duas academias e
depois ler como uma delas, provando que a cópia da outra não aparece. Um teste que só verificasse
"a academia A vê a linha dela" passaria **também no código pré-S3b**, porque leitura sem filtro
devolve a sua linha junto com a de todo mundo. A asserção que importa é sempre a negativa.

| # | Cenário | O que provaria o contrário |
|---|---|---|
| P1 | Migration 011 aplicada; PK, FK e `UNIQUE` no formato novo | — |
| F1 | Duas academias provisionadas, com rótulos de turma diferentes | — |
| 1 | O mesmo lead existe nas duas (chave composta) | Violação de PK no segundo `INSERT` |
| 2 | `get_session` / `session_exists` não vazam | A Alfa lendo "Lead da Beta" |
| 3 | Conversa, não lidas e lista do inbox não vazam | Texto da Beta na conversa da Alfa |
| 4 | Prévia e badge do inbox vêm do mesmo tenant das linhas | A armadilha dos dois `LEFT JOIN LATERAL`: linhas certas, prévia da outra academia |
| 5 | Agendamentos não vazam, nem pela lista nem por id | `get_booking(id_da_alfa, tenant=beta)` devolvendo a linha |
| 6 | O `UNIQUE` de `trial_bookings` é por tenant | O cruzado barrado, ou o repetido aceito |
| 7 | A IA injeta só a reserva da própria academia | O lead ouvindo da Alfa a aula que marcou na Beta |
| 8 | O cache de horários é por tenant | A Beta recebendo os horários da Alfa dentro do TTL |
| 9 | O `CASCADE` viaja pela chave composta | Apagar o lead na Alfa levar as mensagens da Beta |
| 10 | O `1`/`2` do dono resolve só a fila da própria academia | O "1" do dono da Beta carimbar uma notificação da Alfa |
| 11 | `confirm_or_cancel_booking` é guardado pelo tenant | A Beta cancelar a aula da Alfa |
| 12 | O cron resolve o tenant por linha | A mensagem do dono da Beta citando a turma da Alfa |
| 13 | O painel mostra só a academia logada | "Lead da Alfa" no inbox da Beta |
| 14 | Nem digitando a URL da outra academia | 200 em vez de 404 no `/dashboard/inbox/<sender>` alheio |
| 15 | A tela de configurações é por tenant | Salvar na Beta sobrescrever a IA da Alfa |
| 16 | Nenhum marcador `# S3b:` sobrou | Costura aberta |
| 17 | **(S3d)** Cada academia envia pelo próprio número | As duas resolvendo o mesmo `From` |
| 18 | **(S3d)** Academia sem número recusa; só o piloto cai no sandbox | Uma academia qualquer respondendo pela linha do sandbox |
| 19 | **(S3d)** O aviso ao lead sai pela academia dona da reserva | O `send_message` recebendo o tenant errado — ou nenhum |
| 20 | A limpeza não deixa conta nem tenant pendurado | Fixture no banco depois do teardown |

### Convenções desta suíte

- **Prefixo de tenant `suite-s3b-`** e **prefixo de sender `5530000`**. O registro dos outros:
  `5521000` scheduling, `5522000` ai action, `5523000` owner notifications, `5524000` inbox,
  `5525000` confirmation, `5526000` settings, `5527000` class types, `5528000` accounts,
  `5529000` signup.
- **Domínio de e-mail próprio, `@suite-s3b.corujai.test`.** O teardown do `test_accounts` apaga
  `LIKE '%@suite.corujai.test'`, e esta string **não** casa com aquele padrão — de propósito, para
  que o teardown de uma suíte nunca possa apagar a fixture da outra.
- **Sem backup em `/tmp`**, como o `test_accounts` e pelo mesmo motivo: esta suíte nunca escreve
  nas linhas do piloto. Tudo vive em duas academias de fixture criadas por `provision_tenant()`.
  `_drop_orphan_fixtures()`, no início do `main()`, faz o papel de reparo que o backup faz nas
  outras. **Não "conserte" a falta do backup — não há nada do piloto para restaurar.**
- **O passo 12 substitui `list_pending_notifications`** pelas linhas desta suíte. A fila é global
  de propósito, então `drain.main()` varreria também as notificações do piloto e as marcaria como
  enviadas. O que está sob teste é a resolução **por linha** dentro do laço; nada do piloto pode
  ser alterado por uma suíte.
- **O passo 16 pula o próprio arquivo** da suíte. Ele precisa escrever o marcador literalmente
  para poder procurá-lo, então casaria consigo mesmo e nunca poderia passar. O que se verifica é o
  código de produção, que é onde a costura ficou aberta.

---

## 5. A CLI manual

A suíte afirma; esta aqui deixa **ver**. Pareia com o DBeaver: monte o mesmo lead em duas academias
e leia as linhas de volta.

```bash
# 0. As chaves e os índices que a 011 mudou, direto do catálogo do Postgres
python tests/test_tenant_isolation/test_tenant_isolation.py schema

# 1. Duas academias de teste (rótulos de turma diferentes de propósito)
python tests/test_tenant_isolation/test_tenant_isolation.py setup

# 2. O MESMO lead nas duas, com textos que dizem qual é qual
python tests/test_tenant_isolation/test_tenant_isolation.py seed --sender 5530000111111

# 3. Ler como cada uma — é aqui que o vazamento apareceria
python tests/test_tenant_isolation/test_tenant_isolation.py read --sender 5530000111111

# 4. A mesma pergunta em SQL cru, SEM filtro de tenant — o controle do experimento
python tests/test_tenant_isolation/test_tenant_isolation.py rows --sender 5530000111111

# 5. O CASCADE composto: apaga na A, a B fica inteira
python tests/test_tenant_isolation/test_tenant_isolation.py cascade --sender 5530000111111

# 6. Limpar
python tests/test_tenant_isolation/test_tenant_isolation.py teardown
```

O par `rows` + `read` é o mais instrutivo: `rows` mostra que a tabela **realmente** guarda as duas
linhas, e `read` mostra que a aplicação nunca mais devolve as duas juntas.

Depois do `setup`, dá para entrar em `/dashboard/login` com
`suite-s3b-alfa@suite-s3b.corujai.test` e com `suite-s3b-beta@…` (senha `suite-password-s3b`) e
comparar as telas com os próprios olhos.

---

## 6. Regressão sobre o `'default'`

O comportamento do piloto tem que ficar **idêntico**. As nove suítes existentes rodam sobre
`'default'` e são a prova disso:

```bash
cd src
python tests/test_scheduling/test_scheduling_suite.py
python tests/test_ai_action/test_ai_action_suite.py --skip-live
python tests/test_owner_notifications/test_owner_notifications_suite.py
python tests/test_inbox/test_inbox_suite.py
python tests/test_confirmation/test_confirmation_suite.py
python tests/test_settings/test_settings_suite.py
python tests/test_class_types/test_class_types_suite.py
python tests/test_accounts/test_accounts_suite.py
python tests/test_signup/test_signup_suite.py
```

A única mudança que precisou ser feita numa suíte antiga foi em `test_ai_action`: os **sete stubs**
de `get_cached_slots` eram `lambda days_ahead=14: [...]` e viraram
`lambda days_ahead=None, tenant_id="default": [...]`, porque o handler passou a chamar a função com
`tenant_id=`. Nenhuma asserção foi afrouxada e nenhum teste foi removido.

---

## 7. O que este módulo deliberadamente **não** faz

- **Não liga `SIGNUP_ENABLED`.** A flag continua `false` por padrão. O que mudou é o motivo dela
  existir: era uma trava de segurança, virou uma decisão de negócio. Ligar é escolha do fundador.
- **Não cria papéis nem permissões dentro de uma academia.** `users` já aceita várias linhas por
  tenant, mas todo mundo do mesmo tenant enxerga a mesma coisa. Quem respondeu no inbox continua
  anônimo (`author = 'operator'`), como está nos Known Issues do `CLAUDE.md`.
- **Não isola `products`** (migration 002, legado do mercadinho, lido só pelo `sync_agent/`) nem
  `signup_attempts` (a fila de throttle é do sistema, como a de notificações).
- **Não pagina nem limita nenhuma tela.** `list_conversations()` e `list_bookings_for_review()`
  continuam sem `LIMIT` — agora por academia, o que adia o problema sem resolvê-lo.
- ~~**Não isola o que SAI.**~~ Era a última meia-verdade do S3b, e o **Módulo S3d** a resolveu —
  ver os cenários 17-19 abaixo.

---

## 8. Módulo S3d — o vazamento na SAÍDA

O S3b fechou toda leitura. O que ele não tocou foi o **envio**: `send_message()` usava um único
`Config.TWILIO_SANDBOX_NUMBER` para todas as academias. Não é um vazamento de leitura, mas é o
mesmo estrago: a academia era reconhecida pelo número dela na ENTRADA e respondia por outro na
SAÍDA, então o lead responderia para a linha errada e a próxima mensagem dele cairia noutro
tenant. É a mesma família dos dois vazamentos que o próprio S3b encontrou — o Google Calendar do
piloto e o callback do OAuth: código que sabia para qual academia trabalhava e usava a linha de
outra.

`whatsapp_service.resolve_sender_number(tenant_id)` lê `owners.whatsapp_number` e devolve o
`from_`. **Sem número, só o tenant `'default'` cai no sandbox; qualquer outro levanta
`SenderNotConfiguredError`.** A assimetria com a entrada é a decisão do módulo:
`resolve_tenant_by_whatsapp_number()` nunca bloqueia uma mensagem — perder um lead que já
escreveu é pior do que atendê-lo como do piloto —, enquanto na saída uma mensagem despachada pela
linha errada não tem volta.

### Como conferir à mão

```sql
-- Dê um número a cada academia de teste e confira que são diferentes.
SELECT tenant_id, whatsapp_number FROM owners WHERE tenant_id LIKE 'suite-s3b-%';
```

```python
# De src/, com o venv ativo:
import whatsapp.whatsapp_service as w
w.resolve_sender_number("suite-s3b-alfa")   # whatsapp:+<numero da alfa>
w.resolve_sender_number("suite-s3b-beta")   # whatsapp:+<numero da beta>
w.resolve_sender_number("default")          # o TWILIO_SANDBOX_NUMBER do .env
```

### A armadilha que o cenário 19 cobre

Esquecer o `tenant_id` num call site de `send_message` **não levanta erro nenhum** — o default
manda pela linha do piloto, em silêncio. É a mesma armadilha #5 do S3b (parametrizar a função e
esquecer de passar o valor na rota), agora no envio. Por isso o cenário 19 não olha o texto: ele
olha o **argumento** que chegou ao `send_message`.

E o cenário 12 ganhou a mesma verificação para o cron: além do rótulo da turma, ele agora confere
que cada linha da fila foi despachada com o tenant dela.
