from flask import Flask
from config import Config
from webhook.routes import webhook_bp, dashboard_bp


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    
    # Register blueprints (modulos de rota)
    app.register_blueprint(webhook_bp)
    app.register_blueprint(dashboard_bp, url_prefix="/dashboard")
    
    from database.seed import seed_fake_orders
    from bot.session import get_all_orders
    seed_fake_orders()  # Seed fake orders for testing the dashboard
    
    print("all orders:", get_all_orders())
    
    print("App created")
    return app

if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, port=5000)
    