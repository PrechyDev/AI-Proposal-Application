import hashlib
import hmac
import secrets

from passlib.context import CryptContext

# pbkdf2_sha256 is pure Python (no C-extension wheel to worry about) and
# still a NIST-approved adaptive hash - fine for this app's login volume.
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def generate_invite_token() -> str:
    """A long, URL-embedded secret (invite/set-password links) - high
    enough entropy that it needs no separate rate-limiting to resist
    guessing, unlike the short numeric reset code below."""
    return secrets.token_urlsafe(32)


def generate_reset_code() -> str:
    """A short, human-typeable code for the forgot-password flow (the user
    asked for "a code," not another link to click) - deliberately low
    entropy, so app/services/account_tokens.py enforces an attempt limit
    and a short expiry to keep it safe against guessing."""
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_token(raw_token: str) -> str:
    """Only this hash is ever stored (app/models/tokens.py) - a plain
    SHA-256 is appropriate here (unlike password hashing): the input is
    already a high-entropy random secret or a rate-limited short code, not
    a human-chosen password that needs deliberately slow hashing to resist
    offline guessing."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


def verify_token(raw_token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_token(raw_token), token_hash)
