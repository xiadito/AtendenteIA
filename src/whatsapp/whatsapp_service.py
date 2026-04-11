from twilio.rest import Client
from config import Config
import logging

logger  = logging.getLogger(__name__)

def get_client():
    """
    Creates the client for twilio using the account SID and auth token from the config.
    Makes it to simulate the client in future tests.
    Returns:
        Client: client from twilio
    """
    return Client(Config.TWILIO_ACCOUNT_SID, Config.TWILIO_AUTH_TOKEN)

def send_message(to: str, text: str) -> str:
    """

    Args:
        to (str): The phone number to which the message will be sent.
        text (str): The text of the message to be sent.

    Returns:
        str: The SID (UNIQUE ID) of the generated message from twilio.
    """
    try:
        client = get_client()
        
        message = client.messages.create(
            body = text,
            from_ = Config.TWILIO_SANDBOX_NUMBER,
            to = f"whatsapp:+{to}"
        )
        return message.sid
    except Exception as e:
        #All the erros of twilio will be catched here, and we can log them for future debugging.
        #We will need to solve this in webhooks. 
        logger.error(f"Error sending message para {to}: {e}")
        
        #literally re-raise the exception to be handled in the webhooks.
        raise 

def send_main_menu(to: str) -> str:
    """
    This function is responsible for sending the main menu to the user.
    The client get here when he send any saudation message.
    Args:
        to (str): The phone number to which the message will be sent.
    """
    
    text = (
        "👋 Olá! Bem-vindo ao *Mercadinho da Vila!* 🛒\n\n"
        "Escolha uma categoria:\n\n"
        " *1* - Frutas e Verduras\n"
        " *2* - Laticínios\n"
        " *3* - Bebidas\n"
        " *4* - Padaria\n\n"
        "Digite o *número* da categoria desejada."
    )
    
    return send_message(to, text)


def send_category(to: str, category: str, products: list) -> str:
    """
    This function is responsible for sending the category menu to the user.
    The client get here when he send the number of the category in the main menu.
    Args:
        to (str): The phone number to which the message will be sent.
        category (str): The name of the category to be sent.
        products (list): The list of products to be sent. ex: [{"name": "Banana", "price": 3.50}]
    """
    
    lines = [f"*{category}*\n"]
    
    for i, product in enumerate(products, start=1):
        line = f"{i}. {product['name']} - R$ {product['price']:.2f}"
        lines.append(line)
        
    lines.append("\nDigite o *número* do produto para adicionar ao carrinho.")
    lines.append("Digite *0* para voltar ao menu principal.")
    
    text = "\n".join(lines)
    
    return send_message(to, text)

def send_product_added(to: str, product: str, price:float, quantity: int) -> str:
    """
    This function is responsible for sending the message to the user when a product is added to the cart.
    The client get here when he send the number of the product in the category menu.
    Args:
        to (str): The phone number to which the message will be sent.
        product (str): The name of the product that was added to the cart.
        price (float): The price of the product that was added to the cart.
        quantity (int): The quantity of the product that was added to the cart.
    """
    text = (
        f"✅ *{product}* adicionado ao carrinho!\n"
        f"Preço: R$ {price:.2f}\n"
        f"Quantidade: {quantity}\n"
        "O que deseja fazer?\n\n"
        "1. Continuar comprando\n"
        "2. Ver carrinho\n"
        "3. Finalizar pedido\n"
        "4. Falar com atendente"
    )
        
    return send_message(to, text)

def send_attendant(to: str) -> str:
    """
    This function is responsible for sending the message to the user when he wants to talk to an attendant.
    The client get here when he send the number 4 in the product added menu.
    Args:
        to (str): The phone number to which the message will be sent.
    """
    text = (
        "👩‍💼 Um atendente entrará em contato com você em breve. Por favor, aguarde."
    )
    
    return send_message(to, text)

