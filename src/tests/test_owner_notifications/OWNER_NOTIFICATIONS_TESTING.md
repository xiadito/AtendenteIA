# Notificações ao Dono

Este roteiro testa a fila de notificação assíncrona ao dono da academia: quando a IA
fecha um agendamento (`action: book`) ou o lead pede atendimento humano (`action:
handoff`), o request enfileira uma linha em `owner_notifications` — nunca envia
WhatsApp diretamente. Um **serviço de cron separado** (`jobs/drain_notifications.py`)
drena a fila, envia ao dono via Twilio, e faz retry até um teto de tentativas. Quando
o dono responde `1` (confirma) ou `2` (cancela), o webhook reconhece o número e apenas
**registra** a resposta — a baixa efetiva do agendamento é de uma feature futura.

A IA com JSON de ação (que produz o `book`/`handoff` que este roteiro consome) já foi
testada em
[`../test_ai_action/AI_ACTION_TESTING.md`](../test_ai_action/AI_ACTION_TESTING.md).
Aqui o foco é a **camada de notificação**: enfileiramento idempotente, o cron de
drenagem, e o reconhecimento da resposta do dono.

Tudo o que chega ao dono e ao lead é em português; o contrato e o código, em inglês.

---

## Pré-requisitos

### Migrations 001–006 aplicadas, com `owner_phone` em `owners`

`owner_phone` foi adicionado **editando a migration 003 no lugar** (não é uma migration
nova) — por isso um banco que já tinha a `owners` antiga não ganha a coluna sozinho.
Confira as duas coisas separadamente:

```sql
SELECT version FROM schema_migrations ORDER BY version;
-- deve ir de 001_create_sessions até 006_create_owner_notifications

SELECT column_name FROM information_schema.columns
WHERE table_name = 'owners' AND column_name = 'owner_phone';
-- precisa retornar uma linha; se não retornar, recrie o banco (ver CLAUDE.md)
```

Se a coluna não existir, **não há como migrar em cima** — o banco local (e, no primeiro
deploy real, o do Railway) precisa ser derrubado e recriado do zero.

### `owner_phone` configurado no piloto

Não existe tela para isso ainda; defina por SQL:

```sql
UPDATE owners SET owner_phone = '5521999999999' WHERE tenant_id = 'default';
```

Sem isso, `bot/handlers.py` pula o enfileiramento com um `logger.warning` — a conversa
com o lead continua normalmente, só o dono não é avisado.

---

## Como subir o ambiente (Arch Linux)

```bash
# na raiz do repositório
source venv/bin/activate

# sobe a app uma vez para aplicar as migrations (001–006)
cd src && python app.py        # Ctrl-C depois que logar "Migration 006 ... applied"
```

Todos os comandos abaixo rodam **de dentro de `src/`**.

---

## Como testar sem WhatsApp — o CLI manual

`test_owner_notifications.py` enfileira, drena e simula a resposta do dono sem
depender do Twilio real — `whatsapp_service.send_message` é substituído por uma versão
que só imprime no console.

```bash
# configura o número de teste do dono (dígitos puros, sem "whatsapp:+")
python tests/test_owner_notifications/test_owner_notifications.py set-phone --phone 5521999999999

# enfileira uma notificação de handoff para um lead fictício
python tests/test_owner_notifications/test_owner_notifications.py enqueue \
    --lead-sender 5523000000001 --event-type handoff

# lista as pendentes
python tests/test_owner_notifications/test_owner_notifications.py list

# roda o cron uma vez (imprime a mensagem no console em vez de mandar pelo Twilio)
python tests/test_owner_notifications/test_owner_notifications.py drain

# simula a resposta do dono, sem passar pelo Twilio
python tests/test_owner_notifications/test_owner_notifications.py reply --phone 5521999999999 --body 1

# limpa as notificações de um lead de teste
python tests/test_owner_notifications/test_owner_notifications.py reset --lead-sender 5523000000001
```

> Use um `lead-sender` com prefixo `5523000...` nos testes manuais: é o mesmo prefixo
> que a suíte automatizada limpa, então nada de real fica sujo.

---

## Roteiro de testes

Cada passo traz **O que fazer / O que esperar / Como verificar**.

### 1. `enqueue_notification` cria uma linha `pending`

**O que fazer:** enfileire uma notificação (CLI `enqueue`, ou deixe a IA fechar um
agendamento/handoff de verdade via `test_ai_action.py`).

**O que esperar:** uma linha nova em `owner_notifications` com `status='pending'` e
`attempts=0`.

**Como verificar:**
```sql
SELECT event_type, lead_sender, booking_id, status, attempts
FROM owner_notifications ORDER BY created_at DESC LIMIT 1;
```

### 2. Índice parcial de **booking** impede duplicata

**O que fazer:** enfileire duas vezes uma notificação `booking` com o **mesmo**
`booking_id` (via `_execute_booking`, ou chamando `enqueue_notification` duas vezes
com o mesmo `booking_id`).

**O que esperar:** a segunda chamada retorna `False` (nada é inserido) — uma reserva
só gera uma notificação, uma única vez.

**Como verificar:**
```sql
SELECT COUNT(*) FROM owner_notifications WHERE booking_id = '<id da reserva>';
-- deve ser 1
```

### 3. Índice parcial de **handoff**: bloqueia enquanto pendente, libera após resposta

**O que fazer:** peça handoff duas vezes seguidas para o mesmo lead (ou enfileire duas
vezes via CLI com o mesmo `lead_sender`). Depois, marque a primeira como enviada e
registre a resposta do dono; peça handoff de novo.

**O que esperar:** a segunda tentativa, **enquanto a primeira não tem resposta**, é
bloqueada. Depois que o dono responde (`owner_response` preenchido), um **novo**
handoff do mesmo lead volta a ser aceito.

**Como verificar:**
```sql
SELECT lead_sender, status, owner_response FROM owner_notifications
WHERE lead_sender = '<numero>' AND event_type = 'handoff' ORDER BY created_at;
```

### 4. Cron: `pending` → `sent` no caminho feliz

**O que fazer:** com uma notificação `pending`, rode o CLI `drain` (ou espere o serviço
de cron do Railway, em produção).

**O que esperar:** a mensagem é composta em português (resumo da reserva, ou aviso de
handoff) e "enviada" (impressa no console, no CLI manual); a linha vira `status='sent'`
com `sent_at` preenchido.

**Como verificar:**
```sql
SELECT status, sent_at FROM owner_notifications WHERE id = <id>;
```

### 5. Cron: falha de envio incrementa `attempts`; ao esgotar, vira `failed`

**O que fazer:** na suíte automatizada, isso é simulado com um `send_message` que
sempre levanta exceção, rodando o drain `MAX_ATTEMPTS` vezes seguidas. Manualmente,
seria preciso derrubar o Twilio de propósito — não vale a pena reproduzir à mão.

**O que esperar:** cada tentativa falha incrementa `attempts` em 1 e mantém
`status='pending'`; ao atingir `MAX_ATTEMPTS` (5), a linha vira `status='failed'` e
para de aparecer no drain seguinte.

**Como verificar:**
```sql
SELECT attempts, status FROM owner_notifications WHERE id = <id>;
```

### 6. `register_owner_response`: `1`→`confirmed`, `2`→`cancelled`, nunca toca `trial_bookings`

**O que fazer:** com uma notificação `sent` pendente de resposta, simule a resposta do
dono (CLI `reply --body 1` ou `--body 2`).

**O que esperar:** `owner_response` é gravado na notificação `sent` mais recente sem
resposta daquele `owner_phone`. **`trial_bookings.status` não muda** — fechar a reserva
com base nessa resposta é uma feature futura.

**Como verificar:**
```sql
SELECT owner_response FROM owner_notifications WHERE id = <id>;
SELECT status FROM trial_bookings WHERE id = '<booking_id>';  -- inalterado
```

### 7. `get_owner_by_phone` reconhece o dono; número desconhecido segue como lead

**O que fazer:** consulte `get_owner_by_phone()` com o número configurado no
pré-requisito, e depois com um número qualquer não cadastrado.

**O que esperar:** o número do dono retorna a linha de `owners`; um número desconhecido
retorna `None` — é assim que o webhook decide "isso é o dono ou é um lead".

### 8. Roteamento do webhook: número do dono cai em `receive_twilio_owner`, não em `handle_text_message`

**O que fazer:** mande uma mensagem simulando `From=whatsapp:+<numero do dono>` para
`/webhook`, e outra com um número desconhecido.

**O que esperar:** a mensagem do dono é roteada para `receive_twilio_owner()` (nunca
chega a `handle_text_message`); a mensagem do número desconhecido segue o fluxo normal
de lead.

---

## Como confirmar no banco

```sql
-- fila de notificações, mais recentes primeiro
SELECT event_type, lead_sender, booking_id, status, attempts, owner_response, created_at
FROM owner_notifications ORDER BY created_at DESC;

-- owner_phone configurado
SELECT tenant_id, owner_phone FROM owners;
```

---

## Como rodar a suíte automatizada

```bash
python tests/test_owner_notifications/test_owner_notifications_suite.py            # tudo
python tests/test_owner_notifications/test_owner_notifications_suite.py --keep      # não limpa (para depurar)
python tests/test_owner_notifications/test_owner_notifications_suite.py --no-color  # sem cores ANSI
python tests/test_owner_notifications/test_owner_notifications_suite.py --json      # grava relatório em tests/outputs/
```

**Como ler o relatório:** cada passo aparece como `✔` (PASS), `✖` (FAIL/ERROR) ou `○`
(SKIP). A linha final resume `N testes · X passaram · Y falharam · Z pulados`. O
**exit code é 0** somente quando nada falhou.

A suíte é inteiramente determinística — `whatsapp_service.send_message` é sempre
substituído por um dublê, então nenhum WhatsApp real sai durante a run. Como
`owners.tenant_id` é `UNIQUE` e o piloto tem só uma linha, a suíte **não cria uma nova
linha em `owners`**: ela lê a linha existente, guarda o `owner_phone` original num
arquivo temporário fora do repositório, sobrescreve com um número fixo de teste
(`5523099999999`) durante a run, e restaura o valor original na limpeza — mesmo se a
suíte travar no meio (o próximo `run` detecta o backup e restaura sozinho antes de
começar).

A suíte limpa o que criou: notificações e reservas de apoio do prefixo `5523000...`
são apagadas, e o `owner_phone` original é restaurado.

---

## Troubleshooting

| Sintoma | Causa provável | Como resolver |
|---|---|---|
| P1 falha (`owner_notifications` não aplicada / `owner_phone` ausente) | As migrations não rodaram, ou o banco é anterior à edição da migration 003 | Suba a app (`python app.py`) para aplicar 001–006; se `owner_phone` ainda faltar, recrie o banco do zero |
| `enqueue` diz "owner_phone está NULL" | Nenhum número foi configurado ainda | `UPDATE owners SET owner_phone = '...' WHERE tenant_id = 'default';` ou use o CLI `set-phone` |
| Segunda notificação de uma mesma reserva nunca aparece | Comportamento esperado — é o índice parcial de idempotência | Confira `SELECT COUNT(*) FROM owner_notifications WHERE booking_id = '...'`; deve ser 1 |
| Handoff não gera nova notificação depois do dono responder | O `owner_response` da anterior não foi de fato gravado | Confirme que a notificação anterior está `status='sent'` e `owner_response` preenchido antes de tentar de novo |
| `drain` não envia nada | Não há notificação `pending` (todas já `sent`/`failed`, ou `attempts` no teto) | `python tests/test_owner_notifications/test_owner_notifications.py list` para conferir o que está pendente |
| Dono some da lista mesmo após falha | Atingiu `MAX_ATTEMPTS` e virou `failed` | Esperado — não há retry automático além do teto; reenfileirar exigiria uma feature futura |
| Notificações de teste sobraram no banco | A suíte rodou com `--keep` ou travou | Rode a suíte de novo (a limpeza é idempotente) ou apague à mão os `lead_sender LIKE '5523000%'` |
| `owner_phone` do piloto ficou com o número de teste da suíte | A suíte travou antes de restaurar | Rode a suíte de novo (ela restaura sozinha a partir do backup) ou `UPDATE owners SET owner_phone = '<numero real>' WHERE tenant_id = 'default';` |
