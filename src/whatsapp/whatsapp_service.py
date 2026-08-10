"""Outbound WhatsApp, through the number of the gym the message belongs to.

MODULE S3d — THE OTHER HALF OF THE ROUTING S3a STARTED. S3a made the "To" field
of an inbound message identify the gym (integrations/store.py::
find_tenant_by_whatsapp_number), so a lead writing to gym B reaches gym B. But
every reply left through ONE number read from the environment, so the same lead
would have been answered from a line that is not the gym's — and their next
message, sent back to THAT number, would land in another gym's conversation.
Recognized by one number, answering from another: the routing was right on the
way in and wrong on the way out.

So `from_` is resolved per tenant now. The tenant is an ARGUMENT with a default,
never flask.g or current_user: this module is called from the webhook, from the
dashboard, and from the Railway cron, and the cron has no request at all
(decision 14A, Module S3b).

NO CACHE. One indexed read of one row, next to an HTTP round-trip to Twilio —
the same arithmetic that left bot/class_types.py without a TTL. A cache here
would only buy a window in which the settings screen lies about which number the
gym is sending from.
"""

from twilio.rest import Client
from config import Config
import integrations.store as store
import logging

logger  = logging.getLogger(__name__)


class SenderNotConfiguredError(RuntimeError):
    """The tenant has no whatsapp_number, and is not the sandbox tenant.

    Raised instead of quietly falling back, because for a gym that is not the
    pilot the fallback IS the bug: the lead would receive a message from a
    number that is not their gym's, and their reply would be routed to whoever
    owns that number. A refused send is visible; a send from the wrong line is
    not.
    """


def get_client() -> Client:
    """
    Creates the client for twilio using the account SID and auth token from the config.
    Makes it to simulate the client in future tests.
    Returns:
        Client: client from twilio
    """
    return Client(Config.TWILIO_ACCOUNT_SID, Config.TWILIO_AUTH_TOKEN)


def resolve_sender_number(tenant_id: str = store.DEFAULT_TENANT_ID) -> str:
    """Decide which number a message for this tenant goes out from.

    THE FALLBACK IS THE SANDBOX, AND IT IS THE PILOT'S ALONE. The Twilio Sandbox
    hands every gym the same number, so nobody can claim it and the pilot's
    `whatsapp_number` is legitimately NULL — for tenant 'default' the fallback
    reproduces the pre-S3d behaviour byte for byte. For any other tenant a
    missing number is a configuration gap, and answering from the sandbox line
    would cross two gyms' conversations, so it raises instead.

    Note the asymmetry with the INBOUND side: resolve_tenant_by_whatsapp_number()
    never blocks a message, because dropping an incoming lead is worse than
    handling them as the pilot's. Here the trade is reversed — a message sent
    from the wrong line cannot be taken back.

    Args:
        tenant_id (str): The gym this message belongs to. Defaults to the pilot
            so the manual CLIs and the test suites keep working unchanged.

    Returns:
        str: The Twilio "From" value, e.g. "whatsapp:+5521999999999". The column
        stores plain digits; Config.TWILIO_SANDBOX_NUMBER already carries the
        prefix, so it is returned as-is.

    Raises:
        SenderNotConfiguredError: The tenant is not 'default' and has no number.
    """
    number: str | None = store.get_whatsapp_number(tenant_id)

    if number:
        return f"whatsapp:+{number}"

    if tenant_id == store.DEFAULT_TENANT_ID:
        # No number is logged: the pilot's line is identifiable and this
        # repository is public (the rule bot/messages.py follows for text).
        logger.warning(
            "Tenant '%s' has no whatsapp_number; sending from the Twilio Sandbox number.",
            tenant_id,
        )
        return Config.TWILIO_SANDBOX_NUMBER

    raise SenderNotConfiguredError(
        f"Tenant '{tenant_id}' has no whatsapp_number configured; refusing to send "
        "from another gym's number."
    )


def send_message(to: str, text: str, tenant_id: str = store.DEFAULT_TENANT_ID) -> str:
    """

    Args:
        to (str): The phone number to which the message will be sent.
        text (str): The text of the message to be sent.
        tenant_id (str): The gym sending it, which decides the "From" (Module
            S3d). Defaults to the pilot — and that default is the failure mode
            to watch for: omitting it does not raise, it sends from the pilot's
            line. When you add a call site, pass the tenant.

    Returns:
        str: The SID (UNIQUE ID) of the generated message from twilio.
    """
    # Resolved BEFORE the try: a missing number is a configuration problem, not
    # a Twilio one, and it should surface as SenderNotConfiguredError rather than
    # be relabelled by the log line below. It also avoids building a client for a
    # send that is about to be refused.
    from_number: str = resolve_sender_number(tenant_id)

    try:
        client = get_client()

        message = client.messages.create(
            body = text,
            from_ = from_number,
            to = f"whatsapp:+{to}"
        )
        # Never log the text itself: this carries lead conversations and the
        # repository is public (see bot/messages.py).
        logger.info(f"Message sent to {to} ({len(text)} chars) | SID: {message.sid}")
        return message.sid
    except Exception as e:
        #All the erros of twilio will be catched here, and we can log them for future debugging.
        #We will need to solve this in webhooks.
        logger.error(f"Error sending message para {to}: {e}")

        #literally re-raise the exception to be handled in the webhooks.
        raise
