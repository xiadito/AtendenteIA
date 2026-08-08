# Corujai

Chatbot de WhatsApp que qualifica leads e agenda aulas experimentais para academias.

A documentação de arquitetura e desenvolvimento está em [CLAUDE.md](CLAUDE.md); os roteiros de
teste, em `src/tests/`.

---

## Criar a conta de uma academia

Desde o **Módulo S3a** o painel tem contas de verdade: login por e-mail, senha em hash, e cada
conta dona de um tenant. Há **dois caminhos** para criar uma:

- **Pela linha de comando** (seções 1 a 3 abaixo), quando você provisiona no lugar do cliente.
- **Pela tela pública** `/dashboard/signup` (seção 4), em que o próprio dono se cadastra —
  adicionada pelo Módulo S3c e **desligada por padrão**.

> ### ⛔ Não crie uma segunda conta em produção ainda
>
> O S3a dá contas, login e provisionamento. Ele **não** dá isolamento de leitura — isso é o
> **S3b**. Hoje `sessions` não tem coluna `tenant_id` e `get_conversation()`,
> `list_conversations()`, `count_unread()` e `list_bookings_for_review()` devolvem as linhas de
> todos os tenants. **Com duas academias no mesmo banco de produção, cada uma enxerga as
> conversas e os agendamentos da outra** — inclusive se a segunda for a sua própria conta de
> teste.
>
> Com só o tenant `default` existindo, o S3a em produção é seguro e se comporta exatamente como
> antes. Em banco de **desenvolvimento**, provisionar várias academias é seguro e esperado.

### 1. O seu primeiro login

Preencha as duas variáveis no `src/.env` — **`DASHBOARD_USER` precisa ser um e-mail**, porque é
o identificador de login desde o S3a:

```bash
DASHBOARD_USER="voce@exemplo.com"
DASHBOARD_PASSWORD="uma-senha-de-pelo-menos-8-caracteres"
```

Suba a aplicação uma vez. Se a tabela `users` estiver vazia, ela cria esse usuário para o tenant
`default`, com a senha em hash:

```bash
source venv/bin/activate
cd src && python app.py
```

A partir daí essas duas variáveis **não são mais consultadas**: o login valida contra
`users.password_hash`. `DASHBOARD_PASSWORD` deixou de ser a credencial do painel e virou só a
semente inicial. Troque a senha assim que entrar:

```bash
python -m accounts.provision reset-password --email voce@exemplo.com --password -
```

O `-` faz o comando pedir a senha interativamente, para ela não ficar no histórico do shell.

Se você vir `AVISO: DASHBOARD_USER não é um e-mail` no boot, é isso: o valor antigo era `admin`,
e o bootstrap recusa qualquer coisa sem `@`.

### 2. Criar a conta de um cliente novo

```bash
cd src
python -m accounts.provision create \
    --name "Academia Delariva Itaipuaçu" \
    --email dono@academia.com.br \
    --password - \
    --owner-phone 5521999999999
```

O comando cria, **numa transação só**, tudo que uma academia precisa para funcionar:

| Tabela | O que recebe |
|---|---|
| `owners` | a academia, com o Google Calendar ainda desconectado |
| `ai_configs` | a camada de prompt, com os textos-guia entre colchetes para o dono preencher |
| `class_types` | a turma padrão, ilimitada — é ela que segura eventos sem `[MARCADOR]` no título |
| `scheduling_configs` | a janela de busca no Calendar (14 dias) |
| `users` | o login, com a senha em hash |

É tudo ou nada de propósito: uma academia com linha em `owners` mas sem `ai_configs` deixaria o
dono salvar a seção "IA" para sempre sem efeito nenhum, e uma sem `class_types` rodaria ignorando
capacidade de turma, em silêncio.

O identificador (`tenant_id`) sai do nome, de forma mecânica: `"Academia Delariva Itaipuaçu"` vira
`academia-delariva-itaipuacu`. Se quiser um mais curto, passe `--tenant-id delariva-itaipuacu`.
Veja antes de criar, sem escrever nada:

```bash
python -m accounts.provision slug --name "Academia Delariva Itaipuaçu"
```

Repetir o comando com o mesmo e-mail não cria nada — o e-mail é a identidade do pedido.

### 3. Apontar o número da academia

É o que faz o Corujai saber **para qual academia** cada mensagem foi escrita: o campo `To` do
Twilio é comparado com `owners.whatsapp_number`.

```bash
python -m accounts.provision set-whatsapp-number \
    --tenant-id academia-delariva-itaipuacu --number 5521888888888
```

**Enquanto esse número for nulo, toda mensagem cai no tenant `default`** — com um aviso no log. É
o que acontece hoje: o Sandbox do Twilio dá o mesmo número de entrada para todo mundo, então
nenhuma academia pode reivindicá-lo. O comportamento fica idêntico ao de antes do S3a, e a forma
correta já está no lugar para quando os números reais entrarem.

Cuidado com os dois números, que são diferentes e têm papéis diferentes:

- **`whatsapp_number`** é o número **da academia** no Twilio — o `To`. Responde "para qual
  academia isto foi escrito?".
- **`owner_phone`** é o número **pessoal do dono** — o `From`. Responde "quem escreveu é o dono
  respondendo `1`/`2`, ou é um lead?".

O comando recusa um número que já seja o telefone de algum dono ou que já pertença a uma conversa
de lead: um número com dois papéis torna o roteamento ambíguo.

### 4. Ou deixe o próprio cliente se cadastrar

Desde o **Módulo S3c** existe uma tela pública em `/dashboard/signup`: o dono da academia
preenche nome, e-mail e senha, e a conta nasce com as mesmas cinco tabelas do comando acima.
Depois de criar, ele cai numa tela de **primeiros passos** que lista o que ainda falta —
conectar o Google Calendar, descrever a academia para a IA, cadastrar as turmas, e o número de
WhatsApp, que aparece como *"nossa equipe está preparando"* porque é a etapa que só você resolve.

**A tela vem desligada.** Ligue com:

```bash
SIGNUP_ENABLED="true"     # no src/.env
```

> ### ⛔ Não ligue isto em produção antes do Módulo S3b
>
> Um cadastro público não corre o risco de criar uma segunda conta — ele **fabrica** segundas
> contas. E até o S3b as leituras não filtram por tenant: cada academia que se cadastrasse
> passaria a enxergar as conversas e os agendamentos da Delariva, e vice-versa.
>
> Com a flag desligada (o padrão), `/dashboard/signup` responde 404 e o link nem aparece na tela
> de login. Em banco de desenvolvimento, ligar e testar é seguro.

O formulário se defende sozinho de duas formas: um campo escondido que só um robô preenche, e um
teto de 5 tentativas por hora, por IP. Nenhum dos dois para alguém determinado — param o script
que varre formulários pela internet, que é o problema real de um cadastro aberto.

### 5. Os outros comandos

```bash
python -m accounts.provision list            # academias e seus logins (nunca o hash)
python -m accounts.provision reset-password --email … --password -
```

Para testar tudo isso sem tocar em produção, veja
[src/tests/test_accounts/ACCOUNTS_TESTING.md](src/tests/test_accounts/ACCOUNTS_TESTING.md) e
[src/tests/test_signup/SIGNUP_TESTING.md](src/tests/test_signup/SIGNUP_TESTING.md).

---

## Credenciais do Twilio (`TWILIO_ACCOUNT_SID` e `TWILIO_AUTH_TOKEN`)

O envio de mensagens passa por `src/whatsapp/whatsapp_service.py`, que monta o client com
essas duas variáveis. Sem elas nenhuma resposta sai da aplicação.

### 1. Criar a conta

Acesse [twilio.com/try-twilio](https://www.twilio.com/try-twilio) e crie uma conta. A conta
trial é suficiente para o piloto: ela já dá acesso ao **Sandbox de WhatsApp**, que é o que o
projeto usa hoje (a Meta Cloud API está prevista, mas o código dela está comentado em
`src/webhook/routes.py`).

O cadastro pede confirmação de e-mail e de um número de telefone — esse número é o seu, para
verificação da conta, e não tem relação com o número que a academia vai usar.

### 2. Copiar o Account SID e o Auth Token

Depois do login você cai no Console. Existem duas versões no ar, e as duas servem:

- **`console.twilio.com`** — o Console clássico. Na página inicial, role até o painel
  **Account Info**.
- **`1console.twilio.com`** — o *Twilio One Console*, a interface nova. A página da conta tem o
  Account SID **na própria URL**: `1console.twilio.com/account/ACeb5b93…`.

Os dois valores que interessam:

| Campo no Console | Formato | Vai para |
|---|---|---|
| **Account SID** | começa com `AC`, 34 caracteres | `TWILIO_ACCOUNT_SID` |
| **Auth Token** | 32 caracteres, escondido atrás de **Show** | `TWILIO_AUTH_TOKEN` |

O Account SID é um identificador, não um segredo — ele aparece na URL do Console e em toda
resposta da API. O **Auth Token é senha**: quem tem os dois envia mensagens cobrando da sua
conta. Só ele fica escondido atrás do botão **Show**, e só ele precisa de cuidado.

Se o painel não estiver visível na home, o caminho longo é **Account → API keys & tokens**,
onde também fica o botão de rotacionar o Auth Token caso ele vaze.

> Contas com mais de um projeto têm um SID por projeto. Confirme no seletor de conta, no topo
> do Console, que você está no projeto certo antes de copiar.

> O Auth Token não é acessível por fora do Console logado — nenhuma ferramenta consegue lê-lo
> por você. Copiar do **Show** e colar no `src/.env` é o único caminho.

### 3. Ativar o Sandbox de WhatsApp

No Console: **Messaging → Try it out → Send a WhatsApp message**.

A aba **Sandbox** mostra o número do sandbox (normalmente `+1 415 523 8886`, que é o default de
`TWILIO_SANDBOX_NUMBER`) e um código de entrada no formato `join <duas-palavras>`. Cada celular
que for conversar com o bot precisa mandar esse código, por WhatsApp, para o número do sandbox —
inclusive o seu, para testar, e o do dono da academia.

O convite do sandbox expira em 72 horas de inatividade; passado esse prazo é só reenviar o
`join <duas-palavras>`.

Na aba **Sandbox settings**, aponte **When a message comes in** para o webhook da aplicação,
com o método **HTTP POST**:

```
https://<seu-dominio-railway>/webhook
```

Em desenvolvimento local, use um túnel (ngrok, Cloudflare Tunnel) para expor a porta 5000 e
cole a URL pública gerada no mesmo campo.

### 4. Preencher o `src/.env`

```bash
TWILIO_ACCOUNT_SID="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
TWILIO_AUTH_TOKEN="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
TWILIO_SANDBOX_NUMBER="whatsapp:+14155238886"
```

O prefixo `whatsapp:` em `TWILIO_SANDBOX_NUMBER` é obrigatório — é assim que a API do Twilio
distingue o canal, e é o formato em que os números de lead **chegam** no webhook
(`whatsapp:+5521999999999`). Eles não são gravados assim: `receive_twilio()` tira o prefixo antes
de qualquer coisa, então `sessions.sender`, `messages.sender` e `trial_bookings.sender` guardam
dígitos puros (`5521999999999`) — que é também o formato que `send_message()` espera de volta.

`src/.env` está no [.gitignore](.gitignore) e o repositório é público: **nunca** comite o Auth
Token. Se ele for parar em um commit, rotacione o token no Console — remover o commit não basta,
o valor já esteve exposto.

No Railway as mesmas variáveis vão em **Variables**, nos dois serviços (web e cron), já que o
cron de notificações ao dono também envia WhatsApp.
