# Módulo 3 — IA com JSON de Ação

Este roteiro testa a IA orientada a objetivo: a atendente que conduz o lead até o
agendamento de uma aula experimental e, a cada resposta, devolve um bloco
`<corujai_action>` que o Python lê para atualizar o estado da conversa (colunas em
`sessions`) e executar ações (`book` / `handoff`).

O motor de agendamento (Módulo 2) já foi testado em
[`../test_scheduling/SCHEDULING_ENGINE_TESTING.md`](../test_scheduling/SCHEDULING_ENGINE_TESTING.md).
Aqui o foco é a **camada de conversa**: parser defensivo, validação do `event_id`,
pausa de handoff, timeout de 1 hora e persistência do estado.

Tudo o que a IA envia ao lead é em português; o contrato e o código, em inglês.

---

## Pré-requisitos

### Google Calendar conectado (Módulo 1)
Necessário só para os testes de **agendamento real** (adultos, infantil,
`missing_child_name`). Conecte em `/integrations/google`. Sem isso, esses testes
são **pulados** (SKIP), e o restante roda normalmente.

### Migrations 005, 006 e 007 aplicadas
As migrations rodam sozinhas quando a app sobe (`init_db()` em `create_app()`).
Confira:

```sql
SELECT version FROM schema_migrations ORDER BY version;
-- deve incluir 005_add_child_name_to_trial_bookings,
-- 006_add_conversation_state_to_sessions e 007_create_ai_configs
```

### Vagas dos três tipos no calendário
Para o caminho feliz de agendamento, o calendário "Aulas Experimentais" precisa de
pelo menos um evento futuro `[ADULTOS]` e um `[BABY]` ou `[CRIANCAS]`. Use o roteiro
do Módulo 2 para criá-los, ou o comando de listagem:

```bash
python tests/test_scheduling/test_scheduling.py list
```

### `ai_configs` semeada
A migration 007 já insere a linha do tenant `default` com placeholders. Personalize
por SQL (não há tela):

```sql
UPDATE ai_configs
SET academy_name   = 'Academia Corujai',
    assistant_name = 'Bia',
    tone           = 'simpática, direta e acolhedora; trata o lead pelo nome',
    business_info  = 'Jiu-Jitsu, CrossFit e musculação. Rua X, 123. Seg-Sex 6h-22h. Primeira aula experimental gratuita.',
    flow_emphasis  = 'priorizar agendar a aula experimental o quanto antes'
WHERE tenant_id = 'default';
```

---

## Como subir o ambiente (Arch Linux)

```bash
# na raiz do repositório
source venv/bin/activate

# sobe a app uma vez para aplicar as migrations 005–007
cd src && python app.py        # Ctrl-C depois que logar "Migration 007 ... applied"
```

Todos os comandos abaixo rodam **de dentro de `src/`**.

---

## Como testar sem WhatsApp — o CLI manual

`test_ai_action.py` conversa com a IA real pelo terminal, sem Twilio. As respostas
do bot são impressas no console, e o estado da sessão aparece após cada turno.

```bash
python tests/test_ai_action/test_ai_action.py chat --sender 5522000000001
```

Comandos auxiliares:

```bash
python tests/test_ai_action/test_ai_action.py state   --sender 5522000000001   # vê o estado
python tests/test_ai_action/test_ai_action.py reset   --sender 5522000000001   # zera a sessão
python tests/test_ai_action/test_ai_action.py unpause --sender 5522000000001   # tira a pausa
python tests/test_ai_action/test_ai_action.py timeout --sender 5522000000001   # recua updated_at 2h
```

> Use um número com prefixo `5522000...` nos testes manuais: é o mesmo prefixo que a
> suíte automatizada limpa, então nada de real fica sujo.

---

## Roteiro de testes

Cada passo traz **O que fazer / O que esperar / Como verificar**. Os textos entre
aspas são exemplos — a IA responde com as próprias palavras, no tom de `ai_configs`.

### 1. Conversa completa até agendar aula de **adultos**

**O que fazer:** no `chat`, conduza uma conversa até escolher um horário `[ADULTOS]`
listado (a IA oferece os horários injetados). Diga seu nome quando perguntado e
aceite um horário.

**O que esperar:** a IA confirma o agendamento em português. O estado vai para
`stage=booked`.

**Como verificar:**
```sql
SELECT stage, lead_name, qualification FROM sessions WHERE sender = '5522000000001';
SELECT lead_name, child_name, class_type, status FROM trial_bookings
WHERE sender = '5522000000001';   -- child_name deve ser NULL para adultos
```
E confira a descrição do evento no Google Calendar: ganhou a linha
`- <nome> (<telefone>) — confirmado em ...`.

### 2. Conversa completa até agendar aula **infantil** (dois nomes)

**O que fazer:** comece dizendo que a aula é para uma criança. A IA deve perguntar o
nome da criança antes de agendar. Escolha um horário `[BABY]`/`[CRIANCAS]`.

**O que esperar:** só agenda depois de ter **os dois nomes** (responsável + criança).

**Como verificar:**
```sql
SELECT lead_name, child_name, class_type FROM trial_bookings WHERE sender = '<seu numero>';
-- lead_name = responsável, child_name = criança
```
Na descrição do evento a linha vira
`- <criança> (resp.: <responsável> — <telefone>) — confirmado em ...`.

### 3. Tentar agendar aula infantil **sem** o nome da criança → `missing_child_name`

**O que fazer:** force a IA a tentar agendar uma aula infantil antes de informar o
nome da criança (no CLI, é mais fácil observar isso pela suíte; ver passo 3 da suíte).

**O que esperar:** a reserva é **recusada** (`book_slot` devolve `missing_child_name`),
a IA pede o nome da criança e **a conversa continua** — nada é gravado em
`trial_bookings`.

**Como verificar:** `SELECT COUNT(*) FROM trial_bookings WHERE sender = '<numero>';`
deve ser 0 até o nome ser fornecido.

### 4. Lead que já chega dizendo o horário que quer (a IA pula etapas)

**O que fazer:** primeira mensagem já com "quero a aula de adultos de terça 19h, sou o João".

**O que esperar:** a IA pode ir direto ao agendamento (não precisa passar por todas as
etapas), desde que o horário exista na lista injetada.

**Como verificar:** `stage=booked` após um único turno, `lead_name='João'`.

### 5. Lead levanta objeção fora do roteiro (responde e volta ao fluxo)

**O que fazer:** diga "tá muito caro". Depois volte com interesse.

**O que esperar:** a IA contorna a objeção (`stage=objection`) e retoma o fluxo
(`stage=availability`/`proposal`) quando o lead volta a se interessar.

### 6. Lead desqualificado → `qualification` gravada corretamente

**O que fazer:** mande algo claramente fora do público (ex.: "só quero vender um plano
de internet pra vocês").

**O que esperar:** `qualification=unqualified` na sessão.

**Como verificar:** `SELECT qualification FROM sessions WHERE sender = '<numero>';`

### 7. Handoff → sessão pausada → bot silencia aquele lead

**O que fazer:** peça "quero falar com um humano".

**O que esperar:** a IA manda uma mensagem de transição, grava
`stage=handoff_requested` e `is_paused=TRUE`. **As mensagens seguintes daquele lead
não recebem resposta.** Outros leads seguem atendidos normalmente.

**Como verificar:**
```sql
SELECT is_paused, stage FROM sessions WHERE sender = '<numero>';   -- t | handoff_requested
```
Mande outra mensagem pelo `chat`: nenhuma resposta é impressa (o LLM nem é chamado).

### 8. Como **despausar** nos testes

Depois de um handoff, para retomar:

```sql
-- opção A: só remover a pausa
UPDATE sessions SET is_paused = FALSE WHERE sender = '<numero>';
```
```bash
# opção B: zerar a sessão inteira
python tests/test_ai_action/test_ai_action.py reset --sender <numero>
# (equivale a session.clear_session(sender))
```

### 9. Timeout de 1h → a conversa reinicia

**O que fazer:** tenha uma conversa em andamento (ex.: `stage=interest`), depois
simule inatividade:

```sql
UPDATE sessions SET updated_at = NOW() - INTERVAL '2 hours' WHERE sender = '<numero>';
```
Mande uma nova mensagem.

**O que esperar:** como o `stage` anterior não era `booked`, a conversa é registrada
como `closed_no_booking` (via log) e **reinicia do zero** — histórico limpo, estado
de volta ao início, e a saudação aparece de novo.

**Como verificar:** o log mostra `... -> closed_no_booking; restarting.`; o `history`
da sessão volta a ter poucos itens (só o novo turno).

### 10. Timeout **NÃO** desfaz a pausa

**O que fazer:** pause a sessão (handoff), depois envelheça o `updated_at`:

```sql
UPDATE sessions SET is_paused = TRUE, updated_at = NOW() - INTERVAL '2 hours'
WHERE sender = '<numero>';
```
Mande uma nova mensagem.

**O que esperar:** a sessão **continua pausada** — a verificação de pausa vem antes do
timeout, então o handoff sobrevive. Nenhuma resposta é enviada; `is_paused` segue `TRUE`.

### 11. Lead com agendamento ativo que volta após o timeout

**O que fazer:** com um lead que já tem uma reserva ativa em `trial_bookings`, envelheça
a sessão e mande "posso remarcar?".

**O que esperar:** mesmo com a conversa reiniciada, a IA **sabe do agendamento** — os
agendamentos ativos do lead são injetados no contexto **sempre**, não só após o timeout.

**Como verificar:** a resposta da IA reconhece a reserva existente (na suíte, o teste
confere que a seção `ACTIVE BOOKINGS` do prompt traz o agendamento).

### 12. Aviso do timeout aparece na saudação

**O que fazer:** comece uma conversa nova.

**O que esperar:** na **primeira** mensagem, a IA avisa (no tom da academia) que o
atendimento se encerra após 1 hora sem resposta e que o lead pode chamar de novo quando
quiser. Não repete isso nas mensagens seguintes.

### 13. JSON malformado → mensagem chega, nenhuma ação, warning

**O que esperar:** se o modelo emitir um bloco com JSON quebrado, a **mensagem em
português ainda chega ao lead**, nenhuma ação é executada, e um warning é logado. O
estado não muda.

### 14. Bloco ausente → conversa segue normal

**O que esperar:** uma resposta sem bloco de ação é tratada como `action: none`
implícito, **sem** warning. A mensagem passa intacta.

### 15. `event_id` fora da lista injetada → ação recusada, warning

**O que esperar:** se o modelo inventar um `event_id` que não está entre os injetados,
o Python **recusa** a reserva antes de chamar `book_slot`, loga um warning e reoferece
os horários reais. *A IA nunca inventa horário, e o código nunca confia que ela não inventou.*

### 16. Dois blocos na resposta → o último vence

**O que esperar:** se o modelo emitir dois blocos (se corrigindo), o parser usa o
**último** e loga um warning.

### 17. Slot lota entre a oferta e a confirmação → `full` → conversa se recupera

**O que esperar:** `book_slot` pode devolver `full` mesmo depois de a IA ter oferecido o
horário (janela do cache de ~60s). A conversa **avisa o lead** que lotou e oferece os
horários restantes. `stage` não fica `booked`.

### 18. Mesmo lead reserva o mesmo slot de novo → `duplicate`

**O que esperar:** `book_slot` devolve `duplicate`; a IA informa que o lead já tem
aquele horário reservado.

### 19. Google Calendar desconectado → a IA continua conversando, sem oferecer horários

**O que esperar:** se a integração cai, `get_available_slots()` levanta exceção,
capturada no cache, que devolve lista vazia. A IA **segue atendendo**, apenas sem
oferecer horários; o prompt indica `AVAILABLE SLOTS: (none available...)`. Registrado no log.

---

## Como confirmar no banco

```sql
-- estado da conversa
SELECT sender, stage, qualification, lead_name, child_name, is_paused, updated_at
FROM sessions WHERE sender LIKE '5522000%';

-- reservas criadas
SELECT sender, lead_name, child_name, class_type, status, slot_start
FROM trial_bookings WHERE sender LIKE '5522000%' ORDER BY slot_start;

-- camada personalizável
SELECT * FROM ai_configs WHERE tenant_id = 'default';
```

---

## Como rodar a suíte automatizada

```bash
python tests/test_ai_action/test_ai_action_suite.py            # tudo
python tests/test_ai_action/test_ai_action_suite.py --skip-live  # sem escrever no Calendar
python tests/test_ai_action/test_ai_action_suite.py --keep       # não limpa (para depurar)
python tests/test_ai_action/test_ai_action_suite.py --no-color   # sem cores ANSI
python tests/test_ai_action/test_ai_action_suite.py --json       # grava relatório em tests/outputs/
```

**Como ler o relatório:** cada passo aparece como `✔` (PASS), `✖` (FAIL/ERROR) ou `○`
(SKIP). A linha final resume `N testes · X passaram · Y falharam · Z pulados`. O
**exit code é 0** somente quando nada falhou (SKIPs não reprovam a run).

Os testes de agendamento real (passos 1–3) aparecem como **SKIP** quando o Google
Calendar não está conectado ou não há vagas dos tipos necessários — isso é esperado
fora de um ambiente com calendário. Os demais rodam de forma determinística (o LLM é
substituído por respostas controladas), então independem do humor do Haiku.

A suíte limpa o que criou: sessões e reservas do prefixo `5522000...` são apagadas, e a
descrição de qualquer evento real que uma reserva tenha alterado é restaurada.

---

## Troubleshooting

| Sintoma | Causa provável | Como resolver |
|---|---|---|
| P1 falha ("migrations não aplicadas") | 006/007 não rodaram | Suba a app uma vez (`python app.py`) para o `init_db()` aplicar as migrations |
| Passos 1–3 sempre SKIP | Calendar desconectado ou sem vagas dos tipos | Conecte em `/integrations/google` e crie eventos `[ADULTOS]` e `[BABY]`/`[CRIANCAS]` |
| A IA oferece um horário que não existe | O prompt sozinho não garante — mas o Python recusa o `book` | Confirme que o `event_id` recusado não estava na lista injetada (passo 15) |
| Lead não recebe mais respostas | A sessão está pausada (handoff) | Despause (passo 8) — é o comportamento esperado após um handoff |
| Conversa reinicia "sozinha" | `updated_at` tem mais de 1h | É o timeout de inatividade (passo 9); use `reset` ou converse de novo |
| `book_slot` devolve `integration_not_connected` | Token do Google ausente/expirado | Reconecte em `/integrations/google`; a conversa não quebra, só não oferece horários |
| Reservas de teste sobraram no banco | A suíte rodou com `--keep` ou travou | Rode a suíte de novo (a limpeza é idempotente) ou apague à mão os `sender LIKE '5522000%'` |
