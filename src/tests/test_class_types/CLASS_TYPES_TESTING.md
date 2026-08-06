# Tipos de aula e capacidade por tenant (Módulo S2)

Roteiro de teste do módulo que tirou `CLASS_CAPACITY` e `CLASS_TYPE_LABELS` de dentro do
código e os moveu para a tabela `class_types`, uma linha por turma, por academia.

**O que o módulo promete, em uma frase:** depois dele, a Delariva se comporta **exatamente**
como antes, e uma segunda academia com turmas completamente diferentes passa a ser possível.
Os dois lados dessa frase são testados aqui.

---

## Antes de começar

```bash
source venv/bin/activate
cd src
python app.py     # aplica a migration 008 e semeia o tenant 'default'; ^C depois do boot
```

Confira que a 008 entrou e o seed reproduziu os dicts antigos:

```sql
SELECT version FROM schema_migrations ORDER BY version;   -- 001 … 008

SELECT marker, label, capacity, requires_child_name, is_fallback
FROM class_types WHERE tenant_id = 'default' ORDER BY marker;
-- ADULTOS  | Adultos    | NULL | false | true
-- BABY     | Baby Class | 2    | true  | false
-- CRIANCAS | Crianças   | 4    | true  | false

SELECT * FROM scheduling_configs;   -- default | 14
```

`capacity NULL` é **ilimitado**, e só `NULL`. Se você vir `0` ou `-1` ali, alguém inventou um
sentinela e quebrou `get_available_slots()`, que testa `if capacity is not None`.

---

## A suíte automatizada

```bash
python tests/test_class_types/test_class_types_suite.py
```

20 testes, determinística, sem LLM, sem WhatsApp e **sem Calendar real** — o cliente do Google
é substituído por um falso que devolve eventos de mentira, e é isso que torna as asserções de
capacidade, fallback e nome da criança testáveis sem escrever na agenda de ninguém.

Flags: `--keep` (não desfaz nada), `--no-color`, `--json` (grava o relatório em `tests/outputs/`).

> ⚠ **A suíte sobrescreve as turmas reais do piloto.** A tela de configurações só sabe escrever
> no tenant `'default'` (isolamento por tenant é o S3), então os testes de tela mexem nas linhas
> de verdade. Elas são salvas em `/tmp/corujai_class_types_backup.json` antes da primeira escrita
> e restauradas no teardown; se um run morrer no meio, o próximo conserta sozinho no começo.
> Tudo que **não** depende da tela roda em tenants fictícios (`suite_ct_*`), que o teardown apaga.

---

## A CLI manual

A suíte afirma; a CLI deixa **ver**. A diferença que importa: a tela mostra as linhas do banco,
o `show` mostra o **pacote que o motor de agendamento recebe** — incluindo a turma padrão, que
pode ser sintética e portanto não existir em linha nenhuma.

```bash
python tests/test_class_types/test_class_types.py show
python tests/test_class_types/test_class_types.py normalize --marker "Crianças"
python tests/test_class_types/test_class_types.py parse --title "[ CRIANÇAS ] Aula Experimental"
```

---

## Roteiro

### 1. O piloto não mudou de comportamento

```bash
python tests/test_class_types/test_class_types.py show
```

Espere ver, em "O que o motor de agendamento enxerga":

```
capacities          : {'ADULTOS': None, 'BABY': 2, 'CRIANCAS': 4}
labels              : {'ADULTOS': 'Adultos', 'BABY': 'Baby Class', 'CRIANCAS': 'Crianças'}
child_name_required : ['BABY', 'CRIANCAS']
fallback            : ADULTOS
```

São, literalmente, os dois dicts que estavam em `bot/scheduling.py` mais o set
`{"BABY", "CRIANCAS"}` que estava escrito à mão dentro de `book_slot()`. Se algo aí divergir,
o módulo falhou na sua promessa principal e a regressão do Módulo 2 vai acusar.

### 2. O marcador do título e o marcador do cadastro se encontram

O parser normaliza o que lê do título do evento (`_strip_accents(...).upper()`), então o que
está guardado precisa estar na mesma forma canônica — senão a comparação falha **em silêncio**
e todo slot cai na turma padrão.

```bash
python tests/test_class_types/test_class_types.py normalize --marker "Crianças"   # → CRIANCAS
python tests/test_class_types/test_class_types.py normalize --marker "  bebê  "   # → BEBE
python tests/test_class_types/test_class_types.py normalize --marker "WOD-1"      # → RECUSADO
python tests/test_class_types/test_class_types.py normalize --marker "OPEN GYM"   # → RECUSADO
```

As duas últimas recusas não são frescura: o regex do título é `[a-zA-ZÀ-ÿ]+`, então um marcador
com hífen ou espaço poderia ser **cadastrado** mas **nunca lido** de um título. Seria uma turma
que nenhum evento consegue receber.

Confirme dos dois lados:

```bash
python tests/test_class_types/test_class_types.py parse --title "[Crianças] Aula"
python tests/test_class_types/test_class_types.py parse --title "[ CRIANCAS ] Aula"
python tests/test_class_types/test_class_types.py parse --title "[baby] Aula"
```

Os três resolvem para o marcador canônico, com a capacidade e a exigência de nome certas.

### 3. Uma segunda academia existe (o ponto do módulo)

```bash
T="python tests/test_class_types/test_class_types.py"
$T add --tenant box --marker wod  --label "WOD"       --capacity 12
$T add --tenant box --marker open --label "Open Gym"
$T add --tenant box --marker kids --label "Kids"      --capacity 6 --requires-child-name
$T set-default --tenant box --marker WOD
$T set-days --tenant box --days 21
$T show --tenant box
```

Um box de CrossFit, com turmas que não têm nada a ver com Jiu-Jitsu, janela de 21 dias, e
`OPEN` sem `--capacity` (ilimitado). Agora confirme o isolamento:

```bash
$T show            # o 'default' continua com BABY/CRIANCAS/ADULTOS, sem WOD
$T show --tenant box   # o box não enxerga nada do piloto
```

### 4. A armadilha central: título sem marcador, tenant sem `ADULTOS`

Este é o teste que justifica o desenho do módulo. Antes do S2,
`capacity = CLASS_CAPACITY[class_type]` era um acesso **direto**, sem `.get`, e só era seguro
porque `_parse_class_type` só conseguia devolver uma chave do dict literal. Lendo de tabela,
essa garantia some: e se o tenant não tiver `ADULTOS` cadastrado?

```bash
$T add --tenant sem-fb --marker wod --label "WOD" --capacity 12
$T show --tenant sem-fb
```

O `show` avisa, em destaque, que a turma padrão é **sintética**. E o parse não explode:

```bash
$T parse --tenant sem-fb --title "Aula livre"
```

```
Turma  : ADULTOS  (Adultos)
Vagas  : ilimitado
Criança: não exige
  ↳ caiu na TURMA PADRÃO (o título não trazia um marcador conhecido).
```

**Por que sintetizar e não pegar a primeira turma cadastrada:** se o fallback pegasse a primeira
linha em ordem alfabética, um título com erro de digitação cairia em `KIDS` — capacidade 6 e
exigindo o nome de uma criança que não existe — e **bloquearia** o agendamento. O fallback existe
justamente para o contrário: um evento mal digitado tem que **degradar**, nunca travar. Por isso
o sintético é sempre ilimitado e nunca pede nome de criança.

Confirme que nada foi gravado:

```sql
SELECT marker FROM class_types WHERE tenant_id = 'sem-fb';   -- só WOD; ADULTOS não está lá
```

Saia desse estado marcando uma turma de verdade como padrão:

```bash
$T set-default --tenant sem-fb --marker WOD
$T parse --tenant sem-fb --title "Aula livre"   # agora cai em WOD, capacidade 12
```

### 5. A turma padrão não pode ser excluída

```bash
$T delete --tenant box --marker WOD
```

```
[WOD] é a turma padrão de 'box' e não pode ser excluída.
Marque outra como padrão antes (set-default).
```

A recusa mora em `bot/class_types.py`, não na rota, de propósito: ela protege o **dado**, então
vale igual para quem escrever por SQL, pela tela ou pela CLI. Sem ela, o tenant passaria a
depender de uma turma sintética que ninguém escolheu, e o dono não teria como descobrir pela
tela por que os eventos sem marcador mudaram de comportamento.

### 6. O fluxo completo do dono, na tela

`/dashboard/settings`, seção **Aulas**. São dois passos, e é importante entender que são
coisas diferentes:

1. **Cadastrar a turma** (uma vez): marcador, nome, vagas, exige nome da criança.
   Responde *o que* a aula é.
2. **Marcar a aula na agenda** (a cada aula): turma + data + início + fim.
   Responde *quando* a aula acontece.

O passo 2 cria o evento no Google Calendar como `[MARCADOR] Nome da turma`, na data e hora
escolhidas. O título é **montado pelo código** — a turma vem de uma lista, não de um campo de
texto —, então não existe o risco de digitar o marcador errado e a aula cair na turma padrão sem
ninguém perceber.

Confira que o evento volta legível:

```bash
$T slots     # o horário que você acabou de marcar tem que aparecer aqui
```

com as vagas e a exigência de nome vindas do cadastro da turma, não do evento.

Guardas do formulário (todas com aviso em português, **status 200**, sem tocar no Google):

- fim antes ou igual ao início;
- data/hora no passado;
- data ou hora em branco;
- turma não cadastrada.

**O que a tela não faz:** remarcar ou apagar uma aula. Para isso, Google Agenda — é lá que os
horários vivem, e a tela diz isso ao dono.

Ainda na seção, confira o cadastro de turmas:

- A lista mostra as três turmas do piloto, com a padrão destacada.
- Cadastrar `wod` (minúsculo) salva como `WOD` — o mesmo normalizador da CLI.
- Cadastrar `WOD-1` é recusado com aviso em português, **status 200**, sem gravar.
- Capacidade vazia grava `NULL` (a lista passa a mostrar "ilimitado"); capacidade `0` é recusada.
- Excluir `[ADULTOS]` é recusado enquanto ela for a padrão.
- "Tornar padrão" move a marca — a antiga perde, na mesma transação.
- A janela de horários aceita 1–90 e recusa o resto.

Nada disso responde 4xx/5xx: o dono precisa ver a página com o aviso, não uma tela de erro.

### 7. A janela de horários chega ao Calendar

```bash
$T set-days --days 30
$T slots            # lê o Google Calendar de verdade (só leitura)
$T set-days --days 14
```

`slots` é o único comando que sai para a internet. Com `--days` explícito ele ignora a
configuração; sem, usa a do tenant.

### 8. Limpeza

```bash
$T drop-tenant --tenant box
$T drop-tenant --tenant sem-fb
$T show          # o piloto de volta ao normal
```

Se você mexeu no `'default'` à mão, `backup`/`restore` existem para isso.

---

## Regressão obrigatória

O Módulo 2 é a prova de que o piloto não mudou:

```bash
python tests/test_scheduling/test_scheduling_suite.py
```

**Escreve no Google Calendar real** e exige a integração conectada. Ele foi adaptado para ler os
tipos da nova fonte, mas **nenhuma asserção de comportamento foi afrouxada** — inclusive continua
exigindo que `ADULTOS` tenha capacidade `None`, que o slot de adultos nunca encha e que um título
sem marcador caia no fallback com o warning no log.

---

## Onde as coisas moram

| O quê | Onde |
|---|---|
| Tabelas | `database/migrations/008_create_class_types.sql` |
| Leitura e escrita | `bot/class_types.py` |
| Invariante do fallback | `bot/class_types.py::load_class_types()` (docstring) |
| Uso no agendamento | `bot/scheduling.py::get_available_slots()` e `book_slot()` |
| Tela | `templates/settings.html` (seção "Aulas") + as rotas `settings_*_class_type` |
| Rótulos nos outros consumidores | `webhook/routes.py`, `jobs/drain_notifications.py`, `bot/confirmations.py`, `bot/ai_context.py` |
