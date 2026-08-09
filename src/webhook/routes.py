from flask import Blueprint, abort, jsonify, request, render_template, redirect, url_for
from flask_login import current_user, login_user, logout_user
from config import Config
import logging
import threading
from datetime import date, datetime, time
from accounts.auth import User, require_auth
import accounts.onboarding as onboarding_steps
import accounts.provision as provision
import accounts.signup as signup_guard
import accounts.users as accounts_users
from whatsapp.whatsapp_service import SenderNotConfiguredError, send_message
from bot.handlers import handle_text_message
import bot.ai_configs as ai_configs
import bot.bookings as bookings
import bot.class_types as class_types
import bot.confirmations as confirmations
import bot.messages as messages
import bot.metrics as metrics
import bot.owner_notifications as owner_notifications
import bot.scheduling as scheduling
import bot.session as session_store
import integrations.store as store


# Configura o sistema de logs para mostrar data/hora, nível e mensagem
logging.basicConfig(
    level= logging.INFO, # INFO significa: mostra mensagens informativas e acima (WARNING, ERROR)
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)
# logging.getLogger(__name__) cria um logger com o nome do arquivo atual
# __name__ é uma variável especial do Python — vale "webhook.routes" neste caso
# Isso ajuda a identificar de qual arquivo veio cada log

# __name__ diz ao Flask onde esse blueprint está localizado (para encontrar templates, etc)
webhook_bp = Blueprint("webhook", __name__)


@webhook_bp.route("/webhook", methods=["GET"])
def verify():
    """
    A meta chama esse webhook UMA VEZ quando eu cadastrar o webhook no painel da meta web developers
    Ela envia 3 parâmetros via query string (na URL):
      ?hub.mode=subscribe
      &hub.verify_token=o_token_que_voce_cadastrou
      &hub.challenge=um_numero_aleatorio
    
    Você precisa:
    1. Confirmar que hub.mode == "subscribe"
    2. Confirmar que hub.verify_token bate com o seu VERIFY_TOKEN
    3. Devolver o hub.challenge como resposta (só o número, nada mais)
    """
     
    # request é um objeto global do Flask que representa a requisição HTTP atual
    # request.args é um dicionário com os parâmetros da query string (?chave=valor)
    # .get("chave") retorna o valor ou None se não existir
    # hub é o hub da meta
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    logger.info(f"Verificação recebida - mode: {mode}, token: {token}")
    
    # Verifica se os dois critérios de segurança são atendidos
    if mode == "subscribe" and token == Config.VERIFY_TOKEN:
        logger.info("Webhook verificado com sucesso!")
        # Retorna o challenge como texto puro (não JSON)
        # O segundo argumento do return é o status HTTP — 200 significa "OK"
        return challenge, 200
    else:
        logger.warning("Falha de verificação - token inválido ou mode incorreto")
        # 403 significa "Forbidden" — acesso negado
        return "Token inválido", 403

"""
@webhook_bp.route("/webhook", methods=["POST"])
def receive():
    #A meta envia isso toda vez que alguem manda mensagem.
    #Os dados chegam no corpo da requisição em formato JSON.

    # request.get_json() lê o corpo da requisição e converte o JSON em dicionário Python
    # Se o corpo não for JSON válido, retorna None
    data = request.get_json() 
    
    logger.info(f"Payload recebido: {data}")
    
    #verificação defensiva, se não veio JSON retorna erro
    if not data:
        return jsonify({"error": "Payload inválido"}), 400
        # 400 (da classe dos erros) significa "Bad Request" — a requisição veio malformada
     
        #Logar e confirmar recebimento de mensagem
        #meta exigem que 200 (status sucesso) sejam respondido em 20 sec
        #se não responder ela vai ficar reenviando achando que falhou
     
    try:
        #tries to extract the number and text of the message
        #the structure below is the json that meta sends in the post:
        #data["entry"][0]["changes"][0]["value"]["messages"][0]
        entry = data["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]
         
        # Not all the POSTS methods of meta have messagens, sometimes they´re status
        # So we verify if it's messages
        
        if "messages" in value:
            message = value["messages"][0]

            # the number of the sender comes in the internacional format without the +
            # ex: "5521999999999"
            sender = message["from"]
            
            # message["type"] could be "text", "image" and "audio", etc
            #we only treat text at the moment
            if message["type"] == "text":
                text = message["text"]["body"]
                logger.info(f"Mensagem de {sender}: {text}")
            elif message["type"] == "audio":
                audio = message["audio"]
                logger.error(f"Tipo de mensagem não suportado: {message['type']}")
                
    except (KeyError, IndexError) as e:
        # KeyError: tentou acessar uma chave que não existe no dicionário
        # IndexError: tentou acessar um índice que não existe na lista
        logger.error(f"Erro ao processar payload: {e}")
        # Mesmo com erro interno, retornamos 200 para a Meta não reenviar
        return jsonify({"status": "error"}), 200
     
     
    return jsonify({"status": "Ok"}), 200
"""

@webhook_bp.route("/webhook", methods=["POST"])
def receive_twilio() -> tuple:
    """
    O Twilio envia os dados como form data, não como JSON.
    Form data é o mesmo formato que um formulário HTML usa ao ser submetido.
    No Flask, acessamos via request.form.get("campo")
    """

    # request.form é um dicionário com os campos enviados pelo Twilio
    # .get("Campo") retorna o valor ou None se não existir - Twilio usa letra maiúscula nos campos: "From", "Body", "To"
    sender: str = request.form.get("From")  # ex: "whatsapp:+5521999999999"
    body: str   = request.form.get("Body")  # texto da mensagem
    to: str     = request.form.get("To")    # o número DA ACADEMIA — a chave de roteamento do tenant

    # Nunca logamos o texto da mensagem: o repositório é público e essas
    # mensagens são conversas inteiras de leads (ver bot/messages.py).
    logger.info(f"Mensagem recebida de {sender}")

    # Verifica se os campos essenciais chegaram
    if not sender or not body:
        logger.warning("Payload incompleto — From ou Body ausente")
        return jsonify({"error": "Payload inválido"}), 400


    # Twilio sends "whatsapp:+5521999999999" we only need the numbers: "5521999999999"
    # cleans the sender number - .replace() replace one substring for another — here we remove "whatsapp:+"
    clean_number = sender.replace("whatsapp:+", "")
    logger.info(f"Número limpo: {clean_number} | {len(body)} caractere(s)")

    # WHICH GYM WAS THIS WRITTEN TO? (Module S3a.) None means no tenant has
    # claimed this destination number — which is every message today, because
    # the Twilio Sandbox hands every gym the SAME inbound number and nobody can
    # own it. The two branches below are not a style choice:
    #
    #   * Always scoping to the resolved tenant would break the sandbox the day
    #     a second gym exists, since every message resolves to 'default' and
    #     gym B's owner would be read as one of gym A's leads.
    #   * Always scanning globally would let gym B's owner, writing to gym A's
    #     number, be routed to the owner handler — and their "1" would confirm
    #     one of gym A's bookings.
    #
    # So: trust "To" when "To" is informative, and fall back to the old global
    # comparison only when it is not. The fallback dies on its own the day every
    # tenant has a registered number.
    resolved: str | None = store.find_tenant_by_whatsapp_number(to)

    if resolved is None:
        # SANDBOX PATH — byte for byte the pre-S3a behaviour.
        logger.warning(
            "Nenhum tenant registrado para o número de destino; usando '%s'.",
            store.DEFAULT_TENANT_ID,
        )
        tenant_id: str = store.DEFAULT_TENANT_ID
        owner = store.get_owner_by_phone(clean_number)
        if owner is not None:
            tenant_id = owner["tenant_id"]
    else:
        # ROUTED PATH — "To" identified the gym, so owner-vs-lead is decided
        # INSIDE that gym.
        tenant_id = resolved
        owner = store.get_owner_by_phone_in_tenant(clean_number, tenant_id)

    # A message from the gym owner's own number is a reply to a notification,
    # not a lead starting/continuing a conversation — route it separately.
    if owner is not None:
        receive_twilio_owner(clean_number, body, tenant_id=owner["tenant_id"])
    else:
        handle_text_message(clean_number, body, tenant_id=tenant_id)

    # O Twilio espera status 200 para confirmar que você recebeu
    # Se não receber 200, ele tenta reenviar
    return jsonify({"status": "ok"}), 200


def receive_twilio_owner(
    owner_phone: str,
    body: str,
    tenant_id: str = store.DEFAULT_TENANT_ID,
) -> None:
    """Handle a WhatsApp reply from the gym owner (never a lead).

    Maps "1"/"2" to confirmed/cancelled, records the response, and — for a
    'booking' notification — actually closes the booking out through
    bot/confirmations.py. That last part is what Module 6 added; until then the
    reply was only ever recorded on owner_notifications.

    A 'handoff' notification carries no booking_id (there is no class to
    decide), so it is recorded and nothing else happens. A reply with no open
    notification returns None and there is nothing left to answer — which is
    what makes a second "1" harmless.

    Since Module S3b the resolved tenant is carried all the way down: the open
    notification is looked up INSIDE this gym, and the booking is closed inside
    it too. That is what stops gym B's owner — whose "1" arrived on gym B's
    number — from ever confirming a class of gym A's.

    Args:
        owner_phone (str): The owner's number, already in clean_number form.
        body (str): The raw text the owner sent.
        tenant_id (str): The gym this reply belongs to, resolved from the "To"
            field. Defaults to the pilot tenant so the manual CLIs and the test
            suites can keep calling with two arguments.
    """
    stripped = body.strip()
    mapping = {"1": "confirmed", "2": "cancelled"}
    response = mapping.get(stripped)

    if response is None:
        logger.info(f"Owner {owner_phone} sent an unrecognized reply (tenant {tenant_id})")
        # EMBRULHADO DE PROPÓSITO (Módulo S3d). Este é o único envio alcançável
        # com um tenant que não tem whatsapp_number: no caminho sandbox o dono é
        # achado pela varredura GLOBAL de get_owner_by_phone(), então o dono da
        # academia B chega aqui com o tenant dele mesmo sem número registrado.
        # Sem a guarda, a exceção sobe até receive_twilio(), o Twilio recebe 500
        # e reenvia a mesma mensagem em loop. Um "não entendi" que não sai é um
        # aborrecimento; um retry infinito do Twilio não é.
        try:
            send_message(
                owner_phone,
                "Não entendi. Responda 1 para confirmar ou 2 para cancelar.",
                tenant_id=tenant_id,
            )
        except Exception:
            logger.exception(
                "Não foi possível responder ao dono do tenant %s.", tenant_id
            )
        return

    row = owner_notifications.register_owner_response(owner_phone, response, tenant_id=tenant_id)
    if row is None:
        logger.warning(f"Owner {owner_phone} replied '{stripped}' but no open notification was found.")
        return

    if row["event_type"] == "booking" and row["booking_id"]:
        confirmations.confirm_or_cancel_booking(row["booking_id"], response, tenant_id=tenant_id)


@webhook_bp.route("/", methods=["GET"])
def initial_message():
    return redirect(url_for("dashboard.menu"))


@webhook_bp.route("/status", methods=["GET"])
def status():
    #Verificando Status
    return jsonify({"status": "online", "bot": "corujai"}), 200


dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/login", methods=["GET", "POST"])
def login():
    """Authenticate a dashboard user against the `users` table (Module S3a).

    Until S3a this compared the submitted password against DASHBOARD_PASSWORD in
    plain text and set a boolean in the session. Now it verifies an email plus a
    scrypt hash and hands the session to Flask-Login, which is what makes
    current_user.tenant_id available to every protected route.

    ONE ERROR MESSAGE FOR BOTH FAILURES. "E-mail não encontrado" and "senha
    incorreta" would tell whoever is trying which addresses are registered;
    users.authenticate() closes the timing side of the same leak.

    THE FAILED ATTEMPT IS LOGGED WITHOUT THE EMAIL — public repository, real
    people (the rule bot/messages.py follows for conversation text).

    ?next= IS IGNORED, DELIBERATELY. Flask-Login appends it when it bounces an
    anonymous request; honouring it means validating that the target is
    same-origin, and getting that wrong is an open redirect. Always landing on
    the menu is exactly the pre-S3a behaviour, and the menu is the single entry
    point to the UI anyway (GET / redirects there too).
    """
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.menu"))

    error: str | None = None

    if request.method == "POST":
        row: dict | None = accounts_users.authenticate(
            request.form.get("email", ""),
            request.form.get("password", ""),
        )

        if row is not None:
            # No remember=True: a session cookie only, same lifetime as the
            # boolean this replaced.
            login_user(User(row))
            return redirect(url_for("dashboard.menu"))

        error = "E-mail ou senha incorretos."
        logger.warning("Tentativa de login malsucedida.")

    return render_template("login.html", error=error, signup_enabled=Config.SIGNUP_ENABLED)


@dashboard_bp.route("/signup", methods=["GET", "POST"])
def signup():
    """Let a gym owner create their own account (Module S3c).

    Reverses Module S3a's closed-signup decision. The whole business rule is
    already written: this route validates a form and calls
    provision.provision_tenant(), which creates `owners`, `ai_configs`, the
    fallback `class_types` row, `scheduling_configs` and `users` in ONE
    transaction. Nothing about what a tenant needs lives here.

    BEHIND A FLAG THAT NOW DEFAULTS TO ON. A public signup does not merely risk a
    second tenant — it MANUFACTURES them, which was disqualifying while the reads
    ran unfiltered. Module S3b closed that, and the flag was flipped: it was an
    interlock, not a feature toggle, and it had nothing left to protect. A gym
    that signs up still cannot receive a message until its Twilio Sender is
    approved by hand — the last, buttonless line of /dashboard/onboarding.

    404, NOT 403, WHEN DISABLED, and before anything else runs: 403 advertises
    that there is something here to come back for.

    THE SLUG IS NOT ACCEPTED FROM THE FORM. provision_tenant() takes a
    `tenant_id` (the founder's CLI uses it), but reading it from a public form
    would let a stranger choose a primary key, and race other gyms for good
    names. It is simply never read here.

    THE "EMAIL ALREADY EXISTS" ANSWER DOES NOT CONFIRM THE EMAIL EXISTS. The
    login route is deliberately generic so it cannot be used to enumerate
    accounts; a signup form that happily says "this address is registered" hands
    back exactly that oracle.
    """
    if not Config.SIGNUP_ENABLED:
        abort(404)

    if current_user.is_authenticated:
        return redirect(url_for("dashboard.menu"))

    form: dict[str, str] = {"academy_name": "", "email": ""}

    if request.method == "GET":
        return render_template("signup.html", error=None, form=form)

    form["academy_name"] = request.form.get("academy_name", "").strip()
    form["email"] = request.form.get("email", "").strip()
    password: str = request.form.get("password", "")
    password_confirm: str = request.form.get("password_confirm", "")

    # Honeypot: a filled hidden field means a bot walked the DOM. Answer the same
    # success page a human would get, and write nothing — telling it apart is
    # what teaches the next bot to skip the field.
    if signup_guard.is_honeypot_filled(request.form.get(signup_guard.HONEYPOT_FIELD)):
        logger.warning("Signup honeypot triggered; request discarded.")
        return render_template("signup_done.html")

    client_ip: str | None = _client_ip()

    if signup_guard.too_many_attempts(client_ip):
        return render_template(
            "signup.html",
            error="Muitas tentativas de cadastro. Tente novamente daqui a pouco.",
            form=form,
        ), 429

    signup_guard.record_attempt(client_ip)

    # The only validation that belongs to the screen. Everything else already
    # lives in users.normalize_email() / users.validate_password(), which
    # provision_tenant() applies before touching the database.
    if password != password_confirm:
        return render_template("signup.html", error="As senhas não coincidem.", form=form)

    try:
        result: dict = provision.provision_tenant(
            academy_name=form["academy_name"],
            email=form["email"],
            password=password,
        )
    except (ValueError, RuntimeError) as exc:
        # provision_tenant raises with Portuguese messages for a bad email, a
        # short password and an empty name. Re-render with the reason; never 500.
        return render_template("signup.html", error=str(exc), form=form)

    if not result["created"]:
        # The email was taken. Deliberately vague — see the docstring.
        return render_template(
            "signup.html",
            error="Não foi possível criar a conta com esses dados. "
                  "Se você já tem conta, entre.",
            form=form,
        )

    row: dict | None = accounts_users.get_user_by_id(result["user_id"])
    if row is None:
        # Should be unreachable: the row was just committed.
        logger.error("Provisioned tenant but could not load the new user back.")
        return redirect(url_for("dashboard.login"))

    login_user(User(row))
    logger.info("New tenant '%s' signed up from the public form.", result["tenant_id"])
    return redirect(url_for("dashboard.onboarding"))


def _client_ip() -> str | None:
    """Return the address of the client, not of Railway's proxy.

    `request.remote_addr` behind a proxy is the PROXY, so every signup on
    Railway would share one bucket and the per-IP ceiling would lock the form
    for everybody after five attempts, worldwide. X-Forwarded-For's first entry
    is the original client.

    The header is client-controlled and therefore spoofable — which is fine for
    a throttle (the honest cost of spoofing it is that the attacker evades a
    guard that was never the last line anyway) and would NOT be fine for
    anything that granted access.

    Returns:
        str | None: The client address, or None when it cannot be determined.
    """
    forwarded: str | None = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr


@dashboard_bp.route("/logout")
def logout():
    """Clear the login session and redirect to the login page.

    Stays a GET because menu.html's "Sair" is an <a>. Logout over GET is
    CSRF-able in the log-someone-out direction only — annoying, never dangerous.
    """
    logout_user()
    return redirect(url_for("dashboard.login"))

@dashboard_bp.route("/onboarding")
@require_auth
def onboarding():
    """Show what a new gym still has to do before the bot can attend (Module S3c).

    Where signup lands. The checklist has NO state of its own — every step is
    derived from rows that already exist, so it ticks itself as the owner works
    and cannot drift from reality. See accounts/onboarding.py.

    The last step ("número de WhatsApp") is the only one the owner cannot do:
    it needs a Twilio Sender approved by the founder. Saying so on screen is the
    point of this page — otherwise they configure everything correctly and are
    left wondering why the bot is silent.
    """
    steps: list[dict] = onboarding_steps.get_steps(current_user.tenant_id)
    return render_template(
        "onboarding.html",
        steps=steps,
        pending=sum(1 for step in steps if not step["done"]),
    )


@dashboard_bp.route("/menu")
@require_auth
def menu():
    """Hub de navegação pós-login: integrações e futuras features.

    Desde o Módulo S4 o menu também abre com três números do funil da própria
    academia (decisão 19C). É a primeira coisa que o dono vê ao entrar, e existe
    para responder "o Corujai está me dando resultado?" antes de ele precisar
    clicar em qualquer lugar.
    """
    # O tile "Primeiros passos" só aparece enquanto houver pendência, para o
    # painel de uma academia já configurada não carregar um atalho morto.
    return render_template(
        "menu.html",
        onboarding_pending=onboarding_steps.pending_count(current_user.tenant_id),
        summary=metrics.get_funnel_summary(current_user.tenant_id),
    )


#
# FUNNEL METRICS (Module S4)
#
# The screen that shows the owner what the product is producing: how many leads
# arrived, how many became a booking, how many were confirmed, how many fell
# through. Read-only — see bot/metrics.py, which owns every definition.
#
# IT DOES NOT, AND MUST NOT, SHOW ATTENDANCE (problem P1). `confirmed` means the
# owner said the class will happen, not that the lead turned up; no column in
# this schema records the latter. Any label here about "comparecimento" or
# "presença" would be a number invented from one that means something else.
#


@dashboard_bp.route("/metrics")
@require_auth
def metrics_dashboard():
    """Funil da academia no período escolhido (Módulo S4).

    NÃO se chame `metrics`: o topo deste arquivo faz `import bot.metrics as
    metrics`, e uma view com esse nome sombrearia o módulo para TODAS as rotas
    daqui. Mesma armadilha que deu o nome `bookings_review` à tela de
    agendamentos. O endpoint, portanto, é `dashboard.metrics_dashboard`.

    O período vem por query string (`?period=7|30|90`), não por formulário: GET
    não mexe em CSRF, e a URL fica compartilhável. Valor inválido cai em 30 —
    `metrics.parse_period()` é total de propósito, para um erro de digitação na
    URL não derrubar a tela.
    """
    days: int = metrics.parse_period(request.args.get("period"))

    return render_template(
        "metrics.html",
        funnel=metrics.get_funnel(current_user.tenant_id, days=days),
        periods=metrics.ALLOWED_PERIODS,
    )


#
# OPERATOR INBOX (Module 5)
#
# The operator reads and answers leads from here. Replies go out through the
# gym's own Twilio number, so the lead sees one continuous thread and never
# learns whether a human or the AI answered.
#
# Not to be confused with the Module 4 owner flow: the OWNER answers
# notifications over WhatsApp (receive_twilio_owner), while the OPERATOR answers
# leads from this dashboard. Different people, different channels, no overlap.
#
# None of these routes ever log message content — the repository is public and
# these screens handle whole conversations.
#

# How often the HTMX-polled partials refresh, in seconds. Modest on purpose:
# the inbox is a handful of operators on one gym, not a public endpoint.
INBOX_POLL_SECONDS: int = 5


@dashboard_bp.route("/inbox")
@require_auth
def inbox():
    """Lista as conversas DESTA academia, pausadas e não lidas no topo."""
    return render_template(
        "inbox.html",
        conversations=messages.list_conversations(current_user.tenant_id),
        poll_seconds=INBOX_POLL_SECONDS,
    )


@dashboard_bp.route("/inbox/conversations")
@require_auth
def inbox_conversations():
    """Parcial da lista, alvo do polling HTMX.

    Separada da página para que o polling troque só as linhas, sem recarregar
    o <head>, o tema e o script a cada ciclo.
    """
    return render_template(
        "_inbox_list.html",
        conversations=messages.list_conversations(current_user.tenant_id),
    )


def _require_conversation(sender: str, tenant_id: str) -> None:
    """Aborta com 404 se o número da URL não tem sessão NESTA academia.

    As rotas do inbox recebem o `sender` pela URL. Usar get_session() aqui
    criaria uma sessão para qualquer número digitado — uma conversa fantasma que
    passaria a aparecer na lista do operador. Esta checagem é só leitura.

    Desde o Módulo S3b ela também é a porta do isolamento: um lead da academia B
    simplesmente não existe sob o tenant da academia A, então digitar o número
    dele na URL responde 404 em vez de abrir a conversa alheia.
    """
    if not session_store.session_exists(sender, tenant_id=tenant_id):
        abort(404)


@dashboard_bp.route("/inbox/<sender>")
@require_auth
def inbox_conversation(sender: str):
    """Abre uma conversa e marca as mensagens do lead como lidas."""
    tenant_id: str = current_user.tenant_id
    _require_conversation(sender, tenant_id)
    state = session_store.get_session(sender, tenant_id=tenant_id)
    messages.mark_conversation_read(sender, tenant_id=tenant_id)

    return render_template(
        "conversation.html",
        sender=sender,
        state=state,
        conversation=messages.get_conversation(sender, tenant_id=tenant_id),
        poll_seconds=INBOX_POLL_SECONDS,
    )


@dashboard_bp.route("/inbox/<sender>/messages")
@require_auth
def inbox_conversation_messages(sender: str):
    """Parcial das mensagens, alvo do polling HTMX.

    Também marca como lidas: se o operador está com a conversa aberta na tela,
    ele está lendo o que chega.
    """
    tenant_id: str = current_user.tenant_id
    _require_conversation(sender, tenant_id)
    messages.mark_conversation_read(sender, tenant_id=tenant_id)
    return render_template(
        "_conversation_messages.html",
        conversation=messages.get_conversation(sender, tenant_id=tenant_id),
    )


@dashboard_bp.route("/inbox/<sender>/reply", methods=["POST"])
@require_auth
def inbox_reply(sender: str):
    """Envia a resposta do operador ao lead e registra a mensagem.

    ORDEM DELIBERADA — envia primeiro, grava só no sucesso. É o inverso da rota
    da IA (grava-depois-envia), porque quem está no controle aqui é uma pessoa
    olhando a tela: se o envio falhar ela reenvia na hora, e o registro precisa
    refletir o que o lead de fato recebeu. Gravar antes deixaria uma mensagem
    fantasma no histórico — e o próximo turno da IA leria como dito algo que
    nunca chegou ao lead.

    send_message re-lança em falha; a exceção é capturada aqui e devolvida como
    um aviso dentro do HTML, com status 200: o HTMX não faz swap em 4xx/5xx, e
    um 500 nu deixaria o operador olhando uma tela muda sem saber se enviou.
    """
    tenant_id: str = current_user.tenant_id
    _require_conversation(sender, tenant_id)
    text: str = request.form.get("text", "").strip()

    if not text:
        return _conversation_messages_response(
            sender, tenant_id, error="Digite uma mensagem antes de enviar."
        )

    try:
        send_message(sender, text, tenant_id=tenant_id)
    except SenderNotConfiguredError:
        # Capturado ANTES do genérico (Módulo S3d): "verifique a conexão" mandaria
        # o operador tentar de novo para sempre, porque não há nada de errado com
        # a conexão — falta o número da academia, e reenviar não resolve.
        logger.warning("Tenant %s tentou responder sem whatsapp_number.", tenant_id)
        return _conversation_messages_response(
            sender,
            tenant_id,
            error=(
                "Sua academia ainda não tem um número de WhatsApp configurado, "
                "então não é possível enviar mensagens. Cadastre-o em "
                "Configurações → Conta."
            ),
        )
    except Exception:
        logger.exception(f"Falha ao enviar resposta do operador para {sender}")
        return _conversation_messages_response(
            sender,
            tenant_id,
            error="Não foi possível enviar a mensagem. Verifique a conexão e tente de novo.",
        )

    messages.add_message(sender, "operator", text, is_read=True, tenant_id=tenant_id)
    return _conversation_messages_response(sender, tenant_id)


@dashboard_bp.route("/inbox/<sender>/resume", methods=["POST"])
@require_auth
def inbox_resume(sender: str):
    """Devolve a conversa à IA, encerrando o takeover.

    Até o Módulo 5 nada limpava is_paused: um handoff pausava o lead para
    sempre. Esta rota é a única saída da pausa.

    Reseta o stage para 'interest' — um estágio que já existe em
    session.valid_stages e que a camada protegida do prompt descreve, então a IA
    não recebe um valor que nunca viu — e arma needs_resume_note para que o
    próximo prompt avise que um humano acabou de devolver a conversa.
    """
    tenant_id: str = current_user.tenant_id
    _require_conversation(sender, tenant_id)
    state = session_store.get_session(sender, tenant_id=tenant_id)
    state["is_paused"] = False
    state["stage"] = "interest"
    state["needs_resume_note"] = True
    session_store.save_session(sender, state, tenant_id=tenant_id)

    logger.info(f"Conversa de {sender} devolvida à IA pelo operador.")
    return redirect(url_for("dashboard.inbox_conversation", sender=sender))


def _conversation_messages_response(sender: str, tenant_id: str, error: str | None = None):
    """Renderiza a parcial de mensagens, com um aviso opcional ao operador.

    No sucesso devolve o header `HX-Trigger: reply-sent`, que é o que limpa o
    campo de texto no front. O reset é preso ao sucesso de propósito: como a
    falha de envio também volta 200 (ver inbox_reply), limpar sempre apagaria o
    texto que o operador precisa justamente para reenviar.

    Args:
        sender (str): Número do lead.
        tenant_id (str): Academia do operador logado. Recebido por parâmetro, e
            não lido de current_user aqui dentro, para que a camada de dados
            nunca dependa do contexto de request (decisão 14A).
        error (str | None): Mensagem de erro a exibir, em português.

    Returns:
        tuple: (HTML, 200, headers). Sempre 200 — ver a docstring de inbox_reply.
    """
    html = render_template(
        "_conversation_messages.html",
        conversation=messages.get_conversation(sender, tenant_id=tenant_id),
        error=error,
    )
    headers = {} if error else {"HX-Trigger": "reply-sent"}
    return html, 200, headers


#
# BOOKINGS REVIEW (Module 6)
#
# Where the OWNER decides whether a trial class actually happens. It is the
# second of the two channels for that decision — the first is replying 1/2 to
# the WhatsApp notification (receive_twilio_owner, above) — and both end up in
# bot/confirmations.py::confirm_or_cancel_booking(). The rule of the module is
# that no closing logic lives in these routes: they translate a click into a
# call and a result into Portuguese, nothing more.
#
# Unlike the inbox, these screens don't poll. A booking arrives when the AI
# closes one, not every few seconds, and the owner is here to act rather than to
# watch. HTMX is used only so a Confirmar/Cancelar click swaps the list in place
# instead of reloading the page.
#


def _bookings_context(tenant_id: str, notice: dict | None = None) -> dict:
    """Monta o contexto da lista de agendamentos, sempre com os rótulos de aula.

    Os três pontos que renderizam a lista precisam do mesmo par (linhas +
    rótulos): esquecer `class_labels` em um deles imprimiria "CRIANCAS" cru na
    tela do dono. Desde o Módulo S3b os dois lados vêm do MESMO tenant — linhas
    de uma academia com rótulos de outra imprimiriam o marcador cru pelo mesmo
    motivo, só que sem nada na tela denunciando a troca.

    Args:
        tenant_id (str): Academia do dono logado.
        notice (dict | None): Aviso a exibir no topo, como
            {"kind": "success|warning|error", "text": str}.

    Returns:
        dict: kwargs para render_template.
    """
    return {
        "bookings": bookings.list_bookings_for_review(tenant_id),
        "class_labels": class_types.load_class_types(tenant_id)["labels"],
        "notice": notice,
    }


@dashboard_bp.route("/bookings")
@require_auth
def bookings_review():
    """Lista os agendamentos DESTA academia, pendentes de confirmação no topo."""
    return render_template("bookings.html", **_bookings_context(current_user.tenant_id))


@dashboard_bp.route("/bookings/list")
@require_auth
def bookings_list():
    """Parcial da lista, para recarregar só ela.

    Separada da página pelo mesmo motivo do inbox: o swap troca as linhas sem
    recarregar o <head>, o tema e o script.
    """
    return render_template("_bookings_list.html", **_bookings_context(current_user.tenant_id))


@dashboard_bp.route("/bookings/<booking_id>/confirm", methods=["POST"])
@require_auth
def bookings_confirm(booking_id: str):
    """Confirma um agendamento pelo painel."""
    return _booking_decision_response(booking_id, "confirmed", current_user.tenant_id)


@dashboard_bp.route("/bookings/<booking_id>/cancel", methods=["POST"])
@require_auth
def bookings_cancel(booking_id: str):
    """Cancela um agendamento pelo painel."""
    return _booking_decision_response(booking_id, "cancelled", current_user.tenant_id)


def _booking_decision_response(booking_id: str, decision: str, tenant_id: str):
    """Aplica a decisão do dono pela coordenadora e redesenha a lista.

    A regra de fechamento não mora aqui: esta função chama
    confirmations.confirm_or_cancel_booking() e traduz o resultado. O guard de
    transição vem junto — clicar duas vezes cai em "skipped" sem que a rota
    precise checar nada.

    O `booking_id` vem da URL, então o tenant viaja junto como GUARDA: um id de
    outra academia responde "não encontrado", exatamente como um id inexistente
    — e é a mesma tela que a rota já sabia desenhar.

    Agir pelo painel também carimba o owner_response da notificação daquele
    agendamento, quando existe uma em aberto. Sem isso, uma reserva resolvida na
    tela ficaria para sempre "sem resposta" na fila, e as duas fontes contariam
    histórias diferentes sobre o que o dono decidiu.

    Sempre responde 200, como as rotas do inbox: o HTMX não faz swap em 4xx/5xx,
    e um erro nu deixaria o dono olhando uma tela que não mudou sem saber por quê.

    Args:
        booking_id (str): Id do agendamento vindo da URL.
        decision (str): "confirmed" ou "cancelled".
        tenant_id (str): Academia do dono logado.

    Returns:
        tuple: (HTML da parcial, 200).
    """
    result = confirmations.confirm_or_cancel_booking(booking_id, decision, tenant_id=tenant_id)

    if result["result"] == "not_found":
        notice = {"kind": "error", "text": "Agendamento não encontrado. A lista foi atualizada."}
    elif result["result"] == "skipped":
        already = "confirmado" if result["status"] == "confirmed" else "cancelado"
        notice = {"kind": "warning", "text": f"Este agendamento já estava {already}. Nada foi alterado."}
    else:
        owner_notifications.register_response_for_booking(booking_id, decision, tenant_id=tenant_id)
        done = "confirmado" if decision == "confirmed" else "cancelado"
        notice = {"kind": "success", "text": f"Agendamento {done}."}
        if not result["lead_notified"]:
            notice["kind"] = "warning"
            notice["text"] += " Não consegui avisar o lead pelo WhatsApp — avise por outro canal."

    return render_template("_bookings_list.html", **_bookings_context(tenant_id, notice)), 200


#
# SETTINGS (Module S1)
#
# The first write screen for configuration in the project. Until it existed, the
# AI's personality and the owner's phone number were only reachable by hand-run
# SQL — migration 003 says so in its own header.
#
# ONE PAGE, SEVERAL SECTIONS, ONE POST EACH. "IA" edits ai_configs; "Conta"
# edits owners.owner_phone and shows (read-only) the Google Calendar status;
# "Aulas" (Module S2) edits class_types and scheduling_configs. They are
# separate POSTs because they have nothing to do with each other: an owner
# fixing a typo in their phone number must not rewrite the AI's tone as a side
# effect of submitting one big form. The Aulas section takes that further —
# each class type is its own form, and making one the default is its own button,
# so no single click can both edit a class and change which one catches
# unmarked events.
#
# No HTMX here, unlike /inbox and /bookings: there is no list that changes on
# its own, so a plain form post that re-renders the page is the whole
# interaction.
#

# The five editable columns of ai_configs, in the order the form shows them.
# Listed once so the route, the validation and the re-render can't disagree.
_AI_CONFIG_FIELDS: tuple[str, ...] = (
    "academy_name",
    "assistant_name",
    "tone",
    "business_info",
    "flow_emphasis",
)


def _settings_context(
    tenant_id: str,
    notice: dict | None = None,
    ai_form: dict[str, str] | None = None,
) -> dict:
    """Monta o contexto das seções da tela de configurações, para uma academia.

    Args:
        tenant_id (str): Academia do dono logado. Antes do Módulo S3b esta tela
            lia e gravava sempre no tenant piloto: o dono da academia B abriria
            a personalidade da IA do piloto, e salvá-la sobrescreveria a dele.
        notice (dict | None): Aviso a exibir no topo, como
            {"kind": "success|warning|error", "text": str}.
        ai_form (dict[str, str] | None): Valores a mostrar nos campos da IA em vez
            dos que estão no banco. Serve para um POST recusado devolver o que o
            dono digitou — recarregar do banco apagaria a edição dele.

    Returns:
        dict: kwargs para render_template.
    """
    owner: dict | None = store.get_owner_credentials(tenant_id)
    notification_owner: dict | None = store.get_owner_for_notification(tenant_id)

    return {
        "ai_config": ai_form if ai_form is not None else ai_configs.get_ai_config(tenant_id),
        "owner_phone": notification_owner["owner_phone"] if notification_owner else None,
        "integration_status": owner["integration_status"] if owner else "disconnected",
        "google_email": owner["google_email"] if owner else None,
        "class_types": class_types.list_class_types(tenant_id),
        "days_ahead": class_types.get_scheduling_config(tenant_id)["days_ahead"],
        "min_days_ahead": class_types.MIN_DAYS_AHEAD,
        "max_days_ahead": class_types.MAX_DAYS_AHEAD,
        "notice": notice,
    }


@dashboard_bp.route("/settings")
@require_auth
def settings():
    """Tela de configurações: personalidade da IA e dados da conta."""
    return render_template("settings.html", **_settings_context(current_user.tenant_id))


@dashboard_bp.route("/settings/ai", methods=["POST"])
@require_auth
def settings_save_ai():
    """Salva a camada customizável do prompt (ai_configs).

    Sobrescreve, sem histórico: a última gravação do dono vale. Não há cache
    nenhum para invalidar — bot/ai_configs.py lê do banco a cada mensagem —, então
    a próxima resposta ao lead já sai com o texto novo.

    Os cinco campos são obrigatórios porque todos são interpolados no prompt: um
    vazio deixaria a IA sem nome, sem tom ou sem os dados do negócio.
    """
    tenant_id: str = current_user.tenant_id
    submitted: dict[str, str] = {
        field: request.form.get(field, "").strip() for field in _AI_CONFIG_FIELDS
    }

    if not all(submitted.values()):
        notice = {"kind": "error", "text": "Preencha todos os campos da IA antes de salvar."}
        return render_template(
            "settings.html", **_settings_context(tenant_id, notice, ai_form=submitted)
        ), 200

    if not ai_configs.update_ai_config(**submitted, tenant_id=tenant_id):
        notice = {"kind": "error", "text": "Não encontrei a configuração desta academia para salvar."}
        return render_template(
            "settings.html", **_settings_context(tenant_id, notice, ai_form=submitted)
        ), 200

    notice = {"kind": "success", "text": "Configuração da IA salva. Vale a partir da próxima mensagem."}
    return render_template("settings.html", **_settings_context(tenant_id, notice)), 200


@dashboard_bp.route("/settings/account", methods=["POST"])
@require_auth
def settings_save_account():
    """Salva o telefone do dono, com as duas guardas que protegem o roteamento.

    owner_phone NÃO é um campo de cadastro qualquer: receive_twilio() chama
    store.get_owner_by_phone() em toda mensagem que entra para decidir se quem
    escreveu é o dono ou um lead. Gravar errado não mostra erro em lugar nenhum —
    ou o dono deixa de ser reconhecido (o "1"/"2" dele para de fechar
    agendamento), ou, pior, o número de outra pessoa passa a ser lido como o do
    dono e as mensagens dela viram comandos de confirmação.

    Por isso duas guardas antes de gravar:
      (a) normalizar para o mesmo formato que o webhook compara (só dígitos);
      (b) recusar um número que já é o de um lead com conversa em `sessions`.

    A guarda (b) consulta `sessions`, e não `owners`, de propósito: o perigo não é
    haver dois donos com o mesmo número, é um número ser lead e dono ao mesmo
    tempo — aí o roteamento tem duas respostas certas e escolhe a do dono,
    sequestrando a conversa do lead. Desde o Módulo S3a `owner_phone` também tem
    UNIQUE no banco, que cobre a unicidade entre donos.

    A guarda (b) é checada DENTRO desta academia: o mesmo número pode ser lead na
    academia B e dono na A sem ambiguidade nenhuma, porque `receive_twilio()`
    resolve o tenant pelo "To" antes de perguntar quem escreveu.
    """
    tenant_id: str = current_user.tenant_id
    normalized: str | None = store.normalize_owner_phone(request.form.get("owner_phone", ""))

    if normalized is None:
        notice = {
            "kind": "error",
            "text": "Número inválido. Use o formato com DDI e DDD, por exemplo 5521999999999.",
        }
        return render_template("settings.html", **_settings_context(tenant_id, notice)), 200

    if session_store.session_exists(normalized, tenant_id=tenant_id):
        notice = {
            "kind": "error",
            "text": (
                "Esse número já está em uso por uma conversa de lead. "
                "Salvá-lo como número do dono faria as mensagens dessa pessoa "
                "virarem confirmações de agendamento. Nada foi alterado."
            ),
        }
        return render_template("settings.html", **_settings_context(tenant_id, notice)), 200

    if not store.update_owner_phone(normalized, tenant_id=tenant_id):
        notice = {"kind": "error", "text": "Não encontrei o cadastro desta academia para salvar."}
        return render_template("settings.html", **_settings_context(tenant_id, notice)), 200

    notice = {"kind": "success", "text": "Número do dono salvo."}
    return render_template("settings.html", **_settings_context(tenant_id, notice)), 200


# --- Seção "Aulas" (Module S2) ---------------------------------------------
#
# Onde o dono cadastra os tipos de aula que antes eram dois dicts no código.
# As rotas abaixo só traduzem formulário em chamada e resultado em português —
# as regras (marcador canônico, capacidade, não excluir a turma padrão) moram em
# bot/class_types.py, para valerem também para quem escrever por SQL.


def _settings_error(text: str, tenant_id: str) -> tuple[str, int]:
    """Re-renderiza a tela com um aviso de erro, sempre 200.

    200 e não 4xx pelo mesmo motivo do inbox: o dono precisa ver a página com o
    aviso, não uma tela de erro do navegador.

    Args:
        text (str): O aviso, em português.
        tenant_id (str): Academia do dono logado.

    Returns:
        tuple[str, int]: (HTML, 200).
    """
    return render_template(
        "settings.html", **_settings_context(tenant_id, {"kind": "error", "text": text})
    ), 200


def _parse_capacity(raw: str) -> tuple[bool, int | None]:
    """Interpreta o campo de capacidade do formulário.

    Vazio significa ILIMITADO e vira None — a mesma semântica que a coluna
    `capacity` NULL carrega no banco e que `get_available_slots()` testa com
    `if capacity is not None`. Zero ou negativo não é "ilimitado", é uma turma
    que nunca aceita ninguém, então é recusado aqui e pelo CHECK da tabela.

    Args:
        raw (str): O que veio do formulário.

    Returns:
        tuple[bool, int | None]: (válido, capacidade). Capacidade None com
        válido=True significa ilimitado.
    """
    stripped: str = (raw or "").strip()

    if not stripped:
        return True, None

    try:
        value: int = int(stripped)
    except ValueError:
        return False, None

    if value < 1:
        return False, None

    return True, value


@dashboard_bp.route("/settings/class-types", methods=["POST"])
@require_auth
def settings_create_class_type():
    """Cadastra um tipo de aula novo.

    O marcador passa por class_types.normalize_marker(), que é o mesmo
    normalizador que o motor usa ao ler o título do evento no Calendar. É isso
    que garante que "Crianças" digitado aqui e "[ CRIANÇAS ]" digitado na agenda
    sejam o mesmo tipo — e que um marcador com número ou espaço, que o regex do
    título nunca casaria, seja recusado em vez de virar um tipo invisível.
    """
    tenant_id: str = current_user.tenant_id
    marker: str | None = class_types.normalize_marker(request.form.get("marker", ""))
    label: str = request.form.get("label", "").strip()
    valid_capacity, capacity = _parse_capacity(request.form.get("capacity", ""))
    requires_child_name: bool = request.form.get("requires_child_name") == "on"

    if marker is None:
        return _settings_error(
            "Marcador inválido. Use só letras, sem números, espaços ou símbolos — "
            "é ele que vai entre colchetes no título do evento, como [CRIANCAS].",
            tenant_id,
        )

    if not label:
        return _settings_error(
            "Informe o nome da turma como o aluno deve ler, por exemplo Crianças.", tenant_id
        )

    if not valid_capacity:
        return _settings_error(
            "Capacidade inválida. Use um número inteiro a partir de 1, ou deixe vazio para ilimitado.",
            tenant_id,
        )

    created = class_types.create_class_type(
        marker, label, capacity, requires_child_name, tenant_id=tenant_id
    )
    if created == "duplicate":
        return _settings_error(f"Já existe uma turma com o marcador [{marker}].", tenant_id)

    notice: dict = {"kind": "success", "text": f"Turma [{marker}] cadastrada."}
    return render_template("settings.html", **_settings_context(tenant_id, notice)), 200


@dashboard_bp.route("/settings/class-types/<marker>", methods=["POST"])
@require_auth
def settings_save_class_type(marker: str):
    """Salva nome, capacidade e exigência de nome da criança de uma turma.

    O marcador não é editável — ele é a chave e também está escrito à mão no
    título de cada evento do Calendar. Renomeá-lo aqui deixaria todos os eventos
    existentes órfãos, caindo na turma padrão sem ninguém perceber.
    """
    tenant_id: str = current_user.tenant_id
    label: str = request.form.get("label", "").strip()
    valid_capacity, capacity = _parse_capacity(request.form.get("capacity", ""))
    requires_child_name: bool = request.form.get("requires_child_name") == "on"

    if not label:
        return _settings_error(
            "Informe o nome da turma como o aluno deve ler, por exemplo Crianças.", tenant_id
        )

    if not valid_capacity:
        return _settings_error(
            "Capacidade inválida. Use um número inteiro a partir de 1, ou deixe vazio para ilimitado.",
            tenant_id,
        )

    if not class_types.update_class_type(
        marker, label, capacity, requires_child_name, tenant_id=tenant_id
    ):
        return _settings_error(f"Não encontrei a turma [{marker}] para salvar.", tenant_id)

    notice: dict = {"kind": "success", "text": f"Turma [{marker}] salva."}
    return render_template("settings.html", **_settings_context(tenant_id, notice)), 200


@dashboard_bp.route("/settings/class-types/<marker>/fallback", methods=["POST"])
@require_auth
def settings_set_fallback_class_type(marker: str):
    """Define qual turma recebe os eventos sem marcador reconhecido no título."""
    tenant_id: str = current_user.tenant_id
    if not class_types.set_fallback_class_type(marker, tenant_id=tenant_id):
        return _settings_error(f"Não encontrei a turma [{marker}].", tenant_id)

    notice: dict = {
        "kind": "success",
        "text": (
            f"[{marker}] agora é a turma padrão: eventos sem marcador no título "
            "passam a ser tratados como dessa turma."
        ),
    }
    return render_template("settings.html", **_settings_context(tenant_id, notice)), 200


@dashboard_bp.route("/settings/class-types/<marker>/delete", methods=["POST"])
@require_auth
def settings_delete_class_type(marker: str):
    """Exclui uma turma, exceto a padrão.

    A recusa vem de bot/class_types.py, não daqui: sem a turma padrão o sistema
    passaria a inventar uma em memória, e o dono não teria como descobrir pela
    tela por que os eventos sem marcador mudaram de comportamento.
    """
    tenant_id: str = current_user.tenant_id
    result: str = class_types.delete_class_type(marker, tenant_id=tenant_id)

    if result == "not_found":
        return _settings_error(f"Não encontrei a turma [{marker}].", tenant_id)

    if result == "is_fallback":
        return _settings_error(
            f"[{marker}] é a turma padrão e não pode ser excluída. "
            "É ela que recebe os eventos cujo título está sem marcador. "
            "Marque outra como padrão antes de excluir esta.",
            tenant_id,
        )

    notice: dict = {"kind": "success", "text": f"Turma [{marker}] excluída."}
    return render_template("settings.html", **_settings_context(tenant_id, notice)), 200


@dashboard_bp.route("/settings/class-events", methods=["POST"])
@require_auth
def settings_create_class_event():
    """Cria uma aula na agenda a partir de turma + data + horário.

    O dono escolhe a turma numa lista, não digita o marcador: o título do evento
    é montado pelo código como "[MARCADOR] Aula Experimental". É o que garante
    que a IA leia o evento de volta como a turma certa — um marcador digitado à
    mão com erro cairia na turma padrão em silêncio.

    A agenda continua sendo a fonte de verdade dos horários: isto grava o evento
    lá e não guarda nada do nosso lado. Não é uma grade (decisão 7A) — é o mesmo
    que o dono digitar no Google Agenda, só que sem sair do painel.
    """
    tenant_id: str = current_user.tenant_id
    marker: str | None = class_types.normalize_marker(request.form.get("marker", ""))
    if marker is None:
        return _settings_error("Escolha a turma da aula.", tenant_id)

    start, end, error = _parse_class_event_window(
        request.form.get("date", ""),
        request.form.get("start_time", ""),
        request.form.get("end_time", ""),
    )
    if error is not None:
        return _settings_error(error, tenant_id)

    result: dict = scheduling.create_class_event(marker, start, end, tenant_id=tenant_id)

    if result["status"] == "unknown_class_type":
        return _settings_error(f"A turma [{marker}] não está cadastrada.", tenant_id)

    if result["status"] == "integration_not_connected":
        return _settings_error(
            "O Google Agenda não está conectado, então não dá para criar a aula. "
            "Conecte em Configurações → Conta.",
            tenant_id,
        )

    if result["status"] == "needs_reconnect":
        return _settings_error(
            "O Google Agenda precisa ser reconectado antes de criar aulas. "
            "Reconecte em Configurações → Conta.",
            tenant_id,
        )

    notice: dict = {
        "kind": "success",
        "text": f"Aula criada na agenda: {result['label']}. A IA já pode oferecer esse horário.",
    }
    return render_template("settings.html", **_settings_context(tenant_id, notice)), 200


def _parse_class_event_window(
    raw_date: str, raw_start: str, raw_end: str,
) -> tuple[datetime | None, datetime | None, str | None]:
    """Valida data + horário de uma aula e devolve o intervalo com fuso.

    Os campos vêm de `<input type="date">` e `<input type="time">`, que o
    navegador já entrega em ISO — mas o navegador não é a validação: um POST
    direto manda o que quiser, e a data vira uma chamada à API do Google.

    O fuso é aplicado aqui (America/Sao_Paulo), nunca deixado ingênuo: o
    servidor roda em UTC no Railway, então um datetime sem fuso viraria uma aula
    três horas fora do lugar.

    Args:
        raw_date (str): "2026-08-09".
        raw_start (str): "18:00".
        raw_end (str): "19:00".

    Returns:
        tuple[datetime | None, datetime | None, str | None]: (início, fim, erro).
        O erro, quando existe, já está em português e pronto para a tela.
    """
    try:
        day: date = date.fromisoformat(raw_date.strip())
    except ValueError:
        return None, None, "Informe a data da aula."

    try:
        start_time: time = time.fromisoformat(raw_start.strip())
        end_time: time = time.fromisoformat(raw_end.strip())
    except ValueError:
        return None, None, "Informe o horário de início e de fim da aula."

    start: datetime = datetime.combine(day, start_time, tzinfo=scheduling.TIMEZONE)
    end: datetime = datetime.combine(day, end_time, tzinfo=scheduling.TIMEZONE)

    if end <= start:
        return None, None, "O horário de fim precisa ser depois do de início."

    if start <= datetime.now(scheduling.TIMEZONE):
        return None, None, "Essa data e hora já passaram. Escolha um horário futuro."

    return start, end, None


@dashboard_bp.route("/settings/scheduling", methods=["POST"])
@require_auth
def settings_save_scheduling():
    """Salva a janela de busca de horários (days_ahead)."""
    tenant_id: str = current_user.tenant_id
    raw: str = request.form.get("days_ahead", "").strip()

    try:
        days_ahead: int = int(raw)
    except ValueError:
        return _settings_error("Informe um número inteiro de dias.", tenant_id)

    if not class_types.MIN_DAYS_AHEAD <= days_ahead <= class_types.MAX_DAYS_AHEAD:
        return _settings_error(
            f"Use um valor entre {class_types.MIN_DAYS_AHEAD} e "
            f"{class_types.MAX_DAYS_AHEAD} dias.",
            tenant_id,
        )

    if not class_types.update_days_ahead(days_ahead, tenant_id=tenant_id):
        return _settings_error(
            "Não encontrei a configuração de agendamento desta academia para salvar.", tenant_id
        )

    notice: dict = {"kind": "success", "text": f"A IA passa a oferecer horários dos próximos {days_ahead} dias."}
    return render_template("settings.html", **_settings_context(tenant_id, notice)), 200
