from passlib.context import CryptContext

# pbkdf2_sha256 is pure Python (no C-extension wheel to worry about) and
# still a NIST-approved adaptive hash - fine for this app's login volume.
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)
