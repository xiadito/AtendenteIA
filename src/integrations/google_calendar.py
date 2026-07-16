import logging
from typing import Any

import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from config import Config

logger = logging.getLogger(__name__)

# Broad scope is required (not calendar.events) because the OAuth callback must be
# able to call calendarList.list / calendars.insert to create or find the dedicated
# "Aulas Experimentais" calendar. calendar.events only grants access to events on
# calendars the app already knows about, not to the calendars themselves.
SCOPES: list[str] = ["https://www.googleapis.com/auth/calendar"]

CALENDAR_NAME: str = "Aulas Experimentais"
TOKEN_URI: str = "https://oauth2.googleapis.com/token"
REVOKE_URI: str = "https://oauth2.googleapis.com/revoke"


class NeedsReconnectError(Exception):
    """Raised when Google reports the stored refresh_token is no longer valid."""


def _client_config() -> dict[str, Any]:
    """Build the client config dict expected by google_auth_oauthlib.Flow."""
    return {
        "web": {
            "client_id": Config.GOOGLE_CLIENT_ID,
            "client_secret": Config.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": TOKEN_URI,
            "redirect_uris": [Config.GOOGLE_REDIRECT_URI],
        }
    }


def build_authorization_url(state: str) -> str:
    """Build the Google consent screen URL for the connect flow.

    Args:
        state (str): CSRF token to be echoed back on the callback.

    Returns:
        str: Full authorization URL to redirect the user to.
    """
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=Config.GOOGLE_REDIRECT_URI)

    # access_type=offline + prompt=consent guarantee Google returns a refresh_token
    # even if the user already granted consent before (e.g. reconnecting after a
    # revoke). Without prompt=consent, Google silently skips issuing a new
    # refresh_token on repeat consent, leaving the app with only a 1-hour access_token.
    authorization_url: str
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
        state=state,
    )
    return authorization_url


def exchange_code_for_tokens(code: str) -> tuple[str, str, str]:
    """Exchange the authorization code for tokens and set up the dedicated calendar.

    Args:
        code (str): Authorization code returned by Google on the callback.

    Returns:
        tuple[str, str, str]: (refresh_token, google_email, calendar_id).

    Raises:
        RuntimeError: If Google does not return a refresh_token.
    """
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=Config.GOOGLE_REDIRECT_URI)
    flow.fetch_token(code=code)
    credentials: Credentials = flow.credentials

    if not credentials.refresh_token:
        raise RuntimeError("Google did not return a refresh_token; retry the consent flow.")

    service = build("calendar", "v3", credentials=credentials)
    google_email: str = _get_account_email(service)
    calendar_id: str = _find_or_create_calendar(service)

    return credentials.refresh_token, google_email, calendar_id


def _get_account_email(service: Any) -> str:
    """Return the Google account email via the primary calendar's ID.

    The primary calendar's ID is always the account's email address, which
    avoids requesting an extra userinfo/profile scope beyond the broad
    calendar scope already required.

    Args:
        service (Any): Authenticated Google Calendar API client.

    Returns:
        str: Email address of the connected Google account.
    """
    primary: dict[str, Any] = service.calendarList().get(calendarId="primary").execute()
    return primary["id"]


def _find_or_create_calendar(service: Any) -> str:
    """Find the dedicated calendar by name, or create it if missing.

    This name-based lookup only ever runs during the OAuth callback (first
    connect, or reconnect after a disconnect). Once calendar_id is stored in
    the owners table, callers must use it directly instead of calling this.

    Args:
        service (Any): Authenticated Google Calendar API client.

    Returns:
        str: ID of the "Aulas Experimentais" calendar.
    """
    calendar_list: dict[str, Any] = service.calendarList().list().execute()
    for entry in calendar_list.get("items", []):
        if entry.get("summary") == CALENDAR_NAME:
            return entry["id"]

    created: dict[str, Any] = service.calendars().insert(body={"summary": CALENDAR_NAME}).execute()
    logger.info("Created new '%s' calendar (id=%s).", CALENDAR_NAME, created["id"])
    return created["id"]


def build_credentials(refresh_token: str) -> Credentials:
    """Build and refresh a Credentials object from a stored refresh_token.

    Args:
        refresh_token (str): Refresh token stored in the owners table.

    Returns:
        Credentials: Refreshed, ready-to-use credentials.

    Raises:
        NeedsReconnectError: If Google reports invalid_grant (revoked/expired token).
    """
    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=Config.GOOGLE_CLIENT_ID,
        client_secret=Config.GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )
    try:
        credentials.refresh(Request())
    except RefreshError as exc:
        if "invalid_grant" in str(exc):
            logger.warning("Refresh token invalid/revoked: %s", exc)
            raise NeedsReconnectError("Google refresh_token is no longer valid; user must reconnect.") from exc
        raise
    return credentials


def get_calendar_service(refresh_token: str) -> Any:
    """Build an authenticated Calendar API client from a stored refresh_token.

    Args:
        refresh_token (str): Refresh token stored in the owners table.

    Returns:
        Any: Authenticated Google Calendar API client, ready for Module 2.
    """
    credentials = build_credentials(refresh_token)
    return build("calendar", "v3", credentials=credentials)


def revoke_token(refresh_token: str) -> bool:
    """Best-effort revoke of the refresh_token via Google's revoke endpoint.

    Args:
        refresh_token (str): Token to revoke.

    Returns:
        bool: True if Google accepted the revoke request, False otherwise.
        Callers should proceed to clear local state regardless of the result.
    """
    try:
        response = requests.post(REVOKE_URI, params={"token": refresh_token}, timeout=10)
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.warning("Failed to revoke Google token: %s", exc)
        return False
