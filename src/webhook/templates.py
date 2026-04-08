def receive_initial_message(body: str) -> str:
    answer = ""
    if body.lower() == "oi":
        answer = "Olá! Bem-vindo ao Mercadinho! 🛒"
            
    return answer
    