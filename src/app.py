from flask import Flask
from config import Config
from webhook.routes import webhook_bp

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    
    # Register blueprints (modulos de rota)
    app.register_blueprint(webhook_bp)
    
    print("hello, world!")
    return app

if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, port=5000)
    