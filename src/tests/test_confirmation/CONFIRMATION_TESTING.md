# Confirmação do Agendamento pelo Dono

Roteiro de testes do **Módulo 6** — a peça que fecha o ciclo do produto. Até o Módulo 5 a
resposta `1`/`2` do dono era apenas gravada em `owner_notifications.owner_response`: o
agendamento ficava preso em `pending_confirmation` para sempre, o lead nunca sabia se a aula
ia acontecer, e uma vaga cancelada nunca voltava. Agora a resposta do dono **baixa** o
`trial_bookings.status`, **avisa o lead** por WhatsApp e **libera a vaga**.

São dois canais para a mesma decisão — responder no WhatsApp e clicar no painel — e **uma
única** função que decide: `bot/confirmations.py::confirm_or_cancel_booking()`. Testar os dois
canais é testar que ambos chegam lá.

Tudo o que chega ao dono e ao lead é em português; o contrato e o código, em inglês.

Documentos irmãos: `../test_owner_notifications/OWNER_NOTIFICATIONS_TESTING.md` (de onde vem a
notificação) e `../test_inbox/INBOX_TESTING.md` (o outro jeito de um humano entrar na conversa).

---

## Pré-requisitos

### Migrations 001–007 aplicadas

Este módulo **não** tem migration nova — nenhuma coluna foi criada. Ele usa `trial_bookings`
(004), `owner_notifications` (006) e `messages` (007), que já existem:

```sql
SELECT version FROM schema_migrations ORDER BY version;  -- 001 a 007
```

Se faltar alguma, suba a app uma vez (`cd src && python app.py`) para o `init_db()` rodar.

### `owner_phone` configurado no piloto

O caminho do WhatsApp só funciona se o webhook reconhecer o número que responde como sendo o
do dono. Sem UI ainda:

```bash
python tests/test_confirmation/test_confirmation.py set-phone --phone 5521999999999
```

```sql
SELECT owner_phone FROM owners WHERE tenant_id = 'default';
```

O caminho do **painel** não precisa disso — o dono age logado, não pelo número.

---

## Como subir o ambiente (Arch Linux)

```bash
source venv/bin/activate
cd src
python app.py          # sobe o Flask e aplica as migrations
```

Para os testes de tela, abra `http://localhost:5000/dashboard/bookings` (faça login primeiro em
`/dashboard/login`; o link também está no menu, como **Agendamentos**).

---

## Como testar sem WhatsApp — o CLI manual

`test_confirmation.py` troca o `send_message` por um duplo que **imprime no terminal**. Nada sai
pelo Twilio em nenhum dos comandos.

```bash
# cria um lead com uma reserva pendente (aula de adulto)
python tests/test_confirmation/test_confirmation.py seed-booking --sender 5525000000001

# idem, aula infantil, já com a notificação ao dono enfileirada e marcada 'sent'
python tests/test_confirmation/test_confirmation.py seed-booking --sender 5525000000002 \
    --child-name "Miguel" --notify

# o que o dono vê na tela, na mesma ordem
python tests/test_confirmation/test_confirmation.py list

# decidir pelos dois canais
python tests/test_confirmation/test_confirmation.py confirm --booking-id <id>   # como o painel
python tests/test_confirmation/test_confirmation.py cancel  --booking-id <id>   # como o painel
python tests/test_confirmation/test_confirmation.py reply --phone 5521999999999 --body 1  # WhatsApp

# ler a conversa do lead (o aviso fica gravado como 'ai')
python tests/test_confirmation/test_confirmation.py conversation --sender 5525000000001

# limpar um lead (reservas, notificações, sessão; as mensagens vão em cascata)
python tests/test_confirmation/test_confirmation.py reset --sender 5525000000001
```

> **`reply` precisa do número que está em `owners`.** É por ele que o webhook decide se quem
> escreveu é o dono ou um lead. Se você passar outro número, o comando não acha notificação
> aberta e não faz nada — o que, aliás, é exatamente o caso de teste 1.

---

## Roteiro de testes

### 1. `register_owner_response` devolve a linha carimbada

**O que fazer:** com uma notificação de **booking** já `sent`, chame `register_owner_response`
(via `reply`) e depois chame de novo.

**O que esperar:** na primeira vez ela devolve um `dict` com `id`, `event_type` e `booking_id` —
não mais um `bool`. Na segunda, `None`: não há notificação em aberto. É esse `None` que torna
uma resposta duplicada do dono inofensiva.

**Como verificar:**
```sql
SELECT id, event_type, booking_id, owner_response FROM owner_notifications ORDER BY id DESC LIMIT 2;
```

### 2. Confirmar: status baixa e o lead é avisado

**O que fazer:** `confirm --booking-id <id>` sobre uma reserva em `pending_confirmation`.

**O que esperar:** o status vira `confirmed`; o terminal imprime o aviso que iria ao **lead**
(não ao dono); esse mesmo texto é gravado em `messages` com `author = 'ai'` e `is_read = TRUE`.
Aula infantil cita o nome da criança.

**Como verificar:**
```sql
SELECT status FROM trial_bookings WHERE id = '<id>';
SELECT author, is_read, content FROM messages WHERE sender = '<lead>' ORDER BY created_at, id;
```

### 3. Cancelar: a vaga volta

**O que fazer:** `cancel --booking-id <id>` sobre uma reserva pendente.

**O que esperar:** status `cancelled`, lead avisado, e **a vaga liberada**. A liberação é pura
contagem: `count_active_bookings()` conta `status != 'cancelled'`, e é ela que
`get_available_slots()` compara com a capacidade da turma. **Nenhuma chamada ao Google Calendar
acontece** (decisão 1A).

**Como verificar:**
```sql
-- deve cair para 0 (ou para o número de reservas ativas restantes naquele evento)
SELECT COUNT(*) FROM trial_bookings
WHERE calendar_event_id = '<event_id>' AND status != 'cancelled';
```

> **Débito conhecido e aceito:** a descrição do evento no Calendar continua listando o aluno
> cancelado sob `--- Reservas Corujai ---`. É **cosmético** — a vaga está de fato livre e será
> reoferecida. Está registrado em Known Issues no `CLAUDE.md`.

### 4. Guard: um agendamento já resolvido é ignorado

**O que fazer:** confirme uma reserva e, em seguida, tente cancelar **a mesma**.

**O que esperar:** a segunda operação devolve `skipped` com o status atual, **não** sobrescreve
a decisão e **não** manda um segundo aviso ao lead. Pelo painel, o mesmo: o segundo clique cai
no guard e a tela avisa "já estava confirmado".

O guard mora na coordenadora, não nas rotas — por isso vale para os dois canais de graça. Os
botões só aparecerem em `pending_confirmation` é conveniência de tela, não a regra.

### 5. Handoff nunca toca `trial_bookings`

**O que fazer:** com uma reserva pendente **e** uma notificação de **handoff** aberta para o
mesmo lead, responda `1`.

**O que esperar:** `owner_response` é gravado na notificação de handoff, `trial_bookings.status`
fica **intacto** e **nenhum** aviso vai ao lead. Handoff tem `booking_id` NULL: não existe aula
a decidir.

**Como verificar:**
```sql
SELECT event_type, booking_id, owner_response FROM owner_notifications ORDER BY id DESC LIMIT 1;
SELECT status FROM trial_bookings WHERE id = '<id>';  -- pending_confirmation
```

### 6. Falha no aviso ao lead não desfaz nem trava

**O que fazer:** só na suíte automatizada (o CLI não tem como derrubar o envio). O `SendCapture`
levanta exceção no lugar do envio.

**O que esperar:** o status **permanece** decidido, `lead_notified` volta `False`, nada é gravado
em `messages` (o lead não recebeu nada) e **nenhuma exceção escapa** para quem chamou. Isso
importa no webhook do dono: uma exceção ali faria o Twilio reenviar o `1` dele.

A ordem é o que garante isso: primeiro o fato autoritativo (`update_booking_status`), depois o
aviso, isolado num `try/except` que só loga.

### 7. As rotas do painel passam pela coordenadora

**O que fazer:** abra `/dashboard/bookings`, clique em **Confirmar** numa reserva pendente e
depois clique em **Cancelar** na mesma.

**O que esperar:** a lista é trocada no lugar (HTMX), o aviso verde confirma a ação, e o segundo
clique cai no guard com um aviso amarelo. Um `booking_id` inexistente na URL responde **200**
com aviso, nunca 500 — o HTMX não troca conteúdo em 4xx/5xx, e um erro nu deixaria o dono
olhando uma lista que não mudou sem saber por quê.

### 8. O painel carimba o `owner_response` da notificação

**O que fazer:** crie uma reserva **com** notificação (`seed-booking --notify`) e resolva pelo
**painel**, não pelo WhatsApp.

**O que esperar:** além do status, o `owner_response` daquela notificação também é gravado. Sem
isso a notificação ficaria "sem resposta" para sempre e as duas fontes contariam histórias
diferentes: `owner_response` responde "o que o dono decidiu", `status` responde "o que foi feito
da aula".

**Como verificar:**
```sql
SELECT b.id, b.status, n.owner_response
FROM trial_bookings b
LEFT JOIN owner_notifications n ON n.booking_id = b.id AND n.event_type = 'booking'
WHERE b.id = '<id>';
```

### 9. As rotas de agendamentos exigem autenticação

**O que fazer:** numa aba anônima, tente `GET /dashboard/bookings` e os dois `POST`.

**O que esperar:** **302** para `/dashboard/login` nas quatro. Nenhuma decisão pode ser tomada
sem sessão do painel.

---

## Como confirmar no banco

```sql
-- A tela do dono, em SQL: pendentes primeiro, depois por horário
SELECT id, lead_name, child_name, class_type, slot_start, status
FROM trial_bookings ORDER BY (status = 'pending_confirmation') DESC, slot_start;

-- As duas fontes lado a lado — não podem divergir
SELECT b.id, b.lead_name, b.status, n.owner_response, n.sent_at
FROM trial_bookings b
LEFT JOIN owner_notifications n ON n.booking_id = b.id AND n.event_type = 'booking'
ORDER BY b.slot_start DESC;

-- O aviso, do jeito que o lead recebeu (e o operador vê no inbox)
SELECT author, is_read, created_at, content
FROM messages WHERE sender = '<lead>' ORDER BY created_at, id;
```

---

## Como rodar a suíte automatizada

```bash
cd src
python tests/test_confirmation/test_confirmation_suite.py
python tests/test_confirmation/test_confirmation_suite.py --keep      # não limpa, para inspecionar
python tests/test_confirmation/test_confirmation_suite.py --json      # relatório em tests/outputs/
```

Os nove testes acima, mais o pré-requisito de schema e o preparo do `owner_phone`. Tudo
determinístico: o WhatsApp é stubbado e **a IA não entra nesse caminho em momento algum** — a
coordenadora nunca chama o modelo.

A suíte usa o prefixo `5525000...` (as outras usam `5521000`, `5522000`, `5523000` e `5524000`),
e o teardown apaga só o que a run criou: as notificações, as reservas e as sessões desse prefixo
— as mensagens vão junto por `ON DELETE CASCADE`. O `owner_phone` real do dono é salvo em
`/tmp` antes de ser trocado pelo de teste e restaurado no fim, inclusive se a run anterior
morreu no meio.

**Rode também a suíte do Módulo 4** (`../test_owner_notifications/test_owner_notifications_suite.py`):
ela foi adaptada neste módulo, e o teste 6 dela é justamente o que prova que a resposta do dono
passou a fechar a reserva.

---

## Troubleshooting

**`reply` diz que não achou notificação aberta.** Ou o `--phone` não é o que está em
`owners.owner_phone`, ou a notificação não está `sent` (o `register_owner_response` só enxerga
`status = 'sent'` com `owner_response` NULL). Use `seed-booking --notify`, que já marca como
enviada, ou rode o cron do Módulo 4.

**O aviso ao lead não aparece em `messages`.** `messages.sender` tem FK para `sessions`: se o
lead não tem sessão, a gravação falha, é engolida pelo `try/except` do aviso e vira log. O
`seed-booking` cria a sessão justamente por isso. Num fluxo real o lead sempre tem sessão — ele
conversou com a IA para chegar a agendar.

**Cliquei em Confirmar e a tela não mudou.** Veja se o htmx carregou (a página o busca do CDN
`unpkg.com`). Sem internet no navegador, os botões não fazem nada — a rota está correta, mas não
há quem a chame.

**A decisão não muda o status e a tela diz "já estava…".** É o guard 5A: aquela reserva não está
mais em `pending_confirmation`. Confira com `list`.
