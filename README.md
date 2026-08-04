# Corujai

Chatbot de WhatsApp que qualifica leads e agenda aulas experimentais para academias.

A documentação de arquitetura e desenvolvimento está em [CLAUDE.md](CLAUDE.md); os roteiros de
teste, em `src/tests/`.

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
distingue o canal, e é o formato em que os números de lead chegam e são gravados no banco
(`sessions.sender`, `messages.sender`).

`src/.env` está no [.gitignore](.gitignore) e o repositório é público: **nunca** comite o Auth
Token. Se ele for parar em um commit, rotacione o token no Console — remover o commit não basta,
o valor já esteve exposto.

No Railway as mesmas variáveis vão em **Variables**, nos dois serviços (web e cron), já que o
cron de notificações ao dono também envia WhatsApp.
