from flask import Blueprint, jsonify, request, render_template, redirect, url_for, session
from functools import wraps
from config import Config
import logging
import threading
from whatsapp.whatsapp_service import send_message
from bot.handlers import handle_text_message
import bot.messages as messages
import bot.owner_notifications as owner_notifications
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

    Maps "1"/"2" to confirmed/cancelled and records the response. Never
    calls update_booking_status() — actually closing out the booking based
    on this reply is a future feature, not this one.

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

    updated = owner_notifications.register_owner_response(owner_phone, response)
    if not updated:
        logger.warning(f"Owner {owner_phone} replied '{stripped}' but no open notification was found.")

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


@dashboard_bp.route("/inbox/<sender>")
@_require_auth
def inbox_conversation(sender: str):
    """Abre uma conversa e marca as mensagens do lead como lidas."""
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
