import logging
import whatsapp.whatsapp_service as whatsapp_service
import bot.session as session
from bot.states import State
from bot.catalog import Categories  

logger = logging.getLogger(__name__)

# Dict with the products of the store in wich category.
# Just for the sake of simplicity, we are using a dict here, but in a real application, we would use a database to store this information.
# A dictionary in Python is a key-value structure.
# The key here is the number that the customer types ("1", "2", etc).

# triggers that restart the conversation from any point.
greeting_triggers: list[str] = [
    "oi", "olá", "ola", "oi!",
    "bom dia", "boa tarde", "boa noite",
    "menu", "inicio", "início", "reiniciar", "voltar", 
    "voltar ao início", "resetar", "recomeçar"
]

# trigger that route the customer to a human attendant
attendant_triggers: list[str] = [
    "4", "atendente", "humano", "ajuda", "suporte"
]

def handle_message(sender: str, body: str) -> dict:
    """
    Main entry point for incoming messages.
    Fetches the session, checks for global comands, and delegates to the apropriate handler.
    Args:
        sender (str): number of the user that sent the message.
        body (str): text that the client sent.
    Returns:
        dict: the dict of the section uptaded with the new state
    """
    
    text: str = body.strip().lower() # remove spaces and convert to lowercase to make the processing easier
    _session: dict = session.get_session(sender) # get the session of the client
    _session["last_text"] = text 
    session.save_session(sender, _session) # save the last text in the session, so we can use it in the future if we need to.
    
    state: str = _session.get("state", State.INITIAL.value) #catch the current state of the session

    
    logger.info(f"[{sender}] State: {state} | Received message: {text}")
    
    state_handlers: dict = {
        State.INITIAL.value: _handle_initial,
        State.MAIN_MENU.value: _handle_main_menu,
        State.CHOSING_PRODUCT.value: _handle_choosing_product,
        State.WAITING_ACTION.value: _handle_waiting_action,
        State.ATTENDANT.value: _handle_attendant,
    }
    
    handler_fn: callable = state_handlers.get(state, _handle_fallback)
    handler_fn(sender, _session) # call the handler function with the sender and session as arguments.

# ──────────────────────────────────────────────────────────────
# PRIVATE STATE HANDLERS
# ──────────────────────────────────────────────────────────────
def _handle_fallback(sender: str, _session: dict) -> None:
    """ 
    Handle unknown states by resetting the conversation to the main menu.
    Args:        
        sender (str): number of the user that sent the message.
        _session (dict): the session data of the client.
    """

    logger.warning(f"[{sender}] Estado desconhecido: {state}. Resetando a conversa.")
    whatsapp_service.send_main_menu(sender)
    _session["state"] = State.INITIAL.value
    _session["current_category"] = None
    return _session


def _handle_initial(sender: str, _session: dict) -> None:
    """
    Handle the initial state of the conversation. 
    This function is called when the client sends a message for the first time or when the session is reset.
    Args:
        sender (str): number of the user that sent the message.
        _session (dict): the session data of the client.
    """
    whatsapp_service.send_main_menu(sender)
    
    _session["state"] = State.MAIN_MENU.value
    _session["current_category"] = None
    
    session.save_session(sender, _session) # save the session with the new state and current category reseted.

def _handle_main_menu(sender: str, _session: dict) -> None:
    """
    Handle the main menu state of the conversation. 
    This function is called when the client is in the main menu and sends a message.
    Args:
        sender (str): number of the user that sent the message.
        _session (dict): the session data of the client.
    """
    text: str = _session.get("last_text", "")
    
    if text in Categories:
        # the client chosed a valid category

        Category: dict = Categories[text]
        whatsapp_service.send_category(sender, Category["name"], Category["products"])

        _session["state"] = State.CHOSING_PRODUCT.value
        _session["current_category"] = text
    else:
        # the client sent an invalid option, we send the main menu again.
        whatsapp_service.send_message(sender, "Por favor, digite um número de 1 a 4 para escolher a categoria.")
        
def _handle_choosing_product(sender: str, _session: dict) -> None:
    """
    Handle the choosing product state of the conversation. 
    This function is called when the client is in the choosing product state and sends a message.
    Args:
        sender (str): number of the user that sent the message.
        _session (dict): the session data of the client.
    """
    text = _session.get("last_text", "")
    
    if text == "0":
            # the client want to go back to the main menu.
            whatsapp_service.send_main_menu(sender)
            _session["state"] = State.MAIN_MENU.value
            
            session.save_session(sender, _session) # save the session with the new state and current category reseted.
            return
        
    # Pick the category that was being chosed by the client.
    category_key: str = _session.get("current_category", "") 
    category: dict = Categories.get(category_key, {})
    products: list = category.get("products", [])
    
    # try to text to a index
    try:
        # int() convert the string to and integer
        index = int(text) - 1 # -1 bc python list start with 0
        
        #verify if the index is valid
        # len() return the size of the list
        if 0 <= index < len(products):
            product = products[index]
            
            if "cart" not in _session:
                _session["cart"] = []
                
            # _session["cart"] it´s a list of products ->(dicts)
            # item is a prodcut in the cart -> dict with "nome", "preco" and "quantidade"
            # look for the same product in the cart, if it exist, we increase the quantity
            found = False
            for item in _session["cart"]:
                if item["name"] == product["name"]:
                    item["quantity"] += 1
                    found = True
                    break
            
            if not found:
                new_product = product.copy() # copy the product to not change the original one in the Categories dict
                new_product["quantity"] = 1
                _session["cart"].append(new_product)
            
            # Get the current quantity for the message
            current_quantity = next(item["quantity"] for item in _session["cart"] 
                                                        if item["name"] == product["name"])
                    
            whatsapp_service.send_product_added(sender, product["name"], product["price"], current_quantity)
            _session["state"] = State.WAITING_ACTION.value
        else:
            whatsapp_service.send_message(sender, f"Número inválido. Digite um número entre 1 e {len(products)}.")
            
    except ValueError:
        # ValueError happens when int() receive something that isn't a number
        # ex: int("banana") cause a ValueError
        whatsapp_service.send_message(sender, "Por favor, digite apenas o número do produto.")
    
    session.save_session(sender, _session) # save the session with the new state and current category reseted.

def _handle_waiting_action(sender: str, _session: dict) -> None:
    """
    Handle the waiting action state of the conversation. 
    This function is called when the client is in the waiting action state and sends a message.
    Args:
        sender (str): number of the user that sent the message.
        _session (dict): the session data of the client.
    """
    text = _session.get("last_text", "")

    action_map: dict = {
        "1": _continue_shopping,
        "2": _view_cart,
        "3": _finalize_order,
        "4": _handle_attendant,
    }
    
    action_fn: callable = action_map.get(text)
    
    if action_fn:
        action_fn(sender, _session)
    else:
        whatsapp_service.send_message(sender, "Opção inválida. Por favor, digite um número de 1 a 4.")
        session.save_session(sender, _session) # save the session with the new state and current category reseted.

def _handle_attendant(sender: str, _session: dict) -> None:
    """
    Handle the attendant state of the conversation. 
    This function is called when the client asks for an attendant.
    Args:
        sender (str): number of the user that sent the message.
        _session (dict): the session data of the client.
    Returns:
         dict: the dict of the section uptaded with the new state
    """
    whatsapp_service.send_attendant(sender)
    _session["state"] = State.ATTENDANT.value
    whatsapp_service.send_message(sender)
    
    session.save_session(sender, _session) # save the session with the new state.
    
# ──────────────────────────────────────────────────────────────
# PRIVATE ACTION HELPERS
# Called from _handle_awaiting_action
# ──────────────────────────────────────────────────────────────
def _continue_shopping(sender: str, _session: dict) -> None:
    """Returns the customer to the main menu, preserving the cart."""
    whatsapp_service.send_main_menu(sender)
    _session["state"]: str = State.MAIN_MENU.value
    
    session.save_session(sender, _session) # save the session with the new state and current category reseted.   

    
def _view_cart(sender: str, _session: dict) -> None:
    """View the cart and the options after adding a product."""
    cart: list = _session.get("cart", [])
    
    if not cart:
        whatsapp_service.send_message(sender, "Seu carrinho está vazio! 🛒")
        return
    
    lines: list = ["🛒 *Seu Carrinho:*\n"]
    total: float = 0.0
    
    for item in cart:
        quantity: int = item.get("quantity", 1) #quantity of the current item, default is 1 if not specified
        price: float = item.get("price", 0.0) #price of the current item
        subtotal: float = price * quantity #subtotal is the price of the item multiplied by the quantity
        total += subtotal

        if quantity > 1:
            # Output example: "- Hambúrguer | R$ 20.00 x 2 = R$ 40.00"
            lines.append(f"- {item['name']} | R$ {price:.2f} x {quantity} = R$ {subtotal:.2f}")
        else: 
            #output example: "- Refrigerante | R$ 5.00"
            lines.append(f"- {item['name']} | R$ {price:.2f}")
        
    lines.append(f"\n*Total: R$ {total:.2f}*")
    lines.append("\nDigite *1* para continuar comprando.")
    lines.append("Digite *2* para ver o carrinho.")
    lines.append("Digite *3* para finalizar um pedido.")
    lines.append("Digite *4* para falar com um atendente.")
    
    whatsapp_service.send_message(sender, "\n".join(lines))
    session.save_session(sender, _session) # save the session with the new state and current category reseted.  

def _finalize_order(sender: str, _session: dict) -> None:
    """Handle the finalize order action."""
    cart: list = _session.get("cart", [])
    
    if not cart:
        whatsapp_service.send_message(sender, "Seu carrinho está vazio! Adicione produtos antes de finalizar. 🛒")
        session.save_session(sender, _session)
        return
    
    lines: list = ["✅ *Pedido Finalizado!*\n", "📋 *Resumo do Pedido:*\n"]
    total: float = 0.0
    
    for item in cart:
        quantity: int = item.get("quantity", 1) #quantity of the current item, default is 1 if not specified
        price: float = item.get("price", 0.0) #price of the current item
        subtotal: float = price * quantity #subtotal is the price of the item multiplied by the quantity
        total += subtotal

        if quantity > 1:
            # Output example: "- Hambúrguer | R$ 20.00 x 2 = R$ 40.00"
            lines.append(f"- {item['name']} | R$ {price:.2f} x {quantity} = R$ {subtotal:.2f}")
        else: 
            #output example: "- Refrigerante | R$ 5.00"
            lines.append(f"- {item['name']} | R$ {price:.2f}")
        
    lines.append(f"\n*Total: R$ {total:.2f}*")
    lines.append("\nObrigado pela sua compra! 🎉"
                     "Em breve entraremos em contato para confirmar o pedido e o prazo de entrega.")

    #Intregation with payment and order management systems would happen here in a real application.
    whatsapp_service.send_message(sender, "\n".join(lines))

    # Clear session after successful order
    session.clear_session(sender)
    
