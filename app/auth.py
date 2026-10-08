"""Password hashing and signed access-token helpers."""

from datetime import datetime, timedelta, timezone
import re
import uuid

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
password_hasher = PasswordHasher()


def normalize_email(email: str) -> str:
    value = email.strip().lower()
    if not EMAIL_RE.fullmatch(value):
        raise ValueError("A valid email address is required.")
    return value


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def create_access_token(user_id: str, secret: str, ttl_minutes: int) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": user_id, "iat": now, "exp": now + timedelta(minutes=ttl_minutes), "jti": uuid.uuid4().hex},
        secret,
        algorithm="HS256",
    )


def decode_access_token(token: str, secret: str) -> str:
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        user_id = payload.get("sub")
        if not isinstance(user_id, str):
            raise ValueError("Invalid access token.")
        return user_id
    except jwt.PyJWTError as exc:
        raise ValueError("Invalid or expired access token.") from exc
