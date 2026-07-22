# Módulo 2 — Motor de Agendamento

Este documento descreve como testar, de ponta a ponta, o motor de agendamento
implementado no Módulo 2: as funções que leem vagas livres no Google Calendar do dono
e efetivam o agendamento de uma aula experimental no Postgres. O motor ainda não é
chamado pela IA — quem o exercita aqui é o script manual `tests/test_scheduling/test_scheduling.py`.
Este documento é auto-contido: quem for executar os testes não precisa ter
acompanhado a conversa de desenvolvimento.

## Pré-requisitos

### Integração Google Calendar conectada (Módulo 1)

O motor não conecta nada sozinho — ele lê credenciais já salvas pelo Módulo 1.
Confirme antes de tudo:

```sql
SELECT tenant_id, integration_status, calendar_id FROM owners WHERE tenant_id = 'default';
```

Esperado: `integration_status = 'connected'` e `calendar_id` preenchido. Se não estiver,
siga o roteiro do `GOOGLE_CALENDAR_OAUTH_TESTING.md` primeiro (passo 3 — "Caminho feliz —
conectar").

### Migration 004 aplicada

```sql
SELECT version, applied_at FROM schema_migrations WHERE version = '004_create_trial_bookings';
```

Esperado: uma linha. Se não existir, suba o app uma vez (`init_db()` roda todas as
migrations pendentes automaticamente) ou rode `python -c "from database.db import init_db; init_db()"`
dentro de `src/`.

### Preparar o calendário "Aulas Experimentais"

Acesse calendar.google.com na conta conectada e crie, à mão, os seguintes eventos no
calendário **"Aulas Experimentais"** (todos nos próximos 14 dias, para caírem dentro da
janela padrão de `get_available_slots()`):

| Título do evento | Horário sugerido | Propósito no roteiro |
|---|---|---|
| `[BABY] Aula Experimental` | Daqui a 2 dias, 09:00–09:30 | Capacidade 2 — testes de lotação e corrida |
| `[CRIANCAS] Aula Experimental` | Daqui a 3 dias, 16:00–16:45 | Capacidade 4 |
| `[ADULTOS] Aula Experimental` | Daqui a 4 dias, 19:00–20:00 | Capacidade ilimitada |
| `Horário sem marcador` | Daqui a 5 dias, 18:00–18:30 | Título fora do padrão → fallback |
| (dia inteiro, sem horário) | Daqui a 6 dias, evento de dia inteiro | Deve ser ignorado |
| `[ADULTOS] Aula Recorrente` | Recorrência semanal, começando amanhã, 07:00–08:00 | Expansão de recorrência |
| `[BABY] Aula Passada` | 2 dias atrás, 09:00–09:30 | Não deve aparecer (passado) |

Para o evento recorrente, use a opção "Não se repete" → "Personalizar..." → repetir
semanalmente no Google Calendar ao criar o evento.

### Como subir o ambiente (Arch Linux)

```bash
source venv/bin/activate
cd src
python app.py
```

Confira no log de startup que a migration `004_create_trial_bookings` aparece como
`applied successfully` (na primeira subida após esta mudança) ou `already applied,
skipping` (nas subidas seguintes).

## Roteiro de testes

Todos os comandos abaixo rodam a partir de `src/`:
```bash
python tests/test_scheduling/test_scheduling.py list
python tests/test_scheduling/test_scheduling.py book <event_id> --sender <telefone> --name <nome>
```

### 1. Listar slots com o calendário vazio

**O que fazer:** antes de criar qualquer evento (ou temporariamente esvaziando o
calendário "Aulas Experimentais"), rode `python tests/test_scheduling/test_scheduling.py list`.

**O que esperar:** saída `Nenhuma vaga disponível.`, sem erro.

**Como verificar:** é só a saída do comando — não há nada para checar no banco ainda.

### 2. Listar os três tipos e conferir vagas restantes

**O que fazer:** com os eventos da tabela de preparo criados, rode `list` novamente.

**O que esperar:** quatro linhas (baby, crianças, adultos, sem-marcador — o "dia
inteiro" e o "passado" não aparecem; a recorrência aparece à parte, ver item 11).
`[BABY]` mostra `vagas restantes: 2`, `[CRIANCAS]` mostra `4`, `[ADULTOS]` mostra
`ilimitado`, e o evento sem marcador aparece rotulado como "Adultos" com
`ilimitado`.

**Como verificar:** compare a saída do comando com a tabela de preparo. Anote os
`event_id` impressos — são a primeira coluna de cada linha, e são usados nos passos
seguintes.

### 3. Agendar 1 aluno na baby class — slot continua aparecendo

**O que fazer:**
```bash
python tests/test_scheduling/test_scheduling.py book <event_id_baby> --sender 5521900000001 --name "Ana"
```

**O que esperar:** saída com `'status': 'created'`, `'calendar_synced': True`. Rode
`list` de novo — o slot `[BABY]` **continua** na lista, agora com `vagas restantes: 1`.

**Como verificar (banco):**
```sql
SELECT sender, lead_name, class_type, status FROM trial_bookings
WHERE calendar_event_id = '<event_id_baby>';
```
Esperado: uma linha, `status = 'pending_confirmation'`.

### 4. Agendar o 2º aluno na baby class — slot some da lista

**O que fazer:**
```bash
python tests/test_scheduling/test_scheduling.py book <event_id_baby> --sender 5521900000002 --name "Beto"
```

**O que esperar:** `'status': 'created'`. Rode `list` — o slot `[BABY]` **não aparece
mais** (2/2 ocupado).

**Como verificar:** `SELECT COUNT(*) FROM trial_bookings WHERE calendar_event_id = '<event_id_baby>' AND status != 'cancelled';` → `2`.

### 5. Tentar um 3º aluno na baby class — recusado por lotação

**O que fazer:**
```bash
python tests/test_scheduling/test_scheduling.py book <event_id_baby> --sender 5521900000003 --name "Carla"
```

**O que esperar:** saída `{'status': 'full', 'active_count': 2}`. Nenhuma chamada ao
Calendar é feita para este caso (a rejeição acontece só no Postgres).

**Como verificar:** repita a query do passo 4 — a contagem continua `2`, não `3`.

### 6. A corrida na última vaga

Este é o teste mais importante do módulo: prova que dois leads não conseguem ocupar a
mesma última vaga ao mesmo tempo.

**Preparo:** cancele uma das duas reservas da baby class para reabrir 1 vaga:
```sql
UPDATE trial_bookings SET status = 'cancelled'
WHERE calendar_event_id = '<event_id_baby>' AND sender = '5521900000002';
```
Confirme com `list` que o slot volta a aparecer com `vagas restantes: 1`.

**Como simular a corrida (duas opções):**
- **Dois terminais:** abra dois terminais lado a lado, cada um com o venv ativado e
  `cd src`. Digite os dois comandos abaixo em cada terminal **sem apertar Enter ainda**,
  e aperte Enter nos dois o mais próximo possível um do outro:
  ```bash
  python tests/test_scheduling/test_scheduling.py book <event_id_baby> --sender 5521900000010 --name "Racer1"
  python tests/test_scheduling/test_scheduling.py book <event_id_baby> --sender 5521900000011 --name "Racer2"
  ```
- **Duas threads:** alternativa mais confiável (elimina a variável humana), rode um
  script Python ad-hoc que chama `bot.scheduling.book_slot` de duas threads
  simultaneamente para o mesmo `event_id`, como feito durante o desenvolvimento deste
  módulo contra `bot.bookings.create_booking_with_lock` diretamente.

**O que esperar:** exatamente uma das duas chamadas retorna `'status': 'created'`; a
outra retorna `'status': 'full'`.

**Como verificar:** `SELECT COUNT(*) FROM trial_bookings WHERE calendar_event_id = '<event_id_baby>' AND status != 'cancelled';` → deve ser `2`, nunca `3`.

### 7. Mesmo sender tentando reservar o mesmo slot duas vezes

**O que fazer:** escolha um sender que já tem reserva ativa em um slot com vaga (ex.: a
aula de crianças) e tente reservar de novo:
```bash
python tests/test_scheduling/test_scheduling.py book <event_id_criancas> --sender 5521900000001 --name "Ana"
python tests/test_scheduling/test_scheduling.py book <event_id_criancas> --sender 5521900000001 --name "Ana"
```

**O que esperar:** a primeira chamada retorna `'status': 'created'`; a segunda retorna
`'status': 'duplicate'`, sem incrementar a contagem.

**Como verificar:** `SELECT COUNT(*) FROM trial_bookings WHERE calendar_event_id = '<event_id_criancas>' AND sender = '5521900000001';` → `1`.

### 8. Aula de adultos nunca lota

**O que fazer:** agende vários leads diferentes na mesma aula `[ADULTOS]` (5 ou mais).

**O que esperar:** todas as chamadas retornam `'status': 'created'`; `list` sempre
mostra `vagas restantes: ilimitado` para esse evento, não importa quantos já reservaram.

**Como verificar:** a contagem em `trial_bookings` cresce normalmente, mas nunca é
comparada a um limite (capacidade é `None` para `ADULTOS`).

### 9. Título fora do padrão → tratado como adultos + warning

**O que fazer:** rode `list` e observe o slot `Horário sem marcador` — depois confira o
log do terminal onde o app (ou o próprio script) está rodando.

**O que esperar:** o slot aparece rotulado como "Adultos", `vagas restantes: ilimitado`.
No log, uma linha `WARNING - Unrecognized class marker in event title 'Horário sem
marcador'; defaulting to ADULTOS.`

**Como verificar:** é uma verificação de log + saída do comando, não de banco.

### 10. Evento de dia inteiro (ignorado) e evento no passado (não aparece)

**O que fazer:** rode `list` com os eventos "dia inteiro" e "Aula Passada" (2 dias
atrás) já criados no calendário.

**O que esperar:** nenhum dos dois aparece na lista.

**Como verificar:** contagem manual — a saída de `list` deve ter exatamente as linhas
esperadas pela tabela de preparo, nem mais nem menos.

### 11. Evento recorrente → expande em instâncias

**O que fazer:** rode `list` com o evento `[ADULTOS] Aula Recorrente` (semanal)
configurado.

**O que esperar:** mais de uma linha para esse título, uma por instância dentro da
janela de `days_ahead` (padrão 14 dias) — cada uma com seu próprio `event_id`.

**Como verificar:** agende um lead em uma instância específica
(`book <event_id_da_instancia_1> ...`) e confirme com `list` que **só aquela
instância** desaparece/muda de contagem — as outras semanas continuam livres. Isso
prova que cada instância tem capacidade própria, não compartilhada.

### 12. Token revogado no Google → marca `needs_reconnect`, não estoura

**O que fazer:** com a integração conectada, revogue o acesso manualmente em
`https://myaccount.google.com/permissions` (mesmo procedimento do passo 7 do
`GOOGLE_CALENDAR_OAUTH_TESTING.md`). Em seguida rode:
```bash
python tests/test_scheduling/test_scheduling.py list
```

**O que esperar:** a mensagem limpa `O Google recusou o token salvo; reconecte em
/integrations/google.`, **sem traceback**.

**Como verificar:**
```sql
SELECT integration_status FROM owners WHERE tenant_id = 'default';
```
Esperado: `needs_reconnect`. Reconecte pela tela `/integrations/google` antes de
continuar os próximos testes.

### 13. `integration_status = 'disconnected'` → falha limpa

**O que fazer:**
```sql
UPDATE owners SET integration_status = 'disconnected' WHERE tenant_id = 'default';
```
Rode `python tests/test_scheduling/test_scheduling.py list`.

**O que esperar:** mensagem limpa `Integração com o Google Calendar não está conectada.
Acesse /integrations/google para conectar.`, sem traceback e **sem nenhuma chamada à
API do Google** (a checagem acontece só lendo a tabela `owners`).

**Como verificar:** restaure `integration_status = 'connected'` depois do teste para
não atrapalhar os passos seguintes.

## Como confirmar no banco

```sql
-- Estado de todas as reservas de um evento
SELECT sender, lead_name, class_type, status, created_at
FROM trial_bookings
WHERE calendar_event_id = '<event_id>'
ORDER BY created_at;

-- Contagem de reservas ativas por evento (a mesma lógica usada pelo motor)
SELECT calendar_event_id, COUNT(*) AS active_bookings
FROM trial_bookings
WHERE status != 'cancelled'
GROUP BY calendar_event_id;

-- Confirmar que a migration 004 foi aplicada
SELECT version, applied_at FROM schema_migrations WHERE version = '004_create_trial_bookings';
```

## Como confirmar no Google Calendar

Abra o evento agendado em calendar.google.com e confira a **descrição** — deve conter
uma seção `--- Reservas Corujai ---` com uma linha por lead confirmado (nome, telefone,
data/hora do agendamento). Editar o resto da descrição à mão (o dono pode fazer isso
livremente) não afeta essa seção nem quebra o parser, já que o motor só faz *append*
sob o marcador.

`extendedProperties.private.corujai_booked_count` **não aparece na interface do
Google Calendar** — para inspecionar, use a API diretamente:
```bash
curl -s -H "Authorization: Bearer $ACCESS_TOKEN" \
  "https://www.googleapis.com/calendar/v3/calendars/<calendar_id>/events/<event_id>"
```
Procure por `"extendedProperties": {"private": {"corujai_booked_count": "..."}}` no
JSON retornado. Alternativamente, um shell Python dentro de `src/`:
```python
import integrations.store as store
import integrations.google_calendar as google_calendar

owner = store.get_owner_credentials()
service = google_calendar.get_calendar_service(owner["refresh_token"])
event = service.events().get(calendarId=owner["calendar_id"], eventId="<event_id>").execute()
print(event.get("extendedProperties"))
```

## Troubleshooting

| Sintoma | Causa provável | Como resolver |
|---|---|---|
| `invalid_grant` ao rodar `list` ou `book` | Refresh token revogado (pelo dono ou pelo Google) | Comportamento esperado — deve virar `IntegrationNeedsReconnectError` tratada (ver passo 12). Se em vez disso aparecer um traceback cru, é bug: a captura de `NeedsReconnectError` em `_get_service_or_raise()` não funcionou |
| `404` ao chamar `events.list`/`events.get` | `calendar_id` salvo em `owners` não existe mais (dono apagou o calendário "Aulas Experimentais" manualmente) | Recriar o calendário e reconectar a integração (o Módulo 1 recria via `_find_or_create_calendar` só no fluxo de conexão) |
| `insufficient scope` / `403` | Token antigo, gerado antes do escopo `https://www.googleapis.com/auth/calendar` estar correto | Desconectar e reconectar em `/integrations/google` para gerar um token novo com o escopo certo |
| `TypeError: can't compare offset-naive and offset-aware datetimes` | Algum datetime entrou no motor sem passar por `_parse_rfc3339` (ex.: um `datetime.now()` sem timezone comparado direto com o resultado do Calendar) | Toda comparação de horário deve usar `datetime.now(TIMEZONE)` ou o retorno de `_parse_rfc3339`, nunca `datetime.now()` puro |
| Uma das duas chamadas concorrentes do passo 6 demora vários segundos em vez de falhar rápido | Comportamento esperado, não erro: `pg_advisory_xact_lock` bloqueia a segunda chamada até a primeira commitar (ou dar rollback) — a espera é o mecanismo funcionando, não um deadlock |
| A "corrida" nunca falha, as duas sempre criam | O lock não está sendo adquirido — confira se `capacity` chegou como `None` em `create_booking_with_lock` para esse evento (ex.: título mal interpretado como `ADULTOS`) — capacidade ilimitada pula o lock de propósito |
| Reserva criada no Postgres mas a descrição do evento não mudou | `calendar_synced` veio `False` no retorno de `book_slot` — o patch falhou depois do commit (ver log com `Booking ... was committed to Postgres but the Calendar patch failed`). A reserva continua válida; não é preciso desfazer nada, só investigar o erro logado (rede, permissão, 404) |
