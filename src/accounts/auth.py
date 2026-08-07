"""Dashboard authentication: Flask-Login wiring (Module S3a).

THE ONLY FLASK-AWARE FILE IN `accounts/`. Everything else in this package takes
an explicit tenant_id and runs happily from a CLI; this module exists solely to
teach Flask who is logged in.

What it replaced: `webhook/routes.py::_require_auth`, which checked the boolean
`session["dashboard_authenticated"]`, set by comparing the submitted password
against DASHBOARD_PASSWORD in plain text. There was no identity — the dashboard
could not say WHICH human was using it, and `messages.author = 'operator'` still
cannot.

`require_auth` is exported as the project's single name for the decorator, and
is imported by BOTH `webhook/routes.py` and `integrations/routes.py`. Before
S3a, `integrations/routes.py` imported the private `_require_auth` from
`webhook/routes.py`, which dragged the whole `bot/` package in transitively —
the exact import direction `integrations/store.py`'s docstring warns against.
"""

import logging

from flask import Flask
from flask_login import LoginManager, UserMixin, login_required

from accounts import users

logger = logging.getLogger(__name__)

login_manager: LoginManager = LoginManager()

# The project's one name for "this route needs a logged-in human". Aliased
# rather than re-implemented so there is nothing of our own to keep correct.
require_auth = login_required


class User(UserMixin):
    """One logged-in dashboard user.

    The only class in the project outside the test harness, and a documented
    exception: UserMixin is how Flask-Login is meant to be used, and hand-rolling
    is_authenticated / get_id would be reimplementing a library for no reason.

    `tenant_id` IS THE MODULE S3b SEAM. Every protected route can already read
    `current_user.tenant_id`; NO READ FILTERS BY IT YET. Wiring it into
    `list_conversations()`, `get_conversation()`, `list_bookings_for_review()`
    and the rest is S3b's entire job.
    """

    def __init__(self, row: dict) -> None:
        """Wrap a `users` row.

        Args:
            row (dict): {id, email, tenant_id, ...} as returned by
                accounts/users.py. The password hash is neither needed nor kept.
        """
        self.id: int = row["id"]
        self.email: str = row["email"]
        self.tenant_id: str = row["tenant_id"]


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    """Rebuild the User for a session cookie, on every authenticated request.

    Args:
        user_id (str): Flask-Login stores get_id()'s return value, which
            UserMixin defines as str(self.id).

    Returns:
        User | None: The user, or None if the id is malformed or the row is
        gone — a deleted account must not stay logged in on an old cookie.
    """
    try:
        row: dict | None = users.get_user_by_id(int(user_id))
    except (TypeError, ValueError):
        return None

    return User(row) if row is not None else None


def init_auth(app: Flask) -> None:
    """Attach the login manager to the application.

    Called from create_app() before the blueprints are registered.

    Three settings worth the words:

    - **login_view** points at the dashboard's own login route, so an anonymous
      request to a protected page redirects there instead of getting a bare 401.

    - **login_message = None.** Flask-Login flashes "Please log in to access
      this page." by default. `login.html` renders no get_flashed_messages(), so
      those flashes would never be consumed and would pile up in the session
      cookie indefinitely.

    - **session_protection = "basic", not "strong".** "strong" deletes the
      session whenever the request identifier (IP + user-agent) changes. The
      Google OAuth flow stores `oauth_state` and `oauth_code_verifier` in the
      session across a full round-trip to Google, and `google_callback` is
      itself behind require_auth — CLAUDE.md already lists losing that session
      as a known issue. Behind Railway's proxy an IP change mid-flow is
      plausible, and "strong" would turn a rare failure into a routine one.

    There is deliberately NO remember-me: login_user() is called without
    `remember=True`, so the cookie lasts exactly as long as the boolean it
    replaced. Persisting a signed user id on disk is a security decision this
    module has not made.

    Args:
        app (Flask): The application being created.
    """
    login_manager.login_view = "dashboard.login"
    login_manager.login_message = None
    login_manager.session_protection = "basic"
    login_manager.init_app(app)
