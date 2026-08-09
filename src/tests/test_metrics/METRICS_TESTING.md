# Métricas do funil (Módulo S4)

A tela que responde a pergunta que vende o produto: **o Corujai está me dando resultado?**
Quatro números por academia — leads, agendamentos, confirmados, cancelados — e a conversão
entre eles, num recorte de 7, 30 ou 90 dias.

- Tela: `/dashboard/metrics` (e três números-resumo em `/dashboard/menu`)
- Código: `src/bot/metrics.py`, `webhook/routes.py::metrics_dashboard`, `templates/metrics.html`
- **Nenhuma migration.** É só leitura agregada; os índices que ela usa a migration 011 já criou.

---

## 1. O que cada número conta, exatamente

O dono vai perguntar "esse número é o quê?". Estas são as respostas, e elas são a
especificação — não uma descrição aproximada do código.

A janela é de **N dias corridos, incluindo hoje**, das `00:00` no fuso `America/Sao_Paulo`
até agora.

| Número | Sai de | Conta |
|---|---|---|
| **Leads** | `messages` | Pessoas cujo **primeiro contato** com esta academia caiu na janela: `MIN(created_at)` das mensagens com `author = 'lead'`, agrupado por `sender`. |
| **Agendamentos** | `trial_bookings` | Reservas **criadas** na janela (`created_at`), em qualquer status. |
| **Confirmados** | `trial_bookings` | Das reservas criadas na janela, as que **hoje** estão `confirmed`. |
| **Cancelados** | `trial_bookings` | Idem, `cancelled`. |
| **Pendentes** | `trial_bookings` | Idem, `pending_confirmation`. Não é etapa do funil — está ali para a soma fechar. |

E as três taxas:

| Taxa | Conta |
|---|---|
| `lead_to_booking_rate` | agendamentos ÷ leads |
| `booking_to_confirmed_rate` | confirmados ÷ agendamentos |
| `booking_to_cancelled_rate` | cancelados ÷ agendamentos |

**Uma coorte só, ancorada em `created_at`.** Os quatro números de reserva descrevem o mesmo
conjunto de linhas, fatiado pelo status que cada uma carrega hoje. Daí a propriedade que o dono
consegue conferir de cabeça:

```
agendamentos = confirmados + cancelados + pendentes
```

Ancorar as decisões em `updated_at` ("o que o dono decidiu esta semana") seria uma pergunta
legítima, mas **outra** — e misturar as duas âncoras numa tabela só permitiria mais
confirmações do que agendamentos no mesmo período. Isso é aritmética que ninguém consegue
confiar.

> **Uma exceção honesta:** `agendamentos ÷ leads` **pode passar de 100%**. Um lead cujo primeiro
> contato foi antes da janela pode agendar dentro dela, então os dois conjuntos não são
> aninhados. A tela mostra o número real e limita só a **barra**, nunca a cifra.

---

## 2. Comparecimento NÃO é medido — e não pode ser derivado (problema P1)

**Este é o débito central do módulo, e está aqui para nenhum módulo futuro "consertá-lo".**

`trial_bookings` tem três status: `pending_confirmation`, `confirmed`, `cancelled`. **Nenhum
deles registra se o lead apareceu na aula.**

`confirmed` quer dizer que **o dono respondeu que a aula vai acontecer** — uma decisão tomada
dias *antes* da aula, pelo WhatsApp ou pela tela de agendamentos. Não é uma observação feita
depois dela.

Então qualquer rótulo do tipo "taxa de comparecimento", "show rate", "presença" ou "frequência"
seria um número **inventado** a partir de outro que significa coisa diferente — apresentado
como medição, na única tela em que o dono vai tomar decisão. Medir comparecimento exige um
estado novo em `trial_bookings` e um **momento de captura** que este produto não tem: ninguém,
hoje, informa ao sistema que a aula aconteceu e quem entrou pela porta. Isso é decisão de
produto, não de implementação.

Enquanto esse momento não existir, esta tela conta leads, agendamentos, confirmações e
cancelamentos — e nada além disso.

Três coisas defendem esse limite, e as três são testadas:

1. Nenhuma chave de `get_funnel()` / `get_funnel_summary()` tem vocabulário de comparecimento.
2. Nenhum rótulo do funil na tela tem.
3. A tela **carrega uma nota** dizendo isso ao dono em português. A nota é parte do módulo, não
   enfeite: sem ela, "Confirmados" volta a ser lido como presença. O teste 10 falha se ela
   sumir.

---

## 3. A armadilha que derrubaria a implementação óbvia

**`sessions.conversation_started_at` parece ser a coluna de "quando o lead chegou". Não é.**

O timeout de 1 hora de inatividade a reescreve com `NOW()`
(`bot/handlers.py::_reset_session`). Ela significa "início da conversa **atual**". Um lead que
apareceu em março, sumiu, e voltou a escrever hoje ganha o carimbo de hoje — e seria contado
como uma chegada de hoje. E `sessions` **não tem `created_at`**: não existe, naquela tabela,
carimbo de primeiro contato.

Por isso os leads saem de `messages`: mensagem só é apagada pelo `clear_session()`, então
`MIN(created_at)` é um primeiro contato que **não se move**. E o índice que serve esse
`GROUP BY` a migration 011 já criou por outro motivo:
`idx_messages_tenant_sender_created_at (tenant_id, sender, created_at)`.

O mesmo raciocínio vale para o outro lado: **`sessions.stage` não diz quantos agendaram.**
`stage` é o estado atual de uma conversa, não um histórico — um lead que agendou e depois teve
a reserva cancelada pode estar com o `stage` em qualquer valor. Contar por `stage = 'booked'`
subconta e superconta ao mesmo tempo. A fonte é o log: `trial_bookings`.

---

## 4. Como rodar

```bash
cd src

# Suíte automatizada (três academias de teste, determinística, sem rede)
python tests/test_metrics/test_metrics_suite.py

# Variações
python tests/test_metrics/test_metrics_suite.py --keep      # não limpa as fixtures
python tests/test_metrics/test_metrics_suite.py --json      # grava relatório em tests/outputs/
python tests/test_metrics/test_metrics_suite.py --no-color
```

Sai com código 0 só se tudo passou. SKIPs não reprovam a run.

**Sem backup em `/tmp`, de propósito** — mesma razão do `test_accounts` e do
`test_tenant_isolation`: esta suíte nunca escreve nas linhas do piloto. Tudo vive em três
academias criadas pelo `provision_tenant()` sob o prefixo `suite-s4-`, com o domínio de e-mail
próprio `@suite-s4.corujai.test` (que **não** casa com o `%@suite.corujai.test` que o
`test_accounts` apaga no teardown) e o prefixo de sender `5531000`. `_drop_orphan_fixtures()`,
no início do `main()`, faz o papel de reparo pós-crash que o arquivo de backup faz nas outras.

### CLI manual

A suíte afirma; a CLI deixa **ver**. Ela combina com o DBeaver: semeie um funil de forma
conhecida, leia pelo código, depois leia pelo SQL cru e confirme que os dois concordam.

```bash
python tests/test_metrics/test_metrics.py setup      # cria a academia da CLI
python tests/test_metrics/test_metrics.py seed       # semeia leads e reservas datados
python tests/test_metrics/test_metrics.py show                    # o funil, como a tela calcula
python tests/test_metrics/test_metrics.py show --period 7
python tests/test_metrics/test_metrics.py show --tenant default   # o piloto — só leitura
python tests/test_metrics/test_metrics.py rows       # as linhas cruas, dentro/fora da janela
python tests/test_metrics/test_metrics.py teardown
```

`show --tenant default` é o comando útil no piloto: **não escreve nada**. Todo o resto vive sob
`suite-s4-`.

---

## 5. A forma semeada, e por que ela é assim

A academia **A** é a que está sob teste. Contando os dias para trás a partir de hoje:

| Leads (primeiro contato) | Reservas (`created_at` → status de hoje) |
|---|---|
| 3 leads há 2 dias | há 2 dias: 1 confirmada, 1 cancelada, 1 pendente |
| 2 leads há 15 dias | há 15 dias: 1 confirmada |
| 1 lead há 45 dias | há 45 dias: 1 cancelada |
| 2 leads há 200 dias | há 200 dias: 1 confirmada |
| *um deles voltou a escrever ontem* | |

O que dá, por janela:

| | 7d | 30d | 90d |
|---|---|---|---|
| Leads | 3 | 5 | 6 |
| Agendamentos | 3 | 4 | 5 |
| Confirmados | 1 | 2 | 2 |
| Cancelados | 1 | 1 | 2 |
| Pendentes | 1 | 1 | 1 |

Em 30 dias: conversão de 80% (4/5), 50% confirmados (2/4), 25% cancelados (1/4).

A academia **B** é semeada grande e chapada — 7 leads, 6 reservas, todas confirmadas, todas de
2 dias atrás — de propósito: nenhum número dela coincide com um de A, então **um vazamento
aparece como um número estranho** em vez de se esconder num total plausível. Um `COUNT(*)` sem
`WHERE tenant_id` mostraria 12 leads onde deviam estar 5.

A academia **C** é semeada com nada. Ela *é* o caso da divisão por zero.

Cada número esperado acima está escrito à mão na suíte, nunca recalculado com o código sob
teste — um teste que pergunta à implementação qual devia ser a resposta não prova nada.

---

## 6. Os 16 testes

| # | O que prova |
|---|---|
| 1 | Leads contam o primeiro contato dentro da janela: 3 / 5 / 6 em 7 / 30 / 90 dias. Várias mensagens do mesmo lead não o contam duas vezes. |
| 2 | **Lead antigo que volta não vira lead novo.** O de 200 dias atrás, ativo ontem, continua fora — e o teste confirma que ele *de fato* escreveu na janela, para não passar por acidente. |
| 3 | **Os números vêm do log, não do `stage`.** Um lead recebe `stage='booked'` sem nenhuma reserva; outro fica em `'interest'` tendo uma reserva real. Os contadores ignoram os dois palpites. |
| 4 | Confirmados + cancelados + pendentes fecham em agendamentos, nas três janelas. |
| 5 | **Isolamento.** A vê 5/4, B vê 7/6, e nenhum dos dois vê 12. |
| 6 | 7/30/90 recortam certo (a reserva de 45 dias entra só em 90), e a janela abre à **meia-noite de São Paulo** — asserido pelo fuso, não pelo offset. |
| 7 | Período inválido cai em 30 sem levantar: `None`, `""`, `"abc"`, `"0"`, `"-5"`, `"365"`, `"7.5"`, `"30x"`… |
| 8 | Academia vazia: tudo zero, taxas `None`, sem `NaN`/`Infinity`. E `0.0` continua distinto de `None`. |
| 9 | As taxas batem com a conta à mão: 80% / 50% / 25%. |
| 10 | **Nada mede comparecimento**, e a nota do P1 continua na tela. |
| 11 | `/dashboard/metrics` exige login e redireciona o anônimo. |
| 12 | A tela mostra a academia **logada** e honra `?period=`. É onde o isolamento se perde de graça: parametrizar a agregação e esquecer de passar `current_user.tenant_id` deixaria tudo lendo o piloto sem nada quebrar. |
| 13 | A academia vazia renderiza o estado vazio — não um funil de zeros, não um 500. |
| 14 | O menu abre com o resumo da própria academia. |
| 15 | Resumo do menu e tela cheia **nunca discordam**, nos 3 tenants × 3 períodos. |
| 16 | A limpeza não deixa conta, tenant, sessão nem reserva pendurada. |

---

## 7. Conferindo no DBeaver

As duas perguntas, direto no banco. **`tenant_id` não é opcional em nenhuma delas** — é
justamente o que a tela precisa garantir.

```sql
-- LEADS: primeiro contato por lead. Os que caem na janela são os que a tela conta.
SELECT sender, MIN(created_at) AS first_contact
FROM messages
WHERE tenant_id = 'default' AND author = 'lead'
GROUP BY sender
HAVING MIN(created_at) >= NOW() - INTERVAL '30 days'
ORDER BY 2 DESC;

-- AGENDAMENTOS / CONFIRMADOS / CANCELADOS: uma coorte, quatro fatias.
-- Repare no created_at: NÃO é slot_start (quando a aula é) nem updated_at
-- (quando o dono decidiu).
SELECT
    COUNT(*)                                                AS agendamentos,
    COUNT(*) FILTER (WHERE status = 'confirmed')            AS confirmados,
    COUNT(*) FILTER (WHERE status = 'cancelled')            AS cancelados,
    COUNT(*) FILTER (WHERE status = 'pending_confirmation') AS pendentes
FROM trial_bookings
WHERE tenant_id = 'default'
  AND created_at >= NOW() - INTERVAL '30 days';
```

Estas duas usam `NOW() - INTERVAL`, que é uma janela **rolante**; a tela alinha o início na
meia-noite de São Paulo. Para janelas curtas os dois recortes podem diferir por algumas horas
de dados — se você estiver comparando número a número, use o `rows` da CLI, que imprime a
fronteira exata que a tela usou.
