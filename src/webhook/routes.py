from flask import Blueprint, jsonify, request, render_template, redirect, url_for, session
from functools import wraps
from config import Config
import logging
import threading
from whatsapp.whatsapp_service import send_message
from bot.handlers import handle_text_message
import bot.session as store


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

    logger.info(f"Mensagem recebida de {sender}: {body}")

    # Verifica se os campos essenciais chegaram
    if not sender or not body:
        logger.warning("Payload incompleto — From ou Body ausente")
        return jsonify({"error": "Payload inválido"}), 400


    # Twilio sends "whatsapp:+5521999999999" we only need the numbers: "5521999999999" 
    # cleans the sender number - .replace() replace one substring for another — here we remove "whatsapp:+" 
    clean_number = sender.replace("whatsapp:+", "")
    logger.info(f"Número limpo: {clean_number} | Mensagem: {body }")

    # Delegate the handling of the message to the AI
    # The routes.py don't know nothing about the menu or the products
    if sender and body:
        handle_text_message(clean_number, body)

    # O Twilio espera status 200 para confirmar que você recebeu
    # Se não receber 200, ele tenta reenviar
    return jsonify({"status": "ok"}), 200

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
    """Hub de navegação pós-login: pedidos, integrações e futuras features."""
    return render_template("menu.html")

@dashboard_bp.route("/index")
@_require_auth
def index():
    """ Main dashboard view - list of all orders from db"""
    status_filter: str = request.args.get("status", "")
    
    all_orders: list[dict] = store.get_all_orders()
    
    if status_filter and status_filter in store.valid_order_statuses:
        orders: list[dict] = [order for order in all_orders if order["status"] == status_filter]
    else:
        orders: list[dict] = all_orders
    
    
    return render_template("dashboard.html", 
                           orders=orders, 
                           valid_statuses=store.valid_order_statuses, 
                           active_filter=status_filter,
                           )

@dashboard_bp.route("/update-order-status", methods=["POST"])
@_require_auth
def update_status():
    """Endpoint to update the status of an order from the dashboard form.
    
    Receives order_id and the target stattus, validades both and redirects back to  the dashboard.
    """
    
    order_id: str = request.form.get("order_id", "")
    new_status: str = request.form.get("status", "")
    
    if not order_id or not new_status:
        logger.warning("update_status: missing order_id or status in POST request.")
        return redirect(url_for("dashboard.index"))
    
    if new_status not in store.valid_order_statuses:
        logger.warning("update_status: invalid status '%s' received.", new_status)
        return redirect(url_for("dashboard.index"))
    
    success: bool = store.update_order_status(order_id, new_status)
    
    if not success:
        logger.warning("update_status: order_id %s could not be updated.", order_id)
        
    return redirect(url_for("dashboard.index"))
        
    

