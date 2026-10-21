"""Secrets handling: token encryption at rest, signed unsubscribe links, password checks.

All keys are derived from SECRET_KEY. Rotating SECRET_KEY invalidates stored Google
tokens (inboxes must be reconnected) and previously sent unsubscribe links.
"""

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, URLSafeSerializer


def _fernet(secret_key: str) -> Fernet:
    key = hashlib.sha256(f"outreach-wise:fernet:{secret_key}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(secret_key: str, plaintext: str) -> str:
    return _fernet(secret_key).encrypt(plaintext.encode()).decode()


def decrypt(secret_key: str, token: str) -> str:
    try:
        return _fernet(secret_key).decrypt(token.encode()).decode()
    except InvalidToken as err:
        raise ValueError("Stored credential cannot be decrypted (was SECRET_KEY changed?)") from err


def unsubscribe_token(secret_key: str, lead_id: int) -> str:
    return URLSafeSerializer(secret_key, salt="unsubscribe").dumps(lead_id)


def read_unsubscribe_token(secret_key: str, token: str) -> int | None:
    try:
        value = URLSafeSerializer(secret_key, salt="unsubscribe").loads(token)
    except BadSignature:
        return None
    return value if isinstance(value, int) else None


def password_matches(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode(), expected.encode())
