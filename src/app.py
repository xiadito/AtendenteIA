from flask import Flask
from config import Config
from webhook.routes import webhook_bp, dashboard_bp
from integrations.routes import integrations_bp


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

    # Authentication (Module S3a). Wired before the blueprints so the ordering
    # reads in the same direction as the dependency: every dashboard view is
    # decorated with require_auth, which resolves the login manager off the app.
    from accounts.auth import init_auth
    init_auth(app)

    # The first dashboard user, once, idempotently — migration 009 cannot seed
    # one, because a password hash must not live in a public repository. Its own
    # try/except for the same reason init_db has one: a failure here must never
    # stop the app from booting.
    from accounts.bootstrap import bootstrap_first_user
    try:
        bootstrap_first_user()
    except Exception as e:
        print("Could not bootstrap the first dashboard user:", e)

    # Register blueprints (modulos de rota)
    app.register_blueprint(webhook_bp)
    app.register_blueprint(dashboard_bp, url_prefix="/dashboard")
    app.register_blueprint(integrations_bp, url_prefix="/integrations")

    print("App created")
    return app

if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, port=5000)
    