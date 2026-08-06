from flask import Blueprint, abort, jsonify, request, render_template, redirect, url_for, session
from functools import wraps
from config import Config
import logging
import threading
from whatsapp.whatsapp_service import send_message
from bot.handlers import handle_text_message
import bot.ai_configs as ai_configs
import bot.bookings as bookings
import bot.confirmations as confirmations
import bot.messages as messages
import bot.owner_notifications as owner_notifications
import bot.session as session_store
import integrations.store as store
from bot.scheduling import CLASS_TYPE_LABELS


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
    to: str     = request.form.get("To")    # seu número do sandbox

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

    # A message from the gym owner's own number is a reply to a notification,
    # not a lead starting/continuing a conversation — route it separately.
    owner = store.get_owner_by_phone(clean_number)
    if owner is not None:
        receive_twilio_owner(clean_number, body)
    else:
        handle_text_message(clean_number, body)

    # O Twilio espera status 200 para confirmar que você recebeu
    # Se não receber 200, ele tenta reenviar
    return jsonify({"status": "ok"}), 200


def receive_twilio_owner(owner_phone: str, body: str) -> None:
    """Handle a WhatsApp reply from the gym owner (never a lead).

    Maps "1"/"2" to confirmed/cancelled, records the response, and — for a
    'booking' notification — actually closes the booking out through
    bot/confirmations.py. That last part is what Module 6 added; until then the
    reply was only ever recorded on owner_notifications.

    A 'handoff' notification carries no booking_id (there is no class to
    decide), so it is recorded and nothing else happens. A reply with no open
    notification returns None and there is nothing left to answer — which is
    what makes a second "1" harmless.

    Args:
        owner_phone (str): The owner's number, already in clean_number form.
        body (str): The raw text the owner sent.
    """
    stripped = body.strip()
    mapping = {"1": "confirmed", "2": "cancelled"}
    response = mapping.get(stripped)

    if response is None:
        logger.info(f"Owner {owner_phone} sent an unrecognized reply")
        send_message(owner_phone, "Não entendi. Responda 1 para confirmar ou 2 para cancelar.")
        return

    row = owner_notifications.register_owner_response(owner_phone, response)
    if row is None:
        logger.warning(f"Owner {owner_phone} replied '{stripped}' but no open notification was found.")
        return

    if row["event_type"] == "booking" and row["booking_id"]:
        confirmations.confirm_or_cancel_booking(row["booking_id"], response)


@webhook_bp.route("/", methods=["GET"])
def initial_message():
    return redirect(url_for("dashboard.menu"))


@webhook_bp.route("/status", methods=["GET"])
def status():
    #Verificando Status
    return jsonify({"status": "online", "bot": "corujai"}), 200


dashboard_bp = Blueprint("dashboard", __name__)

def _require_auth(f):
    """
    Decorator that redirects unauthenticaded requests to the login page
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("dashboard_authenticated"):
            return redirect(url_for("dashboard.login"))
        return f(*args, **kwargs)
    return decorated

@dashboard_bp.route("/login", methods=["GET", "POST"])
def login():
    """Handle dashboard login via password form."""
    error: str | None = None

    if request.method == "POST":
        password: str = request.form.get("password", "")
        expected: str = Config.DASHBOARD_PASSWORD

        if password == expected:
            session["dashboard_authenticated"] = True
            return redirect(url_for("dashboard.menu"))
        else:
            error = "Senha incorreta. Tente novamente."
            logger.warning("Tentativa de login com senha incorreta.")
    
    return render_template("login.html", error=error)

@dashboard_bp.route("/logout")
def logout():
    """ Clear the current session and redirects to the login page."""
    session.pop("dashboard_authenticated", None)
    return redirect(url_for("dashboard.login"))

@dashboard_bp.route("/menu")
@_require_auth
def menu():
    """Hub de navegação pós-login: integrações e futuras features."""
    return render_template("menu.html")


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
@_require_auth
def inbox():
    """Lista todas as conversas, pausadas e não lidas no topo."""
    return render_template(
        "inbox.html",
        conversations=messages.list_conversations(),
        poll_seconds=INBOX_POLL_SECONDS,
    )


@dashboard_bp.route("/inbox/conversations")
@_require_auth
def inbox_conversations():
    """Parcial da lista, alvo do polling HTMX.

    Separada da página para que o polling troque só as linhas, sem recarregar
    o <head>, o tema e o script a cada ciclo.
    """
    return render_template("_inbox_list.html", conversations=messages.list_conversations())


def _require_conversation(sender: str) -> None:
    """Aborta com 404 se o número da URL não tem sessão.

    As rotas do inbox recebem o `sender` pela URL. Usar get_session() aqui
    criaria uma sessão para qualquer número digitado — uma conversa fantasma que
    passaria a aparecer na lista do operador. Esta checagem é só leitura.
    """
    if not session_store.session_exists(sender):
        abort(404)


@dashboard_bp.route("/inbox/<sender>")
@_require_auth
def inbox_conversation(sender: str):
    """Abre uma conversa e marca as mensagens do lead como lidas."""
    _require_conversation(sender)
    state = session_store.get_session(sender)
    messages.mark_conversation_read(sender)

    return render_template(
        "conversation.html",
        sender=sender,
        state=state,
        conversation=messages.get_conversation(sender),
        poll_seconds=INBOX_POLL_SECONDS,
    )


@dashboard_bp.route("/inbox/<sender>/messages")
@_require_auth
def inbox_conversation_messages(sender: str):
    """Parcial das mensagens, alvo do polling HTMX.

    Também marca como lidas: se o operador está com a conversa aberta na tela,
    ele está lendo o que chega.
    """
    _require_conversation(sender)
    messages.mark_conversation_read(sender)
    return render_template("_conversation_messages.html", conversation=messages.get_conversation(sender))


@dashboard_bp.route("/inbox/<sender>/reply", methods=["POST"])
@_require_auth
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
    _require_conversation(sender)
    text: str = request.form.get("text", "").strip()

    if not text:
        return _conversation_messages_response(sender, error="Digite uma mensagem antes de enviar.")

    try:
        send_message(sender, text)
    except Exception:
        logger.exception(f"Falha ao enviar resposta do operador para {sender}")
        return _conversation_messages_response(
            sender,
            error="Não foi possível enviar a mensagem. Verifique a conexão e tente de novo.",
        )

    messages.add_message(sender, "operator", text, is_read=True)
    return _conversation_messages_response(sender)


@dashboard_bp.route("/inbox/<sender>/resume", methods=["POST"])
@_require_auth
def inbox_resume(sender: str):
    """Devolve a conversa à IA, encerrando o takeover.

    Até o Módulo 5 nada limpava is_paused: um handoff pausava o lead para
    sempre. Esta rota é a única saída da pausa.

    Reseta o stage para 'interest' — um estágio que já existe em
    session.valid_stages e que a camada protegida do prompt descreve, então a IA
    não recebe um valor que nunca viu — e arma needs_resume_note para que o
    próximo prompt avise que um humano acabou de devolver a conversa.
    """
    _require_conversation(sender)
    state = session_store.get_session(sender)
    state["is_paused"] = False
    state["stage"] = "interest"
    state["needs_resume_note"] = True
    session_store.save_session(sender, state)

    logger.info(f"Conversa de {sender} devolvida à IA pelo operador.")
    return redirect(url_for("dashboard.inbox_conversation", sender=sender))


def _conversation_messages_response(sender: str, error: str | None = None):
    """Renderiza a parcial de mensagens, com um aviso opcional ao operador.

    No sucesso devolve o header `HX-Trigger: reply-sent`, que é o que limpa o
    campo de texto no front. O reset é preso ao sucesso de propósito: como a
    falha de envio também volta 200 (ver inbox_reply), limpar sempre apagaria o
    texto que o operador precisa justamente para reenviar.

    Args:
        sender (str): Número do lead.
        error (str | None): Mensagem de erro a exibir, em português.

    Returns:
        tuple: (HTML, 200, headers). Sempre 200 — ver a docstring de inbox_reply.
    """
    html = render_template(
        "_conversation_messages.html",
        conversation=messages.get_conversation(sender),
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


def _bookings_context(notice: dict | None = None) -> dict:
    """Monta o contexto da lista de agendamentos, sempre com os rótulos de aula.

    Os três pontos que renderizam a lista precisam do mesmo par (linhas +
    rótulos): esquecer `class_labels` em um deles imprimiria "CRIANCAS" cru na
    tela do dono.

    Args:
        notice (dict | None): Aviso a exibir no topo, como
            {"kind": "success|warning|error", "text": str}.

    Returns:
        dict: kwargs para render_template.
    """
    return {
        "bookings": bookings.list_bookings_for_review(),
        "class_labels": CLASS_TYPE_LABELS,
        "notice": notice,
    }


@dashboard_bp.route("/bookings")
@_require_auth
def bookings_review():
    """Lista os agendamentos, pendentes de confirmação no topo."""
    return render_template("bookings.html", **_bookings_context())


@dashboard_bp.route("/bookings/list")
@_require_auth
def bookings_list():
    """Parcial da lista, para recarregar só ela.

    Separada da página pelo mesmo motivo do inbox: o swap troca as linhas sem
    recarregar o <head>, o tema e o script.
    """
    return render_template("_bookings_list.html", **_bookings_context())


@dashboard_bp.route("/bookings/<booking_id>/confirm", methods=["POST"])
@_require_auth
def bookings_confirm(booking_id: str):
    """Confirma um agendamento pelo painel."""
    return _booking_decision_response(booking_id, "confirmed")


@dashboard_bp.route("/bookings/<booking_id>/cancel", methods=["POST"])
@_require_auth
def bookings_cancel(booking_id: str):
    """Cancela um agendamento pelo painel."""
    return _booking_decision_response(booking_id, "cancelled")


def _booking_decision_response(booking_id: str, decision: str):
    """Aplica a decisão do dono pela coordenadora e redesenha a lista.

    A regra de fechamento não mora aqui: esta função chama
    confirmations.confirm_or_cancel_booking() e traduz o resultado. O guard de
    transição vem junto — clicar duas vezes cai em "skipped" sem que a rota
    precise checar nada.

    Agir pelo painel também carimba o owner_response da notificação daquele
    agendamento, quando existe uma em aberto. Sem isso, uma reserva resolvida na
    tela ficaria para sempre "sem resposta" na fila, e as duas fontes contariam
    histórias diferentes sobre o que o dono decidiu.

    Sempre responde 200, como as rotas do inbox: o HTMX não faz swap em 4xx/5xx,
    e um erro nu deixaria o dono olhando uma tela que não mudou sem saber por quê.

    Args:
        booking_id (str): Id do agendamento vindo da URL.
        decision (str): "confirmed" ou "cancelled".

    Returns:
        tuple: (HTML da parcial, 200).
    """
    result = confirmations.confirm_or_cancel_booking(booking_id, decision)

    if result["result"] == "not_found":
        notice = {"kind": "error", "text": "Agendamento não encontrado. A lista foi atualizada."}
    elif result["result"] == "skipped":
        already = "confirmado" if result["status"] == "confirmed" else "cancelado"
        notice = {"kind": "warning", "text": f"Este agendamento já estava {already}. Nada foi alterado."}
    else:
        owner_notifications.register_response_for_booking(booking_id, decision)
        done = "confirmado" if decision == "confirmed" else "cancelado"
        notice = {"kind": "success", "text": f"Agendamento {done}."}
        if not result["lead_notified"]:
            notice["kind"] = "warning"
            notice["text"] += " Não consegui avisar o lead pelo WhatsApp — avise por outro canal."

    return render_template("_bookings_list.html", **_bookings_context(notice)), 200


#
# SETTINGS (Module S1)
#
# The first write screen for configuration in the project. Until it existed, the
# AI's personality and the owner's phone number were only reachable by hand-run
# SQL — migration 003 says so in its own header.
#
# ONE PAGE, TWO SECTIONS, TWO POSTS. "IA" edits ai_configs; "Conta" edits
# owners.owner_phone and shows (read-only) the Google Calendar status. They are
# separate POSTs because they have nothing to do with each other: an owner
# fixing a typo in their phone number must not rewrite the AI's tone as a side
# effect of submitting one big form.
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


def _settings_context(notice: dict | None = None, ai_form: dict[str, str] | None = None) -> dict:
    """Monta o contexto das duas seções da tela de configurações.

    Args:
        notice (dict | None): Aviso a exibir no topo, como
            {"kind": "success|warning|error", "text": str}.
        ai_form (dict[str, str] | None): Valores a mostrar nos campos da IA em vez
            dos que estão no banco. Serve para um POST recusado devolver o que o
            dono digitou — recarregar do banco apagaria a edição dele.

    Returns:
        dict: kwargs para render_template.
    """
    owner: dict | None = store.get_owner_credentials()
    notification_owner: dict | None = store.get_owner_for_notification()

    return {
        "ai_config": ai_form if ai_form is not None else ai_configs.get_ai_config(),
        "owner_phone": notification_owner["owner_phone"] if notification_owner else None,
        "integration_status": owner["integration_status"] if owner else "disconnected",
        "google_email": owner["google_email"] if owner else None,
        "notice": notice,
    }


@dashboard_bp.route("/settings")
@_require_auth
def settings():
    """Tela de configurações: personalidade da IA e dados da conta."""
    return render_template("settings.html", **_settings_context())


@dashboard_bp.route("/settings/ai", methods=["POST"])
@_require_auth
def settings_save_ai():
    """Salva a camada customizável do prompt (ai_configs).

    Sobrescreve, sem histórico: a última gravação do dono vale. Não há cache
    nenhum para invalidar — bot/ai_configs.py lê do banco a cada mensagem —, então
    a próxima resposta ao lead já sai com o texto novo.

    Os cinco campos são obrigatórios porque todos são interpolados no prompt: um
    vazio deixaria a IA sem nome, sem tom ou sem os dados do negócio.
    """
    submitted: dict[str, str] = {
        field: request.form.get(field, "").strip() for field in _AI_CONFIG_FIELDS
    }

    if not all(submitted.values()):
        notice = {"kind": "error", "text": "Preencha todos os campos da IA antes de salvar."}
        return render_template("settings.html", **_settings_context(notice, ai_form=submitted)), 200

    if not ai_configs.update_ai_config(**submitted):
        notice = {"kind": "error", "text": "Não encontrei a configuração desta academia para salvar."}
        return render_template("settings.html", **_settings_context(notice, ai_form=submitted)), 200

    notice = {"kind": "success", "text": "Configuração da IA salva. Vale a partir da próxima mensagem."}
    return render_template("settings.html", **_settings_context(notice)), 200


@dashboard_bp.route("/settings/account", methods=["POST"])
@_require_auth
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
    sequestrando a conversa do lead. (A unicidade entre donos é assunto da
    constraint UNIQUE que ainda não existe; ver CLAUDE.md.)
    """
    normalized: str | None = store.normalize_owner_phone(request.form.get("owner_phone", ""))

    if normalized is None:
        notice = {
            "kind": "error",
            "text": "Número inválido. Use o formato com DDI e DDD, por exemplo 5521999999999.",
        }
        return render_template("settings.html", **_settings_context(notice)), 200

    if session_store.session_exists(normalized):
        notice = {
            "kind": "error",
            "text": (
                "Esse número já está em uso por uma conversa de lead. "
                "Salvá-lo como número do dono faria as mensagens dessa pessoa "
                "virarem confirmações de agendamento. Nada foi alterado."
            ),
        }
        return render_template("settings.html", **_settings_context(notice)), 200

    if not store.update_owner_phone(normalized):
        notice = {"kind": "error", "text": "Não encontrei o cadastro desta academia para salvar."}
        return render_template("settings.html", **_settings_context(notice)), 200

    notice = {"kind": "success", "text": "Número do dono salvo."}
    return render_template("settings.html", **_settings_context(notice)), 200
