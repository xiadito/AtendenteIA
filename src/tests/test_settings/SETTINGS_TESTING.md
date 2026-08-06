# Módulo S1 — Aba de Configurações (IA + Conta)

Roteiro de teste da primeira tela de **escrita** de configuração do projeto. Até aqui,
ajustar a personalidade da IA ou o telefone do dono exigia SQL na mão — a própria
migration 003 registra isso no cabeçalho (*"owner_phone has no route yet — it is filled by
a manual UPDATE"*).

A tela tem **duas seções na mesma página**, com **formulários independentes**:

- **IA** — os cinco campos de `ai_configs`, a camada customizável do prompt.
- **Conta** — o `owner_phone` (editável) e o status do Google Calendar (só leitura).

---

## ⚠️ Antes de começar: esta suíte mexe nos dados reais

Diferente das outras suítes, que criam linhas próprias sob o prefixo delas, esta precisa
sobrescrever **a linha real** de `ai_configs` e o **`owner_phone` real** de `owners` — as
duas tabelas têm uma linha por tenant, e o piloto é o único tenant.

As duas são salvas em `/tmp/corujai_settings_backup.json` antes da primeira escrita e
restauradas no teardown, **inclusive o `updated_at`**, para que uma rodada de teste não
deixe rastro nenhum. Se uma rodada morrer no meio, a próxima restaura sozinha na largada.

Se for mexer à mão pelo CLI, rode `backup` antes e `restore` depois.

---

## Como rodar

Tudo a partir de `src/`, com o virtualenv ativo:

```bash
# Suíte automatizada (12 testes, determinística, sem rede)
python tests/test_settings/test_settings_suite.py

# Mantém tudo como ficou, para inspecionar no DBeaver
python tests/test_settings/test_settings_suite.py --keep

# Também grava relatório JSON em tests/outputs/
python tests/test_settings/test_settings_suite.py --json
```

A suíte não usa LLM, WhatsApp nem Google Calendar: a tela só lê e escreve duas linhas do
Postgres. Ela sobe a app com `create_app()` e dirige tudo pelo `test_client` do Flask,
logado como o dono estaria.

Sai com código `0` só se os 12 passarem.

---

## O que a suíte cobre

| # | Teste | O que prova |
|---|---|---|
| P1 | Schema aplicado | `001_create_sessions`, `003_create_owners` e `005_create_ai_configs` presentes |
| F1 | Backup feito | Os valores reais estão salvos antes de qualquer escrita |
| 1 | `update_ai_config` grava os cinco campos | Persistência + `updated_at` avança |
| 2 | `get_ai_config` enxerga a escrita seguinte | **Não há cache** no caminho da config |
| 3 | `normalize_owner_phone` | 6 formatos aceitos, 7 recusados (função pura) |
| 4 | Salvar telefone grava no formato do webhook | `get_owner_by_phone` reconhece o número salvo |
| 5 | Formato sujo chega limpo | `whatsapp:+55 26 09999-8888` → `5526099998888` |
| 6 | **Guarda (b)** | Número de lead é recusado; `owner_phone` intacto |
| 7 | Telefone inválido recusado | Vazio, curto e não-numérico não gravam |
| 8 | Campo vazio da IA recusado | Banco intacto **e** o texto digitado volta na tela |
| 9 | GET mostra o estado atual | As duas seções, cada uma com seu próprio `POST` |
| 10 | Autenticação | As três rotas redirecionam para o login sem sessão |

---

## As duas guardas do telefone — por que existem

`owner_phone` **não é um campo de cadastro qualquer**. O webhook chama
`store.get_owner_by_phone(clean_number)` em **toda mensagem que entra**, para decidir se
quem escreveu é o dono respondendo uma notificação ou um lead conversando. Gravar errado
não mostra erro em lugar nenhum — falha em silêncio, de dois jeitos:

1. **O dono deixa de ser reconhecido.** O `1`/`2` dele para de fechar agendamento.
2. **Pior:** se o número salvo for de alguém que conversa com o bot, essa pessoa passa a
   ser tratada como dona, e as mensagens dela viram comandos de confirmação.

Daí as duas guardas no `POST`:

**(a) Normalizar para o formato que o webhook compara.** O webhook monta `clean_number`
como `sender.replace("whatsapp:+", "")` — só dígitos. `normalize_owner_phone()` descarta
tudo que não é dígito e exige de 10 a 15 (15 é o teto do E.164; o piso barra um paste
truncado). Um número BR com DDI tem 12 ou 13, mas travar aí prenderia o produto a um país.

**(b) Recusar um número que já é `sessions.sender`.** A consulta é em `sessions`, e **não
em `owners`**, de propósito: o perigo aqui não é haver dois donos com o mesmo número — é um
número ser **lead e dono ao mesmo tempo**. Aí o roteamento tem duas respostas certas,
escolhe a do dono, e sequestra a conversa do lead. (Unicidade entre donos é assunto da
constraint `UNIQUE` que ainda não existe na coluna — dívida registrada para o S3.)

---

## Teste manual pelo CLI

Uma operação por chamada, para conferir cada passo no DBeaver entre um comando e outro:

```bash
# 0. Salvar os valores reais antes de mexer
python tests/test_settings/test_settings.py backup

# 1. Ver as duas seções como a tela as exibiria
python tests/test_settings/test_settings.py show

# 2. Editar a IA (só os campos passados mudam)
python tests/test_settings/test_settings.py set-ai \
    --academy-name "Delariva Itaipuaçu" --assistant-name "Corujinha"

# 3. Editar o telefone — o CLI mostra qual guarda recusou
python tests/test_settings/test_settings.py set-phone --phone "whatsapp:+55 21 99999-9999"
python tests/test_settings/test_settings.py set-phone --phone "123"          # guarda (a)

# 4. Ver como um número seria gravado, sem gravar
python tests/test_settings/test_settings.py normalize --phone "(21) 99999-9999"

# 5. Provocar a guarda (b): criar um lead e tentar usar o número dele
python tests/test_settings/test_settings.py seed-lead --sender 5526000000001
python tests/test_settings/test_settings.py set-phone --phone 5526000000001  # guarda (b)

# 6. Desfazer
python tests/test_settings/test_settings.py reset --sender 5526000000001
python tests/test_settings/test_settings.py restore
```

---

## Teste manual pela tela

```bash
cd src && python app.py
```

1. Entrar em `http://localhost:5000/dashboard/login` e logar.
2. No menu, clicar em **⚙️ Configurações**.
3. **Seção IA:** mudar o nome da atendente e salvar. O aviso verde deve dizer que vale a
   partir da próxima mensagem.
4. Mandar uma mensagem de lead pelo `tests/test_ai_action/test_ai_action.py` e conferir que
   a atendente se apresenta com o nome novo — **sem reiniciar a app**. É a prova prática de
   que não há cache.
5. **Seção Conta:** salvar o próprio número com espaços e `+`. Conferir no banco que ficou
   só com dígitos.
6. Apagar o campo e salvar: deve recusar com "Número inválido", sem gravar.
7. Conferir que o status do Google Calendar bate com o da tela de integrações, e que o link
   leva até lá.

Verificação no DBeaver:

```sql
-- O que a seção IA gravou
SELECT academy_name, assistant_name, tone, updated_at FROM ai_configs WHERE tenant_id = 'default';

-- O que a seção Conta gravou — precisa ser só dígitos
SELECT tenant_id, owner_phone, integration_status FROM owners;

-- A prova de que o formato serve ao roteamento: esta busca é a que o webhook faz
SELECT id, tenant_id, owner_phone FROM owners WHERE owner_phone = '5521999999999';
```

---

## O que este módulo deliberadamente **não** faz

- **Não cria migration.** Nenhuma coluna nova. A janela de editar migrations está fechada
  (ver `CLAUDE.md`), e nada aqui precisava de schema novo.
- **Não adiciona `UNIQUE` em `owner_phone`.** A guarda (b) evita o pior caso na aplicação,
  mas a constraint no banco é do S3, junto da migration que cria `whatsapp_number`.
- **Não mostra `whatsapp_number`.** Essa coluna só nasce no S3.
- **Não guarda histórico de versões** da config: a última gravação do dono vale.
- **Não reimplementa o OAuth do Google.** A seção Conta só exibe o status e linka para a
  tela de integrações do Módulo 1.
- **Não tem controle de papéis.** Quem está logado no painel edita as duas seções.
