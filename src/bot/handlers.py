import logging
import whatsapp.whatsapp_service as whatsapp_service
from bot.states import State

logger = logging.getLogger(__name__)

# Dict with the products of the store in wich category.
# Just for the sake of simplicity, we are using a dict here, but in a real application, we would use a database to store this information.
# A dictionary in Python is a key-value structure.
# The key here is the number that the customer types ("1", "2", etc).
Categories = { 
    "1": {
        "name": "Frutas e Verduras",
        "products": [
            {"name": "Banana",  "price": 3.50, "quantity": 0},
            {"name": "Maçã",    "price": 5.99, "quantity": 0},
            {"name": "Alface",  "price": 2.00, "quantity": 0},
            {"name": "Tomate",  "price": 4.50, "quantity": 0},
        ]
    },
    "2": {
        "name": "Laticínios",
        "products": [
            {"name": "Leite integral 1L", "price": 4.99, "quantity": 0},
            {"name": "Queijo mussarela",  "price": 12.90, "quantity": 0},
            {"name": "Iogurte natural",   "price": 3.50, "quantity": 0},
        ]
    },
    "3": {
        "name": "Bebidas",
        "products": [
            {"name": "Água mineral 500ml", "price": 2.00, "quantity": 0},
            {"name": "Refrigerante 2L",    "price": 7.99, "quantity": 0},
            {"name": "Suco de laranja 1L", "price": 6.50, "quantity": 0},
        ]
    },
    "4": {
        "name": "Padaria",
        "products": [
            {"name": "Pão francês (kg)","price": 9.90, "quantity": 0},
            {"name": "Bolo de cenoura", "price": 15.00, "quantity": 0},
            {"name": "Croissant",       "price": 4.50, "quantity": 0},
        ]
    }
}

def handle_message(sender: str, body: str, section: dict) -> dict:
    """
    Decide wich message to send to the user based on the message he sent and the section he is in the conversation.
    
    Args:
        sender (str): number of the user that sent the message.
        body (str): text that the client sent.
        section (dict): dict with the current section of the conversation of the user. 
                    ex: {"state": "main_menu", "category": None}

    Returns:
        dict: the dict of the section uptaded with the new state
    """
    
    #.strip() remove the spaces in the early and end of the string
    #.lower() transform all the string to lowercase
    text = body.strip().lower()
    
    #catch the current state of the session
    #if the state doesn't exist, we consider that the user is in the main menu.
    state = section.get("state", State.MAIN_MENU.value)
    
    logger.info(f"[{sender}] State: {state} | Received message: {text}")
    
    # ── saudation ──────────────────────────────────────────────
    # if the client sent a saudation, shows the main menu.
    saudations = ["oi", "olá", "ola", "oi!", "olá!", "bom dia", 
                 "boa tarde", "boa noite", "menu", "início", "inicio"]
    
    if text in saudations:
        whatsapp_service.send_main_menu(sender)
        section["state"] = State.MAIN_MENU.value
        section["cart"] = section.get("cart", [])
        
        return section
    
    # ── attendant ─────────────────────────────────────────────
    #in any moment the client can ask for an attendant, and we will send a message with the contact of the store.
    if text in ("4", "atendente", "humano", "ajuda"):
        if state == State.ATTENDANT.value:
            # "4" in the menu of action means attendant.
            
            whatsapp_service.send_attendant(sender)
            section["state"] = State.ATTENDANT.value
            
            return section
    
    # ── main menu ─────────────────────────────────────────────
    if state == State.MAIN_MENU.value:
        if text in Categories:
            # the client chosed a valid category
            
            Category = Categories[text]
            whatsapp_service.send_category(sender, Category["name"], Category["products"])
            
            section["state"] = State.CHOSING_PRODUCT.value
            section["current_category"] = text
        else:
            # the client sent an invalid option, we send the main menu again.
            whatsapp_service.send_message(sender, "Por favor, digite um número de 1 a 4 para escolher a categoria.")
            
        return section
    
    # ── chosing product ─────────────────────────────────────────────
    if state == State.CHOSING_PRODUCT.value:
        if text == "0":
            # the client want to go back to the main menu.
            whatsapp_service.send_main_menu(sender)
            section["state"] = State.MAIN_MENU.value
            
            return section
        
        # Pick the category that was being chosed by the client.
        category_key = section.get("current_category")
        category = Categories.get(category_key, {})
        products = category.get("products", [])
        
        # try to text to a index
        try:
            # int() convert the string to and integer
            index = int(text) - 1 # -1 bc python list start with 0
            
            #verify if the index is valid
            # len() return the size of the list
            if 0 <= index < len(products):
                product = products[index]
                
                if "cart" not in section:
                    section["cart"] = []
                    
                # section["cart"] it´s a list of products ->(dicts)
                # item is a prodcut in the cart -> dict with "nome", "preco" and "quantidade"
                # look for the same product in the cart, if it exist, we increase the quantity
                found = False
                for item in section["cart"]:
                    if item["name"] == product["name"]:
                        item["quantity"] += 1
                        found = True
                        break
                
                if not found:
                    new_product = product.copy() # copy the product to not change the original one in the Categories dict
                    new_product["quantity"] = 1
                    section["cart"].append(new_product)
                
                # Get the current quantity for the message
                current_quantity = next(item["quantity"] for item in section["cart"] 
                                                            if item["name"] == product["name"])
                        
                whatsapp_service.send_product_added(sender, product["name"], product["price"], current_quantity)
                section["state"] = State.WAITING_ACTION.value
            else:
                whatsapp_service.send_message(sender, f"Número inválido. Digite um número entre 1 e {len(products)}.")
        except ValueError:
            # ValueError happens when int() receive something that isn't a number
            # ex: int("banana") cause a ValueError
            whatsapp_service.send_message(sender, "Por favor, digite apenas o número do produto.")
        
        return section

    # ── WAITING ACTION ────────────────────────────────────────
    if state == State.WAITING_ACTION.value:
            # Keep buying - come back to main menu
            if text == "1":
                whatsapp_service.send_main_menu(sender)
                section["state"] = State.MAIN_MENU.value

            elif text == "2":
                # View cart
                cart = section.get("cart", [])
                if not cart:
                    whatsapp_service.send_message(sender, "Seu carrinho está vazio! 🛒")
                else:
                    lines = ["🛒 *Seu Carrinho:*\n"]
                    total = 0
                    for item in cart:
                        subtotal = item["price"] * item["quantity"]
                        lines.append(f"- {item['name']} - R$ {item['price']:.2f}")
                        total += subtotal
                    lines.append(f"\n*Total: R$ {total:.2f}*")
                    lines.append("\nDigite *1* para continuar comprando.")
                    lines.append("Digite *2* para ver o carrinho.")
                    lines.append("Digite *3* para finalizar um pedido.")
                    lines.append("Digite *4* para falar com um atendente.")
                    whatsapp_service.send_message(sender, "\n".join(lines))
                    
            elif text == "3":
                # Finalize order
                whatsapp_service.send_message(sender, "✅ Pedido finalizado! Obrigado por comprar conosco! 🎉")
                section["state"] = State.MAIN_MENU.value
                section["cart"] = []
            
            elif text == "4":
                # Talk to attendant
                whatsapp_service.send_attendant(sender)
                section["state"] = State.ATTENDANT.value
            
            else:
                whatsapp_service.send_message(sender, "Opção inválida. Por favor, digite um número de 1 a 4.")
            
            return section
    
    
    # ── UNKOWN STATE ────────────────────────────────────
    # if the state is something that we don't expect, we send the main menu again and reset the conversation.
    
    logger.warning(f"[{sender}] Estado desconhecido: {state}. Resetando a conversa.")
    whatsapp_service.send_main_menu(sender)
    section["state"] = State.MAIN_MENU.value
    return section
        
