# Cadastro público (Módulo S3c)

Roteiro de teste do módulo que deu ao dono de academia uma tela para criar a própria conta —
revertendo a decisão do S3a, em que contas nasciam só pela linha de comando do fundador.

**O que o módulo promete, em uma frase:** um visitante cria a academia dele sozinho, cai numa
tela que explica o que falta para a IA atender, e nada disso liga sem uma flag que **nasce
desligada**. Os três lados dessa frase são testados aqui.

---

## ⛔ A regra rígida — leia antes de ligar qualquer coisa

> **O cadastro público não corre o risco de criar uma segunda conta: ele FABRICA segundas
> contas.** E até o Módulo S3b as leituras não filtram por tenant — `sessions` não tem
> `tenant_id`, e `get_conversation()`, `list_conversations()`, `count_unread()` e
> `list_bookings_for_review()` devolvem as linhas de todo mundo.
>
> **Não coloque `SIGNUP_ENABLED=true` em produção antes do S3b.** Cada academia que se
> cadastrasse passaria a enxergar as conversas e os agendamentos da Delariva, e vice-versa.
>
> O default é `false`, e com ele `/dashboard/signup` responde **404**. Em banco de
> desenvolvimento, ligar e cadastrar é seguro e é o que a suíte faz.

---

## O que mudou neste módulo

| Área | Antes (S3a) | Depois (S3c) |
|---|---|---|
| Criar conta | só `python -m accounts.provision create`, pelo fundador | tela pública `/dashboard/signup`, atrás da flag `SIGNUP_ENABLED` |
| CSRF | **não existia em lugar nenhum** | `CSRFProtect` no app inteiro, com o webhook do Twilio isento |
| Proteção do endpoint | não havia endpoint público de escrita | honeypot + teto por IP contado no Postgres |
| Depois do cadastro | — | `/dashboard/onboarding`, com checklist derivada do banco |
| Menu | quatro tiles fixos | mais o tile "Primeiros passos", enquanto houver pendência |
| Login | só o formulário | mais o link de cadastro (com a flag ligada) e a nota sobre senha |
| Schema | — | migration **010**: `signup_attempts` |

**Arquivos novos:** `010_create_signup_attempts.sql`, `accounts/signup.py`,
`accounts/onboarding.py`, `templates/{signup,signup_done,onboarding}.html`,
`static/css/{signup,onboarding}.css`.

**Modificados:** `requirements.txt` (Flask-WTF + WTForms), `config.py`, `app.py`,
`webhook/routes.py`, cinco templates que ganharam `csrf_token`, dois que ganharam `hx-headers`,
`static/css/{login,menu}.css`, e **seis arquivos de teste** (ver Regressão).

---

## Antes de começar

```bash
source venv/bin/activate
pip install -r requirements.txt     # Flask-WTF é novo
cd src
python app.py                       # aplica a migration 010; ^C depois do boot
```

```sql
SELECT version FROM schema_migrations ORDER BY version;   -- 001 … 010

\d signup_attempts
-- id | ip_hash (char 64) | created_at

SELECT indexname FROM pg_indexes WHERE tablename = 'signup_attempts';
-- idx_signup_attempts_ip_created   ← (ip_hash, created_at), nessa ordem
```

A ordem do índice importa: toda consulta filtra por `ip_hash = %s AND created_at > …`. Liderar
por `created_at` transformaria a checagem numa varredura sobre as tentativas de todo mundo.

---

## A suíte automatizada

**17 testes, determinística**: nada de LLM, WhatsApp ou Google Calendar.

```bash
python tests/test_signup/test_signup_suite.py
python tests/test_signup/test_signup_suite.py --keep
python tests/test_signup/test_signup_suite.py --json
```

Duas coisas que **só esta suíte** faz:

1. **Ela liga e desliga a flag em tempo de execução** (`patched(Config, "SIGNUP_ENABLED", …)`),
   porque a flag desligada é metade do que o módulo promete.
2. **Ela roda dois testes com o CSRF LIGADO.** Todas as outras suítes o desligam — o que é certo
   para elas, que estão testando outra coisa — e isso deixaria a fiação do CSRF sem cobertura
   nenhuma, inclusive a isenção que mantém o Twilio funcionando. Os testes 13 e 14 são a única
   cobertura que existe disso.

Como a suíte, esta também **não tem backup em `/tmp`**: nada aqui escreve nas linhas do piloto.
Os tenants de fixture nascem do próprio formulário, sob o prefixo `suite-s3c-`, e
`_drop_orphan_fixtures()` no início do `main()` faz o papel de reparo pós-crash.

---

## A CLI manual

```bash
python tests/test_signup/test_signup.py status
python tests/test_signup/test_signup.py signup --name "Suite Manual S3C" \
    --email manual-s3c@suite.corujai.test --password senha-boa-123
python tests/test_signup/test_signup.py onboarding --tenant suite-manual-s3c
python tests/test_signup/test_signup.py honeypot                # bot: nada é criado
python tests/test_signup/test_signup.py flood --ip 203.0.113.10 # estoura o teto
python tests/test_signup/test_signup.py csrf                    # painel bloqueia, webhook passa
python tests/test_signup/test_signup.py drop-tenant --tenant suite-manual-s3c
python tests/test_signup/test_signup.py clear-attempts
```

---

## Roteiro

### 1. Com a flag desligada, a rota não existe

**O que fazer:** com o `src/.env` como veio (`SIGNUP_ENABLED` ausente ou `false`),

```bash
cd src && python app.py
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:5000/dashboard/signup
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:5000/dashboard/signup
```

**O que esperar:** **404** nos dois, e a tela de login **sem** o link de cadastro.

**Por que 404 e não 403:** 403 diz "existe algo aqui, volte quando puder". 404 não diz nada. E o
`abort` é a primeira linha da rota, antes de qualquer leitura do formulário — com a flag
desligada não há caminho de execução nenhum.

### 2. Com a flag ligada, o formulário aparece

**O que fazer:** `SIGNUP_ENABLED=true python app.py` e abra `/dashboard/login`.

**O que esperar:** o link "Criar a conta da sua academia" aparece. A tela de cadastro tem quatro
campos — nome da academia, e-mail, senha, confirmação — e mais nada. Telefone do dono e número da
academia ficam para Configurações, onde já têm as guardas do S1; puxá-los para cá levaria essas
validações para um endpoint público sem ganho nenhum.

Veja o fonte da página (`Ctrl+U`): há um `<input name="csrf_token">` e um
`<input name="website">` que você não enxerga na tela. O segundo é o honeypot.

### 3. Um cadastro válido cria a academia inteira

**O que fazer:** preencha e envie.

**O que esperar:** você cai em `/dashboard/onboarding`, já logado.

```sql
SELECT tenant_id, integration_status FROM owners ORDER BY tenant_id;
SELECT academy_name, assistant_name FROM ai_configs WHERE tenant_id = '<nova>';
-- o nome é o que você digitou; o resto são os textos-guia entre colchetes
SELECT marker, label, capacity, is_fallback FROM class_types WHERE tenant_id = '<nova>';
-- ADULTOS | Adultos | NULL | true      ← a turma padrão, ilimitada
SELECT days_ahead FROM scheduling_configs WHERE tenant_id = '<nova>';   -- 14
SELECT email, tenant_id FROM users WHERE tenant_id = '<nova>';
```

Cinco tabelas, **numa transação só**. A rota não tem regra de negócio nenhuma: ela valida o
formulário e chama o `provision_tenant()` que o S3a já tinha escrito.

### 4. O identificador não é escolhido pelo visitante

**O que fazer:** mande um `tenant_id` a mais no POST, à mão:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:5000/dashboard/signup \
  -d "academy_name=Teste Slug" -d "email=slug@suite.corujai.test" \
  -d "password=senha-boa-123" -d "password_confirm=senha-boa-123" \
  -d "tenant_id=escolhido-por-mim"
```
(vai dar 400 por falta do token CSRF — use a suíte ou a CLI para o teste completo)

**O que esperar:** o slug sai do **nome**, nunca do campo. `provision_tenant()` aceita
`tenant_id` porque o CLI do fundador usa, mas lê-lo de um formulário público deixaria um estranho
escolher uma chave primária — e disputar nomes bons com outras academias.

### 5. As três recusas do formulário

| Entrada | Resposta esperada |
|---|---|
| senhas diferentes | 200, "As senhas não coincidem." |
| e-mail sem `@` | 200, "E-mail inválido…" (mensagem do `provision_tenant`) |
| senha com menos de 8 | 200, "A senha precisa ter pelo menos 8 caracteres." |
| nome vazio | 200, "O nome da academia não pode ficar em branco." |

**Nenhuma delas pode dar 500**, e nenhuma pode escrever linha nenhuma. As validações não são da
tela: `users.normalize_email()` e `users.validate_password()` já existiam, e o
`provision_tenant()` as aplica **antes** de tocar o banco.

### 6. E-mail repetido não conta se o e-mail existe

**O que fazer:** cadastre duas vezes com o mesmo e-mail.

**O que esperar:** *"Não foi possível criar a conta com esses dados. Se você já tem conta,
entre."* — e nenhuma das cinco tabelas cresce.

**Por que essa frase morna:** o `/login` do S3a é genérico de propósito (uma mensagem só para
e-mail errado e senha errada, mais um hash falso contra ataque de tempo), justamente para não
servir de oráculo de quais endereços têm conta. Um cadastro que responde "esse e-mail já está
cadastrado" devolve exatamente esse oráculo.

### 7. O honeypot

**O que fazer:**

```bash
python tests/test_signup/test_signup.py honeypot
```

**O que esperar:** status **200**, a mesma tela de sucesso que um humano veria, e **zero** linhas
criadas.

**Por que fingir sucesso:** responder um erro — ou um status diferente — ensina o próximo bot a
pular o campo, e a armadilha deixa de funcionar. O campo é escondido com `position: absolute;
left: -9999px`, e **não** com `display: none`: esconder com `display` é o primeiro filtro que
qualquer script de spam aplica, e um campo que ele ignora nunca é preenchido.

### 8. O teto por IP

**O que fazer:**

```bash
python tests/test_signup/test_signup.py flood --ip 203.0.113.10
```

**O que esperar:** as cinco primeiras tentativas passam pela rota; a sexta devolve **429**. Um IP
diferente continua passando.

```sql
SELECT ip_hash, created_at FROM signup_attempts ORDER BY created_at DESC LIMIT 5;
```

**Duas decisões visíveis nessa tabela:**

- **A contagem vive no Postgres, não em memória.** O gunicorn roda vários workers, cada um com
  seus próprios globais; um contador em processo veria ~1/N das tentativas e deixaria passar N
  vezes a taxa pretendida. Os workers só compartilham o banco.
- **O IP é gravado em hash, nunca em claro.** A pergunta é só "já vi este cliente?", que
  igualdade responde. Um IP é dado pessoal e o schema deste projeto é público; o `sha256` usa o
  `FLASK_SECRET_KEY` como sal, então um dump sozinho não reverte nada.

Limpe com `clear-attempts` antes de seguir.

### 9. ⚠️ O CSRF, e a armadilha central do módulo

**O que fazer:**

```bash
python tests/test_signup/test_signup.py csrf
```

**O que esperar:**

```
POST /dashboard/signup sem token : 400
POST /dashboard/login  sem token : 400
POST /webhook          sem token : 200   ← TEM que ser 200
```

**`CSRFProtect(app)` intercepta TODO POST da aplicação — inclusive o do Twilio.** O Twilio não
manda token nenhum: sem `csrf.exempt(webhook_bp)` no `app.py`, cada mensagem de lead passaria a
receber 400, ninguém seria respondido, e **nada no log pareceria um erro** — o bot simplesmente
ficaria mudo. É a única falha deste módulo que derruba o produto em silêncio.

Confirme também à mão, no painel logado, os **14 pontos de POST**: responder um lead no inbox,
devolver a conversa para a IA, confirmar e cancelar um agendamento, salvar as três seções de
Configurações (IA, uma turma, tornar padrão, excluir, criar turma, marcar aula, janela, conta) e
desconectar o Google Calendar. **Qualquer 400 ali significa que faltou o token naquele
formulário.**

Os três `hx-post` (responder no inbox, confirmar e cancelar agendamento) não têm token no botão:
o `<body>` da página carrega `hx-headers='{"X-CSRFToken": "…"}'` e o HTMX herda isso dos
ancestrais, inclusive no conteúdo que o polling troca a cada 5s. É uma linha por página em vez de
uma por botão — e os parciais, que são re-renderizados o tempo todo, não carregam token nenhum.

### 10. A tela de primeiros passos

**O que fazer:** entre na conta nova e olhe `/dashboard/onboarding`. Depois conecte o Google
Calendar e volte.

**O que esperar:** 4 pendências no começo; o item do Calendar marca sozinho quando você conecta.
Preencha a seção IA em Configurações e ele marca outro. Cadastre uma turma além da padrão e marca
mais um.

O último — **número de WhatsApp** — não tem botão, e diz "aguardando". É o único passo que o dono
não resolve sozinho: depende de um Sender aprovado no Twilio, que é etapa sua. **Dizer isso na
tela é o ponto da página.** Sem essa frase, o dono configura tudo corretamente e fica sem
entender por que a IA não atende ninguém.

Para marcar o último, à mão:

```sql
UPDATE owners SET whatsapp_number = '5529000123456' WHERE tenant_id = '<nova>';
```

**A checklist não tem estado próprio.** Não existe coluna `onboarding_completed`, e não deve
existir: uma lista com estado próprio é um segundo registro de um fato que as tabelas já guardam,
e os dois divergem. Cada passo é derivado — `integration_status`, os textos-guia de `ai_configs`,
quantas turmas o tenant tem, `whatsapp_number`. Desfaça o trabalho e o passo desmarca.

### 11. Limpeza

```bash
python tests/test_signup/test_signup.py drop-tenant --tenant <nova>
python tests/test_signup/test_signup.py clear-attempts
python tests/test_signup/test_signup.py status
```

```sql
SELECT tenant_id FROM owners;        -- só 'default'
SELECT email, tenant_id FROM users;  -- só a sua conta
```

E **tire o `SIGNUP_ENABLED=true`** do ambiente.

---

## Regressão obrigatória

As nove suítes:

```bash
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

**Seis arquivos de teste precisaram de uma linha por causa do CSRF.** O Flask-WTF **não** desliga
a proteção por causa de `TESTING = True` — ele olha só `WTF_CSRF_ENABLED`. Sem
`app.config["WTF_CSRF_ENABLED"] = False`, todo POST de test client volta 400 sem nunca chegar ao
código sob teste. São 7 pontos, em `test_confirmation`, `test_inbox`, `test_settings`,
`test_class_types` (dois), `test_accounts_suite` e `test_accounts.py`.

`test_owner_notifications` **não** precisou: ele monta um `Flask(__name__)` cru e registra só o
`webhook_bp`, sem `CSRFProtect` nenhum.

---

## O que este módulo deliberadamente **não** faz

- **Verificação de e-mail.** Não há canal de e-mail no projeto — nem SMTP, nem lib, nem serviço —
  e o e-mail aqui é identificador de login, não canal. Quem se cadastrar com endereço errado
  simplesmente não consegue operar, e a conta fica parada, visível para você.
- **Recuperação de senha.** Consequência da anterior. A tela de login diz "fale com o suporte" em
  vez de oferecer um link que não funcionaria; você resolve com
  `python -m accounts.provision reset-password`.
- **Aprovação manual.** O acesso é imediato; o que segura o bot é o `whatsapp_number` estar nulo,
  que já era verdade tecnicamente antes deste módulo.
- **CAPTCHA.** Seria mais eficaz que o honeypot e quebraria a regra de "sem pipeline de build,
  sem script de terceiro" que o projeto mantém desde o começo.
- **Rate limit no `/login`.** O teto por IP cobre só o cadastro. O `/login` continua sem nenhum.

---

## Onde as coisas moram

| Arquivo | Responsabilidade |
|---|---|
| `config.py` | `SIGNUP_ENABLED`, default `false` |
| `app.py` | `CSRFProtect(app)` e — **crucial** — `csrf.exempt(webhook_bp)` |
| `webhook/routes.py::signup` | Valida o formulário e chama `provision_tenant()`. Sem regra de negócio |
| `webhook/routes.py::_client_ip` | `X-Forwarded-For` antes de `remote_addr` (o proxy da Railway) |
| `accounts/signup.py` | Honeypot e teto por IP. Sem Flask |
| `accounts/onboarding.py` | A checklist, derivada. Sem estado próprio |
| `templates/signup_done.html` | A resposta do honeypot, e só dela |
