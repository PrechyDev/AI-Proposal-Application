import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_BREVO_URL = "https://api.brevo.com/v3/smtp/email"
_TIMEOUT = 15.0
_GMAIL_SMTP_HOST = "smtp.gmail.com"
_GMAIL_SMTP_PORT = 587


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
    (EMAIL_FROM_ADDRESS/EMAIL_FROM_NAME) via Brevo - never a per-salesperson
    address. `cc`/`reply_to` are per-call, not global config, since who
    they point at depends on which salesperson owns the specific proposal
    being emailed about, not a fixed setting.

    Falls back to Gmail SMTP if Brevo isn't configured at all, or if a real
    send attempt to it fails (e.g. the account gets suspended) - only
    raises `EmailError` if neither path works. This is a backup path, not
    an equal alternative: mail sent this way shows the real Gmail address
    as the sender, not the central Brevo one, since Gmail's SMTP relay
    won't send "as" an unrelated address without domain delegation this
    app doesn't have.
    """
    settings = get_settings()
    brevo_configured = bool(settings.brevo_api_key and settings.email_from_address)

    if brevo_configured:
        try:
            _send_via_brevo(to, subject, html, cc, reply_to)
            return
        except EmailError as exc:
            logger.warning("Brevo send failed, falling back to Gmail: %s", exc)
    else:
        logger.warning("Brevo not configured, falling back to Gmail for this send.")

    _send_via_gmail(to, subject, html, cc, reply_to)


def _send_via_brevo(to: str, subject: str, html: str, cc: str | None, reply_to: str | None) -> None:
    settings = get_settings()
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


def _send_via_gmail(to: str, subject: str, html: str, cc: str | None, reply_to: str | None) -> None:
    settings = get_settings()
    if not settings.gmail_address or not settings.gmail_app_password:
        raise EmailError(
            "No email provider available - Brevo failed/unconfigured, and the Gmail fallback "
            "(GMAIL_ADDRESS/GMAIL_APP_PASSWORD) isn't configured either - see .env.example."
        )

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = f"{settings.email_from_name} <{settings.gmail_address}>"
    message["To"] = to
    recipients = [to]
    if cc:
        message["Cc"] = cc
        recipients.append(cc)
    if reply_to:
        message["Reply-To"] = reply_to
    message.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(_GMAIL_SMTP_HOST, _GMAIL_SMTP_PORT, timeout=_TIMEOUT) as server:
            server.starttls()
            server.login(settings.gmail_address, settings.gmail_app_password)
            server.sendmail(settings.gmail_address, recipients, message.as_string())
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(f"Gmail fallback also failed: {exc}") from None
