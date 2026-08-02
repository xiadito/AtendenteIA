# Inbox do Operador com Takeover

Este roteiro testa a camada que permite a uma **pessoa** assumir o atendimento de um lead:
a tabela `messages` como fonte única da conversa, a gravação da mensagem do lead enquanto
a conversa está pausada, a resposta enviada pelo painel, e a devolução da conversa à IA.

Até o Módulo 4 o handoff era um beco sem saída: a IA setava `sessions.is_paused = TRUE` e
**nada** limpava essa flag. O lead ficava mudo para sempre. O inbox é o que despausa.

A IA com bloco de ação (que produz o `handoff` consumido aqui) já foi testada em
[`../test_ai_action/AI_ACTION_TESTING.md`](../test_ai_action/AI_ACTION_TESTING.md), e a
fila de notificação ao dono em
[`../test_owner_notifications/OWNER_NOTIFICATIONS_TESTING.md`](../test_owner_notifications/OWNER_NOTIFICATIONS_TESTING.md).
Aqui o foco é o **registro da conversa e o takeover**.

Tudo o que chega ao lead e ao operador é em português; o contrato e o código, em inglês.

---

## Pré-requisitos

### Banco recriado do zero, com as migrations 001–007

**Este módulo editou a migration 001 no lugar** (removeu `history`, acrescentou
`needs_resume_note` e `conversation_started_at`). O runner de migrations só confere se o
nome do arquivo está em `schema_migrations` — **nunca** se o conteúdo mudou. Um banco que
já aplicou a 001 antiga fica com a `sessions` antiga para sempre.

Ou seja: **não há como migrar por cima. O banco precisa ser derrubado e recriado.**

```bash
psql -U postgres -c "DROP DATABASE IF EXISTS corujai;"
psql -U postgres -c "CREATE DATABASE corujai;"
psql -U postgres -c "ALTER DATABASE corujai OWNER TO corujai_app;"

cd src && python app.py     # aplica 001–007 e sai
```

Confira as três coisas separadamente:

```sql
SELECT version FROM schema_migrations ORDER BY version;
-- deve ir de 001_create_sessions até 007_create_messages (sete linhas)

SELECT column_name FROM information_schema.columns
WHERE table_name = 'sessions' AND column_name IN ('history', 'needs_resume_note', 'conversation_started_at');
-- precisa retornar needs_resume_note e conversation_started_at, e NÃO retornar history

SELECT to_regclass('public.messages');
-- precisa retornar 'messages'; se vier NULL, a 007 não rodou
```

> Depois deste módulo o banco passa a existir de verdade e **a janela de editar migration
> fecha**: volta a regra normal de nunca editar uma migration já aplicada.

### Nada mais é necessário

Diferente do roteiro de notificações, aqui **não** é preciso configurar `owner_phone`, nem
conectar o Google Calendar: a suíte não fecha agendamento e não notifica o dono. O envio
por WhatsApp é dublado, então **nenhuma mensagem real sai** durante os testes.

---

## Como rodar

Sempre a partir de `src/`:

```bash
# Suíte automática — PASS/FAIL e exit code
python tests/test_inbox/test_inbox_suite.py

# Preserva o que a run criou, para inspecionar no DBeaver
python tests/test_inbox/test_inbox_suite.py --keep

# Grava também um relatório JSON
python tests/test_inbox/test_inbox_suite.py --json
```

A suíte usa o prefixo `5524000...` para os leads de teste (o Módulo 2 usa `5521000`, o
Módulo 3 `5522000`, o Módulo 4 `5523000`), e o teardown apaga **apenas** esses. As
mensagens somem junto com a sessão, porque `messages.sender` tem `ON DELETE CASCADE`.

---

## A CLI manual

A suíte afirma; a CLI **mostra**. Cada comando faz uma operação por vez:

```bash
python tests/test_inbox/test_inbox.py list
python tests/test_inbox/test_inbox.py show    --sender 5524000000001
python tests/test_inbox/test_inbox.py unread  --sender 5524000000001
python tests/test_inbox/test_inbox.py send    --sender 5524000000001 --author lead --text "quero uma aula"
python tests/test_inbox/test_inbox.py reply   --sender 5524000000001 --text "Oi! Aqui é o Isac"
python tests/test_inbox/test_inbox.py pause   --sender 5524000000001
python tests/test_inbox/test_inbox.py resume  --sender 5524000000001
python tests/test_inbox/test_inbox.py prompt  --sender 5524000000001
```

`reply` percorre a rota real do operador com o envio dublado — **nada sai pelo Twilio**.
Use `--live` para enviar de verdade, e só quando quiser testar contra um número seu.

`prompt` imprime se a nota de retomada entraria no próximo prompt. É a forma mais rápida
de ver o 4B funcionando: rode `resume`, depois `prompt` (a nota aparece), mande um turno
pela CLI do Módulo 3, e rode `prompt` de novo (a nota sumiu).

---

## Roteiro de testes

### 1. `add_message` grava os três autores; autor inválido levanta erro

`lead`, `ai` e `operator` gravam. Qualquer outro valor levanta `ValueError` **em Python** —
não há `CHECK` no banco, mesmo padrão de `session.valid_stages`. O motivo de levantar em
vez de degradar: uma mensagem gravada sob um autor inválido ficaria invisível tanto para o
payload da IA quanto para a contagem de não lidas.

### 2. Pausa: a mensagem do lead é gravada e a IA **não** é chamada

O ponto central do módulo. Com `is_paused = TRUE`, `handle_text_message()` grava a
mensagem e retorna — sem custo de token e sem resposta ao lead.

Esquecer essa gravação é a falha mais fácil de cometer e a mais difícil de notar: tudo
parece funcionar (o bot fica quieto, como deve), mas o operador fica **cego** para o que o
lead disse durante o takeover.

Confira no banco:

```sql
SELECT author, is_read, created_at FROM messages
WHERE sender = '5524000000002' ORDER BY created_at, id;
-- a mensagem do lead está lá, com is_read = false, e não há linha 'ai' depois dela
```

### 3. Payload: últimos N em ordem cronológica, com autor→role correto

`get_recent_messages` busca em `created_at DESC` (para o `LIMIT` pegar as **mais recentes**)
e **reverte** antes de devolver. Buscar em ordem crescente com `LIMIT` entregaria ao modelo
o começo da conversa e jogaria fora tudo que acabou de acontecer.

Mapeamento: `lead` → `user`; `ai` **e** `operator` → `assistant`. O operador vira
"assistant" de propósito — para o lead, IA e humano são o mesmo atendente.

O payload também **descarta mensagens `assistant` no início da janela**. A janela tem
tamanho fixo, então ela pode abrir no meio de uma sequência de respostas do operador — e o
endpoint compatível da Anthropic recusa um payload que começa em `assistant`.

### 4. Não lidas: contagem correta e `mark_conversation_read` zera as do lead

`is_read` só tem significado para `author = 'lead'`: ele responde "o **operador** já viu
isso?". Mensagens da IA e do operador nascem lidas — mensagem que sai não é coisa para o
operador se atualizar.

```sql
SELECT sender, COUNT(*) FROM messages
WHERE author = 'lead' AND is_read = FALSE GROUP BY sender;
```

### 5. Resposta do operador: **envia → grava**; falha de envio **não** grava

A ordem é o inverso da rota da IA (que grava e depois envia), e isso é deliberado —
guarde a pergunta, ela volta no desafio do fim.

Com o envio dublado para levantar exceção, a rota precisa:
- **não** gravar nada em `messages`;
- devolver **200** com um aviso em português dentro do HTML (o HTMX não troca conteúdo em
  4xx/5xx — um 500 nu deixaria o operador olhando uma tela muda);
- **não** limpar o campo de texto, que é justamente o que ele vai reenviar.

### 6. Despausar (4B): `is_paused` cai, `stage` reseta, e a nota entra **uma vez**

`POST /dashboard/inbox/<sender>/resume` é a única coisa no código que limpa `is_paused`.

O `stage` volta para `interest` — um valor que **já existe** em `session.valid_stages` e
que a camada protegida do prompt descreve. Inventar um estágio novo exigiria mexer em
**dois** lugares em sincronia (o `set` em `session.py` e a lista de milestones no
`PROTECTED_LAYER`); mexer só no `set` faria a IA receber um estágio que nunca viu descrito.

A nota de retomada precisa aparecer no **próximo** prompt e sumir no seguinte:

```sql
SELECT sender, is_paused, stage, needs_resume_note FROM sessions
WHERE sender = '5524000000006';
-- depois do resume:      is_paused=false, stage=interest, needs_resume_note=true
-- depois do turno seguinte: needs_resume_note=false
```

### 7. `list_conversations`: pausadas e não lidas no topo

A ordenação responde "o que precisa de mim?" primeiro. Pausadas e não lidas dividem o
mesmo posto em vez de aninhar, porque uma conversa pausada sem nada novo continua
precisando ser devolvida. Depois disso, por atividade recente.

A lista é dirigida por `sessions`, não por `messages`: um lead com sessão e nenhuma
mensagem ainda **aparece**, em vez de sumir da vista do operador.

---

## Teste fim a fim, pelo navegador

A suíte não abre navegador. Depois que ela passar, vale rodar isto uma vez à mão:

1. Suba a app (`cd src && python app.py`) e entre em `/dashboard/login`.
2. No menu, clique em **Inbox**.
3. Mande uma mensagem pelo sandbox do Twilio. A linha aparece sozinha em ~5s (polling HTMX).
4. Escreva "quero falar com uma pessoa". A conversa vira **⏸ Você assumiu** e sobe ao topo.
5. Mande mais uma mensagem como lead. Ela **aparece** no inbox e a IA **não** responde.
6. Abra a conversa e responda pelo painel. A mensagem chega no WhatsApp **pelo mesmo
   número de sempre** — o lead não percebe que trocou quem responde.
7. Clique em **Devolver para a IA** e mande outra mensagem como lead. A IA responde
   retomando o assunto, **sem se reapresentar** e sem repetir o aviso de 1 hora.

---

## Armadilhas que este roteiro cobre

| # | Armadilha | Onde dói |
|---|---|---|
| 1 | Editar a 001 é invisível para o `init_db()` | Banco antigo fica com a `sessions` velha para sempre; **recrie**, não migre |
| 2 | A branch de pausa precisa gravar antes de retornar | Operador cego durante o takeover |
| 3 | `send_message` re-lança; a rota precisa capturar | 500 nu, ou mensagem fantasma que o lead nunca recebeu |
| 4 | PII em log (repo público) | Conteúdo de conversa em log — nunca; só `sender` e contagens |
| 5 | Ordem do payload | `DESC` + `LIMIT` + reverter; desempate por `id`, nunca só `created_at` |
| 6 | Estágio neutro tem que existir nos dois lugares | Por isso reusamos `interest` em vez de inventar um |
| 7 | Operador-painel ≠ dono-WhatsApp | Canais diferentes, pessoas diferentes, sem sobreposição |

Sobre a #5, vale destacar: `DEFAULT NOW()` no Postgres é `transaction_timestamp()`. Duas
linhas gravadas na **mesma transação** recebem o timestamp **idêntico** — por isso toda
ordenação é por `(created_at, id)`, nunca por `created_at` sozinho.

---

## Consultas úteis no DBeaver

```sql
-- A conversa inteira de um lead, na ordem certa
SELECT author, is_read, created_at, content FROM messages
WHERE sender = 'whatsapp:+55...' ORDER BY created_at, id;

-- O que o inbox mostra, na ordem em que mostra
SELECT s.sender, s.is_paused, s.stage,
       (SELECT COUNT(*) FROM messages m
        WHERE m.sender = s.sender AND m.author = 'lead' AND m.is_read = FALSE) AS nao_lidas
FROM sessions s ORDER BY s.updated_at DESC;

-- Conversas pausadas (alguém assumiu e talvez não devolveu)
SELECT sender, stage, updated_at FROM sessions WHERE is_paused = TRUE;

-- Volume por autor
SELECT author, COUNT(*) FROM messages GROUP BY author;
```

**Cuidado com `DELETE FROM sessions`**: `messages.sender` tem `ON DELETE CASCADE`, então
apagar uma sessão apaga a conversa inteira junto. É o comportamento desejado (esquecer um
lead não deve deixar conversa órfã), mas é a única operação que destrói histórico do inbox.
