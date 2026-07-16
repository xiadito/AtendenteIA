# Módulo 1 — Onboarding Google Calendar

Este documento descreve como testar, de ponta a ponta, a integração OAuth 2.0 com o
Google Calendar implementada no Módulo 1. Ele é auto-contido: quem for executar os
testes não precisa ter acompanhado a conversa de desenvolvimento.

## Pré-requisitos

### Variáveis de ambiente (`src/.env`)

```
FLASK_SECRET_KEY=<qualquer string aleatória>
DASHBOARD_PASSWORD=<senha do dashboard>
DATABASE_URL=postgresql://<user>:<senha>@localhost:5432/<banco>

GOOGLE_CLIENT_ID=<Client ID gerado no Google Cloud Console>
GOOGLE_CLIENT_SECRET=<Client Secret gerado no Google Cloud Console>
GOOGLE_REDIRECT_URI=http://localhost:5000/integrations/google/callback
```

### Google Cloud Console

Antes de testar, confirme que:
- O projeto tem a **Google Calendar API** ativada.
- A **OAuth consent screen** está em modo "Testing", com o e-mail Google que você vai
  usar para testar cadastrado em **"Usuários de teste"**.
- As credenciais OAuth (tipo "Aplicativo da Web") têm `http://localhost:5000/integrations/google/callback`
  cadastrado em **"URIs de redirecionamento autorizados"** — precisa ser string idêntica
  ao valor de `GOOGLE_REDIRECT_URI` acima.

### Dependências e banco

```bash
cd /caminho/para/AtendenteIA
pip install -r requirements.txt
```

O Postgres apontado por `DATABASE_URL` precisa estar rodando e acessível — a migration
`003_create_owners.sql` é aplicada automaticamente na primeira subida do app (não precisa
rodar nada manualmente).

## Como subir o ambiente (Arch Linux)

```bash
source venv/bin/activate
cd src
python app.py
```

Confira no log de startup as linhas:
```
INFO - Migration 001_create_sessions_and_orders already applied, skipping.
INFO - Migration 002_create_products already applied, skipping.
INFO - Migration 003_create_owners applied successfully.
Migrations rodaram com sucesso.
App created
```

Acesse `http://localhost:5000/dashboard/login` e entre com `DASHBOARD_PASSWORD`.

## Roteiro de testes

### 1. Estado inicial (linha seed)

**O que fazer:** logo após a primeira subida do app, antes de qualquer interação.

**O que esperar:** a tabela `owners` já tem uma linha, criada pela própria migration.

**Como verificar:**
```sql
SELECT tenant_id, google_email, refresh_token, calendar_id, integration_status
FROM owners;
```
Esperado: uma linha, `tenant_id = 'default'`, `google_email`/`refresh_token`/`calendar_id`
nulos, `integration_status = 'disconnected'`.

### 2. Tela de status — não conectado

**O que fazer:** logado no dashboard, acesse `http://localhost:5000/integrations/google`.

**O que esperar:** badge "Não conectado" e botão "Conectar Google Calendar".

**Como verificar:** visual — não depende do banco além do que já foi checado no passo 1.

### 3. Caminho feliz — conectar

**O que fazer:** clique em "Conectar Google Calendar". Você será redirecionado à tela de
consentimento do Google. Escolha a conta cadastrada como usuário de teste e autorize.

**O que esperar:** volta para `/integrations/google` mostrando badge "Conectado", o e-mail
da conta Google e "Aulas Experimentais". Confira também no Google Calendar
(calendar.google.com, na sua conta) que existe um novo calendário chamado
**"Aulas Experimentais"** na lista à esquerda.

**Como verificar:**
```sql
SELECT google_email, refresh_token, calendar_id, integration_status
FROM owners WHERE tenant_id = 'default';
```
Esperado: `integration_status = 'connected'`, `google_email` preenchido com a conta
usada, `refresh_token` preenchido (uma string longa), `calendar_id` preenchido (formato
`algumacoisa@group.calendar.google.com`).

### 4. Caminho de erro — `state` inválido (proteção CSRF)

**O que fazer:** com uma sessão de dashboard autenticada, acesse manualmente no navegador:
`http://localhost:5000/integrations/google/callback?state=valor-forjado&code=qualquercoisa`

**O que esperar:** a tela de status é exibida com uma mensagem de erro
("Falha de segurança na autenticação..."), **sem** erro 500 e sem consumir o `code`.

**Como verificar:** confira no terminal do servidor o log
`WARNING - OAuth state mismatch or missing on Google callback.`. Repita o fluxo de
conexão normal (passo 3) logo em seguida e confirme que ainda funciona — isso prova que
o `state` inválido não deixou a sessão em um estado quebrado.

### 5. Desconectar

**O que fazer:** na tela de status conectada, clique em "Desconectar".

**O que esperar:** volta para a tela mostrando "Não conectado" novamente.

**Como verificar:**
```sql
SELECT google_email, refresh_token, calendar_id, integration_status
FROM owners WHERE tenant_id = 'default';
```
Esperado: todos os campos de credencial voltam a `NULL`, `integration_status = 'disconnected'`.
No log do servidor, confira a tentativa de revogação — pode aparecer um
`WARNING - Failed to revoke Google token: ...` se o Google já tiver expirado o token
por outro motivo, o que é aceitável (revogação é *best-effort*: o disconnect local
acontece de qualquer forma).

### 6. Reconectar sem duplicar o calendário

**O que fazer:** logo após o passo 5, clique em "Conectar Google Calendar" de novo e
autorize novamente.

**O que esperar:** volta para "Conectado". No Google Calendar, confirme que **não** foi
criado um segundo calendário "Aulas Experimentais" — o sistema deve ter reaproveitado o
calendário já existente (busca por nome).

**Como verificar:**
```sql
SELECT calendar_id FROM owners WHERE tenant_id = 'default';
```
Esperado: o mesmo `calendar_id` do passo 3 (ou seja, o valor não mudou entre a primeira
conexão e a reconexão).

### 7. Token revogado externamente (`needs_reconnect`)

Este cenário simula o dono revogando o acesso do ZAP AI pela própria conta Google
(situação real: ele pode fazer isso em `myaccount.google.com/permissions` sem passar
pelo SaaS).

**O que fazer:**
1. Com a integração conectada (passo 3 ou 6), acesse
   `https://myaccount.google.com/permissions` na conta Google usada, encontre o app e
   revogue o acesso.
2. Abra um shell Python dentro do ambiente do projeto:
   ```bash
   cd src
   python
   ```
   ```python
   import integrations.store as store
   import integrations.google_calendar as google_calendar

   owner = store.get_owner_credentials()
   google_calendar.build_credentials(owner["refresh_token"])
   ```

**O que esperar:** a chamada levanta `integrations.google_calendar.NeedsReconnectError`
em vez de deixar uma exceção genérica subir.

**Como verificar:** é uma verificação de código, não de banco — o teste passa se a
exceção específica for levantada. (No Módulo 1 nada chama `build_credentials`
automaticamente ainda; isso é usado pelo motor de agendamento do Módulo 2. Para
simular o estado na tela agora, rode manualmente `store.mark_needs_reconnect()` no
mesmo shell e recarregue `/integrations/google` — deve aparecer o badge "Reconexão
necessária" com o botão "Reconectar Google Calendar".)

## Como confirmar no banco (resumo das queries)

```sql
-- Ver o estado atual da integração
SELECT * FROM owners WHERE tenant_id = 'default';

-- Confirmar que a migration 003 foi aplicada
SELECT version, applied_at FROM schema_migrations WHERE version = '003_create_owners';
```

## Troubleshooting

| Sintoma | Causa provável | Como resolver |
|---|---|---|
| Erro `redirect_uri_mismatch` na tela do Google | O valor de `GOOGLE_REDIRECT_URI` no `.env` não é **exatamente igual, caractere a caractere**, a um dos URIs cadastrados no Google Cloud Console (protocolo, domínio, porta, path) | Comparar os dois valores lado a lado; `http://localhost:5000/...` e `http://127.0.0.1:5000/...` são URIs **diferentes** para o Google |
| Acesso negado / "app não verificado" na tela de consentimento | A conta usada para testar não está cadastrada em "Usuários de teste" na OAuth consent screen | Adicionar o e-mail em Google Cloud Console → Tela de consentimento OAuth → Usuários de teste |
| `owners.refresh_token` fica `NULL` mesmo após conectar / erro "Google did not return a refresh_token" | Faltou `access_type=offline` e/ou `prompt=consent` na URL de autorização, ou o usuário já tinha consentido antes e o Google não reemitiu o refresh_token | Conferir que `integrations/google_calendar.py::build_authorization_url` sempre passa os dois parâmetros; se persistir, revogar o acesso manualmente em myaccount.google.com/permissions e reconectar do zero |
| `invalid_grant` ao tentar usar as credenciais salvas | O refresh_token foi revogado (pelo dono, pelo Google, ou por expiração de um app em modo "Testing" após ~7 dias) | É o cenário do passo 7 do roteiro — a tela deve indicar "Reconexão necessária"; o dono precisa clicar em "Conectar" de novo |
| 500 Internal Server Error ao acessar `/integrations/google` | Banco de dados inacessível (`DATABASE_URL` errado ou Postgres fora do ar) — mesmo comportamento já existente em `/dashboard/index`, o projeto não tem tratamento de erro de conexão nas rotas | Verificar se o Postgres está no ar e se `DATABASE_URL` está correto |
| `403 accessNotConfigured` ao tentar criar/listar calendários | A Google Calendar API não foi ativada no projeto do Google Cloud Console | Ativar em APIs e Serviços → Biblioteca → Google Calendar API |
