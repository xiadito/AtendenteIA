# Contas, login e provisionamento de tenant (Módulo S3a)

Roteiro de teste do módulo que deu contas de verdade ao Corujai: a senha única em texto puro
virou e-mail + hash, o painel passou a saber **quem** está logado, e o webhook passou a saber
**para qual academia** cada mensagem foi escrita.

**O que o módulo promete, em uma frase:** depois dele uma segunda academia é *criável* e a
Delariva se comporta **exatamente** como antes. Os dois lados dessa frase são testados aqui.

---

## ⛔ A regra rígida — leia antes de rodar qualquer coisa

> **Nenhuma segunda conta em produção até o Módulo S3b mergear.** Nem a sua própria conta ADM
> de teste.
>
> O S3a resolve o tenant, mas **as leituras ainda não filtram por ele**: `sessions` não tem
> coluna `tenant_id`, a FK de `messages` não é composta, e `get_conversation()`,
> `list_conversations()`, `count_unread()` e `list_bookings_for_review()` devolvem as linhas de
> todo mundo. Com duas academias no mesmo banco de produção, cada uma enxerga as conversas e os
> agendamentos da outra.
>
> Com **só** o `'default'` existindo, o S3a em produção é seguro e se comporta como antes.
>
> Em banco de **desenvolvimento**, criar vários tenants é seguro e é exatamente o que a suíte
> faz. Todo comando deste roteiro pressupõe banco de desenvolvimento.

---

## O que mudou neste módulo

| Área | Antes | Depois |
|---|---|---|
| Login | um campo de senha, comparado em texto puro com `DASHBOARD_PASSWORD` | e-mail + senha, verificada contra `users.password_hash` (werkzeug/scrypt) |
| Sessão | `session["dashboard_authenticated"] = True` | Flask-Login, com `current_user.tenant_id` disponível |
| Decorator | `_require_auth`, privado, importado de `webhook/routes.py` | `require_auth`, em `accounts/auth.py`, nas mesmas 24 rotas |
| Criar conta | não existia | `python -m accounts.provision create` — **sem rota web** |
| Primeiro usuário | não existia | auto-bootstrap no `create_app()`, a partir do `.env`, uma vez só |
| Schema | — | migration **009**: `users`, `owners.whatsapp_number`, `UNIQUE` em `owner_phone` |
| Webhook | lia o campo `To` e **descartava** | o `To` resolve o tenant, com queda para `'default'` |
| Dono-vs-lead | varredura **global** por telefone | decisão **dentro** do tenant resolvido |
| `DASHBOARD_USER` | lida pelo `config.py` e usada por nada | o e-mail do fundador, usado **uma vez** |
| `DASHBOARD_PASSWORD` | a credencial do painel | **semente inicial**, nunca comparada no login |

**Arquivos novos:** `src/database/migrations/009_create_users.sql` e o pacote `src/accounts/`
(`users.py`, `tenants.py`, `provision.py`, `auth.py`, `bootstrap.py`).

**Modificados:** `app.py`, `webhook/routes.py`, `integrations/routes.py`, `integrations/store.py`,
`bot/handlers.py`, `templates/login.html`, `templates/menu.html`, `static/css/login.css`,
`static/css/menu.css`, `requirements.txt`, e cinco suítes de teste.

---

## Antes de começar

A migration 009 põe um índice **único** em `owners.owner_phone` — o que o Módulo S1 adiou — e
ele roda contra uma tabela que já tem dados. Confira que não há duplicata **antes**:

```sql
SELECT owner_phone, COUNT(*) FROM owners
WHERE owner_phone IS NOT NULL GROUP BY 1 HAVING COUNT(*) > 1;
-- tem que vir vazio
```

Se vier alguma linha, resolva à mão primeiro. Se a migration falhar, `init_db()` levanta,
`create_app()` **só imprime** o erro, e o app sobe **sem a tabela `users`** — todo login dá 500 e
a falha não se parece nada com a causa.

```bash
source venv/bin/activate
cd src
python app.py     # aplica a 009 e cria o primeiro usuário; ^C depois do boot
```

```sql
SELECT version FROM schema_migrations ORDER BY version;   -- 001 … 009

\d users
-- id | email (UNIQUE) | password_hash (text) | tenant_id (FK → owners) | created_at

SELECT indexname FROM pg_indexes WHERE tablename = 'owners';
-- idx_owners_owner_phone, idx_owners_whatsapp_number, idx_owners_tenant_id, ...

SELECT id, email, tenant_id FROM users;   -- o seu usuário
```

⚠️ **`DASHBOARD_USER` precisa ser um e-mail.** Era `admin`, e o bootstrap recusa qualquer coisa
sem `@` — com um aviso que ele **imprime** no boot, não só loga. Se você vir

```
AVISO: DASHBOARD_USER não é um e-mail. ...
```

troque o valor em `src/.env` por um endereço e reinicie. `DASHBOARD_PASSWORD` também precisa ter
pelo menos 8 caracteres.

---

## A suíte automatizada

**22 testes, determinística**: nada de LLM, WhatsApp ou Google Calendar — só Postgres e o test
client do Flask.

```bash
python tests/test_accounts/test_accounts_suite.py
python tests/test_accounts/test_accounts_suite.py --keep     # não limpa, para depurar
python tests/test_accounts/test_accounts_suite.py --json     # relatório em tests/outputs/
```

**Esta é a primeira suíte do projeto SEM backup em `/tmp`, e isso é de propósito.** As suítes
`test_settings` e `test_class_types` tiram snapshot das linhas reais do piloto porque as telas
que elas exercitam só sabem escrever no `'default'`. Esta aqui **nunca escreve no piloto**: todo
cenário roda em tenants de fixture criados pelo próprio `provision_tenant()`, sob o prefixo
`suite-s3a-`. No lugar do backup existe `_drop_orphan_fixtures()`, chamado no início do `main()`,
que faz o mesmo papel de reparo pós-crash. **Não "conserte" a etapa de backup que falta** — não
há nada do piloto para restaurar.

Os usuários da suíte vivem todos em `@suite.corujai.test`, e a limpeza é escopada a esse
domínio: ela não consegue apagar a sua conta.

---

## A CLI manual

Onde a suíte afirma, a CLI deixa **ver**. Ela combina com o DBeaver: provisione aqui, leia as
tabelas lá.

```bash
python tests/test_accounts/test_accounts.py tenants
python tests/test_accounts/test_accounts.py slug --name "Academia Delariva Itaipuaçu"
python tests/test_accounts/test_accounts.py provision \
    --name "Suite Manual Box" --email manual@suite.corujai.test --password senha-de-teste-123
python tests/test_accounts/test_accounts.py show --tenant suite-manual-box
python tests/test_accounts/test_accounts.py login \
    --email manual@suite.corujai.test --password senha-de-teste-123
python tests/test_accounts/test_accounts.py resolve --to "whatsapp:+14155238886"
python tests/test_accounts/test_accounts.py drop-tenant --tenant suite-manual-box
```

---

## Roteiro

### 1. O login de verdade

**O que fazer:** `cd src && python app.py`, abra `http://localhost:5000/dashboard/menu`.

**O que esperar:**

- Sem sessão, você é mandado para `/dashboard/login`, agora com **dois** campos.
- Errar a senha dá **"E-mail ou senha incorretos."** Errar o e-mail dá **a mesma frase**. Isso é
  intencional: duas mensagens diferentes contariam a quem tenta quais endereços estão
  cadastrados. `accounts/users.py::authenticate()` fecha o mesmo vazamento pelo lado do tempo,
  comparando contra um hash falso quando o e-mail não existe.
- Acertando, você cai no menu — e o seu e-mail aparece discretamente acima do "Sair".
- Clicar em "Sair" e voltar a `/dashboard/settings` te devolve ao login.

**Detalhe deliberado:** o Flask-Login acrescenta `?next=/dashboard/settings` na URL do login, e
**nós ignoramos**. Honrar esse parâmetro exige validar que o destino é da mesma origem, e errar
isso é um open redirect. Você sempre cai no menu, que é o comportamento pré-S3a.

### 2. As 24 rotas continuam protegidas, e o webhook continua aberto

**O que fazer:**

```bash
curl -s -o /dev/null -w "%{http_code} %{redirect_url}\n" http://localhost:5000/dashboard/settings
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:5000/status
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:5000/webhook
```

**O que esperar:** `302 .../dashboard/login`, depois `200`, depois `400` (payload inválido — mas
**não** um redirect para o login).

Pôr `require_auth` no webhook derrubaria o produto inteiro em silêncio: o Twilio receberia 302 e
o lead nunca seria respondido. O teste 17 da suíte existe só para isso.

### 3. O primeiro usuário nasce sozinho — e só uma vez

**O que fazer:**

```sql
DELETE FROM users;      -- só em desenvolvimento
```
```bash
python app.py           # ^C depois do boot
```
```sql
SELECT email, tenant_id FROM users;
```

**O que esperar:** um usuário, com o e-mail de `DASHBOARD_USER` e a senha do `.env` **em hash**.
Suba o app de novo e confira que continua **um** — a checagem `count_users()` é o que impede um
restart de ressuscitar a senha do `.env` depois que você já a trocou pela CLI.

O gunicorn chama `create_app()` **uma vez por worker**, então vários podem passar por essa
checagem no mesmo instante; quem realmente impede a duplicata é o `ON CONFLICT (email) DO
NOTHING` dentro de `create_user()`.

### 4. Provisionar uma academia nova

**O que fazer:**

```bash
python tests/test_accounts/test_accounts.py provision \
    --name "Suite Manual Box" --email manual@suite.corujai.test --password senha-de-teste-123
python tests/test_accounts/test_accounts.py show --tenant suite-manual-box
```

**O que esperar:** as **cinco** tabelas preenchidas, numa transação só.

```sql
SELECT tenant_id, owner_phone, whatsapp_number, integration_status
FROM owners WHERE tenant_id = 'suite-manual-box';

SELECT academy_name, assistant_name FROM ai_configs WHERE tenant_id = 'suite-manual-box';
-- Suite Manual Box | [NOME DA ATENDENTE]   ← o nome é real, o resto são os textos-guia

SELECT marker, label, capacity, is_fallback
FROM class_types WHERE tenant_id = 'suite-manual-box';
-- ADULTOS | Adultos | NULL | true

SELECT days_ahead FROM scheduling_configs WHERE tenant_id = 'suite-manual-box';   -- 14

SELECT email, tenant_id FROM users WHERE tenant_id = 'suite-manual-box';
```

**Por que uma transação só:** um tenant meio provisionado é pior que nenhum, porque funciona
*silenciosamente errado*. Os dois próximos passos são exatamente essas duas falhas.

### 5. A Armadilha #4 — a turma padrão precisa ser uma linha real

**O que fazer:** olhe a query de `class_types` acima. Depois:

```sql
DELETE FROM class_types WHERE tenant_id = 'suite-manual-box';
```
```bash
python tests/test_accounts/test_accounts.py show --tenant suite-manual-box
```

**O que esperar:** a CLI avisa `NENHUMA LINHA — o tenant está rodando no fallback sintético`, e
mesmo assim a invariante do S2 continua valendo (`fallback 'ADULTOS' ESTÁ em capacities`).

**É esse o ponto.** `load_class_types()` inventa um fallback ilimitado em memória quando o tenant
não tem nenhum, então um tenant sem turma **não quebra** — ele ignora capacidade em silêncio, e o
único sintoma é um WARNING num log que ninguém lê. Por isso a suíte lê a tabela **direto** (teste
3) em vez de perguntar ao `load_class_types()`: perguntando, o bug se esconderia.

Refaça o tenant antes de seguir (`drop-tenant` e `provision` de novo).

### 6. A armadilha do `UPDATE` — a seção IA precisa ter linha

**O que fazer:** logue com `manual@suite.corujai.test`, abra `/dashboard/settings` e salve a
seção "IA".

**O que esperar:** salva. Sem a linha em `ai_configs`, salvaria para sempre sem efeito:
`update_ai_config()` é um `UPDATE`, **não um upsert**, e devolveria `False` eternamente enquanto
a tela diz que não achou o cadastro. O teste 4 da suíte é o que falha se o passo 2 do
provisionamento for removido.

### 7. Colisão de slug

**O que fazer:**

```bash
python tests/test_accounts/test_accounts.py slug --name "Academia Delariva Itaipuaçu"
```

**O que esperar:** `academia-delariva-itaipuacu` — a palavra "Academia" **não** é removida. A
regra é puramente mecânica (sem acento, minúsculo, o resto vira hífen); uma lista de palavras
genéricas para descartar seria específica de cultura e daria respostas surpreendentes para uma
academia chamada só de palavras genéricas. Quando você quiser o slug curto, passe
`--tenant-id delariva-itaipuacu`, que é validado pelas mesmas regras.

Provisione duas academias com o mesmo nome e confira que a segunda vira `...-2`.

### 8. Idempotência

**O que fazer:** rode o mesmo `provision` duas vezes, com o mesmo e-mail.

**O que esperar:** `Esse e-mail já tem conta, no tenant '...'. Nada foi criado.` O **e-mail é a
identidade do pedido**, e nenhuma das cinco tabelas cresce.

### 9. A costura do sandbox — o cenário do piloto HOJE

**O que fazer:**

```bash
python tests/test_accounts/test_accounts.py resolve --to "whatsapp:+14155238886"
```

**O que esperar:** `tenant: nenhum → cai em 'default' com WARNING no log`.

**Por que isso mantém o piloto funcionando.** O Sandbox do Twilio dá o **mesmo** número de entrada
para todo mundo, então nenhuma academia pode reivindicá-lo e `owners.whatsapp_number` fica `NULL`
em todo mundo. Com isso `find_tenant_by_whatsapp_number()` sempre devolve `None`, o caminho
sandbox sempre roda, e `store.get_owner_by_phone(clean_number)` é **a mesma chamada da mesma
linha** de antes do S3a. Tudo a jusante recebe `tenant_id='default'`, que já era o default de
todos os callees. Nada observável muda — e a forma correta já está no lugar para quando os
números reais chegarem.

Mande uma mensagem de verdade pelo sandbox e confira no log:

```
WARNING - Nenhum tenant registrado para o número de destino; usando 'default'.
INFO - Handling text message from 5521... (12 chars) for tenant default.
```

### 10. A Armadilha #2 — o passo mais importante do roteiro

**O que fazer:** com dois tenants e números registrados,

```sql
UPDATE owners SET whatsapp_number = '5528000555555', owner_phone = '5528000666666'
WHERE tenant_id = 'default';
UPDATE owners SET owner_phone = '5528000888888' WHERE tenant_id = 'suite-manual-box';
```

Mande dois POSTs para `/webhook`, os dois com `To=whatsapp:+5528000555555` (o número do
`default`): um com `From` do dono do `default`, outro com `From` do dono do `suite-manual-box`.

**O que esperar:** o primeiro cai em `receive_twilio_owner`. **O segundo cai em
`handle_text_message`, como lead do `default`.**

**Por que isso importa:** se o dono da academia B fosse reconhecido como dono ao escrever para o
número da academia A, o "1" dele confirmaria uma aula **da academia A**. Era exatamente isso que
uma varredura global por telefone faria. A pergunta certa deixou de ser *"esse número é de algum
dono?"* e passou a ser *"esse número é o dono da academia para quem isto foi escrito?"*.

Desfaça (`UPDATE owners SET whatsapp_number = NULL ...`) antes de voltar ao sandbox.

### 11. Limpeza

```bash
python tests/test_accounts/test_accounts.py drop-tenant --tenant suite-manual-box
python tests/test_accounts/test_accounts.py tenants
```

```sql
SELECT tenant_id FROM owners;        -- só 'default'
SELECT email, tenant_id FROM users;  -- só a sua conta
```

`drop-tenant` apaga das cinco tabelas. `users` tem FK com `ON DELETE CASCADE` para `owners`, mas
`class_types`, `ai_configs` e `scheduling_configs` **não têm FK nenhuma** — sem o DELETE
explícito, elas ficariam órfãs para sempre.

---

## Regressão obrigatória

As oito suítes, todas verdes:

```bash
python tests/test_scheduling/test_scheduling_suite.py
python tests/test_ai_action/test_ai_action_suite.py --skip-live
python tests/test_owner_notifications/test_owner_notifications_suite.py
python tests/test_inbox/test_inbox_suite.py
python tests/test_confirmation/test_confirmation_suite.py
python tests/test_settings/test_settings_suite.py
python tests/test_class_types/test_class_types_suite.py
python tests/test_accounts/test_accounts_suite.py
```

**Cinco suítes precisaram ser tocadas por este módulo**, e vale saber por quê:

- **Quatro** (`test_confirmation`, `test_inbox`, `test_settings`, `test_class_types`)
  autenticavam empurrando `flask_session["dashboard_authenticated"] = True`. Isso não autentica
  mais nada. Elas agora criam um usuário descartável e logam **pela rota de verdade** — mais
  honesto que forjar as chaves privadas do Flask-Login (`_user_id`, `_fresh`), que além de
  indocumentadas ainda precisariam de uma linha real em `users` para o `user_loader` resolver.
- **A quinta** (`test_owner_notifications`) tinha dublês com exatamente dois parâmetros
  posicionais. `receive_twilio()` passou a mandar um terceiro, e **o default no parâmetro não
  salva** — quem passa o argumento é o chamador. Os dois dublês ganharam `tenant_id`, e a
  asserção ganhou uma checagem de tenant, transformando a quebra em cobertura.

`test_ai_action` ficou intocada de propósito: seus sete stubs de `get_cached_slots` provam que a
mudança de assinatura do `handle_text_message` é invisível para quem chama com dois argumentos.

---

## O que este módulo deliberadamente **não** faz

- **Isolamento de leitura por tenant.** É o S3b, e é o conserto de verdade. Quando este módulo
  fechou, o `tenant_id` chegava a `handle_text_message()` e a `receive_twilio_owner()` e **parava
  ali** — os pontos exatos ficaram marcados com `# S3b:` no `bot/handlers.py`. **Isso já foi
  feito:** o Módulo S3b costurou todos eles e o isolamento existe (ver
  `tests/test_tenant_isolation/TENANT_ISOLATION_TESTING.md`). A regra rígida que este arquivo
  descrevia, de não criar uma segunda conta, caiu junto.
- **Reset de senha pela UI.** É `python -m accounts.provision reset-password`. Não há tela, e não
  há link de "esqueci minha senha".
- **Cadastro público.** Decisão fechada: cada academia depende de um número aprovado no Twilio,
  que é etapa manual sua. Um cadastro aberto criaria contas órfãs sem número.
- **Rate limit no login.** O scrypt encarece o brute force, mas não há bloqueio por tentativas.
- **Multi-operador.** `users` já aceita duas linhas com o mesmo `tenant_id`, mas
  `messages.author = 'operator'` continua sem dizer **qual** humano respondeu.
- **`?next=`.** Ignorado de propósito (ver passo 1).

---

## Onde as coisas moram

| Arquivo | Responsabilidade |
|---|---|
| `accounts/users.py` | A tabela `users`. `normalize_email` (o canônico), `authenticate` (com hash falso contra timing). Sem Flask. |
| `accounts/tenants.py` | Geração de slug e colisão. Puro + uma checagem no `owners`. Sem Flask. |
| `accounts/provision.py` | `provision_tenant()` numa transação só, e a CLI `python -m accounts.provision`. Sem Flask. |
| `accounts/auth.py` | **O único arquivo com Flask.** `LoginManager`, `User(UserMixin)`, `user_loader`, `require_auth`. |
| `accounts/bootstrap.py` | O primeiro usuário, a partir do `.env`, uma vez. |
| `integrations/store.py` | `find_tenant_by_whatsapp_number`, `resolve_tenant_by_whatsapp_number`, `get_owner_by_phone_in_tenant`, `update_whatsapp_number` — porque só leem `owners`. |
| `webhook/routes.py` | `login`/`logout`, e os dois caminhos (sandbox e roteado) do `receive_twilio`. |
| `bot/handlers.py` | Recebia o `tenant_id` e não o propagava — era a costura do S3b, hoje fechada. |
