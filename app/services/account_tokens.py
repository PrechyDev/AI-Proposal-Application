from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountToken, User
from app.security import generate_invite_token, generate_reset_code, hash_token, verify_token

# Doesn't commit - matches app/services/proposal_generation.py's convention:
# the service builds/validates, the calling router owns the transaction.

INVITE_TTL = timedelta(days=7)
RESET_TTL = timedelta(minutes=15)
MAX_RESET_ATTEMPTS = 5


def _invalidate_outstanding(db: Session, user: User, purpose: str) -> None:
    """An old unused link/code shouldn't stay valid alongside a fresh one -
    same principle as reopening a proposal killing its old client token."""
    now = datetime.now(timezone.utc)
    outstanding = db.execute(
        select(AccountToken).where(
            AccountToken.user_id == user.id, AccountToken.purpose == purpose, AccountToken.used_at.is_(None)
        )
    ).scalars().all()
    for token in outstanding:
        token.used_at = now


def issue_invite_token(db: Session, user: User) -> str:
    _invalidate_outstanding(db, user, "invite")
    raw_token = generate_invite_token()
    db.add(
        AccountToken(
            user_id=user.id,
            purpose="invite",
            token_hash=hash_token(raw_token),
            expires_at=datetime.now(timezone.utc) + INVITE_TTL,
        )
    )
    return raw_token


def issue_reset_code(db: Session, user: User) -> str:
    _invalidate_outstanding(db, user, "reset")
    raw_code = generate_reset_code()
    db.add(
        AccountToken(
            user_id=user.id,
            purpose="reset",
            token_hash=hash_token(raw_code),
            expires_at=datetime.now(timezone.utc) + RESET_TTL,
        )
    )
    return raw_code


def find_valid_invite(db: Session, raw_token: str) -> AccountToken | None:
    """Invite links are high-entropy enough to look up directly by hash,
    without the caller already knowing which user they belong to - unlike
    the reset code below, no separate rate limit is needed here."""
    now = datetime.now(timezone.utc)
    candidates = db.execute(
        select(AccountToken).where(
            AccountToken.purpose == "invite", AccountToken.used_at.is_(None), AccountToken.expires_at > now
        )
    ).scalars().all()
    for candidate in candidates:
        if verify_token(raw_token, candidate.token_hash):
            return candidate
    return None


def check_reset_code(db: Session, user: User, code: str) -> AccountToken | None:
    """Unlike the invite link, a 6-digit code isn't unique enough to look
    up on its own - the caller already resolved `user` from the submitted
    email. A wrong guess is counted; after MAX_RESET_ATTEMPTS the code is
    dead even if the correct value is later submitted (an expiring,
    rate-limited code is this flow's substitute for the invite link's raw
    entropy - the "have you seen the actual email" property still holds).
    """
    now = datetime.now(timezone.utc)
    token = db.execute(
        select(AccountToken)
        .where(
            AccountToken.user_id == user.id,
            AccountToken.purpose == "reset",
            AccountToken.used_at.is_(None),
            AccountToken.expires_at > now,
        )
        .order_by(AccountToken.created_at.desc())
    ).scalars().first()
    if token is None or token.attempts >= MAX_RESET_ATTEMPTS:
        return None
    if not verify_token(code, token.token_hash):
        token.attempts += 1
        return None
    return token


def consume_token(token: AccountToken) -> None:
    token.used_at = datetime.now(timezone.utc)
