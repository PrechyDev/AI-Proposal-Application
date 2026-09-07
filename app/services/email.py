import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_RESEND_URL = "https://api.resend.com/emails"
_TIMEOUT = 15.0


class EmailError(Exception):
    """Raised when an email fails to send - callers log this to delivery_logs,
    never let it block the action that triggered the email (spec section 7:
    "Email API is down" must never silently block or corrupt the underlying
    state change, e.g. a proposal submission)."""


def send_email(to: str, subject: str, html: str) -> None:
    settings = get_settings()
    if not settings.resend_api_key:
        raise EmailError("No email provider configured (RESEND_API_KEY missing) - see .env.example.")

    body = {"from": settings.email_from, "to": [to], "subject": subject, "html": html}
    if settings.email_reply_to:
        body["reply_to"] = settings.email_reply_to

    try:
        resp = httpx.post(
            _RESEND_URL,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json=body,
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise EmailError(f"Could not reach Resend: {exc}") from None

    if resp.status_code not in (200, 201):
        logger.error("Resend send failed (%s): %s", resp.status_code, resp.text)
        raise EmailError(f"Resend request failed with status {resp.status_code}: {resp.text}")
