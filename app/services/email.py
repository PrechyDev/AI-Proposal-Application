import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_BREVO_URL = "https://api.brevo.com/v3/smtp/email"
_TIMEOUT = 15.0


class EmailError(Exception):
    """Raised when an email fails to send - callers log this to delivery_logs,
    never let it block the action that triggered the email (spec section 7:
    "Email API is down" must never silently block or corrupt the underlying
    state change, e.g. a proposal submission)."""


def send_email(
    to: str,
    subject: str,
    html: str,
    cc: str | None = None,
    reply_to: str | None = None,
) -> None:
    """Every proposal-related email is sent from the one central address
    (EMAIL_FROM_ADDRESS/EMAIL_FROM_NAME) - never a per-salesperson address.
    `cc`/`reply_to` are per-call, not global config, since who they point at
    depends on which salesperson owns the specific proposal being emailed
    about, not a fixed setting.
    """
    settings = get_settings()
    if not settings.brevo_api_key or not settings.email_from_address:
        raise EmailError(
            "No email provider configured (BREVO_API_KEY/EMAIL_FROM_ADDRESS missing) - see .env.example."
        )

    body = {
        "sender": {"name": settings.email_from_name, "email": settings.email_from_address},
        "to": [{"email": to}],
        "subject": subject,
        "htmlContent": html,
    }
    if cc:
        body["cc"] = [{"email": cc}]
    if reply_to:
        body["replyTo"] = {"email": reply_to}

    try:
        resp = httpx.post(
            _BREVO_URL,
            headers={
                "api-key": settings.brevo_api_key,
                "content-type": "application/json",
                "accept": "application/json",
            },
            json=body,
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise EmailError(f"Could not reach Brevo: {exc}") from None

    if resp.status_code not in (200, 201):
        logger.error("Brevo send failed (%s): %s", resp.status_code, resp.text)
        raise EmailError(f"Brevo request failed with status {resp.status_code}: {resp.text}")
