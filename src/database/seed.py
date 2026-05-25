import bot.session as store
import config

def seed_fake_orders() -> None:
    """
    Populate the session store with fake orders for dashboard testing.
    """
    if config.FLASK_ENV != "development":
        print("Skipping seeding fake orders in non-development environment.")
        return
    
    if len(store.get_all_orders()) > 0:
        print("Orders already exist in the database. Skipping seeding.")
        return
    
    fake_orders: list[dict] = [
        {
            "items": [
                {"name": "Banana",          "price": 3.50, "quantity": 2},
                {"name": "Leite integral 1L","price": 4.99, "quantity": 1},
            ],
            "total": 11.99,
            "address": "Rua das Flores, 42 — Apto 3B, Vila Nova",
        },
        {
            "items": [
                {"name": "Pão francês (kg)", "price": 9.90, "quantity": 1},
                {"name": "Queijo mussarela", "price": 12.90, "quantity": 1},
            ],
            "total": 22.80,
            "address": "Av. Brasil, 1500 — Casa, Jardim América",
        },
        {
            "items": [
                {"name": "Refrigerante 2L", "price": 7.99, "quantity": 3},
            ],
            "total": 23.97,
            "address": "Retirada na loja",
        },
    ]

    senders: list[str] = [
        "5521991110001",
        "5521991110002",
        "5521991110001",  # same sender, second order — tests multiple orders per customer
    ]

    for sender, order in zip(senders, fake_orders):
        store.save_order(sender, order)