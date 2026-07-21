import logging
import secrets

from flask import Blueprint, redirect, render_template, request, session, url_for

import integrations.google_calendar as google_calendar
import integrations.store as store
from webhook.routes import _require_auth

logger = logging.getLogger(__name__)

integrations_bp = Blueprint("integrations", __name__)


@integrations_bp.route("/google", methods=["GET"])
@_require_auth
def google_status():
    """Show the current Google Calendar connection status."""
    owner: dict | None = store.get_owner_credentials()
    return render_template("integrations_google.html", owner=owner)


@integrations_bp.route("/google/connect", methods=["GET"])
@_require_auth
def google_connect():
    """Generate the CSRF state, store it in the Flask session and redirect to Google."""
    state: str = secrets.token_urlsafe(32)

    authorization_url: str
    code_verifier: str
    authorization_url, code_verifier = google_calendar.build_authorization_url(state)

    # Both values must survive until the callback: state proves the callback is
    # ours (CSRF), code_verifier proves to Google we started the flow (PKCE).
    session["oauth_state"] = state
    session["oauth_code_verifier"] = code_verifier

    return redirect(authorization_url)


@integrations_bp.route("/google/callback", methods=["GET"])
@_require_auth
def google_callback():
    """Handle Google's OAuth redirect: validate state, exchange code, persist credentials."""
    expected_state: str | None = session.pop("oauth_state", None)
    code_verifier: str | None = session.pop("oauth_code_verifier", None)
    returned_state: str | None = request.args.get("state")

    if not expected_state or expected_state != returned_state or not code_verifier:
        logger.warning("OAuth state mismatch or missing on Google callback.")
        return render_template(
            "integrations_google.html",
            owner=store.get_owner_credentials(),
            error="Falha de segurança na autenticação. Tente conectar novamente.",
        )

    code: str | None = request.args.get("code")
    if not code:
        logger.warning("Google callback received without an authorization code.")
        return render_template(
            "integrations_google.html",
            owner=store.get_owner_credentials(),
            error="Autorização do Google cancelada ou incompleta.",
        )

    try:
        refresh_token, google_email, calendar_id = google_calendar.exchange_code_for_tokens(code, code_verifier)
    except Exception as exc:
        logger.error("Failed to complete Google OAuth callback: %s", exc)
        return render_template(
            "integrations_google.html",
            owner=store.get_owner_credentials(),
            error="Não foi possível concluir a conexão com o Google Calendar.",
        )

    store.save_owner_credentials(google_email=google_email, refresh_token=refresh_token, calendar_id=calendar_id)
    return redirect(url_for("integrations.google_status"))


@integrations_bp.route("/google/disconnect", methods=["POST"])
@_require_auth
def google_disconnect():
    """Revoke the Google token (best-effort) and clear the stored credentials."""
    owner: dict | None = store.get_owner_credentials()
    if owner and owner.get("refresh_token"):
        google_calendar.revoke_token(owner["refresh_token"])

    store.clear_owner_credentials()
    return redirect(url_for("integrations.google_status"))
