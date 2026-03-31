from flask import Blueprint, jsonify, request                     
import logging #modulo padrão de python para logs
from config import Config

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
    # Módulo 2 — verificação do whatsapp
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
    token = requests.args.get("hub.verify_token")
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

@webhook_bp.route("/webhook", methods=["POST"])
def receive():
    """
    A meta envia isso toda vez que alguem manda mensagem.
    Os dados chegam no corpo da requisição em formato JSON.
    """
    # request.get_json() lê o corpo da requisição e converte o JSON em dicionário Python
    # Se o corpo não for JSON válido, retorna None
    data = request.json()
    
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
            message = value["message"][0]

            # the number of the sender comes in the internacional format without the +
            # ex: "5521999999999"
            sender = message["from"]
            
            # message["type"] could be "text", "image" and "audio", etc
            #we only treat text at the moment
            if message["type"] == "text":
                text = message["text"]["body"]
                logger.info(f"Meensagem de {sender}: {sender}")
                
    except (KeyError, IndexError) as e:
        # KeyError: tentou acessar uma chave que não existe no dicionário
        # IndexError: tentou acessar um índice que não existe na lista
        logger.error(f"Erro ao processar payload: {e}")
        # Mesmo com erro interno, retornamos 200 para a Meta não reenviar
        return jsonify({"status": "error"}), 200
     
     
    return jsonify({"status": "Ok"}), 200
    

@webhook_bp.route("/", methods=["GET"])
def initial_message():
    return "<p>Started the mercadinho bot server</p>"


@webhook_bp.route("/health", methods=["GET"])
def health():
    #Verificando Status
    return jsonify({"status": "online", "bot": "mercadinho"}), 200
