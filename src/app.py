from flask import Flask
from config import Config
from webhook.routes import webhook_bp, dashboard_bp


from database.seed import seed_fake_orders
from bot.session import get_all_orders
#seed_fake_orders()  # Seed fake orders for testing the dashboard

def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = Config.SECRET_KEY

    #initialize database and create tables if they don't exist
    from database.db import init_db
    try: 
        with app.app_context():
            init_db()
            print("Migrations rodaram com sucesso.")
    except Exception as e:
        print("Error initializing database:", e)
    
    # Register blueprints (modulos de rota)
    app.register_blueprint(webhook_bp)
    app.register_blueprint(dashboard_bp, url_prefix="/dashboard")
   
    
    #print("all orders:", get_all_orders())
    
    print("App created")
    return app

if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, port=5000)
    